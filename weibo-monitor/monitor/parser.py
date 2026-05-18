# -*- coding: utf-8 -*-

"""微博数据解析器"""

import re
from datetime import datetime
from typing import Dict, Any, List, Optional


class WeiboParser:
    """微博时间线数据解析器"""

    def __init__(self, json_data: Dict[str, Any]):
        self.data = json_data
        self.statuses = self.data.get("statuses", [])

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WeiboParser":
        return cls(data)

    def extract_weibo(self, weibo: Dict[str, Any]) -> Dict[str, Any]:
        """提取单条微博为结构化 dict"""
        user = weibo.get("user", {})
        created_at = weibo.get("created_at", "")
        timestamp = self._parse_time(created_at)
        images = self._extract_images(weibo)
        retweeted = weibo.get("retweeted_status")
        is_retweet = retweeted is not None

        result = {
            "id": weibo.get("id"),
            "idstr": weibo.get("idstr", ""),
            "mblogid": weibo.get("mblogid", ""),
            "created_at": created_at,
            "timestamp": timestamp,
            "user": {
                "id": user.get("id"),
                "idstr": user.get("idstr", ""),
                "screen_name": user.get("screen_name", ""),
                "avatar": user.get("profile_image_url", ""),
                "verified": user.get("verified", False),
            },
            "text": self._clean_text(weibo.get("text", "")),
            "text_raw": weibo.get("text_raw", ""),
            "images": images,
            "reposts_count": weibo.get("reposts_count", 0),
            "comments_count": weibo.get("comments_count", 0),
            "attitudes_count": weibo.get("attitudes_count", 0),
            "is_retweet": is_retweet,
            "isLongText": weibo.get("isLongText", False),
            "source": self._clean_source(weibo.get("source", "")),
            "region_name": weibo.get("region_name", ""),
        }

        if is_retweet:
            result["retweeted_status"] = self.extract_weibo(retweeted)

        return result

    def _parse_time(self, time_str: str) -> Optional[int]:
        """解析 'Sun May 17 08:27:18 +0800 2026' → Unix 时间戳"""
        if not time_str:
            return None
        try:
            parts = time_str.rsplit(' ', 2)
            if len(parts) >= 2:
                clean_time = f"{parts[0]} {parts[1]}"
                dt = datetime.strptime(clean_time, "%a %b %d %H:%M:%S %Y")
                return int(dt.timestamp())
        except Exception:
            pass
        return None

    @staticmethod
    def _img_key(url: str) -> str:
        """提取文件名用于跨分辨率去重"""
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
        return re.sub(r'<[^>]+>', '', text).strip()

    def _clean_source(self, source: str) -> str:
        """清理来源字段"""
        if not source:
            return ""
        match = re.search(r'>(.*?)</a>', source)
        return match.group(1) if match else source

    def get_all_weibo(self, max_count: int = 50) -> List[Dict[str, Any]]:
        """批量提取所有微博"""
        result = []
        for weibo in self.statuses[:max_count]:
            try:
                result.append(self.extract_weibo(weibo))
            except Exception as e:
                print(f"解析失败: {e}")
        return result
