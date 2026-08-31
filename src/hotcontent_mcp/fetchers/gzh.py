#!/usr/bin/env python3
"""
公众号爆款文章查询脚本
数据源：onetotenvip.com /skill/cozeSkill/getWxCozeSkillData（与小红书技能同服务商）
榜单类型：低粉爆款、1万阅读榜、原创榜、10万阅读榜

与 fetch_xhs_trends.py 的差异：本接口**返回文章正文**（content 中位 1176 字），
且 startDate 真实生效、photoId 无重复、oriUrl 是可直接打开的微信正文链接。
"""

import re
import sys
import html
import json
import math
import socket
import ssl
import gzip
import ipaddress
import argparse
import urllib.request
from datetime import datetime, date, timedelta
from urllib.parse import quote, urlparse

API_BASE = "onetotenvip.com/skill/cozeSkill/getWxCozeSkillData"
SOURCE_TAG = "公众号爆款文章洞察-SkillHub"

# ── 网络层：标准 TLS（SNI + 证书校验）────────────────────────
# 实测服务端持有有效 DigiCert 证书，带 SNI 且完整校验可正常握手，因此不关闭
# check_hostname / verify_mode。本机 DNS 被分流工具劫持成 fake-ip 时回退 DoH
# 解析真实 IP 直连——证书校验依旧开启，DoH 结果被污染也无法中间人。
RESERVED_BENCHMARK = ipaddress.ip_network("198.18.0.0/15")


def _is_unusable_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return addr in RESERVED_BENCHMARK or addr.is_loopback or addr.is_unspecified


