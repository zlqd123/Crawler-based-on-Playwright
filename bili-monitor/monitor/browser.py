# -*- coding: utf-8 -*-

"""浏览器管理 — 生命周期 + 关注 + API拦截 + 页面扫描 + WS监控"""

import asyncio
import re
import base64
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Any
from playwright.async_api import async_playwright, BrowserContext, Page
from loguru import logger


class BrowserManager:
    """统一浏览器管理器"""

    def __init__(self, config):
        self.config = config
        self.playwright = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

        # API feed 缓存
        self.feed_cache: list = []
        self.feed_captured = False

        # 页面扫描
        self.seen_dynamic_ids: set = set()

        # WS 监控
        self._ws_connections: dict = {}
        self._ws_running = False
        self._ws_task = None

    # ========== 生命周期 ==========

    async def start(self):
        logger.info("启动浏览器...")
        self.playwright = await async_playwright().start()
        cfg = self.config.browser
        headless = not self.config.development
        user_data_dir = Path(cfg.get('user_data_dir', './data/browser_profile'))
        user_data_dir.mkdir(parents=True, exist_ok=True)

        self.context = await self.playwright.chromium.launch_persistent_context(
            str(user_data_dir),
            channel=cfg.get('channel', 'chromium'),
            headless=headless,
            args=["--disable-blink-features=AutomationControlled",
                  "--no-sandbox", "--disable-dev-shm-usage"],
            viewport=cfg.get('viewport', {"width": 1280, "height": 720})
        )
        logger.info(f"浏览器启动完成 (headless={headless})")

    async def login_and_get_page(self) -> Page:
        assert self.context is not None
        page = await self.context.new_page()
        self._setup_feed_interceptor(page)

        logger.info("访问B站动态页面...")
        await page.goto("https://t.bilibili.com/")
        await page.wait_for_load_state("networkidle")
        await asyncio.sleep(1)
        logger.info(f"API 拦截: 捕获到 {len(self.feed_cache)} 条动态")

        if await page.query_selector(".bili-dyn-login-register"):
            logger.info("检测到未登录，开始二维码登录...")
            await self._qrcode_login(page)
            await page.reload()
            await page.wait_for_load_state("networkidle")
        else:
            logger.info("已登录状态")

        self.page = page
        return page

    async def stop(self):
        if self._ws_running:
            self._ws_running = False
            if self._ws_task:
                self._ws_task.cancel()
        if self.context:
            await self.context.close()
        if self.playwright:
            await self.playwright.stop()
        logger.info("浏览器已关闭")

    # ========== 二维码登录 ==========

    async def _qrcode_login(self, page: Page):
        btn = await page.query_selector(".bili-dyn-login-register__login-btn")
        if not btn:
            raise Exception("找不到登录按钮")
        await btn.click()
        logger.info("已点击登录按钮")
        await page.wait_for_selector(".bili-mini-scan-code img", timeout=10000)

        canvas = await page.query_selector(".bili-mini-scan-code canvas")
        if canvas:
            title = await canvas.get_attribute("title") or ""
            m = re.search(r"qrcode_key=([^&]+)", title)
            if m:
                login_url = f"https://passport.bilibili.com/qrcode/getLoginInfo?qrcode_key={m.group(1)}"
                try:
                    import qrcode  # type: ignore
                    qr = qrcode.QRCode(box_size=2, border=1)
                    qr.add_data(login_url)
                    qr.make(fit=True)
                    print("\n" + "=" * 50)
                    print("请使用B站APP扫描以下二维码登录：")
                    print("=" * 50)
                    qr.print_ascii(tty=True)
                    print("=" * 50 + "\n")
                except ImportError:
                    img_src = await page.get_attribute(".bili-mini-scan-code img", "src")
                    if img_src and img_src.startswith("data:image"):
                        data = base64.b64decode(img_src.split(",")[1])
                        p = Path("./data/qrcode.png")
                        p.parent.mkdir(exist_ok=True)
                        p.write_bytes(data)
                        logger.info(f"二维码已保存: {p}")
        logger.info("等待扫码登录...")
        await page.wait_for_selector(".bili-mini-scan", state="detached", timeout=120000)
        logger.info("登录成功！")

    # ========== 页面操作 ==========

    async def create_temp_page(self) -> Page:
        assert self.context is not None
        return await self.context.new_page()

    async def follow_user(self, user_id: str) -> bool:
        page = await self.create_temp_page()
        try:
            await page.goto(f"https://space.bilibili.com/{user_id}",
                            wait_until="networkidle", timeout=15000)
            await page.wait_for_selector(".follow-btn-wrapper", timeout=10000)
            btn = await page.query_selector(".space-follow-btn")
            if not btn:
                return False
            text = (await btn.text_content() or "").strip()
            if "已关注" in text:
                return True
            if "关注" in text:
                await btn.click()
                await page.wait_for_timeout(2000)
                captcha = await page.query_selector(".risk-captcha-adapt")
                if captcha:
                    logger.warning(f"⚠️ 风控验证码，请手动关注 {user_id}: https://space.bilibili.com/{user_id}")
                    return False
                new_btn = await page.query_selector(".space-follow-btn")
                new_text = (await new_btn.text_content() if new_btn else None) or ""
                return "已关注" in new_text
            return False
        except Exception as e:
            logger.error(f"关注失败 {user_id}: {e}")
            return False
        finally:
            await page.close()

    # ========== 状态读取 ==========

    async def get_my_uid(self) -> Optional[str]:
        assert self.page is not None
        link = await self.page.query_selector("a[href*='space.bilibili.com'][href*='fan']")
        if link:
            href = await link.get_attribute("href") or ""
            m = re.search(r"space\.bilibili\.com/(\d+)", href)
            if m:
                return m.group(1)
        return None

    async def get_following_count(self) -> Optional[int]:
        assert self.page is not None
        try:
            await self.page.wait_for_selector(".bili-dyn-my-info__stat__item", timeout=8000)
            await asyncio.sleep(0.5)
            for item in await self.page.query_selector_all(".bili-dyn-my-info__stat__item"):
                href = await item.get_attribute("href") or ""
                label_el = await item.query_selector(".item-desc")
                num_el = await item.query_selector(".item-num")
                if label_el and num_el:
                    label = (await label_el.text_content() or "").strip()
                    val = (await num_el.text_content() or "").strip()
                    if "fans/follow" in href and "关注" in label and val.isdigit():
                        return int(val)
        except Exception as e:
            logger.error(f"读取关注数失败: {e}")
        return None

    @property
    def is_logged_in(self) -> bool:
        return self.page is not None

    # ========== API Feed 拦截 ==========

    def _setup_feed_interceptor(self, page: Page):
        async def on_response(response):
            if "/x/polymer/web-dynamic/v1/feed/" in response.url and response.status == 200:
                try:
                    body = await response.json()
                    items = body.get("data", {}).get("items", [])
                    if items:
                        self.feed_cache = items
                        self.feed_captured = True
                except Exception:
                    pass
        page.on("response", on_response)

    def get_feed_dynamics(self) -> list[dict]:
        """解析 API feed 为统一格式"""
        if not self.feed_cache:
            return []
        SKIP_IMG = ["/face/", "/game/", "/garb/open/", "/garb/item/"]
        dynamics = []
        for item in self.feed_cache:
            mod = item.get("modules", {})
            author = mod.get("module_author", {})
            dyn = mod.get("module_dynamic", {})
            stat = mod.get("module_stat", {})
            orig = item.get("orig")

            major = dyn.get("major", {}) or {}
            opus = major.get("opus", {}) or {}
            archive = major.get("archive", {}) or {}
            desc = (dyn.get("desc") or {}).get("text", "")

            # 图片
            images = []
            seen = set()
            for pic in opus.get("pics", []):
                u = pic.get("url", "")
                if u and not any(p in u for p in SKIP_IMG) and u not in seen:
                    seen.add(u); images.append(u)
            cover = archive.get("cover", "")
            if cover and not any(p in cover for p in SKIP_IMG) and cover not in seen:
                images.append(cover)

            # 类型
            dtype = item.get("type", "")
            tmap = {"DYNAMIC_TYPE_AV": "video", "DYNAMIC_TYPE_DRAW": "text",
                    "DYNAMIC_TYPE_FORWARD": "forward", "DYNAMIC_TYPE_WORD": "text",
                    "DYNAMIC_TYPE_ARTICLE": "text"}
            dyn_type = tmap.get(dtype, "text")
            is_article = (dtype == "DYNAMIC_TYPE_ARTICLE")

            # 内容：desc 优先，否则取 opus 的 title + summary 合并
            opus_title = opus.get("title", "")
            opus_text = opus.get("summary", {}).get("text", "")
            has_more = opus.get("summary", {}).get("has_more", False)
            opus_id = item.get("id_str", "")
            if desc:
                content_text = desc
            elif opus_title and opus_text:
                content_text = f"{opus_title}\n\n{opus_text}"
            else:
                content_text = opus_title or opus_text

            result = {
                "id": opus_id,
                "type": dyn_type,
                "author_id": str(author.get("mid", "")),
                "author": author.get("name", ""),
                "publish_time": author.get("pub_time", ""),
                "publish_ts": author.get("pub_ts", 0),
                "content": content_text,
                "title": opus_title or archive.get("title", ""),
                "images": images,
                "visible": item.get("visible", True),
                "like": stat.get("like", {}).get("count", 0),
                "comment": stat.get("comment", {}).get("count", 0),
                "forward_count": stat.get("forward", {}).get("count", 0),
            }

            # 仅 ARTICLE 类型从详情页补完
            if is_article and opus_id:
                result["_need_detail"] = opus_id

            if dyn_type == "video":
                result["bvid"] = archive.get("bvid", "")
                result["url"] = archive.get("jump_url", "")

            if dyn_type == "forward" and orig:
                orig_mod = orig.get("modules", {})
                result["forward_comment"] = desc
                result["forward_author"] = orig_mod.get("module_author", {}).get("name", "")
                result["forward_summary"] = (orig_mod.get("module_dynamic", {}).get("desc") or {}).get("text", "")
                fwd = desc
                if ("恭喜" in fwd and "中奖" in fwd) or ("已私信通知" in fwd and "抽奖" in fwd):
                    result["skip_notify"] = True
                    result["skip_reason"] = "转发抽奖结果"
                else:
                    result["skip_notify"] = False

            dynamics.append(result)
        return dynamics

    async def fetch_opus_detail(self, opus_id: str) -> dict:
        """访问详情页，从 __INITIAL_STATE__ 提取完整文本和图片"""
        page = await self.create_temp_page()
        try:
            url = f"https://www.bilibili.com/opus/{opus_id}"
            await page.goto(url, wait_until="networkidle", timeout=15000)
            await page.wait_for_timeout(1000)

            # 提取 __INITIAL_STATE__ JSON
            state_raw = await page.evaluate("() => window.__INITIAL_STATE__")
            if not state_raw:
                logger.debug(f"[opus] {opus_id}: 无 __INITIAL_STATE__")
                return {}

            detail = state_raw.get("detail", {})
            modules = detail.get("modules", [])
            title = ""
            full_text = ""
            images = []
            seen = set()

            for mod in modules:
                mtype = mod.get("module_type", "")
                if mtype == "MODULE_TYPE_TITLE":
                    title = mod.get("text", "")
                elif mtype == "MODULE_TYPE_CONTENT":
                    for para in mod.get("paragraphs", []):
                        for node in para.get("text", {}).get("nodes", []):
                            if node.get("type") == "TEXT_NODE_TYPE_WORD":
                                full_text += node.get("word", {}).get("words", "")
                            elif node.get("type") == "TEXT_NODE_TYPE_EMOJI":
                                full_text += node.get("emoji", {}).get("text", "")
                elif mtype == "MODULE_TYPE_TOP":
                    album = mod.get("module_top", {}).get("display", {}).get("album", {})
                    for pic in album.get("pics", []):
                        u = pic.get("url", "")
                        if u and u not in seen:
                            seen.add(u); images.append(u)

            # 合并 title + 正文
            if title and full_text:
                content = f"{title}\n\n{full_text}"
            else:
                content = title or full_text

            logger.info(
                f"[opus] {opus_id}: title='{title[:30]}' "
                f"text_len={len(full_text)} images={len(images)}"
            )
            return {"content": content, "images": images} if images else {"content": content}

        except Exception as e:
            logger.error(f"[opus] {opus_id} 抓取失败: {e}")
            return {}
        finally:
            await page.close()

    # ========== 页面扫描（新动态通知） ==========

    async def check_and_fetch_new(self) -> List[Dict[str, Any]]:
        if not await self._has_notification():
            return []
        await self._click_notification()
        await asyncio.sleep(3)
        all_dyn = self.get_feed_dynamics()
        new_dyn = []
        for d in all_dyn:
            did = d.get("id", "")
            if did and did not in self.seen_dynamic_ids:
                self.seen_dynamic_ids.add(did)
                new_dyn.append(d)
        if new_dyn:
            types = {}
            for d in new_dyn:
                t = d.get("type", "?")
                types[t] = types.get(t, 0) + 1
            logger.info(f"[扫描] 新动态: {len(new_dyn)} 条 " +
                        " ".join(f"{k}={v}" for k, v in types.items()))
        return new_dyn

    async def _has_notification(self) -> bool:
        try:
            el = await self.page.query_selector(".bili-dyn-list-notification")
            if el:
                logger.info(f"检测到新动态通知: {await el.text_content()}")
                return True
            return False
        except Exception:
            return False

    async def _click_notification(self):
        try:
            el = await self.page.query_selector(".bili-dyn-list-notification")
            if el:
                await el.click()
                logger.info("已点击新动态通知按钮")
                await self.page.wait_for_timeout(2000)
        except Exception as e:
            logger.error(f"点击通知失败: {e}")

    # ========== WebSocket 监控 ==========

    def start_ws_monitor(self):
        self._ws_running = True
        self.page.on("websocket", self._on_ws)
        self._ws_task = asyncio.create_task(self._ws_health_loop())
        logger.info("WebSocket 监听已启动")

    def _on_ws(self, ws):
        url = ws.url
        if "bilibili.com" not in url and "broadcast.chat" not in url:
            return
        cid = id(ws)
        self._ws_connections[cid] = {"url": url, "start": datetime.now(), "alive": True}
        logger.info(f"WS 连接: {url[:60]}...")

        def on_close():
            if cid in self._ws_connections:
                dur = (datetime.now() - self._ws_connections[cid]["start"]).total_seconds()
                logger.info(f"WS 断开: {url[:60]}... (存活 {dur:.0f}s)")
                self._ws_connections[cid]["alive"] = False
        ws.on("close", on_close)

    async def _ws_health_loop(self):
        interval = self.config.ws_monitor.get('check_interval', 300)
        while self._ws_running:
            await asyncio.sleep(interval)
            alive = sum(1 for c in self._ws_connections.values() if c["alive"])
            if alive:
                logger.debug(f"WS 健康: {alive} 个连接正常")

    async def stop_ws_monitor(self):
        self._ws_running = False
        if self._ws_task:
            self._ws_task.cancel()
