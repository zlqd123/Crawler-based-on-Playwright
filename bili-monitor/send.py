#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""测试用 API — 读取本地缓存，每次返回一条随机动态（id 替换为时间戳）"""

import asyncio
import sys
import signal
import random
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from aiohttp import web
from monitor.config import Config
from monitor.storage import StorageManager
from loguru import logger


class SendServer:
    """模拟 API — 只读缓存，不抓取"""

    def __init__(self, config_path: str = "config.yaml"):
        self.config = Config(config_path)
        self.storage: StorageManager | None = None
        self.app: web.Application | None = None
        self.runner: web.AppRunner | None = None
        self._setup_logging()

    def _setup_logging(self):
        log_cfg = self.config.logging
        logger.remove()
        logger.add(sys.stdout, level="INFO", colorize=True,
                   format="<green>{time:HH:mm:ss}</green> | <level>{message}</level>")

    async def start(self):
        logger.info("Send 测试服务启动")

        # 加载存储（只读缓存，不抓取）
        self.storage = StorageManager(self.config)
        await self.storage.initialize()

        # 构建 API
        self.app = web.Application(middlewares=[self._log_middleware])
        self._setup_routes()

        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        api_cfg = self.config.api
        host = api_cfg.get("host", "127.0.0.1")
        port = api_cfg.get("port", 8787)
        site = web.TCPSite(self.runner, host, port)
        await site.start()
        logger.info(f"监听: http://{host}:{port} (send 模式)")

        # 保持运行
        while True:
            await asyncio.sleep(3600)

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()

    def _setup_routes(self):
        assert self.app is not None
        ep = self.config.api.get("endpoints", {})
        self.app.router.add_get(ep.get("user", "/api/user/{id}"), self.get_user_info)
        self.app.router.add_get(ep.get("dynamic", "/api/dynamic/{id}"), self.get_user_dynamics)
        self.app.router.add_get(ep.get("dynamics", "/api/dynamics"), self.get_all_dynamics)
        self.app.router.add_get("/health", self.health_check)

    @web.middleware
    async def _log_middleware(self, request, handler):
        logger.info(f"[send] {request.method} {request.path_qs}")
        return await handler(request)

    # ========== 用户昵称（从缓存读） ==========

    async def get_user_info(self, request):
        user_id = request.match_info.get("id", "")
        name = self.storage.get_nickname(user_id) if self.storage else ""
        return web.json_response({
            "id": user_id,
            "name": name or f"用户_{user_id}",
            "cached": bool(name)
        })

    # ========== 随机返回一条动态 ==========

    async def get_user_dynamics(self, request):
        user_id = request.match_info.get("id", "")
        if not user_id:
            return web.json_response({"error": "missing user id"}, status=400)

        # 读取该用户所有缓存动态
        all_dyn = await self.storage.get_user_dynamics(user_id, limit=200)

        if not all_dyn:
            return web.json_response({
                "user_id": user_id, "count": 0, "dynamics": []
            })

        # 随机选一条
        dyn = random.choice(all_dyn).copy()
        # 替换 ID 为毫秒时间戳
        dyn["id"] = str(int(time.time() * 1000))
        # 更新时间戳
        dyn["publish_time"] = "刚刚"
        dyn["publish_ts"] = int(time.time())

        logger.info(
            f"[send] {user_id} -> id={dyn['id']} "
            f"type={dyn.get('type','?')} title={dyn.get('title','')[:30]}"
        )

        return web.json_response({
            "user_id": user_id, "count": 1, "dynamics": [dyn]
        })

    async def get_all_dynamics(self, request):
        """每个用户返回一条随机"""
        assert self.storage is not None
        result = {}
        for uid in await self.storage.get_watchlist():
            all_dyn = await self.storage.get_user_dynamics(uid, limit=200)
            if all_dyn:
                dyn = random.choice(all_dyn).copy()
                dyn["id"] = str(int(time.time() * 1000))
                dyn["publish_time"] = "刚刚"
                dyn["publish_ts"] = int(time.time())
                result[uid] = [dyn]
        return web.json_response({
            "user_count": len(result), "dynamics": result
        })

    async def health_check(self, request):
        return web.json_response({"status": "ok"})


async def main():
    server = SendServer("config.yaml")
    try:
        await server.start()
    except asyncio.CancelledError:
        pass
    finally:
        await server.stop()


if __name__ == "__main__":
    asyncio.run(main())
