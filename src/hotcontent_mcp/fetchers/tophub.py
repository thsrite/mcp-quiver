#!/usr/bin/env python3
"""
全网热榜查询脚本（tophub.today）
一次抓取首页即可拿到 88 个榜单的完整条目，覆盖微博/知乎/微信/百度/B站/
抖音/快手/贴吧/虎扑/36氪/掘金/少数派等。无需登录、无需 API key。

粒度说明：tophub 是**话题级**数据——只有标题、排名、热度值，
没有单条内容的点赞/收藏/评论数。需要内容级互动数据请用 fetch_xhs_trends.py。
注意：tophub 首页不含小红书榜。
"""

import re
import sys
import html
import json
import socket
import ssl
import gzip
import ipaddress
import argparse
import urllib.request
from datetime import datetime
from urllib.parse import quote, unquote, urlparse, parse_qs

TOPHUB_URL = "https://tophub.today/"

# ── 网络层：标准 TLS（SNI + 证书校验），与 fetch_xhs_trends.py 同款 ──
RESERVED_BENCHMARK = ipaddress.ip_network("198.18.0.0/15")


def _is_unusable_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return addr in RESERVED_BENCHMARK or addr.is_loopback or addr.is_unspecified


def resolve_via_doh(host, timeout=15):
    """系统 DNS 被分流工具劫持成 fake-ip 时，用 DoH 拿真实 A 记录。"""
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


def http_get(url, headers, timeout=45):
    """先按域名直连；DNS 被劫持时回退 DoH 解析真实 IP。两条路径都走完整 TLS 校验。"""
    rest = url.split("://", 1)[1]
    host, _, path = rest.partition("/")
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


def safe_filename(name: str, max_len: int = 120) -> str:
    base = (name or "").strip()
    if not base:
        return ""
    base = base.replace("\\", "_").replace("/", "_").replace("\x00", "")
    base = re.sub(r"\.+", "_", base)
    if base in (".", "..") or ".." in base:
        base = "board"
    return base[:max_len]


# ── 解析 ──────────────────────────────────────────
CARD_RE = re.compile(r'<div class="cc-cd"[^>]*>.*?(?=<div class="cc-cd"[^>]*>|\Z)', re.S)
PLAT_RE = re.compile(r'cc-cd-lb.*?<span>\s*(.*?)\s*</span>', re.S)
BOARD_RE = re.compile(r'cc-cd-sb-st"?>\s*(.*?)\s*</span>', re.S)
NODE_RE = re.compile(r'href="/n/([A-Za-z0-9]+)"')
ITEM_RE = re.compile(
    r'<a href="([^"]*)"[^>]*itemid="[^"]*">.*?'
    r'<span class="s[^"]*">\s*(\d+)\s*</span>.*?'
    r'<span class="t">(.*?)</span>.*?'
    r'(?:<span class="e">(.*?)</span>)?\s*</div>', re.S)


def clean(text):
    """去标签 + 反转义 + 压空白。"""
    t = re.sub(r"<[^>]+>", "", text or "")
    return re.sub(r"\s+", " ", html.unescape(t)).strip()


def real_link(href):
    """tophub 的跳转链接里 url= 参数才是真实地址；解不出就原样返回。"""
    href = html.unescape((href or "").strip())
    if "tophub.today/link" in href:
        q = parse_qs(urlparse(href).query)
        target = unquote(q.get("url", [""])[0])
        if target:
            return sanitize_http_url(target)
    if href.startswith("/"):
        return "https://tophub.today" + href
    return sanitize_http_url(href)


def parse_boards(page):
    boards = []
    for card in CARD_RE.findall(page):
        plat = PLAT_RE.search(card)
        board = BOARD_RE.search(card)
        node = NODE_RE.search(card)
        if not plat:
            continue
        items = []
        for href, rank, title, heat in ITEM_RE.findall(card):
            t = clean(title)
            if not t:
                continue
            items.append({
                "rank": int(rank),
                "title": t,
                "heat": clean(heat) or None,
                "link": real_link(href),
            })
        if not items:
            continue
        boards.append({
            "platform": clean(plat.group(1)),
            "board": clean(board.group(1)) if board else "",
            "node": node.group(1) if node else None,
            "items": items,
        })
    # tophub 首页会在"推荐区"和分类区重复展示同一榜单，按 node（无 node 时按
    # 平台+榜单名）去重，保留条目更多的那份。
    dedup = {}
    for b in boards:
        key = b["node"] or f"{b['platform']}|{b['board']}"
        if key not in dedup or len(b["items"]) > len(dedup[key]["items"]):
            dedup[key] = b
    return list(dedup.values())


