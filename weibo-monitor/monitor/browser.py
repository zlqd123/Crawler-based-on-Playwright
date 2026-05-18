# -*- coding: utf-8 -*-

"""浏览器管理 — 微博登录 + API拦截 + NEW按钮检测 + 长文本补全"""

import asyncio
import base64
import random
import time
import aiohttp
from pathlib import Path
from typing import Optional, List, Dict, Any, Callable
from playwright.async_api import async_playwright, BrowserContext, Page
from loguru import logger
from .parser import WeiboParser


class BrowserManager:
    """统一浏览器管理器"""

    def __init__(self, config):
        self.config = config
        self.playwright = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.feed_cache: Dict[str, Any] = {}
        self.seen_weibo_ids: set = set()
        self._setup_interceptor = False
        self._last_response_time = 0
        self._response_count = 0
        self._on_new_weibo_callback: Optional[Callable] = None
        self._last_refresh_time = 0
        self._refresh_cooldown = 30

    async def start(self):
        """启动浏览器"""
        logger.info("启动浏览器...")
        self.playwright = await async_playwright().start()
        cfg = self.config.browser
        headless = not self.config.development
        user_data_dir = Path(cfg.get('user_data_dir', './data/browser_profile'))
        user_data_dir.mkdir(parents=True, exist_ok=True)

        try:
            self.context = await self.playwright.chromium.launch_persistent_context(
                str(user_data_dir),
                channel=cfg.get('channel', 'chromium'),
                headless=headless,
                args=["--disable-blink-features=AutomationControlled",
                      "--no-sandbox", "--disable-dev-shm-usage"],
                viewport=cfg.get('viewport', {"width": 1280, "height": 720})
            )
        except Exception as e:
            logger.warning(f"浏览器启动失败: {e}，清理 profile 后重试...")
            import shutil
            shutil.rmtree(user_data_dir, ignore_errors=True)
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

    async def login_and_get_page(self, on_new_weibo_callback: Optional[Callable] = None) -> Optional[Page]:
        """登录并返回页面"""
        assert self.context is not None
        self._on_new_weibo_callback = on_new_weibo_callback
        page = await self.context.new_page()

        logger.info("访问微博...")
        await page.goto("https://weibo.com/", wait_until="networkidle")
        await asyncio.sleep(2)

        need_login = await self._check_need_login(page)
        if need_login:
            logger.info("检测到未登录，开始二维码登录...")
            await self._qrcode_login(page)
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(3)
            logger.info("登录完成")
        else:
            logger.info("已检测到登录状态")

        self.page = page
        self._setup_feed_interceptor()
        logger.info("feed API 拦截器已启动")

        delay = random.uniform(3, 5)
        logger.info(f"等待 {delay:.1f} 秒后进入最新微博...")
        await asyncio.sleep(delay)
        await self._goto_latest_feed()
        return page

    async def _check_need_login(self, page: Page) -> bool:
        """检查是否需要登录"""
        try:
            login_btn = await page.query_selector("button:has-text('登录')")
            if login_btn and await login_btn.is_visible():
                return True
            home_btn = await page.query_selector("a[href='/']")
            if home_btn and await home_btn.is_visible():
                return False
            return "passport" in page.url or "login" in page.url
        except Exception:
            return True

    async def _qrcode_login(self, page: Page):
        """二维码登录"""
        try:
            async with page.context.expect_page(timeout=90000) as popup_info:
                login_btn = await page.wait_for_selector("button:has-text('登录')", timeout=10000)
                if login_btn is None:
                    raise Exception("未找到登录按钮")
                await login_btn.click()
                logger.info("已点击登录按钮，等待弹窗...")

            popup = await popup_info.value
            logger.info("登录弹窗已打开")
            await popup.wait_for_load_state()
            await asyncio.sleep(2)

            qr_img = await self._find_qrcode_in_popup(popup)
            if qr_img:
                qr_path = await self._save_qrcode(popup, qr_img)
                await self._print_ascii_qrcode(qr_path)
                logger.info("请使用微博 APP 扫描二维码登录")
            else:
                logger.warning("未找到二维码")

            await self._wait_for_login_success(page, popup)
            logger.info("登录成功！")
        except Exception as e:
            logger.error(f"二维码登录失败: {e}")
            raise

    async def _find_qrcode_in_popup(self, popup: Page):
        """查找登录弹窗中的二维码图片"""
        for selector in ["img[src*='qr.weibo.cn']", "img[src*='qrcode']", "img[src*='qr']"]:
            try:
                img = await popup.query_selector(selector)
                if img and await img.is_visible():
                    return img
            except Exception:
                continue
        return None

    async def _save_qrcode(self, popup: Page, qr_img) -> Path:
        """保存二维码图片"""
        save_path = Path("./data/qrcode.png")
        save_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            src = await qr_img.get_attribute("src")
            if src and src.startswith("data:image"):
                data = base64.b64decode(src.split(",")[1])
                save_path.write_bytes(data)
            else:
                await qr_img.screenshot(path=str(save_path))
        except Exception:
            await popup.screenshot(path=str(save_path))
        return save_path

    async def _print_ascii_qrcode(self, qr_path: Path):
        """在终端打印 ASCII 二维码"""
        print("\n" + "=" * 60)
        print("请使用微博 APP 扫描下方二维码登录")
        print("=" * 60)
        try:
            from PIL import Image
            import qrcode
            from pyzbar.pyzbar import decode
            img = Image.open(qr_path)
            decoded = decode(img)
            if decoded:
                qr_url = decoded[0].data.decode("utf-8")
                qr = qrcode.QRCode(box_size=1, border=1)
                qr.add_data(qr_url)
                qr.make(fit=True)
                qr.print_ascii(tty=True)
        except Exception:
            print(f"请手动打开图片: {qr_path}")
        print("=" * 60 + "\n")

    async def _wait_for_login_success(self, page: Page, popup: Page):
        """等待扫码完成"""
        try:
            await popup.wait_for_event("close", timeout=90000)
            logger.info("登录弹窗已关闭")
            await page.reload()
            await page.wait_for_load_state()
            await asyncio.sleep(2)
        except Exception:
            raise Exception("登录超时")

    async def _goto_latest_feed(self):
        """点击"最新微博"按钮"""
        if not self.page:
            logger.error("页面未初始化")
            return
        try:
            latest_btn = await self.page.query_selector("a:has-text('最新微博')")
            if latest_btn:
                await latest_btn.click()
                logger.info("已点击最新微博按钮")
                await self.page.wait_for_load_state("networkidle")
                await asyncio.sleep(2)
            else:
                await self.page.goto("https://weibo.com/mygroups?gid=110003762127105")
                await self.page.wait_for_load_state("networkidle")
                await asyncio.sleep(2)
        except Exception as e:
            logger.warning(f"进入最新微博失败: {e}")

    async def _check_and_refresh_by_new_badge(self):
        """检测 NEW 徽章并刷新页面"""
        if not self.page:
            return False
        current_time = time.time()
        if current_time - self._last_refresh_time < self._refresh_cooldown:
            return False
        try:
            new_selectors = [
                "span.woo-badge-msg.woo-badge-new",
                ".woo-badge-new:has-text('NEW')",
                "span[class*='woo-badge-new']",
            ]
            new_badge = None
            for sel in new_selectors:
                new_badge = await self.page.query_selector(sel)
                if new_badge:
                    break
            if new_badge:
                try:
                    visible = await new_badge.is_visible()
                except Exception:
                    visible = True
                if visible:
                    logger.info("🔔 检测到 NEW 徽章，5秒后刷新页面...")
                    await asyncio.sleep(5)
                    await self.page.reload()
                    await self.page.wait_for_load_state("networkidle")
                    await asyncio.sleep(3)
                    self._last_refresh_time = time.time()
                    logger.info("页面刷新完成")
                    return True
        except Exception as e:
            logger.debug(f"NEW 按钮检测失败: {e}")
        return False

    def _setup_feed_interceptor(self):
        """设置 feed API 拦截器"""
        if self._setup_interceptor or not self.page:
            return

        async def on_response(response):
            if "/ajax/feed/friendstimeline" in response.url and response.status == 200:
                try:
                    body = await response.json()
                    if not body.get("statuses"):
                        return
                    self._response_count += 1
                    self._last_response_time = time.time()

                    statuses = body["statuses"]
                    raw_by_id: Dict[str, Dict[str, Any]] = {
                        str(s.get("idstr") or s.get("id") or ""): s
                        for s in statuses
                        if s.get("idstr") or s.get("id")
                    }

                    parser = WeiboParser.from_dict(body)
                    all_weibos = parser.get_all_weibo()

                    new_weibos = []
                    for w in all_weibos:
                        wid = w.get("idstr") or w.get("id")
                        if wid and str(wid) not in self.seen_weibo_ids:
                            self.seen_weibo_ids.add(str(wid))
                            new_weibos.append(w)

                    if new_weibos:
                        authors = {}
                        for w in new_weibos:
                            author = w.get("user", {}).get("screen_name", "未知")
                            authors[author] = authors.get(author, 0) + 1
                        summary = ", ".join([f"{a}({c})" for a, c in authors.items()])
                        logger.info(f"📢 抓取到 {len(new_weibos)} 条新微博: {summary}")

                        expanded_ids = []
                        for weibo in new_weibos:
                            wid = str(weibo.get("idstr") or weibo.get("id") or "")
                            raw = raw_by_id.get(wid, {})
                            raw_html = raw.get("text", "")
                            if (
                                weibo.get("isLongText") or
                                raw.get("isLongText") or
                                '<span class="expand">' in raw_html or
                                "展开" in raw_html or
                                weibo.get("text_raw", "").rstrip().endswith("...") or
                                weibo.get("text", "").rstrip().endswith("...")
                            ):
                                expanded_ids.append(wid)
                                await self._expand_long_text(weibo)
                        if expanded_ids:
                            preview = ", ".join(expanded_ids[:3])
                            if len(expanded_ids) > 3:
                                preview += f" ...+{len(expanded_ids)-3}"
                            logger.info(f"✅ 补全长文本: {len(expanded_ids)} 条 ({preview})")

                        if self._on_new_weibo_callback:
                            for weibo in new_weibos:
                                await self._on_new_weibo_callback(weibo)

                    self.feed_cache = body
                except Exception as e:
                    logger.debug(f"解析 feed 响应失败: {e}")

        self.page.on("response", on_response)
        self._setup_interceptor = True

    async def _expand_long_text(self, weibo: Dict[str, Any]):
        """调用 longtext API 补全截断文本"""
        mblogid = weibo.get("mblogid")
        if not mblogid:
            return
        try:
            cookies = {}
            if self.context:
                browser_cookies = await self.context.cookies()
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

    async def monitor_loop(self):
        """监控循环"""
        interval = self.config.monitor.get('check_interval', 5)
        while self._scanning:
            try:
                await self._check_and_refresh_by_new_badge()
                await asyncio.sleep(interval)
            except Exception as e:
                logger.error(f"监控循环异常: {e}")
                await asyncio.sleep(interval)

    async def get_cookies(self) -> Dict[str, str]:
        """获取当前浏览器 cookies"""
        if self.context:
            cookies = await self.context.cookies()
            return {c.get('name', ''): c.get('value', '') for c in cookies}
        return {}

    async def download_image(self, url: str, save_path: str):
        """下载图片（带 Referer 头绕过防盗链）"""
        headers = {
            "Referer": "https://weibo.com/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:150.0) Gecko/20100101 Firefox/150.0",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status == 200:
                        Path(save_path).parent.mkdir(parents=True, exist_ok=True)
                        Path(save_path).write_bytes(await resp.read())
                        logger.debug(f"图片下载成功: {save_path}")
                    else:
                        logger.debug(f"图片下载失败 {url}: {resp.status}")
        except Exception as e:
            logger.debug(f"图片下载异常 {url}: {e}")

    async def create_temp_page(self) -> Page:
        """创建临时页面"""
        assert self.context is not None
        return await self.context.new_page()

    async def stop(self):
        """停止浏览器"""
        self._scanning = False
        if self.context:
            await self.context.close()
        if self.playwright:
            await self.playwright.stop()
        logger.info("浏览器已关闭")
