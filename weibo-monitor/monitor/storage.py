# -*- coding: utf-8 -*-

"""存储管理 — 文件缓存 + 关注列表"""

import json
import asyncio
from pathlib import Path
from typing import Dict, Any, List, Set
from datetime import datetime
from loguru import logger


class StorageManager:
    """文件存储管理器"""

    def __init__(self, config):
        self.config = config
        storage_cfg = config.storage
        self.cache_dir = Path(storage_cfg.get('cache_dir', './data/cache'))
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.watchlist_file = Path(config.watchlist.get('file', './data/watchlist.json'))
        self.watchlist_file.parent.mkdir(parents=True, exist_ok=True)
        self.max_per_user = storage_cfg.get('max_per_user', 20)
        self._watchlist_cache: Set[str] = set()
        self._nicknames: Dict[str, str] = {}
        self._watchlist_lock = asyncio.Lock()

    async def initialize(self):
        """初始化存储"""
        await self._load_watchlist()
        logger.info(f"存储初始化完成，缓存目录: {self.cache_dir}")

    async def _load_watchlist(self):
        """加载关注列表"""
        if self.watchlist_file.exists():
            try:
                content = self.watchlist_file.read_text(encoding='utf-8').strip()
                if not content:
                    self._watchlist_cache = set()
                    self._nicknames = {}
                    await self._save_watchlist()
                    return
                data = json.loads(content)
                self._watchlist_cache = set(data.get('users', []))
                self._nicknames = data.get('nicknames', {})
                logger.info(f"加载关注列表: {len(self._watchlist_cache)} 个用户")
            except json.JSONDecodeError as e:
                logger.warning(f"关注列表文件格式错误，重新初始化: {e}")
                self._watchlist_cache = set()
                self._nicknames = {}
                await self._save_watchlist()
        else:
            self._watchlist_cache = set()
            self._nicknames = {}
            await self._save_watchlist()

    async def _save_watchlist(self):
        """保存关注列表"""
        try:
            data = {
                'users': list(self._watchlist_cache),
                'nicknames': self._nicknames,
                'updated_at': datetime.now().isoformat()
            }
            self.watchlist_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8'
            )
        except Exception as e:
            logger.error(f"保存关注列表失败: {e}")

    def get_nickname(self, user_id: str) -> str:
        return self._nicknames.get(user_id, "")

    def set_nickname(self, user_id: str, name: str):
        if name:
            self._nicknames[user_id] = name

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

    async def save_weibo(self, weibo: Dict[str, Any]):
        """保存微博到缓存"""
        user_id = weibo.get("user", {}).get("idstr", "unknown")
        user_dir = self.cache_dir / f"user_{user_id}"
        user_dir.mkdir(parents=True, exist_ok=True)
        weibo_id = weibo.get("idstr") or weibo.get("id") or datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        weibo['_saved_at'] = datetime.now().isoformat()
        (user_dir / f"{weibo_id}.json").write_text(
            json.dumps(weibo, ensure_ascii=False, indent=2), encoding='utf-8'
        )
        await self._cleanup_old_weibo(user_id)

    async def _cleanup_old_weibo(self, user_id: str):
        user_dir = self.cache_dir / f"user_{user_id}"
        if not user_dir.exists():
            return
        files = sorted(user_dir.glob("*.json"), key=lambda f: f.stat().st_mtime)
        for f in files[:-self.max_per_user]:
            f.unlink()

    async def get_user_weibo(self, user_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        user_dir = self.cache_dir / f"user_{user_id}"
        if not user_dir.exists():
            return []
        files = sorted(user_dir.glob("*.json"), reverse=True)[:limit]
        weibos = []
        for f in files:
            try:
                weibos.append(json.loads(f.read_text(encoding='utf-8')))
            except Exception as e:
                logger.error(f"读取微博失败 {f}: {e}")
        return weibos

    async def get_all_weibo(self, limit_per_user: int = 5) -> Dict[str, List[Dict[str, Any]]]:
        result = {}
        for user_id in await self.get_watchlist():
            weibos = await self.get_user_weibo(user_id, limit_per_user)
            if weibos:
                result[user_id] = weibos
        return result
