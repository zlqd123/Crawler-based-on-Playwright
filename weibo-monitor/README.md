# 微博动态监控系统

基于 Playwright 浏览器操控 + API 拦截的微博动态实时监控系统，搭配 NoneBot-Bison 实现 QQ 群推送。

## 架构

```
浏览器 (Playwright)
  ├── 登录（二维码/持久化 Cookie）
  ├── 拦截 /ajax/feed/friendstimeline
  └── 检测 NEW 徽章触发刷新
         │
         ▼ 解析 + 存储 (JSON 文件)
         │
    REST API :39787
         │
         ▼ 消费
   NoneBot-Bison (Platform: weibo_monitor)
         │
         ▼
      QQ 群推送
```

## 文件说明

### 爬虫核心 (`weibo/`)

| 文件 | 行数 | 职责 |
|------|------|------|
| `main.py` | 128 | 入口，组装各模块生命周期并启动扫描循环 |
| `config.yaml` | 39 | 浏览器/监控/存储/API/日志的 YAML 配置 |
| `requirements.txt` | 6 | Python 依赖 |

#### `weibo/monitor/`

| 文件 | 行数 | 职责 |
|------|------|------|
| `browser.py` | 403 | 浏览器管理器 — 启动 Playwright、二维码登录、拦截 `friendstimeline` API、NEW 徽章检测刷新、长文本补全、图片下载 |
| `parser.py` | 136 | 微博数据解析器 — 从 API JSON 提取结构化字段（文本/图片/视频封面/时间戳/转发链），图片按文件名跨分辨率去重 |
| `api.py` | 241 | REST 服务器 — 暴露 `/api/user/{id}` `/api/weibo/{id}` `/api/weibos` 等端点，内置图片代理绕过防盗链 |
| `storage.py` | 139 | 文件存储 — 每条微博存为独立 JSON，关注列表持久化，自动淘汰旧缓存 |
| `user_weibo_fetcher.py` | 274 | 按需抓取指定用户微博 — 打开用户主页 → 拦截 `mymblog` API → 滚动加载 → 解析 |
| `config.py` | 64 | YAML 配置读取器 |

### Bison Platform 适配器

| 文件 | 行数 | 职责 |
|------|------|------|
| `weibo_monitor.py` | 223 | NoneBot-Bison 平台适配器 — 每 15s 从自建 API 拉取微博，生成 Post 推送到 QQ 群 |

## 运行流程

```
1. main.py 启动
2. BrowserManager 打开 Chromium，加载 user_data_dir 持久化登录态
3. 未登录 → 弹出二维码 → 终端 ASCII 码 → 用户扫描
4. 已登录 → 点击「最新微博」→ 设置 API 拦截器
5. 拦截 /ajax/feed/friendstimeline 响应 → 解析 statuses → 去重
6. 新微博 → 补全长文本（/ajax/statuses/longtext）→ 存入文件缓存
7. 图片 → download_image（带 Referer 头）→ 本地保存
8. API 服务在 127.0.0.1:39787 提供查询
9. 后台每 5s 检测 NEW 徽章 → 刷新页面获取增量
```

## Bison 适配链路

```
Bison Scheduler (15s) → WeiboMonitor.get_sub_list()
  → GET http://host.docker.internal:39787/api/weibo/{uid}
  → 返回 { weibos: [...] }
  → 转 RawPost 列表
  → Bison 内置去重（filter_common_with_diff，基于 id）
  → parse(RawPost) → Post
  → 拼接正文/转发/图片/来源/互动统计
  → send_msgs → QQ 群
```

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/user/{id}` | 获取用户昵称 |
| POST | `/api/subscribe` | 订阅用户 `{"user_id":"..."}` |
| DELETE | `/api/subscribe` | 取消订阅 |
| GET | `/api/weibo/{id}` | 获取用户最近微博 `?limit=10` |
| GET | `/api/weibos` | 获取所有订阅用户微博 |
| GET | `/api/image/proxy` | 图片代理（绕过防盗链） |
| GET | `/health` | 健康检查 |

## 关键反爬措施

- `user_data_dir` 持久化 Cookie，避免重复登录
- `--disable-blink-features=AutomationControlled` 隐藏 Playwright 特征
- 图片下载/代理需带 `Referer: https://weibo.com/`，否则 403
- 随机 3-5 秒延迟模拟人类操作
- API 拦截替代主动轮询，减少请求量

## 配置要点

```yaml
# config.yaml
development: true          # true=有头浏览器 false=无头
monitor:
  check_interval: 5        # NEW 徽章检测间隔（秒）
api:
  host: "127.0.0.1"
  port: 39787
  delay_ms: 1500           # 请求合并延迟
storage:
  max_per_user: 20         # 每用户最多缓存条数
```

## 依赖

```
playwright>=1.40.0
pyyaml>=6.0
aiohttp>=3.9.1
loguru>=0.7.2
qrcode>=7.4
Pillow>=10.0
pyzbar>=0.1.9
```

## 启动

```bash
cd weibo
pip install -r requirements.txt
playwright install chromium
python main.py
```
