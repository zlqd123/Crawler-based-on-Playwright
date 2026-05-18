# -*- coding: utf-8 -*-

"""存储管理 — 文件缓存 + 关注列表 + 昵称"""

import json
import asyncio
from pathlib import Path
from typing import Dict, Any, List, Set
from datetime import datetime
from loguru import logger


class StorageManager:
    """存储管理器"""

    def __init__(self, config):
        self.config = config
        self.cache_dir = Path(config.dynamic.get('cache_dir', './data/cache'))
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.watchlist_file = Path(config.watchlist.get('file', './data/watchlist.json'))
        self.watchlist_file.parent.mkdir(parents=True, exist_ok=True)
        self.max_per_user = config.dynamic.get('max_per_user', 20)

        self._watchlist_cache: Set[str] = set()
        self._nicknames: Dict[str, str] = {}
        self._watchlist_lock = asyncio.Lock()

    # ========== 初始化 ==========

    async def initialize(self):
        await self._load_watchlist()
        logger.info(f"存储初始化完成，缓存目录: {self.cache_dir}")

    async def _load_watchlist(self):
        if self.watchlist_file.exists():
            try:
                data = json.loads(self.watchlist_file.read_text(encoding='utf-8'))
                self._watchlist_cache = set(data.get('users', []))
                self._nicknames = data.get('nicknames', {})
                logger.info(f"加载关注列表: {len(self._watchlist_cache)} 个用户")
            except Exception as e:
                logger.error(f"加载关注列表失败: {e}")
                self._watchlist_cache = set()
                self._nicknames = {}
        else:
            self._watchlist_cache = set()
            self._nicknames = {}
            await self._save_watchlist()

    async def _save_watchlist(self):
        try:
            data = {
                'users': list(self._watchlist_cache),
                'nicknames': self._nicknames,
                'updated_at': datetime.now().isoformat()
            }
            self.watchlist_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding='utf-8'
            )
        except Exception as e:
            logger.error(f"保存关注列表失败: {e}")

    # ========== 昵称 ==========

    def get_nickname(self, user_id: str) -> str:
        return self._nicknames.get(user_id, "")

    def set_nickname(self, user_id: str, name: str):
        if name:
            self._nicknames[user_id] = name

    # ========== 关注列表 ==========

    async def get_watchlist(self) -> Set[str]:
        async with self._watchlist_lock:
            return self._watchlist_cache.copy()

    async def add_to_watchlist(self, user_id: str) -> bool:
        async with self._watchlist_lock:
            if user_id in self._watchlist_cache:
                return False
            self._watchlist_cache.add(user_id)
            await self._save_watchlist()
            logger.info(f"添加到关注列表: {user_id}")
            return True

    async def remove_from_watchlist(self, user_id: str) -> bool:
        async with self._watchlist_lock:
            if user_id not in self._watchlist_cache:
                return False
            self._watchlist_cache.remove(user_id)
            await self._save_watchlist()
            logger.info(f"从关注列表移除: {user_id}")
            return True

    # ========== 动态缓存 ==========

    async def save_dynamic(self, user_id: str, dynamic: Dict[str, Any]):
        user_dir = self.cache_dir / f"user_{user_id}"
        user_dir.mkdir(parents=True, exist_ok=True)
        dynamic_id = dynamic.get('id') or datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        dynamic['_saved_at'] = datetime.now().isoformat()
        (user_dir / f"{dynamic_id}.json").write_text(
            json.dumps(dynamic, ensure_ascii=False, indent=2),
            encoding='utf-8'
        )

    async def cleanup_old_dynamics(self, user_id: str):
        user_dir = self.cache_dir / f"user_{user_id}"
        if not user_dir.exists():
            return
        files = sorted(user_dir.glob("*.json"), key=lambda f: f.stat().st_mtime)
        for f in files[:-self.max_per_user]:
            f.unlink()

    async def get_user_dynamics(self, user_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        user_dir = self.cache_dir / f"user_{user_id}"
        if not user_dir.exists():
            return []
        files = sorted(user_dir.glob("*.json"), reverse=True)[:limit]
        dynamics = []
        for f in files:
            try:
                dynamics.append(json.loads(f.read_text(encoding='utf-8')))
            except Exception as e:
                logger.error(f"读取动态失败 {f}: {e}")
        return dynamics

    async def get_all_dynamics(self, limit_per_user: int = 5) -> Dict[str, List[Dict[str, Any]]]:
        result = {}
        for user_id in await self.get_watchlist():
            dynamics = await self.get_user_dynamics(user_id, limit_per_user)
            if dynamics:
                result[user_id] = dynamics
        return result