def resolve_via_doh(host, timeout=15):
    req = urllib.request.Request(
        f"https://dns.google/resolve?name={quote(host)}&type=A",
        headers={"Accept": "application/dns-json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=ssl.create_default_context()) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
    except Exception:
        return None
    for ans in data.get("Answer") or []:
        ip = str(ans.get("data", "")).strip()
        if ans.get("type") == 1 and not _is_unusable_ip(ip):
            return ip
    return None


def decode_chunked(data):
    chunks, idx = [], 0
    while idx < len(data):
        end = data.find(b"\r\n", idx)
        if end == -1:
            break
        try:
            size = int(data[idx:end], 16)
        except Exception:
            break
        if size == 0:
            break
        start = end + 2
        if start + size > len(data):
            break
        chunks.append(data[start:start + size])
        idx = start + size
    return b"".join(chunks)


def _request_once(connect_host, sni_host, path, headers, timeout):
    ctx = ssl.create_default_context()
    sock = socket.create_connection((connect_host, 443), timeout=timeout)
    try:
        ss = ctx.wrap_socket(sock, server_hostname=sni_host)
    except Exception:
        sock.close()
        raise
    try:
        req = [f"GET /{path} HTTP/1.1", f"Host: {sni_host}"]
        req += [f"{k}: {v}" for k, v in headers.items()]
        ss.send("\r\n".join(req + ["", ""]).encode())
        buf = b""
        while True:
            try:
                c = ss.recv(65536)
            except (socket.timeout, TimeoutError):
                break
            if not c:
                break
            buf += c
    finally:
        try:
            ss.close()
        except Exception:
            pass

    head_end = buf.find(b"\r\n\r\n")
    if head_end == -1:
        raise Exception("响应不完整：未收到完整 HTTP 头")
    body = buf[head_end + 4:]
    hdrs = {}
    for line in buf[:head_end].decode("utf-8", "ignore").split("\r\n")[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            hdrs[k.strip().lower()] = v.strip()
    if hdrs.get("transfer-encoding", "").lower() == "chunked":
        body = decode_chunked(body)
    if hdrs.get("content-encoding", "").lower() == "gzip":
        try:
            body = gzip.decompress(body)
        except Exception:
            pass
    status = int(buf.split(b"\r\n", 1)[0].split()[1])
    return status, body.decode("utf-8", errors="ignore")


def http_get(base_url, params, headers, timeout=45):
    host, path = base_url.split("://", 1)[1].split("/", 1)
    if params:
        path += "?" + "&".join(f"{quote(str(k))}={quote(str(v))}" for k, v in params.items())
    try:
        return _request_once(host, host, path, headers, timeout)
    except Exception as direct_err:
        ip = resolve_via_doh(host)
        if not ip:
            raise direct_err
        return _request_once(ip, host, path, headers, timeout)


# ── 安全工具 ──────────────────────────────────────
def sanitize_http_url(url) -> str:
    if url is None:
        return ""
    url = str(url).strip()
    if not url or len(url) > 4096:
        return ""
    p = urlparse(url)
    return url if p.scheme in ("http", "https") else ""


def safe_href_url(url) -> str:
    u = sanitize_http_url(url)
    return html.escape(u, quote=True) if u else "#"


def safe_filename_from_keyword(keyword: str, max_len: int = 120) -> str:
    base = (keyword or "").strip()
    if not base:
        return ""
    base = base.replace("\\", "_").replace("/", "_").replace("\x00", "")
    base = re.sub(r"\.+", "_", base)
    if base in (".", "..") or ".." in base:
        base = "keyword"
    return base[:max_len]


def parse_count(value):
    """'10w+' -> 100000；'1,234' -> 1234；空/None -> 0。"""
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    v = str(value).replace("+", "").replace(",", "").strip()
    if not v or v.lower() in ("none", "null"):
        return 0
    if "w" in v.lower():
        try:
            return int(float(v.lower().replace("w", "")) * 10000)
        except Exception:
            return 0
    try:
        return int(float(v))
    except Exception:
        return 0


def fetch_gzh_trends(keyword, start_date=None, max_retries=3):
    params = {"keyword": keyword, "source": SOURCE_TAG}
    if start_date:
        params["startDate"] = start_date
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept-Encoding": "identity",
        "Connection": "close",
    }
    import time
    last = None
    for attempt in range(max_retries):
        try:
            status, body = http_get(f"https://{API_BASE}", params, headers)
            if status >= 400:
                raise Exception(f"HTTP {status}")
            data = json.loads(body)  # 截断响应会在此抛 JSONDecodeError → 重试
            if data.get("code") != 2000 or "data" not in data:
                raise Exception(f"API 错误: {data.get('msg', data.get('error', '未知'))}")
            d = data["data"]
            return {
                "keyword": keyword,
                "low_fan_explosive": d.get("lowPowderExplosiveArticle", []),
                "one_w_reading": d.get("oneWReadingRank", []),
                "original_rank": d.get("originalRank", []),
                "ten_w_reading": d.get("tenWReadingRank", []),
            }
        except Exception as e:
            last = str(e)
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
    raise Exception(f"{last}（已尝试 {max_retries} 次）")


CATEGORIES = [
    ("low_fan_explosive", "低粉爆款"),
    ("ten_w_reading", "10万阅读"),
    ("one_w_reading", "1万阅读"),
    ("original_rank", "原创榜"),
]

# 实测标定：raw 分落在 81~289（全站 200/200、"职场" 178/181 条 >=100），
# 直接 min(100, raw) 会让大量条目同为 100 分、排序失效。除以 SCORE_DIVISOR
# 做单调缩放后零撞顶、区分度最好（全站 151 个唯一分 / 200 条）。
SCORE_DIVISOR = 3.0


def calc_score(item, cat_key):
    read = parse_count(item.get("clicksCount"))
    like = parse_count(item.get("likeCount"))
    share = parse_count(item.get("shareCount"))
    watch = parse_count(item.get("watchCount"))
    comment = parse_count(item.get("commentCount"))
    if read + like + share + watch + comment == 0:
        return 0.0
    # clicksCount 有 "10w+" 封顶（全站 75% 撞该值、仅 2 个唯一值），区分度低，
    # 故权重最低；分享与在看最能反映公众号传播力，权重最高。
    raw = (
        math.log10(read + 1) * 14
        + math.log10(share + 1) * 20
        + math.log10(watch + 1) * 18
        + math.log10(like + 1) * 14
        + math.log10(comment + 1) * 12
    )
    score = raw / SCORE_DIVISOR
    if cat_key == "low_fan_explosive":
        fans = parse_count(item.get("fans"))
        if 0 < fans < 10000:
            score += 10
        elif 0 < fans < 50000:
            score += 6
    if item.get("originalFlag") == 1:
        score += 3
    return min(100.0, score)


def merge_and_sort(data, max_items=12):
    pool, seen = [], set()
    for key, name in CATEGORIES:
        for item in data.get(key, []):
            pid = item.get("photoId", "")
            if pid and pid not in seen:
                seen.add(pid)
                pool.append({"cat_key": key, "cat_name": name, "item": item,
                             "score": calc_score(item, key)})
    pool.sort(key=lambda x: x["score"], reverse=True)
    by_cat = {}
    for x in pool:
        by_cat.setdefault(x["cat_key"], []).append(x)
    order = sorted(by_cat, key=lambda k: by_cat[k][0]["score"], reverse=True)
    picked, idx = [], {k: 0 for k in by_cat}
    while len(picked) < max_items:
        added = False
        for k in order:
            if idx[k] < len(by_cat[k]):
                picked.append(by_cat[k][idx[k]])
                idx[k] += 1
                added = True
                if len(picked) >= max_items:
                    break
        if not added:
            break
    picked.sort(key=lambda x: x["score"], reverse=True)
    return picked


def esc(s):
    return html.escape(str(s or ""), quote=False)


def clean_text(s):
    """正文里有大量换行和空白，压一压便于喂模型。"""
    return re.sub(r"\s+", " ", str(s or "")).strip()


def format_json(picked, content_len=800):
    out = []
    for x in picked:
        it = x["item"]
        body = clean_text(it.get("content"))
        out.append({
            "category": x["cat_name"],
            "title": (it.get("title") or "").strip() or None,
            "summary": clean_text(it.get("summary")) or None,
            "content": (body[:content_len] if content_len > 0 else body) or None,
            "has_body": bool(body),
            "content_chars": len(body),
            "author": it.get("userName"),
            "fans": it.get("fans"),
            "publicTime": it.get("publicTime"),
            "read": it.get("clicksCount"),
            "like": it.get("likeCount"),
            "comment": it.get("commentCount"),
            "share": it.get("shareCount"),
            "watch": it.get("watchCount"),
            "original": it.get("originalFlag") == 1,
            "topic": it.get("type"),
            "link": sanitize_http_url(it.get("oriUrl")),
            "coverUrl": sanitize_http_url(it.get("coverUrl")),
            "score": round(x["score"], 1),
        })
    return json.dumps({
        "source": "onetotenvip/wx",
        "granularity": "content",
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total": len(out),
        "items": out,
    }, ensure_ascii=False, indent=1)


def format_text(picked):
    lines = [f"公众号爆款文章 · {datetime.now():%Y-%m-%d %H:%M} · 共 {len(picked)} 条", ""]
    for i, x in enumerate(picked, 1):
        it = x["item"]
        orig = " [原创]" if it.get("originalFlag") == 1 else ""
        lines.append(f"{i:>2}. [{x['cat_name']}]{orig} {it.get('title') or '无标题'}  ({x['score']:.1f}分)")
        lines.append(f"    {it.get('userName')} · {it.get('fans')}粉 · {(it.get('publicTime') or '')[:10]} · {it.get('type') or '-'}")
        lines.append(f"    阅读{it.get('clicksCount')} 赞{it.get('likeCount')} 在看{it.get('watchCount')} "
                     f"分享{it.get('shareCount')} 评论{it.get('commentCount')}")
        s = clean_text(it.get("summary"))
        if s:
            lines.append(f"    摘要：{s[:100]}")
        lines.append(f"    {sanitize_http_url(it.get('oriUrl'))}")
        lines.append("")
    return "\n".join(lines)


def gen_card(x, i):
    it = x["item"]
    title = esc(it.get("title") or "无标题")
    author = esc(it.get("userName") or "未知")
    fans = esc(it.get("fans") or "-")
    pub = esc((it.get("publicTime") or "")[:10])
    link = safe_href_url(it.get("oriUrl"))
    cover = safe_href_url(it.get("coverUrl"))
    summary = esc(clean_text(it.get("summary"))[:80])
    orig = '<span class="orig">原创</span>' if it.get("originalFlag") == 1 else ""
    return f'''
    <div class="card">
      <a href="{link}" target="_blank" rel="noopener noreferrer" class="cover-wrap">
        <img class="cover" src="{cover}" alt="" loading="lazy"
             onerror="this.parentNode.classList.add('no-img')">
        <span class="rank">{i+1}</span>
        <span class="tag">{esc(x["cat_name"])}</span>
      </a>
      <div class="body">
        <a href="{link}" target="_blank" rel="noopener noreferrer" class="title">{title}</a>
        <div class="meta">{author} · {fans}粉 {orig}</div>
        <div class="summary">{summary}</div>
        <div class="stats"><span>👁 {esc(it.get("clicksCount") or "-")}</span><span>👍 {esc(it.get("likeCount") or "-")}</span>
          <span>👀 {esc(it.get("watchCount") or "-")}</span><span>↗ {esc(it.get("shareCount") or "-")}</span></div>
        <div class="date">发布 {pub}</div>
      </div>
    </div>'''


def format_html(data, picked):
    kw = esc(data.get("keyword") or "全站热门")
    dates = sorted(d for d in ((it.get("publicTime") or "")[:10]
                               for key, _ in CATEGORIES for it in data.get(key, [])) if d)
    rng = f"{dates[0]} ~ {dates[-1]}" if dates else "无数据"
    cards = "".join(gen_card(x, i) for i, x in enumerate(picked)) or \
        '<p style="text-align:center;color:#999;padding:40px">该关键词暂无数据，建议换更热门的赛道词</p>'
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>公众号爆款文章报告 · {kw}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif;background:#f7f7f8;padding:16px;color:#333}}
.container{{max-width:1200px;margin:0 auto}}
.header{{background:linear-gradient(135deg,#07c160 0%,#10ad7a 100%);color:#fff;padding:22px 24px;border-radius:14px;margin-bottom:20px}}
.header h1{{font-size:21px;margin-bottom:6px}}
.header .sub{{font-size:13px;opacity:.92}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:14px}}
.card{{background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.07);transition:transform .2s,box-shadow .2s;display:flex;flex-direction:column}}
.card:hover{{transform:translateY(-3px);box-shadow:0 6px 18px rgba(7,193,96,.15)}}
.cover-wrap{{position:relative;display:block;background:#e8f7ef;aspect-ratio:16/9}}
.cover{{width:100%;height:100%;object-fit:cover;display:block}}
.cover-wrap.no-img::after{{content:'无封面';position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:#7bcfa4;font-size:14px}}
.rank{{position:absolute;top:8px;left:8px;background:rgba(0,0,0,.55);color:#fff;font-size:12px;font-weight:700;padding:2px 8px;border-radius:10px}}
.tag{{position:absolute;top:8px;right:8px;background:#07c160;color:#fff;font-size:11px;padding:2px 8px;border-radius:10px}}
.body{{padding:10px 12px 12px;display:flex;flex-direction:column;gap:6px;flex:1}}
.title{{font-size:14px;font-weight:700;color:#222;text-decoration:none;line-height:1.45;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}}
.title:hover{{color:#07c160}}
.meta{{font-size:12px;color:#888}}
.orig{{background:#fff3e0;color:#e6820e;font-size:10px;padding:1px 5px;border-radius:4px;margin-left:4px}}
.summary{{font-size:12px;color:#666;line-height:1.5;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}}
.stats{{display:flex;flex-wrap:wrap;gap:8px;font-size:12px;color:#555;background:#f2fbf6;padding:6px 8px;border-radius:8px}}
.date{{font-size:11px;color:#aaa;margin-top:auto}}
.note{{text-align:center;color:#999;font-size:12px;margin-top:20px;padding:12px;background:#fff;border-radius:10px}}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>📗 公众号爆款文章分析报告</h1>
    <div class="sub">关键词：{kw} ｜ 数据源发布日期范围：{rng} ｜ 生成时间：{now}</div>
  </div>
  <div class="grid">{cards}</div>
  <div class="note">数据来源：第三方爆款监测库（每日更新），互动量为入库快照、可能持续增长<br>榜单维度：低粉爆款 / 10万阅读 / 1万阅读 / 原创榜，跨榜去重后按数据分综合排序</div>
</div>
</body>
</html>'''


def main():
    ap = argparse.ArgumentParser(description="公众号爆款文章查询")
    ap.add_argument("--keyword", default="", help="赛道/细分关键词（空=全站）")
    ap.add_argument("--days", type=int, default=7, help="时间窗（天，默认7，最大30；本接口真实生效）")
    ap.add_argument("--max-items", type=int, default=12)
    ap.add_argument("--content-len", type=int, default=800,
                    help="JSON 中正文截断长度，0=全文（默认800）")
    ap.add_argument("--original-only", action="store_true", help="只保留原创文章")
    ap.add_argument("--output-format", choices=["json", "html", "text"], default="json")
    ap.add_argument("--output-file", default=None)
    ap.add_argument("--stdout", action="store_true",
                    help="内容打印到标准输出、不写文件（统计信息走 stderr，可直接管道）")
    args = ap.parse_args()

    start = (date.today() - timedelta(days=min(max(args.days, 1), 30))).isoformat()
    data = fetch_gzh_trends(args.keyword or "", start_date=start)

    if args.original_only:
        for key, _ in CATEGORIES:
            data[key] = [it for it in data.get(key, []) if it.get("originalFlag") == 1]

    picked = merge_and_sort(data, args.max_items)

    if args.output_format == "json":
        content = format_json(picked, args.content_len)
    elif args.output_format == "text":
        content = format_text(picked)
    else:
        content = format_html(data, picked)

    raw = sum(len(data[k]) for k, _ in CATEGORIES)
    uniq = len({it.get("photoId") for k, _ in CATEGORIES
                for it in data[k] if it.get("photoId")})

    if args.stdout:
        log = sys.stderr
        sys.stdout.write(content)
        if not content.endswith("\n"):
            sys.stdout.write("\n")
    else:
        log = sys.stdout
        safe_kw = safe_filename_from_keyword(args.keyword) or "全站"
        ext = "html" if args.output_format == "html" else ("txt" if args.output_format == "text" else "json")
        out = args.output_file or f"{safe_kw}_公众号爆款.{ext}"
        with open(out, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"✅ 已保存: {out}", file=log)

    print(f"📊 关键词: {args.keyword or '全站'} | 请求起始日: {start}", file=log)
    print(f"   候选 {raw} 条（跨榜去重后 {uniq} 条唯一文章）→ 精选 {len(picked)} 条", file=log)
    if uniq < args.max_items:
        print(f"   ⚠️  唯一文章不足 {args.max_items} 条，已如实按 {len(picked)} 条输出", file=log)


if __name__ == "__main__":
    main()
