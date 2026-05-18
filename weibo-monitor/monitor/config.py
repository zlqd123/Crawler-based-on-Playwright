# -*- coding: utf-8 -*-

"""配置管理"""

import yaml
from pathlib import Path
from typing import Any, Dict


class Config:
    """YAML 配置读取器"""

    def __init__(self, config_path: str = "config.yaml"):
        self.config_path = Path(config_path)
        self._config = self._load_config()

    def _load_config(self) -> Dict[str, Any]:
        if not self.config_path.exists():
            raise FileNotFoundError(f"配置文件不存在: {self.config_path}")
        with open(self.config_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)

    def reload(self):
        self._config = self._load_config()

    @property
    def development(self) -> bool:
        return self._config.get('development', True)

    @property
    def browser(self) -> Dict[str, Any]:
        return self._config.get('browser', {})

    @property
    def monitor(self) -> Dict[str, Any]:
        return self._config.get('monitor', {})

    @property
    def storage(self) -> Dict[str, Any]:
        return self._config.get('storage', {})

    @property
    def watchlist(self) -> Dict[str, Any]:
        return self._config.get('watchlist', {})

    @property
    def api(self) -> Dict[str, Any]:
        return self._config.get('api', {})

    @property
    def logging(self) -> Dict[str, Any]:
        return self._config.get('logging', {})

    def get(self, key: str, default: Any = None) -> Any:
        keys = key.split('.')
        value = self._config
        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)
                if value is None:
                    return default
            else:
                return default
        return value
