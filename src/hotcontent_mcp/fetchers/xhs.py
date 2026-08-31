#!/usr/bin/env python3
"""
小红书爆款笔记查询脚本（HTML 封面卡片页输出）
数据源：onetotenvip.com /skill/cozeSkill/getXhsCozeSkillData（与公众号爆款技能同服务商）
榜单类型：点赞Top500、低粉爆款、7日增量、单日增量
"""

import sys
import re
import html
import json
import math
import socket
import ssl
import gzip
import argparse
import ipaddress
import urllib.request
from datetime import datetime, date, timedelta
from urllib.parse import quote, urlparse

API_BASE = "onetotenvip.com/skill/cozeSkill/getXhsCozeSkillData"
NOTE_URL = "https://www.xiaohongshu.com/explore/"


# ── 安全工具（与 gzh 技能同款模式）──────────────────────────
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


def safe_note_url(photo_id) -> str:
    s = str(photo_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9]{8,64}", s):
        return "#"
    return html.escape(NOTE_URL + s, quote=True)


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


# ── 网络层：标准 TLS（SNI + 证书校验）────────────────────────
# 说明：实测服务端持有有效 DigiCert 证书，带 SNI 且完整校验可正常握手，
# 因此不再关闭 check_hostname / verify_mode。若本机 DNS 被分流工具劫持
# （解析到 198.18.0.0/15 等保留网段导致连不上），退化为 DoH 解析真实 IP
# 后直连——证书校验依旧开启，所以 DoH 结果即使被污染也无法中间人。

RESERVED_BENCHMARK = ipaddress.ip_network("198.18.0.0/15")


def _is_unusable_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return addr in RESERVED_BENCHMARK or addr.is_loopback or addr.is_unspecified


def resolve_via_doh(host, timeout=15):
    """系统 DNS 不可用时，用 DoH 拿真实 A 记录。失败返回 None。"""
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


def _read_all(ss):
    buf = b""
    while True:
        try:
            c = ss.recv(65536)
        except (socket.timeout, TimeoutError):
            break  # 服务端未按时关闭；用已收到的数据继续（后续有 JSON 完整性校验）
        if not c:
            break
        buf += c
    return buf


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
    ctx = ssl.create_default_context()  # 完整证书链 + 主机名校验
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
        buf = _read_all(ss)
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
    """先按域名直连；本机 DNS 被劫持导致失败时，回退 DoH 解析真实 IP。
    两条路径都走完整 TLS 校验（SNI 始终为真实域名）。"""
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


def fetch_xhs_trends(keyword, start_date=None, max_retries=3):
    params = {"keyword": keyword, "source": "小红书爆款内容洞察-Hermes"}
    if start_date:
        params["startDate"] = start_date
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
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
                "like_top500": d.get("likeTheTop500", []),
                "low_fan_explosive": d.get("lowPowderExplosiveArticle", []),
                "seven_day_inc": d.get("sevenDaysOfIncrements", []),
                "single_day_inc": d.get("singleDayIncrements", []),
            }
        except Exception as e:
            last = str(e)
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
    raise Exception(f"{last}（已尝试 {max_retries} 次）")


CATEGORIES = [
    ("like_top500", "点赞Top500"),
    ("low_fan_explosive", "低粉爆款"),
    ("seven_day_inc", "7日飙升"),
    ("single_day_inc", "单日飙升"),
]


# 飙升两榜（sevenDaysOfIncrements / singleDayIncrements）的互动数不在顶层，
# 而嵌套在 anaAdd 里（顶层同名键恒为 None）。所有取数统一走这里。
INCREMENT_KEYS = ("seven_day_inc", "single_day_inc")

# 评分缩放系数，见 calc_score 内的实测标定说明
SCORE_DIVISOR = 3.5


def stat(item, field):
    """顶层取不到时回退 anaAdd。注意 anaAdd 把收藏拼成 addCollectedCunt（源端拼写）。"""
    v = item.get(field)
    if v is not None:
        return v
    ana = item.get("anaAdd") or {}
    v = ana.get(field)
    if v is not None:
        return v
    if field == "collectedCount":
        return ana.get("addCollectedCunt")
    return None


def calc_score(item, cat_key):
    like = parse_count(stat(item, "useLikeCount"))
    col = parse_count(stat(item, "collectedCount"))
    com = parse_count(stat(item, "useCommentCount"))
    shr = parse_count(stat(item, "useShareCount"))
    if like + col + com + shr == 0:
        return 0.0
    raw = (
        math.log10(like + 1) * 16
        + math.log10(col + 1) * 18
        + math.log10(com + 1) * 15
        + math.log10(shr + 1) * 15
    )
    # 实测标定：真实爆款的 raw 分落在 143~319（全站 196/196、穿搭 133/133 条
    # 全部 >=100）。原先直接 min(100, raw) 会让所有条目同为 100 分，排序失效。
    # 除以 SCORE_DIVISOR 做单调缩放（不改变相对次序）后再叠加分类加分。
    score = raw / SCORE_DIVISOR
    if cat_key == "low_fan_explosive":
        fans = parse_count(item.get("fans"))
        if 0 < fans < 10000:
            score += 10
        elif 0 < fans < 50000:
            score += 6
    if cat_key == "single_day_inc":
        score += 4
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
    # 分类多样性：轮转选取
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


