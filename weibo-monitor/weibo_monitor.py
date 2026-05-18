"""Weibo Monitor Platform for NoneBot-Bison — 适配自建微博监控 API"""

import re
from typing import Any, ClassVar, Optional
from datetime import datetime

from httpx import AsyncClient, ConnectError, TimeoutException
from nonebot_bison.post import Post
from nonebot_bison.types import RawPost, Tag, Target
from nonebot_bison.utils import Site
from nonebot_bison.platform.platform import NewMessage
from nonebot import get_driver
from loguru import logger

WEIBO_MONITOR_API_BASE = "http://host.docker.internal:39787"

driver = get_driver()
bot_logger = driver.logger if hasattr(driver, 'logger') else logger


class WeiboMonitorSite(Site):
    """微博监控站点配置"""
    name = "weibo_monitor"
    schedule_type = "interval"
    schedule_setting: ClassVar[dict] = {"seconds": 15}
    require_browser = False


class WeiboMonitor(NewMessage):
    """微博监控平台适配器"""

    platform_name = "weibo_monitor"
    name = "微博动态监控"
    enabled = True
    is_common = True
    has_target = True
    enable_tag = False
    use_batch = False
    categories: ClassVar[dict] = {}
    default_theme = "basic"
    site = WeiboMonitorSite
    parse_target_promot = "请输入微博用户 ID（数字）或用户空间主页链接"

    @classmethod
    async def parse_target(cls, text: str) -> Target:
        """解析用户输入为 Target"""
        text = text.strip()
        if re.fullmatch(r"\d+", text):
            return Target(text)
        m = re.search(r"weibo\.com/u/(\d+)", text)
        if m:
            return Target(m.group(1))
        m = re.search(r"weibo\.com/(\d+)", text)
        if m:
            return Target(m.group(1))
        raise cls.ParseTargetException(
            prompt="❌ 无法识别的格式。请使用以下格式之一：\n"
                   "1. 纯数字用户ID（如：3219724922）\n"
                   "2. 用户空间链接（如：https://weibo.com/u/3219724922）"
        )

    @classmethod
    async def get_target_name(cls, client: AsyncClient, target: Target) -> Optional[str]:
        """获取用户昵称"""
        try:
            resp = await client.get(f"{WEIBO_MONITOR_API_BASE}/api/user/{target}", timeout=5.0)
            if resp.status_code != 200:
                return None
            name = resp.json().get("name")
            if name:
                bot_logger.info(f"[WeiboMonitor] get_target_name: {target} -> {name}")
                return name
            return None
        except (ConnectError, TimeoutException):
            return None
        except Exception as e:
            bot_logger.debug(f"[WeiboMonitor] get_target_name error: {e}")
            return None

    async def get_sub_list(self, target: Target) -> list[RawPost]:
        """从自建 API 获取用户微博列表"""
        try:
            client = await self.ctx.get_client(target)
            resp = await client.get(f"{WEIBO_MONITOR_API_BASE}/api/weibo/{target}", timeout=5.0)
            if resp.status_code != 200:
                return []

            weibos = resp.json().get("weibos", [])
            bot_logger.info(f"[WeiboMonitor] get_sub_list: {target} 获取到 {len(weibos)} 条")

            raw_posts = []
            for w in weibos:
                raw_posts.append({
                    "id": w.get("id"),
                    "idstr": w.get("idstr"),
                    "author": w.get("user", {}).get("screen_name", ""),
                    "author_id": w.get("user", {}).get("idstr", ""),
                    "author_avatar": w.get("user", {}).get("avatar", ""),
                    "publish_time": w.get("created_at"),
                    "publish_ts": w.get("timestamp"),
                    "content": w.get("text", ""),
                    "content_raw": w.get("text_raw", ""),
                    "images": w.get("images", []),
                    "reposts_count": w.get("reposts_count", 0),
                    "comments_count": w.get("comments_count", 0),
                    "attitudes_count": w.get("attitudes_count", 0),
                    "is_retweet": w.get("is_retweet", False),
                    "source": w.get("source", ""),
                    "region_name": w.get("region_name", ""),
                    "_raw": w,
                })

                if w.get("is_retweet") and w.get("retweeted_status"):
                    orig = w["retweeted_status"]
                    raw_posts[-1]["retweeted_status"] = {
                        "id": orig.get("id"),
                        "author": orig.get("user", {}).get("screen_name", ""),
                        "author_id": orig.get("user", {}).get("idstr", ""),
                        "content": orig.get("text", ""),
                        "images": orig.get("images", []),
                    }

            return raw_posts
        except (ConnectError, TimeoutException):
            return []
        except Exception as e:
            bot_logger.debug(f"[WeiboMonitor] get_sub_list error: {e}")
            return []

    def get_id(self, raw_post: RawPost) -> Any:
        return raw_post.get("id")

    def get_date(self, raw_post: RawPost) -> Optional[int]:
        """返回秒级时间戳"""
        ts = raw_post.get("publish_ts")
        if ts:
            return int(ts)
        publish_time = raw_post.get("publish_time")
        if publish_time and isinstance(publish_time, str):
            try:
                dt = datetime.strptime(publish_time, "%a %b %d %H:%M:%S +0800 %Y")
                return int(dt.timestamp())
            except (ValueError, TypeError):
                pass
        return None

    def get_tags(self, raw_post: RawPost) -> Optional[list[Tag]]:
        return None

    async def parse(self, raw_post: RawPost) -> Post:
        """将 RawPost 解析为可发送的 Post"""
        author = raw_post.get("author", "未知用户")
        content = raw_post.get("content", "")
        images = raw_post.get("images", [])
        ts = self.get_date(raw_post)

        text = self._build_text(raw_post, content)

        return Post(
            self,
            content=text,
            title=f"{author} 的微博",
            url=f"https://weibo.com/{raw_post.get('author_id', '')}/{raw_post.get('idstr', '')}",
            images=images,
            nickname=author,
            timestamp=ts,
        )

    def _build_text(self, raw_post: RawPost, content: str) -> str:
        """拼接微博正文 + 转发 + 图片 + 来源 + 互动统计"""
        lines = []
        images = raw_post.get("images", [])

        # 正文（去 HTML）
        content = re.sub(r'<[^>]+>', '', content).strip() if content else ""
        if raw_post.get("is_retweet"):
            if content:
                lines.append(f"💬 {content}")
            lines.append("")
            retweeted = raw_post.get("retweeted_status")
            if retweeted:
                lines.append(f"📌 转发自 @{retweeted.get('author', '未知用户')}")
                orig_content = re.sub(r'<[^>]+>', '', retweeted.get("content", "")).strip()
                if orig_content:
                    lines.append(orig_content)
                orig_imgs = retweeted.get("images", [])
                if orig_imgs:
                    lines.append(f"[{len(orig_imgs)}张图片]")
        elif content:
            lines.append(content)
        else:
            lines.append("[无正文]")

        # 图片标记
        if images:
            lines.append("")
            lines.append(f"[图片]" if len(images) == 1 else f"[{len(images)}张图片]")

        # 来源 / 地区
        source = raw_post.get("source", "")
        region = raw_post.get("region_name", "")
        if source or region:
            parts = []
            if source:
                parts.append(f"via {source}")
            if region:
                parts.append(f"📍 {region}")
            lines.append("")
            lines.append(", ".join(parts))

        # 互动统计
        parts = []
        for label, key in [("转发", "reposts_count"), ("评论", "comments_count"), ("点赞", "attitudes_count")]:
            v = raw_post.get(key, 0)
            if v:
                parts.append(f"{label} {v}")
        if parts:
            lines.append("")
            lines.append(" · ".join(parts))

        text = "\n".join(lines)
        if len(text) > 2000:
            text = text[:1997] + "..."
        return text
