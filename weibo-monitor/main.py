#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""微博动态监控系统 — 入口"""

import asyncio
import sys
import signal
from pathlib import Path
from typing import Optional, Dict, Any

sys.path.insert(0, str(Path(__file__).parent))

from monitor.config import Config
from monitor.browser import BrowserManager
from monitor.storage import StorageManager
from monitor.api import APIServer
from loguru import logger


class WeiboMonitor:
    """监控系统主类"""

    def __init__(self, config_path: str = "config.yaml"):
        self.config = Config(config_path)
        self._setup_logging()
        self.browser: Optional[BrowserManager] = None
        self.storage: Optional[StorageManager] = None
        self.api: Optional[APIServer] = None
        self._scanner_task = None
        self.is_running = False

    def _setup_logging(self):
        log_cfg = self.config.logging
        logger.remove()
        logger.add(sys.stdout, level=log_cfg.get('level', 'INFO'), colorize=True,
                   format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>")
        log_file = Path(log_cfg.get('file', './data/logs/weibo_monitor.log'))
        log_file.parent.mkdir(parents=True, exist_ok=True)
        logger.add(log_file, level=log_cfg.get('level', 'INFO'), encoding="utf-8",
                   format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}",
                   rotation=f"{log_cfg.get('max_size_mb', 10)} MB",
                   retention=log_cfg.get('backup_count', 2))
        logger.info("日志系统初始化完成")

    def _signal_handler(self, signum, frame):
        logger.info(f"收到信号 {signum}，准备退出...")
        self.is_running = False

    async def _on_new_weibo(self, weibo: Dict[str, Any]):
        """新微博回调（日志已在 browser 拦截器中汇总输出）"""
        if self.storage:
            await self.storage.save_weibo(weibo)
            if weibo.get("images"):
                for idx, img_url in enumerate(weibo["images"][:3]):
                    save_path = f"./data/images/{weibo['id']}_{idx}.jpg"
                    if self.browser:
                        await self.browser.download_image(img_url, save_path)

    async def start(self):
        """启动监控系统"""
        logger.info("=" * 60)
        logger.info(f"微博动态监控系统启动 ({'开发' if self.config.development else '生产'}模式)")
        logger.info("=" * 60)

        self.is_running = True
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        try:
            self.storage = StorageManager(self.config)
            await self.storage.initialize()

            self.browser = BrowserManager(self.config)
            await self.browser.start()
            page = await self.browser.login_and_get_page(on_new_weibo_callback=self._on_new_weibo)
            if not page:
                logger.error("登录失败，退出")
                return

            if self.config.api.get('enabled', True):
                self.api = APIServer(self.config, self.storage, self.browser)
                await self.api.start()

            self._scanner_task = asyncio.create_task(self._scanner_loop())
            logger.info("系统运行中... 按 Ctrl+C 停止")
            while self.is_running:
                await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"运行异常: {e}")
        finally:
            await self.stop()

    async def _scanner_loop(self):
        """后台扫描 NEW 按钮"""
        interval = self.config.monitor.get('check_interval', 2)
        while self.is_running:
            try:
                if self.browser:
                    await self.browser._check_and_refresh_by_new_badge()
                await asyncio.sleep(interval)
            except Exception as e:
                logger.error(f"扫描异常: {e}")
                await asyncio.sleep(interval)

    async def stop(self):
        """停止监控系统"""
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
            await self.browser.stop()
        logger.info("监控系统已停止")


async def main():
    monitor = WeiboMonitor("config.yaml")
    await monitor.start()


if __name__ == "__main__":
    asyncio.run(main())