# 源端 desc = 正文（常缺）+ 一长串 #话题标签。实测 133 条样本中，去标签后
# 实质文字占比中位数仅 16%，44% 的条目去标签后完全为空。接口不提供笔记正文，
# 这里只做拆分，不臆造内容：body 是去标签后的真实文字（可能为空），tags 独立成数组。
TAG_RE = re.compile(r"#([^#\s][^#]*?)(?=\s|#|$)")


def split_desc(raw):
    """把 desc 拆成 (正文, 标签列表)。正文可能为空字符串——如实反映源端缺正文。"""
    raw = (raw or "").strip()
    if not raw:
        return "", []
    tags = [t.strip() for t in TAG_RE.findall(raw) if t.strip()]
    body = TAG_RE.sub("", raw)
    body = re.sub(r"[ \t]+", " ", body)
    body = "\n".join(ln.strip() for ln in body.split("\n"))
    return body.strip(), tags


def esc(s):
    return html.escape(str(s or ""), quote=False)


def gen_card(x, i):
    it = x["item"]
    _body, _tags = split_desc(it.get("desc"))
    title = esc((it.get("title") or "").strip() or (_body.split("\n")[0][:40] if _body else "")
                or (("#" + _tags[0]) if _tags else "无标题"))
    author = esc(it.get("userName") or "未知")
    fans = esc(it.get("fans") or "-")
    pub = (it.get("publicTime") or "")[:10]
    link = safe_note_url(it.get("photoId"))
    cover = safe_href_url(it.get("coverUrl"))
    like = esc(stat(it, "useLikeCount") or "-")
    col = esc(stat(it, "collectedCount") or "-")
    com = esc(stat(it, "useCommentCount") or "-")
    shr = esc(stat(it, "useShareCount") or "-")
    tag = esc(x["cat_name"])
    return f'''
    <div class="card">
      <a href="{link}" target="_blank" rel="noopener noreferrer" class="cover-wrap">
        <img class="cover" src="{cover}" alt="" loading="lazy"
             onerror="this.parentNode.classList.add('no-img')">
        <span class="rank">{i+1}</span>
        <span class="tag">{tag}</span>
      </a>
      <div class="body">
        <a href="{link}" target="_blank" rel="noopener noreferrer" class="title">{title}</a>
        <div class="meta">{author} · {fans}粉</div>
        <div class="stats"><span>❤ {like}</span><span>⭐ {col}</span><span>💬 {com}</span><span>↗ {shr}</span></div>
        <div class="date">发布 {esc(pub)}</div>
      </div>
    </div>'''


