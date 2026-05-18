# -*- coding: utf-8 -*-

"""API 服务 — aiohttp REST + 昵称抓取 + 自动订阅"""

import asyncio
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
        self.port = api_cfg.get('port', 8787)
        self.delay_ms = api_cfg.get('delay_ms', 1000)

        self.app: Optional[web.Application] = None
        self.runner: Optional[web.AppRunner] = None
        self.site: Optional[web.TCPSite] = None
        self.merger = RequestMerger(self.delay_ms)
        self._nickname_cache: Dict[str, str] = {}

    # ========== 生命周期 ==========

    async def start(self):
        self.app = web.Application(middlewares=[self._log_middleware])
        self._setup_routes()
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, self.host, self.port)
        await self.site.start()
        logger.info(f"API 服务启动: http://{self.host}:{self.port}")

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()
        logger.info("API 服务已停止")

    def _setup_routes(self):
        assert self.app is not None
        ep = self.config.api.get('endpoints', {})
        self.app.router.add_get(ep.get('user', '/api/user/{id}'), self.get_user_info)
        sub = ep.get('subscribe', '/api/subscribe')
        self.app.router.add_post(sub, self.subscribe_user)
        self.app.router.add_delete(sub, self.unsubscribe_user)
        self.app.router.add_get(ep.get('dynamic', '/api/dynamic/{id}'), self.get_user_dynamics)
        self.app.router.add_get(ep.get('dynamics', '/api/dynamics'), self.get_all_dynamics)
        self.app.router.add_get('/health', self.health_check)

    @web.middleware
    async def _log_middleware(self, request, handler):
        logger.info(f"[API] {request.method} {request.path_qs}")
        return await handler(request)

    # ========== 用户昵称 ==========

    async def get_user_info(self, request):
        user_id = request.match_info.get('id')
        if not user_id:
            return web.json_response({'error': 'missing user id'}, status=400)
        result = await self.merger.execute(f"user_{user_id}", self._fetch_user_info, user_id)
        return web.json_response(result)

    async def _fetch_user_info(self, user_id: str) -> Dict[str, Any]:
        if user_id in self._nickname_cache:
            return {'id': user_id, 'name': self._nickname_cache[user_id], 'cached': True}

        name = await self._scrape_nickname(user_id)
        if name:
            self._nickname_cache[user_id] = name
            self.storage.set_nickname(user_id, name)
        return {'id': user_id, 'name': name or f"用户_{user_id}", 'cached': False}

    async def _scrape_nickname(self, user_id: str) -> Optional[str]:
        if not self.browser:
            return None
        page = None
        try:
            page = await self.browser.create_temp_page()
            await page.goto(f"https://space.bilibili.com/{user_id}",
                            wait_until="networkidle", timeout=15000)
            await page.wait_for_selector(".nickname", timeout=10000)
            el = await page.query_selector(".nickname")
            if el:
                name = (await el.text_content() or "").strip()
                if name:
                    logger.info(f"昵称: {user_id} -> {name}")
                    return name
            return None
        except Exception as e:
            logger.error(f"抓取昵称失败 {user_id}: {e}")
            return None
        finally:
            if page:
                await page.close()

    # ========== 订阅管理 ==========

    async def subscribe_user(self, request):
        try:
            data = await request.json()
            user_id = data.get('user_id')
            if not user_id:
                return web.json_response({'error': 'missing user_id'}, status=400)

            await self.storage.add_to_watchlist(user_id)
            followed = False
            if self.browser:
                followed = await self.browser.follow_user(user_id)
            return web.json_response({
                'success': True, 'user_id': user_id,
                'bilibili_followed': followed,
                'message': '已添加' + ('，已在B站关注' if followed else '')
            })
        except Exception as e:
            logger.error(f"订阅失败: {e}")
            return web.json_response({'error': str(e)}, status=500)

    async def unsubscribe_user(self, request):
        try:
            data = await request.json()
            user_id = data.get('user_id')
            if not user_id:
                return web.json_response({'error': 'missing user_id'}, status=400)
            ok = await self.storage.remove_from_watchlist(user_id)
            return web.json_response({
                'success': ok, 'user_id': user_id,
                'message': '已移除' if ok else '不在列表中'
            })
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)

    # ========== 动态查询 ==========

    async def get_user_dynamics(self, request):
        user_id = request.match_info.get('id')
        if not user_id:
            return web.json_response({'error': 'missing user id'}, status=400)

        limit = min(int(request.query.get('limit', 10)), 50)

        # 自动订阅
        if self.config.watchlist.get('auto_subscribe', True):
            watchlist = await self.storage.get_watchlist()
            if user_id not in watchlist:
                await self.storage.add_to_watchlist(user_id)
                logger.info(f"自动订阅: {user_id}")
                if self.browser:
                    followed = await self.browser.follow_user(user_id)
                    name = self.storage.get_nickname(user_id)
                    logger.info(f"B站关注: {user_id}({name}) -> {'成功' if followed else '失败'}")

        dynamics = await self.storage.get_user_dynamics(user_id, limit)
        return web.json_response({'user_id': user_id, 'count': len(dynamics), 'dynamics': dynamics})

    async def get_all_dynamics(self, request):
        limit = min(int(request.query.get('limit', 5)), 20)
        return web.json_response({
            'user_count': 0, 'limit_per_user': limit,
            'dynamics': await self.storage.get_all_dynamics(limit)
        })

    async def health_check(self, request):
        return web.json_response({'status': 'ok', 'timestamp': datetime.now().isoformat()})
