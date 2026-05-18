# -*- coding: utf-8 -*-

"""用户微博获取器 - 独立获取指定用户的微博"""

import asyncio
import aiohttp
import json
from pathlib import Path
from typing import Optional, List, Dict, Any
from loguru import logger


class UserWeiboFetcher:
    """访问用户主页获取微博列表"""

    def __init__(self, browser_manager):
        self.browser = browser_manager
        self._pending_fetches: Dict[str, asyncio.Future] = {}

    async def fetch_user_weibo(self, user_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        """获取指定用户的微博，自动去重并发请求"""
        if user_id in self._pending_fetches:
            logger.debug(f"等待已有的 {user_id} 请求完成")
            return await self._pending_fetches[user_id]

        future = asyncio.Future()
        self._pending_fetches[user_id] = future
        try:
            result = await self._do_fetch_user_weibo(user_id, limit)
            future.set_result(result)
            return result
        except Exception as e:
            future.set_exception(e)
            raise
        finally:
            self._pending_fetches.pop(user_id, None)

    async def _do_fetch_user_weibo(self, user_id: str, limit: int) -> List[Dict[str, Any]]:
        """实际执行：打开用户页 → 拦截 API → 滚动加载 → 解析"""
        logger.info(f"开始获取用户 {user_id} 的微博")
        page = None
        weibos = []

        try:
            page = await self.browser.create_temp_page()
            intercepted_data = {"items": []}

            async def on_response(response):
                if "/ajax/statuses/mymblog" in response.url and response.status == 200:
                    try:
                        body = await response.json()
                        if body.get("ok") == 1 and body.get("data", {}).get("list"):
                            items = body["data"]["list"]
                            intercepted_data["items"].extend(items)
                            logger.info(f"拦截到 {len(items)} 条 (累计 {len(intercepted_data['items'])})")
                    except Exception as e:
                        logger.debug(f"解析响应失败: {e}")

            page.on("response", on_response)
            await page.goto(f"https://weibo.com/u/{user_id}", wait_until="networkidle", timeout=30000)
            await asyncio.sleep(30)

            scroll_count = 0
            while len(intercepted_data["items"]) < limit and scroll_count < 3:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(3)
                scroll_count += 1
                logger.info(f"滚动加载第 {scroll_count} 次，当前 {len(intercepted_data['items'])} 条")

            for item in intercepted_data["items"][:limit]:
                weibo = self._parse_weibo(item)
                weibos.append(weibo)

            for weibo in weibos:
                if (
                    weibo.get("isLongText") or
                    "展开" in weibo.get("text_raw", "") or
                    weibo.get("text", "").rstrip().endswith("...")
                ):
                    await self._expand_long_text(weibo)

            logger.info(f"获取完成: 用户 {user_id}，共 {len(weibos)} 条微博")
        except Exception as e:
            logger.error(f"获取用户 {user_id} 微博失败: {e}")
        finally:
            if page:
                await page.close()

        return weibos

    def _parse_weibo(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """解析单条微博为结构化 dict"""
        user = item.get("user", {})
        created_at = item.get("created_at", "")
        timestamp = self._parse_time(created_at)
        images = self._extract_images(item)
        retweeted = item.get("retweeted_status")
        is_retweet = retweeted is not None

        result = {
            "id": item.get("id"),
            "idstr": item.get("idstr", ""),
            "mblogid": item.get("mblogid", ""),
            "created_at": created_at,
            "timestamp": timestamp,
            "user": {
                "id": user.get("id"),
                "idstr": user.get("idstr", ""),
                "screen_name": user.get("screen_name", ""),
                "profile_url": user.get("profile_url", ""),
                "avatar": user.get("profile_image_url", ""),
                "verified": user.get("verified", False),
            },
            "text": self._clean_text(item.get("text", "")),
            "text_raw": item.get("text_raw", ""),
            "images": images,
            "reposts_count": item.get("reposts_count", 0),
            "comments_count": item.get("comments_count", 0),
            "attitudes_count": item.get("attitudes_count", 0),
            "is_retweet": is_retweet,
            "isLongText": item.get("isLongText", False),
            "source": self._clean_source(item.get("source", "")),
            "region_name": item.get("region_name", ""),
        }

        if is_retweet:
            result["retweeted_status"] = self._parse_weibo(retweeted)

        return result

    def _parse_time(self, time_str: str) -> Optional[int]:
        """解析 'Sun May 17 08:27:18 +0800 2026' → Unix 时间戳"""
        if not time_str:
            return None
        try:
            parts = time_str.rsplit(' ', 2)
            if len(parts) >= 2:
                from datetime import datetime
                dt = datetime.strptime(f"{parts[0]} {parts[1]}", "%a %b %d %H:%M:%S %Y")
                return int(dt.timestamp())
        except (ValueError, IndexError):
            pass
        return None

    @staticmethod
    def _img_key(url: str) -> str:
        """提取文件名用于跨分辨率去重"""
        import re
        return re.sub(r'\?.*$', '', url.rsplit('/', 1)[-1])

    def _extract_images(self, weibo: Dict[str, Any]) -> List[str]:
        """提取图片 URL（含视频封面），按文件名去重"""
        images = []
        seen = set()

        def add(url: str):
            key = self._img_key(url)
            if key and key not in seen:
                seen.add(key)
                images.append(url)

        for info in weibo.get("pic_infos", {}).values():
            for size in ["large", "bmiddle", "thumbnail"]:
                url = info.get(size, {}).get("url")
                if url:
                    add(url)
                    break

        page_info = weibo.get("page_info", {})
        if page_info:
            page_pic = page_info.get("page_pic", "")
            if page_pic:
                add(page_pic)
            for source in ["big_pic_info", "pic_info"]:
                container = page_info.get("media_info", {}).get(source, {})
                if not container:
                    container = page_info.get(source, {})
                for pic_key in ["pic_big", "pic_middle", "pic_small"]:
                    url = container.get(pic_key, {}).get("url", "")
                    if url:
                        add(url)
                        break

        return images[:9]

    def _clean_text(self, text: str) -> str:
        """剥离 HTML 标签"""
        if not text:
            return ""
        import re
        return re.sub(r'<[^>]+>', '', text).strip()

    def _clean_source(self, source: str) -> str:
        """清理来源字段"""
        if not source:
            return ""
        import re
        match = re.search(r'>(.*?)</a>', source)
        return match.group(1) if match else source

    async def _expand_long_text(self, weibo: Dict[str, Any]):
        """调用 longtext API 补全截断文本"""
        mblogid = weibo.get("mblogid")
        if not mblogid:
            return
        try:
            cookies = {}
            if self.browser and self.browser.context:
                browser_cookies = await self.browser.context.cookies()
                cookies = {c.get('name', ''): c.get('value', '') for c in browser_cookies}
            headers = {
                "Referer": "https://weibo.com/",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:150.0) Gecko/20100101 Firefox/150.0",
                "X-Requested-With": "XMLHttpRequest",
            }
            async with aiohttp.ClientSession() as session:
                url = f"https://weibo.com/ajax/statuses/longtext?id={mblogid}"
                async with session.get(url, headers=headers, cookies=cookies,
                                       timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("ok") == 1:
                            long_text = data.get("data", {}).get("longTextContent", "")
                            if long_text:
                                weibo["text"] = long_text
                                weibo["text_raw"] = data.get("data", {}).get("longTextContent_raw", long_text)
                                logger.debug(f"补全长文本: {mblogid} -> {len(long_text)} 字符")
        except Exception as e:
            logger.error(f"补全长文本异常 {mblogid}: {e}")


class UserCacheManager:
    """用户微博缓存管理器"""

    def __init__(self, cache_dir: str = "./data/user_cache"):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _get_cache_path(self, user_id: str) -> Path:
        return self.cache_dir / f"{user_id}.json"

    def get(self, user_id: str, max_age: int = 300) -> Optional[List[Dict]]:
        """获取缓存的微博（max_age 秒内有效）"""
        cache_path = self._get_cache_path(user_id)
        if cache_path.exists():
            import time
            mtime = cache_path.stat().st_mtime
            if time.time() - mtime <= max_age:
                try:
                    data = json.loads(cache_path.read_text(encoding='utf-8'))
                    logger.debug(f"从缓存获取用户 {user_id}，共 {len(data)} 条")
                    return data
                except Exception:
                    pass
        return None

    def set(self, user_id: str, weibos: List[Dict]):
        """缓存微博"""
        cache_path = self._get_cache_path(user_id)
        try:
            cache_path.write_text(
                json.dumps(weibos, ensure_ascii=False, indent=2),
                encoding='utf-8'
            )
            logger.debug(f"缓存用户 {user_id}，共 {len(weibos)} 条")
        except Exception as e:
            logger.error(f"缓存用户 {user_id} 失败: {e}")

    def clear(self, user_id: str):
        """清除缓存"""
        cache_path = self._get_cache_path(user_id)
        if cache_path.exists():
            cache_path.unlink()
            logger.debug(f"清除用户 {user_id} 的缓存")
