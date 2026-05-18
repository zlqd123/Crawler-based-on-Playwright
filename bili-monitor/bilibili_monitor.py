"""
Bilibili Monitor Platform for NoneBot-Bison

适配自建的 Bilibili 监控系统 API
API 地址: http://127.0.0.1:8787/api/dynamic/{user_id}
"""

import re
from typing import Any, ClassVar, Collection, Optional
from datetime import datetime

from httpx import AsyncClient
from nonebot_bison.post import Post
from nonebot_bison.types import Category, RawPost, Tag, Target
from nonebot_bison.utils import Site
from nonebot_bison.platform.platform import NewMessage

from nonebot import get_driver
from loguru import logger

# ========== 配置区域 ==========
# 修改为你的 Bilibili Monitor API 地址
# Docker 环境使用 host.docker.internal
BILIBILI_MONITOR_API_BASE = "http://host.docker.internal:38787"
# =============================

driver = get_driver()
bot_logger = driver.logger if hasattr(driver, 'logger') else logger


class BilibiliMonitorSite(Site):
    """Bilibili Monitor 站点配置"""
    
    name = "bilibili_monitor"
    schedule_type = "interval"
    schedule_setting: ClassVar[dict] = {"seconds": 10}
    require_browser = False


class BilibiliMonitor(NewMessage):
    """Bilibili Monitor 平台适配器"""
    
    # ========== 平台元信息 ==========
    platform_name = "bilibili_monitor"
    name = "B站动态监控"
    enabled = True
    is_common = True
    has_target = True
    enable_tag = False
    use_batch = False
    
    categories: ClassVar[dict[Category, str]] = {}
    default_theme = "basic"
    
    site = BilibiliMonitorSite
    
    parse_target_promot = "请输入 B站用户 ID（数字）或用户空间主页链接"
    
    # ========== Target 解析 ==========
    @classmethod
    async def parse_target(cls, text: str) -> Target:
        """解析用户输入的 Target"""
        bot_logger.debug(f"[BilibiliMonitor] parse_target: {text}")
        
        text = text.strip()
        
        # 纯数字
        if re.fullmatch(r"\d+", text):
            return Target(text)
        
        # space.bilibili.com/{id}
        m = re.search(r"space\.bilibili\.com/(\d+)", text)
        if m:
            return Target(m.group(1))
        
        # t.bilibili.com/{id}
        m = re.search(r"t\.bilibili\.com/(\d+)", text)
        if m:
            return Target(m.group(1))
        
        # opus ID 错误提示
        m = re.search(r"bilibili\.com/opus/(\d+)", text)
        if m:
            raise cls.ParseTargetException(
                prompt="❌ 请输入用户ID（数字），而不是动态ID。\n"
                       "用户ID可在用户空间主页URL中找到：\n"
                       "https://space.bilibili.com/【用户ID】"
            )
        
        raise cls.ParseTargetException(
            prompt="❌ 无法识别的格式。请使用以下格式之一：\n"
                   "1. 纯数字用户ID（如：1265652806）\n"
                   "2. 用户空间链接（如：https://space.bilibili.com/1265652806）"
        )
    
    # ========== 获取用户名称 ==========
    @classmethod
    async def get_target_name(cls, client: AsyncClient, target: Target) -> Optional[str]:
        """获取用户昵称"""
        bot_logger.debug(f"[BilibiliMonitor] get_target_name: {target}")
        
        try:
            url = f"{BILIBILI_MONITOR_API_BASE}/api/user/{target}"
            resp = await client.get(url, timeout=10.0)
            
            if resp.status_code != 200:
                bot_logger.warning(f"[BilibiliMonitor] get_target_name failed: {resp.status_code}")
                return None
            
            data = resp.json()
            name = data.get("name")
            bot_logger.info(f"[BilibiliMonitor] get_target_name success: {target} -> {name}")
            return name
            
        except Exception:
            return None
    
    # ========== 抓取动态列表 ==========
    async def get_sub_list(self, target: Target) -> list[RawPost]:
        """获取指定用户的动态列表（API 不可用时静默跳过）"""
        bot_logger.debug(f"[BilibiliMonitor] get_sub_list: {target}")
        
        try:
            client = await self.ctx.get_client(target)
            url = f"{BILIBILI_MONITOR_API_BASE}/api/dynamic/{target}"
            resp = await client.get(url, timeout=10.0)
            
            if resp.status_code != 200:
                bot_logger.debug(f"[BilibiliMonitor] get_sub_list status={resp.status_code}")
                return []
            
            data = resp.json()
            dynamics = data.get("dynamics", [])
            bot_logger.info(f"[BilibiliMonitor] get_sub_list got {len(dynamics)} dynamics for {target}")
            
            # 转换为 RawPost 列表（透传全部字段）
            raw_posts = []
            for dynamic in dynamics:
                raw_posts.append({
                    "id": dynamic.get("id"),
                    "type": dynamic.get("type"),
                    "author": dynamic.get("author"),
                    "author_id": dynamic.get("author_id"),
                    "publish_time": dynamic.get("publish_time"),
                    "publish_ts": dynamic.get("publish_ts"),
                    "content": dynamic.get("content", ""),
                    "images": dynamic.get("images", []),
                    "title": dynamic.get("title"),
                    "url": dynamic.get("url"),
                    "bvid": dynamic.get("bvid"),
                    "description": dynamic.get("description"),
                    "interactions": dynamic.get("interactions", {}),
                    "like": dynamic.get("like", 0),
                    "comment": dynamic.get("comment", 0),
                    "forward_count": dynamic.get("forward_count", 0),
                    "forward_comment": dynamic.get("forward_comment"),
                    "forward_author": dynamic.get("forward_author"),
                    "forward_summary": dynamic.get("forward_summary"),
                    "cached_at": dynamic.get("cached_at"),
                    "_raw": dynamic
                })
            
            return raw_posts
            
        except Exception:
            # API 不可用时静默跳过，等待下次轮询自动恢复
            return []
    
    # ========== 动态 ID（用于去重）==========
    def get_id(self, raw_post: RawPost) -> Any:
        """返回动态的唯一 ID"""
        return raw_post.get("id")
    
    # ========== 动态时间戳 ==========
    def get_date(self, raw_post: RawPost) -> Optional[int]:
        """返回动态的发布时间戳（秒级）"""
        publish_time = raw_post.get("publish_time")
        
        if not publish_time:
            return None
        
        try:
            if isinstance(publish_time, str):
                dt = datetime.strptime(publish_time, "%Y-%m-%d %H:%M:%S")
                return int(dt.timestamp())
            
            if isinstance(publish_time, (int, float)):
                # 毫秒转秒
                if publish_time > 10000000000:
                    return int(publish_time // 1000)
                return int(publish_time)
                
        except (ValueError, TypeError):
            pass
        
        return None
    
    # ========== 动态分类 ==========
    # Category 是 int 别名，使用自定义编号
    _CAT_MAP: ClassVar[dict[str, int]] = {
        "text": 1,
        "forward": 2,
        "video": 3,
        "article": 4,
    }
    
    categories: ClassVar[dict[Category, str]] = {
        1: "图文",
        2: "转发",
        3: "视频", 
        4: "专栏",
    }

    def get_category(self, raw_post: RawPost) -> Optional[Category]:
        """根据动态类型返回分类"""
        dynamic_type = raw_post.get("type", "unknown")
        cat_val = self._CAT_MAP.get(dynamic_type)
        if cat_val is not None:
            return Category(cat_val)
        return None
    
    # ========== 动态标签 ==========
    def get_tags(self, raw_post: RawPost) -> Optional[Collection[Tag]]:
        return None
    
    # ========== 解析为 Post ==========
    async def parse(self, raw_post: RawPost) -> Post:
        """将 RawPost 解析为 Post 对象"""
        dynamic_id = raw_post.get("id")
        dynamic_type = raw_post.get("type", "unknown")
        bot_logger.debug(f"[BilibiliMonitor] parse: id={dynamic_id}, type={dynamic_type}")
        
        # 兼容：content 可能是字符串或字典
        content_data = raw_post.get("content", {})
        if isinstance(content_data, str):
            content_data = {"text": content_data}
        
        # 顶层字段合并到 content_data
        for key in ("title", "url", "bvid", "description",
                     "forward_comment", "forward_author", "forward_summary"):
            if key not in content_data and key in raw_post:
                content_data[key] = raw_post[key]

        author = raw_post.get("author", "未知用户")
        
        # 根据类型构建消息内容
        if dynamic_type == "text":
            text = self._parse_text_post(content_data)
            # 正文从 content 中分离：去掉重复的标题前缀
            api_title = content_data.get("title") or raw_post.get("title", "")
            if api_title and text.startswith(api_title):
                text = text[len(api_title):].lstrip("\n")
        elif dynamic_type == "forward":
            text = self._parse_forward_post(content_data)
        elif dynamic_type == "video":
            text = self._parse_video_post(content_data)
        else:
            text = f"[未知类型] {raw_post}"
        
        url = content_data.get("url", "") or raw_post.get("url", "")
        images = raw_post.get("images", [])
        ts = self.get_date(raw_post)
        
        return Post(
            self,
            content=text,
            title=content_data.get("title") or raw_post.get("title") or text.split("\n")[0][:50],
            url=url,
            images=images,
            nickname=author,
            timestamp=ts
        )
    
    # ========== 各类型动态解析辅助方法 ==========
    
    @staticmethod
    def _parse_text_post(content: dict) -> str:
        """解析图文动态"""
        text = content.get("content") or content.get("text", "")
        if not text:
            return content.get("title", "")
        return text

    @staticmethod
    def _parse_forward_post(content: dict) -> str:
        """解析转发动态"""
        lines = []

        forward_comment = content.get("forward_comment", "")
        if forward_comment:
            lines.append(f"💬 {forward_comment}")
            lines.append("")

        forward_author = content.get("forward_author", "")
        forward_summary = content.get("forward_summary", "")
        if forward_author:
            lines.append(f"📌 转发自 @{forward_author}")
        if forward_summary:
            lines.append(forward_summary)

        return "\n".join(lines) if lines else (forward_comment or forward_summary or "")

    @staticmethod
    def _parse_video_post(content: dict) -> str:
        """解析视频动态"""
        lines = []

        title = content.get("title", "无标题")
        bvid = content.get("bvid", "")

        lines.append(f"🎬 **{title}**")
        if bvid:
            lines.append(f"BV: {bvid}")

        desc = content.get("description", "")
        if desc and desc != "-":
            if len(desc) > 200:
                desc = desc[:200] + "..."
            lines.append("")
            lines.append(desc)

        return "\n".join(lines)