def fetch_boards(max_retries=3):
    headers = {
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept-Encoding": "identity",
        "Connection": "close",
    }
    import time
    last = None
    for attempt in range(max_retries):
        try:
            status, page = http_get(TOPHUB_URL, headers)
            if status >= 400:
                raise Exception(f"HTTP {status}")
            boards = parse_boards(page)
            if not boards:
                raise Exception("未解析到任何榜单（页面结构可能已变更）")
            return boards
        except Exception as e:
            last = str(e)
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
    raise Exception(f"{last}（已尝试 {max_retries} 次）")


def select(boards, platform=None, keyword=None, limit=20, max_boards=0):
    plats = [p.strip() for p in (platform or "").split(",") if p.strip()]
    out = []
    for b in boards:
        if plats and not any(p in b["platform"] or p in b["board"] for p in plats):
            continue
        items = b["items"]
        if keyword:
            items = [i for i in items if keyword in i["title"]]
        if not items:
            continue
        out.append({**b, "items": items[:limit] if limit > 0 else items})
        if max_boards and len(out) >= max_boards:
            break
    return out


def to_records(boards):
    recs = []
    for b in boards:
        cat = f"{b['platform']}·{b['board']}" if b["board"] else b["platform"]
        for it in b["items"]:
            recs.append({
                "category": cat,
                "platform": b["platform"],
                "board": b["board"],
                "node": b["node"],
                "rank": it["rank"],
                "title": it["title"],
                "heat": it["heat"],
                "link": it["link"],
            })
    return recs


def format_json(boards):
    recs = to_records(boards)
    return json.dumps({
        "source": "tophub.today",
        "granularity": "topic",  # 话题级：无点赞/收藏/评论数
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "boards": len(boards),
        "total": len(recs),
        "items": recs,
    }, ensure_ascii=False, indent=1)


def format_text(boards):
    lines = [f"全网热榜 · tophub.today · {datetime.now():%Y-%m-%d %H:%M}",
             f"共 {len(boards)} 个榜单 / {sum(len(b['items']) for b in boards)} 条", ""]
    for b in boards:
        head = f"【{b['platform']}"
        head += f" · {b['board']}】" if b["board"] else "】"
        lines.append(head)
        for it in b["items"]:
            heat = f"  ({it['heat']})" if it["heat"] else ""
            t = it["title"]
            if len(t) > 60:  # 抖音等平台的标题是整段文案，截断保证可读
                t = t[:60] + "…"
            lines.append(f"  {it['rank']:>3}. {t}{heat}")
        lines.append("")
    return "\n".join(lines)


def format_list(boards):
    lines = [f"{'平台':<14}{'榜单':<18}{'node':<12}条目"]
    for b in boards:
        lines.append(f"{b['platform']:<14}{b['board']:<18}{str(b['node'] or '-'):<12}{len(b['items'])}")
    lines.append(f"\n共 {len(boards)} 个榜单，{sum(len(b['items']) for b in boards)} 条")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="全网热榜查询（tophub.today）")
    ap.add_argument("--platform", default="", help="按平台/榜单名筛选，逗号分隔（子串匹配），如 '微博,知乎,抖音'")
    ap.add_argument("--keyword", default="", help="按条目标题过滤")
    ap.add_argument("--limit", type=int, default=20, help="每个榜单最多取几条（0=全部，默认20）")
    ap.add_argument("--max-boards", type=int, default=0, help="最多几个榜单（0=全部）")
    ap.add_argument("--list", action="store_true", help="只列出有哪些榜单，不输出条目")
    ap.add_argument("--output-format", choices=["json", "text"], default="json")
    ap.add_argument("--output-file", default=None)
    ap.add_argument("--stdout", action="store_true",
                    help="内容打印到标准输出、不写文件（统计信息走 stderr，可直接管道）")
    args = ap.parse_args()

    all_boards = fetch_boards()
    boards = select(all_boards, args.platform, args.keyword, args.limit, args.max_boards)

    if args.list:
        content = format_list(boards)
    elif args.output_format == "json":
        content = format_json(boards)
    else:
        content = format_text(boards)

    if args.stdout or args.list:
        log = sys.stderr
        sys.stdout.write(content)
        if not content.endswith("\n"):
            sys.stdout.write("\n")
    else:
        log = sys.stdout
        tag = safe_filename(args.platform or args.keyword) or "全网"
        out = args.output_file or f"{tag}_热榜.{args.output_format}"
        with open(out, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"✅ 已保存: {out}", file=log)

    n = sum(len(b["items"]) for b in boards)
    print(f"📊 抓取 {len(all_boards)} 个榜单 → 筛选后 {len(boards)} 个榜单 / {n} 条", file=log)
    if not boards:
        if args.platform:
            print(f"   ⚠️  没有匹配平台 '{args.platform}' 的榜单，用 --list 看可用榜单", file=log)
        if args.keyword:
            print(f"   ⚠️  没有标题包含 '{args.keyword}' 的条目", file=log)


if __name__ == "__main__":
    main()
