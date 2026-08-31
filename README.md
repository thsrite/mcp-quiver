# self-mcp

自建 MCP 服务集合。

| 服务 | 说明 | 传输 |
|---|---|---|
| `hotcontent` | 爆款内容雷达：全网热榜（85 榜）、公众号爆款文章（含全文正文）、小红书爆款笔记 | http / stdio |
| `docx-comments` | Word 批注：提取批注文本、把批注内联进正文 | stdio / http |

## 安装

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

装一次两个服务都可用，分别由 `mcp-hotcontent` 和 `mcp-docx-comments` 启动。

---

## hotcontent — 爆款内容雷达

把三个**零凭据**数据源暴露成 MCP 工具，供 AI 助理查询后交给模型做选题对标、热点追踪和内容分析。无需 API key，无需登录。

### 运行

```bash
# HTTP（默认，监听 0.0.0.0:8931）
mcp-hotcontent

# 换端口
PORT=9000 mcp-hotcontent

# stdio
MCP_TRANSPORT=stdio mcp-hotcontent
```

Docker：

```bash
docker build -t hotcontent-mcp .
docker run -d -p 8931:8931 hotcontent-mcp
```

### 客户端配置

HTTP：
```json
{ "transport": "http", "url": "http://127.0.0.1:8931/mcp" }
```

stdio：
```json
{
  "command": "/path/to/.venv/bin/mcp-hotcontent",
  "env": { "MCP_TRANSPORT": "stdio" }
}
```

### 工具

| 工具 | 参数 | 说明 |
|---|---|---|
| `search_hot_topics` | `platform` `keyword` `limit` `max_boards` | 全网 85 榜实时热点 |
| `list_hot_boards` | — | 列出所有可用榜单 |
| `search_wechat_articles` | `keyword` `days` `limit` `original_only` `content_len` | 公众号爆款，**唯一带全文正文** |
| `search_xiaohongshu_notes` | `keyword` `days` `limit` | 小红书爆款，带赞藏评转 |

返回统一带 `granularity` 字段，助理据此判断能谈什么：

- **`topic`**（全网热榜）— 只有标题、排名、热度值，**没有互动数**。回答「什么话题在火」
- **`content`**（公众号、小红书）— 完整互动数据 + 作者粉丝数。回答「哪条内容爆了、为什么爆」

### 覆盖范围

**全网热榜 85 个**（实时）：微博 · 知乎 · 微信 · 百度 · B站 · 抖音 · 快手 · 贴吧 · 虎扑 · 36氪 · 少数派 · 掘金 · 机器之心 · 量子位 · GitHub Trending · Product Hunt · App Store · 雪球 · 华尔街见闻 · 豆瓣 · 猫眼 · IMDb · QQ音乐 · 懂球帝 · 站酷 · Behance · 汽车之家 · FreeBuf 等

**公众号**（快照）：低粉爆款 / 10万阅读 / 1万阅读 / 原创榜

**小红书**（快照）：点赞Top500 / 低粉爆款 / 7日飙升 / 单日飙升

### 已知边界

这些是数据源本身的限制，不是 bug——`SKILL.md` 里也写明了，让助理如实告知用户而不是粉饰：

- 全网热榜**不含小红书**，也不含推特 / YouTube 等境外平台
- 不同平台的「热度」口径不同（微博万级、B站播放量、抖音播放次数），**不能跨平台比大小**
- 公众号、小红书是入库快照，通常**滞后 1–2 天**；热榜是实时的
- 小红书接口**不返回笔记正文**；`link` 缺 `xsec_token`，未登录浏览器点开会跳 404
- 公众号 `read` 字段的 `"10w+"` 是平台封顶显示，非精确值；排序更该看分享和在看

### 给助理的技能说明

`src/hotcontent_mcp/SKILL.md` 是配套的技能指令，写明工具选择、粒度区分、诚实边界和输出规范（数字只用原值、条数不足如实说、禁止凑数）。支持技能库的客户端可以导入它，与 MCP 配合使用。

### 实现说明

- 抓取逻辑在 `src/hotcontent_mcp/fetchers/`，三个模块也能脱离 MCP 单独当 CLI 跑：
  ```bash
  python3 -m hotcontent_mcp.fetchers.tophub --list
  ```
- 全程标准 TLS 证书校验；本机 DNS 被分流工具劫持成 `198.18.0.0/15` fake-ip 时，回退 DoH 解析真实 IP 直连，证书校验保持开启
- 结果缓存 5 分钟；单次返回上限 50 条，正文默认截断 600 字，避免撑爆助理上下文


---

## docx-comments — Word 批注处理

把 `.docx` 里的批注读出来，或内联进正文——让模型能一次看到「原文 + 审阅意见」的对应关系，而不是丢失批注。

### 运行

```bash
mcp-docx-comments                        # stdio（默认）
MCP_TRANSPORT=http PORT=8932 mcp-docx-comments   # HTTP
```

### 客户端配置

```json
{
  "command": "/path/to/.venv/bin/mcp-docx-comments"
}
```

### 工具

| 工具 | 参数 | 说明 |
|---|---|---|
| `extract_comments` | `file_path` 或 `file_base64` | 提取全部批注文本 |
| `inline_comments_file` | `input_path` `output_path` | 读文件，批注以 `【批注：…】` 插入正文后另存 |
| `inline_comments_base64` | `file_base64` `filename` | 同上，收发都走 base64 |

内联后的批注是红色加粗的 run，紧跟被批注内容之后。

> 两个服务统一使用 fastmcp v3。docx-comments 早期基于官方 `mcp` SDK 的 FastMCP，
> 迁移后**结构化返回不再包一层 `result`**：原先 `{"result": "..."}`，现在直接是字符串。
> 按 MCP 标准读 content 文本的客户端不受影响。
