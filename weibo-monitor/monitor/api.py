# -*- coding: utf-8 -*-

"""API 服务 — aiohttp REST + 请求合并"""

import asyncio
import aiohttp
from aiohttp import web
from typing import Dict, Any, Optional
from datetime import datetime
from loguru import logger


class RequestMerger:
    """请求合并器 — 延迟执行，合并相同 key 的请求"""

    def __init__(self, delay_ms: int = 1000):
        self.delay = delay_ms / 1000
        self._futures: Dict[str, list] = {}

    async def execute(self, key: str, func, *args, **kwargs):
        if key in self._futures:
            fut = asyncio.Future()
            self._futures[key].append(fut)
            return await fut

        fut = asyncio.Future()
        self._futures[key] = [fut]

        async def delayed():
            await asyncio.sleep(self.delay)
            try:
                result = await func(*args, **kwargs)
                for f in self._futures.get(key, []):
                    if not f.done():
                        f.set_result(result)
            except Exception as e:
                for f in self._futures.get(key, []):
                    if not f.done():
                        f.set_exception(e)
            finally:
                self._futures.pop(key, None)

        asyncio.create_task(delayed())
        return await fut


class APIServer:
    """API 服务器"""

    def __init__(self, config, storage, browser_manager):
        self.config = config
        self.storage = storage
        self.browser = browser_manager
        api_cfg = config.api
        self.host = api_cfg.get('host', '127.0.0.1')
        self.port = api_cfg.get('port', 39787)
        self.delay_ms = api_cfg.get('delay_ms', 1000)
        self.app: Optional[web.Application] = None
        self.runner: Optional[web.AppRunner] = None
        self.merger = RequestMerger(self.delay_ms)
        self._nickname_cache: Dict[str, str] = {}

    async def start(self):
        """启动 API 服务"""
        self.app = web.Application(middlewares=[self._log_middleware])
        self._setup_routes()
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, self.host, self.port)
        await self.site.start()
        logger.info(f"API 服务启动: http://{self.host}:{self.port}")

    async def stop(self):
        """停止 API 服务"""
        if self.runner:
            await self.runner.cleanup()
        logger.info("API 服务已停止")

    def _setup_routes(self):
        assert self.app is not None
        self.app.router.add_get('/api/user/{id}', self.get_user_info)
        self.app.router.add_post('/api/subscribe', self.subscribe_user)
        self.app.router.add_delete('/api/subscribe', self.unsubscribe_user)
        self.app.router.add_get('/api/weibo/{id}', self.get_user_weibo)
        self.app.router.add_get('/api/weibos', self.get_all_weibo)
        self.app.router.add_get('/api/image/proxy', self.proxy_image)
        self.app.router.add_get('/health', self.health_check)

    @web.middleware
    async def _log_middleware(self, request, handler):
        logger.debug(f"[API] {request.method} {request.path}")
        return await handler(request)

    # ========== 图片代理 ==========

    async def proxy_image(self, request):
        """代理微博图片，绕过防盗链"""
        url = request.query.get("url")
        if not url:
            return web.Response(status=400)
        headers = {
            "Referer": "https://weibo.com/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:150.0) Gecko/20100101 Firefox/150.0",
        }
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        return web.Response(
                            body=await resp.read(),
                            content_type=resp.headers.get("Content-Type", "image/jpeg")
                        )
                    return web.Response(status=resp.status)
            except Exception as e:
                logger.error(f"图片代理失败: {e}")
                return web.Response(status=500)

    # ========== 用户昵称 ==========

    async def get_user_info(self, request):
        """获取用户昵称"""
        user_id = request.match_info.get('id')
        if not user_id:
            return web.json_response({'error': 'missing user id'}, status=400)

        if user_id in self._nickname_cache:
            return web.json_response({'id': user_id, 'name': self._nickname_cache[user_id]})

        name = self.storage.get_nickname(user_id)
        if name:
            self._nickname_cache[user_id] = name
            return web.json_response({'id': user_id, 'name': name})

        name = await self._fetch_nickname(user_id)
        self._nickname_cache[user_id] = name
        self.storage.set_nickname(user_id, name)
        return web.json_response({'id': user_id, 'name': name})

    async def _fetch_nickname(self, user_id: str) -> str:
        """通过 API 或页面抓取用户昵称"""
        cached_name = self.storage.get_nickname(user_id)
        if cached_name:
            return cached_name

        api_url = f"https://weibo.com/ajax/profile/info?uid={user_id}"
        try:
            cookies = {}
            if self.browser and self.browser.context:
                browser_cookies = await self.browser.context.cookies()
                cookies = {c.get('name', ''): c.get('value', '') for c in browser_cookies}
            headers = {
                "Referer": "https://weibo.com/",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:150.0) Gecko/20100101 Firefox/150.0",
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/plain, */*",
            }
            async with aiohttp.ClientSession() as session:
                async with session.get(api_url, headers=headers, cookies=cookies,
                                       timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("ok") == 1:
                            screen_name = data.get("data", {}).get("user", {}).get("screen_name")
                            if screen_name:
                                logger.info(f"[API] 通过 API 获取昵称: {user_id} -> {screen_name}")
                                return screen_name
        except Exception as e:
            logger.debug(f"[API] API 请求异常: {e}")

        logger.info(f"[API] API 方式失败，尝试页面抓取: {user_id}")
        temp_page = None
        try:
            if not self.browser or not self.browser.context:
                return f"用户_{user_id}"
            temp_page = await self.browser.context.new_page()
            await temp_page.goto(f"https://weibo.com/u/{user_id}", wait_until="networkidle", timeout=20000)
            await asyncio.sleep(3)
            title = await temp_page.title()
            if title and "的微博" in title:
                name = title.replace("的微博", "").strip()
                if name and len(name) < 50:
                    logger.info(f"[API] 从标题提取昵称: {user_id} -> {name}")
                    return name
            return f"用户_{user_id}"
        except Exception as e:
            logger.error(f"[API] 页面抓取昵称异常: {e}")
            return f"用户_{user_id}"
        finally:
            if temp_page:
                await temp_page.close()

    # ========== 订阅管理 ==========

    async def subscribe_user(self, request):
        """订阅用户"""
        try:
            data = await request.json()
            user_id = data.get('user_id')
            if not user_id:
                return web.json_response({'error': 'missing user_id'}, status=400)
            await self.storage.add_to_watchlist(user_id)
            name = await self._fetch_nickname(user_id)
            self.storage.set_nickname(user_id, name)
            return web.json_response({'success': True, 'user_id': user_id, 'name': name})
        except Exception as e:
            logger.error(f"订阅失败: {e}")
            return web.json_response({'error': str(e)}, status=500)

    async def unsubscribe_user(self, request):
        """取消订阅"""
        try:
            data = await request.json()
            user_id = data.get('user_id')
            if not user_id:
                return web.json_response({'error': 'missing user_id'}, status=400)
            ok = await self.storage.remove_from_watchlist(user_id)
            return web.json_response({'success': ok, 'user_id': user_id})
        except Exception as e:
            logger.error(f"取消订阅失败: {e}")
            return web.json_response({'error': str(e)}, status=500)

    # ========== 微博查询 ==========

    async def get_user_weibo(self, request):
        """获取指定用户的微博"""
        user_id = request.match_info.get('id')
        limit = min(int(request.query.get('limit', 10)), 50)
        if not user_id:
            return web.json_response({'error': 'missing user id'}, status=400)
        weibos = await self.storage.get_user_weibo(user_id, limit)
        name = self.storage.get_nickname(user_id) or self._nickname_cache.get(user_id, f"用户_{user_id}")
        return web.json_response({'user_id': user_id, 'name': name, 'count': len(weibos), 'weibos': weibos})

    async def get_all_weibo(self, request):
        """获取所有订阅用户的微博"""
        limit = min(int(request.query.get('limit', 5)), 20)
        return web.json_response({'limit_per_user': limit, 'weibos': await self.storage.get_all_weibo(limit)})

    async def health_check(self, request):
        """健康检查"""
        return web.json_response({'status': 'ok', 'timestamp': datetime.now().isoformat()})