def format_html(data, picked, start_date=None, max_items=12):
    kw = esc(data.get("keyword") or "全站热门")
    # 不按请求参数标注窗口：实测 startDate 只是下界且对飙升榜不生效，
    # 直接展示数据源实际返回的发布日期范围，避免"近7天"这类假标注。
    dates = sorted(d for d in (
        (it.get("publicTime") or "")[:10]
        for key, _ in CATEGORIES for it in data.get(key, [])
    ) if d)
    days = f"{dates[0]} ~ {dates[-1]}" if dates else "无数据"
    cards = "".join(gen_card(x, i) for i, x in enumerate(picked)) or \
        '<p style="text-align:center;color:#999;padding:40px">该关键词暂无数据，建议换更热门的赛道词</p>'
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>小红书爆款笔记报告 · {kw}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif;background:#f7f7f8;padding:16px;color:#333}}
.container{{max-width:1200px;margin:0 auto}}
.header{{background:linear-gradient(135deg,#ff2442 0%,#ff5f8f 100%);color:#fff;padding:22px 24px;border-radius:14px;margin-bottom:20px}}
.header h1{{font-size:21px;margin-bottom:6px}}
.header .sub{{font-size:13px;opacity:.92}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:14px}}
.card{{background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.07);transition:transform .2s,box-shadow .2s;display:flex;flex-direction:column}}
.card:hover{{transform:translateY(-3px);box-shadow:0 6px 18px rgba(255,36,66,.15)}}
.cover-wrap{{position:relative;display:block;background:#ffe9ee;aspect-ratio:3/4}}
.cover{{width:100%;height:100%;object-fit:cover;display:block}}
.cover-wrap.no-img::after{{content:'无封面';position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:#ff8fa8;font-size:14px}}
.rank{{position:absolute;top:8px;left:8px;background:rgba(0,0,0,.55);color:#fff;font-size:12px;font-weight:700;padding:2px 8px;border-radius:10px}}
.tag{{position:absolute;top:8px;right:8px;background:#ff2442;color:#fff;font-size:11px;padding:2px 8px;border-radius:10px}}
.body{{padding:10px 12px 12px;display:flex;flex-direction:column;gap:6px;flex:1}}
.title{{font-size:14px;font-weight:700;color:#222;text-decoration:none;line-height:1.45;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}}
.title:hover{{color:#ff2442}}
.meta{{font-size:12px;color:#888}}
.stats{{display:flex;flex-wrap:wrap;gap:8px;font-size:12px;color:#555;background:#fff5f7;padding:6px 8px;border-radius:8px}}
.date{{font-size:11px;color:#aaa;margin-top:auto}}
.note{{text-align:center;color:#999;font-size:12px;margin-top:20px;padding:12px;background:#fff;border-radius:10px}}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>📕 小红书爆款笔记分析报告</h1>
    <div class="sub">关键词：{kw} ｜ 数据源发布日期范围：{days} ｜ 生成时间：{now}</div>
  </div>
  <div class="grid">{cards}</div>
  <div class="note">数据来源：第三方爆款监测库（每日更新），互动量为入库快照、可能持续增长<br>榜单维度：点赞Top500 / 低粉爆款 / 7日飙升 / 单日飙升，跨榜去重后按数据分综合排序</div>
</div>
</body>
</html>'''


def format_json(picked):
    out = []
    for x in picked:
        it = x["item"]
        body, tags = split_desc(it.get("desc"))
        # title 为空时（实测约 12%）用去标签后的正文首行兜底；仍为空则置 null，
        # 不再拿标签串充数。
        title = (it.get("title") or "").strip() or (body.split("\n")[0][:40] if body else None)
        out.append({
            "category": x["cat_name"],
            "title": title,
            "desc": body[:300],
            "tags": tags,
            "has_body": bool(body),
            "author": it.get("userName"),
            "fans": it.get("fans"),
            "publicTime": it.get("publicTime"),
            "like": stat(it, "useLikeCount"),
            "collected": stat(it, "collectedCount"),
            "comment": stat(it, "useCommentCount"),
            "share": stat(it, "useShareCount"),
            "link": sanitize_http_url(NOTE_URL + str(it.get("photoId", ""))),
            "coverUrl": sanitize_http_url(it.get("coverUrl")),
            "score": round(x["score"], 1),
        })
    return json.dumps({
        "source": "onetotenvip/xhs",
        "granularity": "content",  # 内容级：有赞/藏/评/转，但接口不返回笔记正文
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total": len(out),
        "items": out,
    }, ensure_ascii=False, indent=1)


def main():
    ap = argparse.ArgumentParser(description="小红书爆款笔记查询")
    ap.add_argument("--keyword", default="", help="赛道/细分关键词（空=全站）")
    ap.add_argument("--days", type=int, default=7, help="时间窗（天，默认7，最大30）")
    ap.add_argument("--max-items", type=int, default=12)
    ap.add_argument("--output-format", choices=["html", "json"], default="html")
    ap.add_argument("--output-file", default=None)
    ap.add_argument("--stdout", action="store_true",
                    help="内容打印到标准输出、不写文件（统计信息走 stderr，可直接管道）")
    args = ap.parse_args()

    start = (date.today() - timedelta(days=min(max(args.days, 1), 30))).isoformat()
    data = fetch_xhs_trends(args.keyword or "", start_date=start)
    picked = merge_and_sort(data, args.max_items)

    content = (format_json(picked) if args.output_format == "json"
               else format_html(data, picked, start, args.max_items))

    raw = sum(len(data[k]) for k, _ in CATEGORIES)
    uniq = len({it.get("photoId") for k, _ in CATEGORIES
                for it in data[k] if it.get("photoId")})

    if args.stdout:
        # 内容独占 stdout，统计信息走 stderr，保证 `... --stdout | jq` 可用
        log = sys.stderr
        sys.stdout.write(content)
        if not content.endswith("\n"):
            sys.stdout.write("\n")
    else:
        log = sys.stdout
        safe_kw = safe_filename_from_keyword(args.keyword) or "全站"
        out = args.output_file or f"{safe_kw}_小红书爆款.{args.output_format}"
        with open(out, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"✅ 已保存: {out}", file=log)

    print(f"📊 关键词: {args.keyword or '全站'} | 请求起始日: {start}", file=log)
    print(f"   候选 {raw} 条（跨榜去重后 {uniq} 条唯一笔记）→ 精选 {len(picked)} 条", file=log)
    if uniq < args.max_items:
        print(f"   ⚠️  唯一笔记不足 {args.max_items} 条，已如实按 {len(picked)} 条输出", file=log)


if __name__ == "__main__":
    main()
