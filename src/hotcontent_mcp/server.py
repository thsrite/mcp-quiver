#!/usr/bin/env python3
"""爆款内容 MCP 服务：把公众号 / 小红书 / 全网热榜三个数据源暴露成助理可调用的工具。

设计取向：工具返回**结构化 dict**（不是 HTML/文本），供助理拿去交给大模型继续分析。
每个结果都带 granularity 字段，助理据此判断能不能谈"互动数据"：
  - content ：公众号、小红书——有阅读/赞/藏/评/转，能对标单条内容
  - topic   ：全网热榜——只有标题和热度值，只能谈"什么话题在火"

抓取逻辑复用 fetchers/ 下三个独立脚本，它们也能单独当 CLI 跑。
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from datetime import date, timedelta
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from .fetchers import gzh, tophub, xhs

mcp = FastMCP("hotcontent")

# 单次调用的硬上限，防止把助理的上下文撑爆
MAX_ITEMS = 50
DEFAULT_ITEMS = 12
# 正文默认截断长度：公众号正文中位 1176 字，全量返回会迅速吃满上下文
DEFAULT_CONTENT_LEN = 600

_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_LOCK = threading.Lock()
CACHE_TTL = 300  # 秒。热榜 5 分钟内重复问不重复抓


def _cached(key: str, fn):
    """同参数 5 分钟内复用结果——助理常在一轮对话里反复问同一个榜。
    注意：本函数运行在 asyncio.to_thread 的工作线程里，没有 event loop，
    只能用 time.monotonic()，不能用 loop.time()。"""
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]
    val = fn()
    with _CACHE_LOCK:
        _CACHE[key] = (time.monotonic(), val)
    return val


def _clamp(n: int | None, default: int = DEFAULT_ITEMS) -> int:
    if not n or n < 1:
        return default
    return min(n, MAX_ITEMS)


@mcp.tool
async def search_wechat_articles(
    keyword: Annotated[str, Field(description="赛道/细分关键词，留空=全站热门。细分词（如「职场沟通」）比大类词（如「职场」）结果更准")] = "",
    days: Annotated[int, Field(description="时间窗口天数 1-30，默认 7。本数据源该参数真实生效", ge=1, le=30)] = 7,
    limit: Annotated[int, Field(description=f"返回条数，默认 {DEFAULT_ITEMS}，上限 {MAX_ITEMS}", ge=1, le=MAX_ITEMS)] = DEFAULT_ITEMS,
    original_only: Annotated[bool, Field(description="只要原创文章")] = False,
    content_len: Annotated[int, Field(description=f"每篇正文截断长度，默认 {DEFAULT_CONTENT_LEN}，0=不截断（谨慎，正文最长可达 2.6 万字）", ge=0)] = DEFAULT_CONTENT_LEN,
) -> dict:
    """查公众号爆款文章。三个数据源里**唯一带全文正文**的，最适合做选题对标和写作风格分析。

    榜单：低粉爆款 / 10万阅读 / 1万阅读 / 原创榜。
    返回每篇的标题、摘要、正文、阅读/点赞/在看/分享/评论、作者粉丝数、是否原创、原文链接。
    数据为入库快照（非实时），通常滞后 1-2 天。
    """
    n = _clamp(limit)
    start = (date.today() - timedelta(days=days)).isoformat()

    def _run():
        data = gzh.fetch_gzh_trends(keyword or "", start_date=start)
        if original_only:
            for key, _ in gzh.CATEGORIES:
                data[key] = [it for it in data.get(key, []) if it.get("originalFlag") == 1]
        picked = gzh.merge_and_sort(data, n)
        return json.loads(gzh.format_json(picked, content_len))

    return await asyncio.to_thread(_cached, f"gzh:{keyword}:{start}:{n}:{original_only}:{content_len}", _run)


@mcp.tool
async def search_xiaohongshu_notes(
    keyword: Annotated[str, Field(description="赛道/细分关键词，留空=全站热门。冷门词可能无数据，建议用热门赛道词")] = "",
    days: Annotated[int, Field(description="时间窗口天数 1-30。注意：实测该参数对本数据源基本不生效，保留仅为兼容", ge=1, le=30)] = 7,
    limit: Annotated[int, Field(description=f"返回条数，默认 {DEFAULT_ITEMS}，上限 {MAX_ITEMS}", ge=1, le=MAX_ITEMS)] = DEFAULT_ITEMS,
) -> dict:
    """查小红书爆款笔记。有完整互动数据但**没有笔记正文**（接口不提供）。

    榜单：点赞Top500 / 低粉爆款 / 7日飙升 / 单日飙升。
    返回标题、话题标签、作者粉丝数、点赞/收藏/评论/分享、封面图。

    两个已知限制，回答用户时要如实说明：
    - desc 字段常为空：源数据的 desc 就是「标题+话题标签串」，去标签后 44% 完全为空，不是抓取失败
    - link 打不开：笔记链接缺 xsec_token，未登录浏览器会跳 404，只能当唯一标识用
    """
    n = _clamp(limit)
    start = (date.today() - timedelta(days=days)).isoformat()

    def _run():
        data = xhs.fetch_xhs_trends(keyword or "", start_date=start)
        picked = xhs.merge_and_sort(data, n)
        return json.loads(xhs.format_json(picked))

    return await asyncio.to_thread(_cached, f"xhs:{keyword}:{start}:{n}", _run)


@mcp.tool
async def search_hot_topics(
    platform: Annotated[str, Field(description="平台/榜单名筛选，逗号分隔，子串匹配，如「微博,知乎,抖音」。留空=全部 85 个榜单。先调 list_hot_boards 看有哪些")] = "",
    keyword: Annotated[str, Field(description="按条目标题过滤，可跨所有榜单找某主题在全网的热度")] = "",
    limit: Annotated[int, Field(description=f"每个榜单最多取几条，默认 {DEFAULT_ITEMS}，上限 {MAX_ITEMS}", ge=1, le=MAX_ITEMS)] = DEFAULT_ITEMS,
    max_boards: Annotated[int, Field(description="最多返回几个榜单，默认 8，防止一次拉回太多撑爆上下文", ge=1, le=85)] = 8,
) -> dict:
    """查全网实时热榜（85 个榜单），覆盖微博/知乎/微信/百度/B站/抖音/快手/贴吧/虎扑/36氪/GitHub 等。

    这是**话题级**数据：只有标题、排名、热度值，**没有点赞收藏等互动数**。
    回答"现在什么在火"用它；回答"哪条内容爆了、怎么爆的"要用另两个工具。

    数据实时（不同于公众号/小红书的快照）。注意：本数据源不含小红书榜。
    """
    n = _clamp(limit)

    def _run():
        boards = tophub.fetch_boards()
        sel = tophub.select(boards, platform, keyword, n, max_boards)
        recs = tophub.to_records(sel)
        return {
            "source": "tophub.today",
            "granularity": "topic",
            "boards": len(sel),
            "total": len(recs),
            "items": recs,
        }

    return await asyncio.to_thread(_cached, f"top:{platform}:{keyword}:{n}:{max_boards}", _run)


@mcp.tool
async def list_hot_boards() -> dict:
    """列出全网热榜里所有可用的榜单（平台名 + 榜单名 + 条目数），供 search_hot_topics 的 platform 参数使用。

    当用户问"能查哪些网站/平台"，或你不确定某平台在不在覆盖范围内时，先调这个。
    """
    def _run():
        boards = tophub.fetch_boards()
        return {
            "source": "tophub.today",
            "total_boards": len(boards),
            "boards": [{"platform": b["platform"], "board": b["board"], "items": len(b["items"])}
                       for b in boards],
            "note": "不含小红书；小红书用 search_xiaohongshu_notes，公众号用 search_wechat_articles",
        }

    return await asyncio.to_thread(_cached, "boards", _run)


def main() -> None:
    """入口：默认 HTTP 传输；设 MCP_TRANSPORT=stdio 可切成 stdio。"""
    transport = os.environ.get("MCP_TRANSPORT", "http")
    if transport == "stdio":
        mcp.run(transport="stdio")
        return
    mcp.run(transport="http",
            host=os.environ.get("HOST", "0.0.0.0"),
            port=int(os.environ.get("PORT", "8931")))


if __name__ == "__main__":
    main()
