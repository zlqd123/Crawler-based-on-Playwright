#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""B站动态监控系统 — 入口"""

import asyncio
import sys
import signal
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))

from monitor.config import Config
from monitor.browser import BrowserManager
from monitor.storage import StorageManager
from monitor.api import APIServer
from loguru import logger


class BilibiliMonitor:
    """监控系统主类 — 编排启动/停止流程"""

    def __init__(self, config_path: str = "config.yaml"):
        self.config = Config(config_path)
        self._setup_logging()

        self.browser: Optional[BrowserManager] = None
        self.storage: Optional[StorageManager] = None
        self.api: Optional[APIServer] = None
        self._scanner_task = None
        self.is_running = False

    # ========== 日志 ==========

    def _setup_logging(self):
        log_cfg = self.config.logging
        logger.remove()
        logger.add(sys.stdout, level=log_cfg.get('level', 'INFO'), colorize=True,
                   format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>")
        log_file = Path(log_cfg.get('file', './data/logs/monitor.log'))
        log_file.parent.mkdir(parents=True, exist_ok=True)
        logger.add(log_file, level=log_cfg.get('level', 'INFO'), encoding="utf-8",
                   format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}",
                   rotation=f"{log_cfg.get('max_size_mb', 100)} MB",
                   retention=log_cfg.get('backup_count', 5))
        logger.info("日志系统初始化完成")

    # ========== 生命周期 ==========

    async def start(self):
        logger.info("=" * 60)
        logger.info(f"B站动态监控系统启动 ({'开发' if self.config.development else '生产'}模式)")
        logger.info("=" * 60)

        self.is_running = True
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        try:
            # 1. 存储
            self.storage = StorageManager(self.config)
            await self.storage.initialize()

            # 2. 浏览器
            self.browser = BrowserManager(self.config)
            await self.browser.start()
            page = await self.browser.login_and_get_page()
            if not page:
                logger.error("登录失败，退出")
                return

            # 3. WS 监控 + 页面扫描
            self.browser.start_ws_monitor()
            self._scanner_task = asyncio.create_task(self._scanner_loop())

            # 4. API 服务
            if self.config.api.get('enabled', True):
                self.api = APIServer(self.config, self.storage, self.browser)
                await self.api.start()

            # 5. 启动抓取 + 关注同步
            await self._startup_scrape()
            await self._check_following_sync()

            # 6. 保持运行
            logger.info("系统运行中... 按 Ctrl+C 停止")
            while self.is_running:
                await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"运行异常: {e}")
        finally:
            await self.stop()

    async def stop(self):
        logger.info("正在停止...")
        if self._scanner_task:
            self._scanner_task.cancel()
            try:
                await self._scanner_task
            except asyncio.CancelledError:
                pass
        if self.api:
            await self.api.stop()
        if self.browser:
            self.browser.stop_ws_monitor()
            await self.browser.stop()
        logger.info("监控系统已停止")

    def _signal_handler(self, signum, frame):
        logger.info(f"收到信号 {signum}，准备退出...")
        self.is_running = False

    # ========== 启动任务 ==========

    async def _startup_scrape(self):
        """启动时从 API feed 抓取已有动态"""
        assert self.browser and self.storage
        dynamics = self.browser.get_feed_dynamics()
        if not dynamics:
            logger.info("API 缓存为空，无初始动态")
            return
        await self._complete_details(dynamics)
        for d in dynamics:
            await self.storage.save_dynamic(d.get("author_id", "unknown"), d)
            self.browser.seen_dynamic_ids.add(d.get("id", ""))
        types = {}
        for d in dynamics:
            t = d.get("type", "?")
            types[t] = types.get(t, 0) + 1
        logger.info(f"启动抓取: {len(dynamics)} 条 " +
                    " ".join(f"{k}={v}" for k, v in types.items()))

    async def _complete_details(self, dynamics: list):
        """补充需要详情页补完的动态（长文本+图片）"""
        for d in dynamics:
            opus_id = d.pop("_need_detail", None)
            if opus_id:
                detail = await self.browser.fetch_opus_detail(opus_id)
                if detail.get("content"):
                    d["content"] = detail["content"]
                if detail.get("images"):
                    d["images"] = detail["images"]
                logger.debug(f"[detail] {opus_id}: text={len(d['content'])} imgs={len(d['images'])}")

    async def _check_following_sync(self):
        """对比监控数量与 B站 关注数量"""
        assert self.storage and self.browser
        wl = await self.storage.get_watchlist()
        wc = len(wl)
        fc = await self.browser.get_following_count()
        if fc is None:
            logger.warning("无法获取 B站 关注数")
            return
        if wc == fc:
            users = []
            for uid in sorted(wl):
                n = self.storage.get_nickname(uid)
                users.append(f"{uid}({n})" if n else uid)
            logger.info(f"✅ 关注同步 ({wc} 人): " + ", ".join(users))
        else:
            logger.warning(f"⚠️ 数量不一致: 监控 {wc} | B站 {fc} (差 {abs(wc-fc)})")

    # ========== 后台扫描 ==========

    async def _scanner_loop(self):
        """每 3 秒检测新动态通知"""
        assert self.browser and self.storage
        while self.is_running:
            try:
                new_dyn = await self.browser.check_and_fetch_new()
                await self._complete_details(new_dyn)
                for d in new_dyn:
                    if d.get("skip_notify"):
                        logger.info(f"跳过: {d.get('skip_reason','')} [{d['id']}]")
                    else:
                        await self.storage.save_dynamic(d.get("author_id", "unknown"), d)
            except Exception as e:
                logger.error(f"扫描异常: {e}")
            await asyncio.sleep(3)


async def main():
    await BilibiliMonitor("config.yaml").start()


if __name__ == "__main__":
    asyncio.run(main())
