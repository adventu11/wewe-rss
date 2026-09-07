#!/usr/bin/env python3
"""
lw_daily.py —— 抓取语鲸自己生成的「个人专属日报」。

已确认的接口(来自浏览器抓包):
    POST /api/lingowhale/v1/lingowhale_daily/get
    {"daily_id": "6a8dea9a34cc9ec434cb0762", "query_generated": false}

还需要探测的: 每天新日报的 daily_id 从哪个列表接口来。
按以下顺序尝试，成功的接口名会记进 lw_daily_state.json，之后直接复用，
不会每天重复探测:
    1. 候选列表接口(lingowhale_daily/list 等)取最新一条
    2. 详情接口传空 daily_id —— 不少 API 这样会直接返回最新一期
    3. 环境变量 LW_DAILY_ID 手动指定(兜底，只能拿固定那一期)

【重要】每次都会把接口返回的原始 JSON 存一份:
    data/reports/lingowhale-daily-YYYY-MM-DD.json
下面的解析器是按常见字段名写的容错解析，如果某天发现渲染出来的内容不对，
把这个 json 发出来就能精确修解析器，数据本身不会丢。

产出:
    data/reports/lingowhale-daily-YYYY-MM-DD.md   归档
    data/reports/lingowhale-daily-YYYY-MM-DD.json 原始返回
    data/feeds/<slug>.atom                        Miniflux 订阅
    Server酱微信推送(可关)

单独跑:
    cd /app && python3 lw_daily.py --dry-run --dump
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape

import lingowhale2rss as lw2r

DAILY_GET_EP = lw2r.API + "lingowhale_daily/get"

# daily_id 列表接口的候选。命中后写进 state 复用。
LIST_CANDIDATES = [
    "lingowhale_daily/list",
    "lingowhale_daily/history",
    "lingowhale_daily/calendar",
    "lingowhale_daily/get_list",
    "lingowhale_daily/dates",
    "daily_report/list",
]
LIST_PAYLOADS = [
    {},
    {"cursor": "", "limit": 10},
    {"limit": 10},
    {"cursor": "", "limit": 10, "sort_type": lw2r.SORT_TYPE},
]

# 容错解析用的字段名候选。语鲸改字段名的话，往这里加就行。
SECTION_LIST_KEYS = ("sections", "blocks", "categories", "modules", "groups", "parts")
SECTION_NAME_KEYS = ("name", "title", "category", "section_name", "block_name")
ITEM_LIST_KEYS = ("items", "entries", "articles", "list", "contents", "children", "cards")
TITLE_KEYS = ("title", "headline", "name", "entry_title")
SUMMARY_KEYS = ("summary", "abstract", "desc", "description", "content", "summary_text", "brief")
CHANNEL_KEYS = ("channel_name", "channel", "source", "source_name", "author", "site")
URL_KEYS = ("orig_url", "url", "link", "jump_url", "origin_url", "web_url")


def post_raw(url, headers, payload):
    """不重试、不 sys.exit 的请求，探测阶段要看到原始错误码。"""
    import gzip
    import urllib.error
    import urllib.request

    body = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
        return r.status, json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            raw = e.read()
            if e.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return e.code, json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return e.code, None
    except Exception as e:  # noqa: BLE001
        return None, {"_error": repr(e)}


def find_daily_id(headers, state):
    """
    返回 (daily_id, 命中的列表接口名 或 None)。

    先用 state 里记着的接口(如果之前探测成功过)，避免每天重复试一堆 404。
    """
    known = state.get("list_endpoint")
    order = ([known] if known else []) + [c for c in LIST_CANDIDATES if c != known]

    for name in order:
        for payload in LIST_PAYLOADS:
            status, data = post_raw(lw2r.API + name, headers, payload)
            if status != 200 or not isinstance(data, dict) or data.get("code") != 0:
                continue
            text = json.dumps(data.get("data") or {}, ensure_ascii=False)
            m = re.search(r'"daily_id"\s*:\s*"([^"]+)"', text)
            if m:
                print(f"[语鲸日报] 列表接口 {name} 命中，daily_id={m.group(1)}", file=sys.stderr)
                return m.group(1), name

    # 兜底一: 详情接口传空 id，很多 API 会返回最新一期
    for payload in ({"daily_id": "", "query_generated": False}, {"query_generated": False}):
        status, data = post_raw(DAILY_GET_EP, headers, payload)
        if status == 200 and isinstance(data, dict) and data.get("code") == 0:
            d = data.get("data") or {}
            text = json.dumps(d, ensure_ascii=False)
            m = re.search(r'"daily_id"\s*:\s*"([^"]+)"', text)
            if d:
                print("[语鲸日报] 详情接口空 id 直接返回了最新一期", file=sys.stderr)
                return (m.group(1) if m else ""), None

    # 兜底二: 手动指定
    manual = os.environ.get("LW_DAILY_ID", "")
    if manual:
        print(f"[语鲸日报] 用环境变量指定的 daily_id={manual}", file=sys.stderr)
        return manual, None
    return "", None


def fetch_daily(headers, daily_id):
    status, data = post_raw(
        DAILY_GET_EP, headers, {"daily_id": daily_id, "query_generated": False}
    )
    if status != 200 or not isinstance(data, dict):
        raise RuntimeError(f"日报接口异常 status={status}")
    if data.get("code") != 0:
        raise RuntimeError(f"日报接口 code={data.get('code')} msg={data.get('msg')}")
    return data.get("data") or {}


# ---------------------------------------------------------------- 解析
# 真实结构(2026-09 抓包确认):
#   data.lingowhale_daily
#     ├ daily_id / upper_time / rec_reason(开场白)
#     ├ user_sub_channel_cnt(订阅频道数) / sub_channel_update_cnt(昨日更新篇数)
#     ├ columnar_blocks[] : {theme, percent, color}   ← 顶部那条占比色带
#     └ blocks[] : {theme, contents[] : {content, entry_list[] : {...}}}
#          entry_list[] : {entry_id, entry_type, content, bold_content,
#                          channel_id, name, surface_url[]}
#
# 注意两个坑:
#   1. 条目里的 name 是【公众号名】("医药观澜-公众号")，不是标题。
#      标题在 bold_content，正文在 content(且 content 以 bold_content 开头)。
#   2. 条目里【没有原文链接】，只有 entry_id —— 靠 lw_cache.json 查表补，
#      查不到的再回源取详情。


def _clean_channel(name):
    """'医药观澜-公众号' -> '医药观澜'"""
    return re.sub(r"[-－]\s*(公众号|订阅号|服务号)$", "", (name or "").strip())


def _split_title(entry):
    """
    返回 (标题, 摘要)。

    bold_content 是那句加粗导语，content 是完整段落且以它开头，
    所以摘要要把开头这段减掉，否则标题和正文会重复一遍。
    """
    bold = (entry.get("bold_content") or "").strip()
    content = (entry.get("content") or "").strip()
    if bold:
        title = bold.rstrip("。.！!　 ")
        summary = content
        if content.startswith(bold):
            summary = content[len(bold) :].strip()
        return title, summary
    # 没有 bold_content 时退而求其次: 拿第一句当标题
    if content:
        m = re.split(r"(?<=[。！!？?])", content, maxsplit=1)
        title = m[0].rstrip("。.！!　 ")
        summary = (m[1] if len(m) > 1 else "").strip()
        return title, summary
    return "", ""


def parse_daily(data):
    """按真实结构解析；结构变了就回落到通用递归解析，不至于整个空掉。"""
    root = data.get("lingowhale_daily") if isinstance(data, dict) else None
    if not isinstance(root, dict):
        root = data if isinstance(data, dict) else {}

    sections = []
    for blk in root.get("blocks") or []:
        if not isinstance(blk, dict):
            continue
        name = (blk.get("theme") or "").strip() or "未命名版块"
        items = []
        for c in blk.get("contents") or []:
            if not isinstance(c, dict):
                continue
            for e in c.get("entry_list") or []:
                if not isinstance(e, dict):
                    continue
                title, summary = _split_title(e)
                if not title:
                    continue
                items.append(
                    {
                        "title": title,
                        "summary": summary,
                        "channel": _clean_channel(e.get("name")),
                        "url": "",  # 稍后由 resolve_links 补
                        "entry_id": e.get("entry_id") or "",
                        "entry_type": e.get("entry_type") or 7,
                    }
                )
        if items:
            sections.append({"name": name, "items": items})

    if not sections:
        print("[语鲸日报] 按已知结构解析为空，改用通用递归解析", file=sys.stderr)
        sections = parse_daily_generic(root)
    return sections


def extract_meta(data, tz):
    root = data.get("lingowhale_daily") if isinstance(data, dict) else None
    if not isinstance(root, dict):
        root = data if isinstance(data, dict) else {}
    meta = {}
    ts = root.get("upper_time")
    if isinstance(ts, (int, float)) and ts > 0:
        meta["date"] = datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d")
    for k in ("rec_reason", "intro", "greeting"):
        v = root.get(k)
        if isinstance(v, str) and v.strip():
            meta["intro"] = re.sub(r"<[^>]+>", "", v).strip()
            break
    meta["sub_cnt"] = root.get("user_sub_channel_cnt")
    meta["update_cnt"] = root.get("sub_channel_update_cnt")
    meta["mix"] = [
        (b.get("theme") or "", b.get("percent"))
        for b in root.get("columnar_blocks") or []
        if isinstance(b, dict) and b.get("theme")
    ]
    return meta


def resolve_links(sections, cache, headers=None, max_fetch=40, delay=0.5):
    """
    日报条目只有 entry_id，没有原文链接。

    先查 lw_cache.json —— 抓取模块本来就按 entry_id 存着 orig_url，
    绝大多数能直接命中，零额外请求。剩下的(比如语鲸日报收录了某篇
    但抓取窗口没覆盖到)再回源取详情，并限量，避免一次打太多请求。
    """
    hit = miss = fetched = 0
    pending = []
    for sec in sections:
        for it in sec["items"]:
            eid = it.get("entry_id")
            if not eid:
                continue
            row = cache.get(eid)
            if row and row.get("orig_url"):
                it["url"] = row["orig_url"]
                hit += 1
            else:
                miss += 1
                pending.append(it)

    if pending and headers is not None:
        for it in pending[:max_fetch]:
            try:
                res = lw2r.fetch_detail(headers, it["entry_id"], it.get("entry_type") or 7)
                url = (res.get("orig_url") or "").replace(
                    "http://mp.weixin.qq.com", "https://mp.weixin.qq.com", 1
                )
                if url:
                    it["url"] = url
                    fetched += 1
                time.sleep(delay)
            except Exception as e:  # noqa: BLE001
                print(f"  [!] 补链接失败 {it['entry_id']}: {e}", file=sys.stderr)

    print(
        f"[语鲸日报] 链接: 缓存命中 {hit}，缺失 {miss}，回源补到 {fetched}",
        file=sys.stderr,
    )
    return sections


# ---------------------------------------------------------------- 通用兜底解析
# 万一语鲸改了字段名，上面的精确解析会返回空，这时靠下面这套"递归找形状"，
# 至少不会整份日报变成空白。

SECTION_LIST_KEYS = ("blocks", "sections", "categories", "modules", "groups")
SECTION_NAME_KEYS = ("theme", "name", "title", "category", "section_name")
ITEM_LIST_KEYS = ("entry_list", "items", "entries", "articles", "list", "contents", "cards")
TITLE_KEYS = ("bold_content", "title", "headline", "entry_title")
SUMMARY_KEYS = ("content", "summary", "abstract", "desc", "description", "brief")
CHANNEL_KEYS = ("name", "channel_name", "channel", "source", "source_name", "author")
URL_KEYS = ("orig_url", "url", "link", "jump_url", "origin_url", "web_url")


def _first(d, keys):
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, dict):
            for kk in ("name", "title"):
                if isinstance(v.get(kk), str) and v[kk].strip():
                    return v[kk].strip()
    return ""


def _parse_items_generic(lst):
    out = []
    for it in lst:
        if not isinstance(it, dict):
            continue
        title = _first(it, TITLE_KEYS)
        if not title:
            continue
        out.append(
            {
                "title": title.rstrip("。.！!　 "),
                "summary": re.sub(r"<[^>]+>", "", _first(it, SUMMARY_KEYS)),
                "channel": _clean_channel(_first(it, CHANNEL_KEYS)),
                "url": _first(it, URL_KEYS),
                "entry_id": it.get("entry_id") or "",
                "entry_type": it.get("entry_type") or 7,
            }
        )
    return out


def parse_daily_generic(root):
    sections = []

    def walk(node, label=""):
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
                    if k in SECTION_LIST_KEYS:
                        for sec in v:
                            name = _first(sec, SECTION_NAME_KEYS) or label or "未命名版块"
                            walk(sec, name)
                        continue
                    if k in ITEM_LIST_KEYS:
                        items = _parse_items_generic(v)
                        if items:
                            sections.append(
                                {
                                    "name": label or _first(node, SECTION_NAME_KEYS) or "内容",
                                    "items": items,
                                }
                            )
                            continue
                    for x in v:
                        walk(x, label)
                elif isinstance(v, dict):
                    walk(v, _first(v, SECTION_NAME_KEYS) or label)
        elif isinstance(node, list):
            for x in node:
                walk(x, label)

    walk(root)

    merged, order = {}, []
    for sec in sections:
        if sec["name"] not in merged:
            merged[sec["name"]] = list(sec["items"])
            order.append(sec["name"])
        else:
            have = {i["title"] for i in merged[sec["name"]]}
            merged[sec["name"]] += [i for i in sec["items"] if i["title"] not in have]
    return [{"name": n, "items": merged[n]} for n in order]


# ---------------------------------------------------------------- 渲染


def render_markdown(date_str, meta, sections):
    out = [f"# 语鲸个人专属日报 {meta.get('date') or date_str}", ""]
    if meta.get("sub_cnt") and meta.get("update_cnt"):
        out += [f"订阅 {meta['sub_cnt']} 个频道，昨日更新 {meta['update_cnt']} 篇。", ""]
    if meta.get("mix"):
        mix = "　".join(f"{t} {p}%" for t, p in meta["mix"] if p)
        out += [f"<sub>{mix}</sub>", ""]
    if not sections:
        out.append("解析不出内容，请查看同目录下的 .json 原始返回。")
    for sec in sections:
        out.append(f"## {sec['name']}（{len(sec['items'])}）")
        out.append("")
        for it in sec["items"]:
            head = f"[{it['title']}]({it['url']})" if it["url"] else it["title"]
            out.append(f"**{head}**")
            if it["channel"]:
                out.append(f"<sub>{it['channel']}</sub>")
            if it["summary"]:
                out += ["", it["summary"]]
            out.append("")
    out.append("---")
    out.append("<sub>由语鲸生成，本模块仅做抓取归档。</sub>")
    return "\n".join(out)


def render_html(date_str, meta, sections):
    out = [f"<h2>语鲸个人专属日报 {escape(meta.get('date') or date_str)}</h2>"]
    if meta.get("sub_cnt") and meta.get("update_cnt"):
        out.append(
            f"<p>订阅 {meta['sub_cnt']} 个频道，昨日更新 {meta['update_cnt']} 篇。</p>"
        )
    if meta.get("mix"):
        mix = "　".join(f"{escape(t)} {p}%" for t, p in meta["mix"] if p)
        out.append(f"<p><small>{mix}</small></p>")
    if not sections:
        out.append("<p>解析不出内容，请查看归档的 .json 原始返回。</p>")
    for sec in sections:
        out.append(f"<h3>{escape(sec['name'])}（{len(sec['items'])}）</h3><ul>")
        for it in sec["items"]:
            t = escape(it["title"])
            link = f'<a href="{escape(it["url"])}">{t}</a>' if it["url"] else t
            out.append(f"<li>{link}")
            if it["channel"]:
                out.append(f" <small>{escape(it['channel'])}</small>")
            if it["summary"]:
                out.append(f"<p>{escape(it['summary'])}</p>")
            out.append("</li>")
        out.append("</ul>")
    out.append("<hr><small>由语鲸生成，本模块仅做抓取归档。</small>")
    return "".join(out)


# ---------------------------------------------------------------- 主流程


def generate(
    cache_path="./data/lw_cache.json",
    out_dir="./data/feeds",
    reports_dir="./data/reports",
    base_url="",
    slug="lingowhale-daily",
    keep=30,
    push=True,
    dry_run=False,
    dump=False,
    daily_id="",
    tz_offset=8,
):
    tz = timezone(timedelta(hours=tz_offset))
    now = datetime.now(tz)
    date_str = now.strftime("%Y-%m-%d")

    data_dir = os.path.dirname(os.path.abspath(cache_path))
    state_path = os.path.join(data_dir, "lw_daily_state.json")
    state = lw2r.load_json(state_path, {})

    headers = lw2r.build_headers()

    if not daily_id:
        daily_id, list_ep = find_daily_id(headers, state)
        if list_ep and state.get("list_endpoint") != list_ep:
            state["list_endpoint"] = list_ep  # 记住命中的接口，下次不用再探
    if not daily_id:
        print(
            "[语鲸日报] 拿不到 daily_id。可从浏览器地址栏复制一个，"
            "用 --daily-id 或环境变量 LW_DAILY_ID 指定",
            file=sys.stderr,
        )
        return None

    # 同一期不重复处理(语鲸当天没出新日报时，daily_id 不变)
    if not dry_run and state.get("last_daily_id") == daily_id:
        print(f"[语鲸日报] daily_id={daily_id} 上次已抓过，跳过", file=sys.stderr)
        return None

    data = fetch_daily(headers, daily_id)

    if dump:
        print(json.dumps(data, ensure_ascii=False, indent=2)[:8000])

    meta = extract_meta(data, tz)
    sections = parse_daily(data)

    # 补原文链接: 先查抓取模块的缓存(零额外请求)，缺的再回源
    cache = lw2r.load_json(cache_path)
    resolve_links(sections, cache, headers=headers)

    total = sum(len(s["items"]) for s in sections)
    print(
        f"[语鲸日报] daily_id={daily_id} 解析出 {len(sections)} 个版块 / {total} 条",
        file=sys.stderr,
    )

    md = render_markdown(date_str, meta, sections)
    html = render_html(date_str, meta, sections)

    if dry_run:
        print(md)
        return md

    os.makedirs(reports_dir, exist_ok=True)
    # 原始返回一定要存: 解析器是猜字段名写的，出问题时这是唯一的翻案依据
    with open(
        os.path.join(reports_dir, f"lingowhale-daily-{date_str}.json"), "w", encoding="utf-8"
    ) as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    with open(
        os.path.join(reports_dir, f"lingowhale-daily-{date_str}.md"), "w", encoding="utf-8"
    ) as f:
        f.write(md)

    hist_path = os.path.join(data_dir, "lw_daily_history.json")
    hist = lw2r.load_json(hist_path, [])
    if not isinstance(hist, list):
        hist = []
    hist = [h for h in hist if h.get("entry_id") != f"lwdaily-{daily_id}"]
    hist.insert(
        0,
        {
            "entry_id": f"lwdaily-{daily_id}",
            "title": f"语鲸日报 {meta.get('date') or date_str}（{total} 条）",
            "pub_time": time.time(),
            "html": html,
            "channel": "语鲸日报",
            "author": "lingowhale",
            "orig_url": f"https://lingowhale.com/daily-report?daily_id={daily_id}",
        },
    )
    hist = hist[:keep]
    lw2r.save_json(hist_path, hist)

    os.makedirs(out_dir, exist_ok=True)
    self_url = f"{base_url.rstrip('/')}/{slug}.atom" if base_url else ""
    with open(os.path.join(out_dir, f"{slug}.atom"), "w", encoding="utf-8") as f:
        f.write(lw2r.build_atom("语鲸个人专属日报", self_url, hist))

    state["last_daily_id"] = daily_id
    state["last_run"] = int(time.time())
    lw2r.save_json(state_path, state)

    if push:
        body = md if len(md) < 30000 else md[:30000] + "\n\n…（内容过长已截断）"
        lw2r.send_wechat_notify(f"语鲸日报 {meta.get('date') or date_str}（{total} 条）", body)

    print(f"[语鲸日报] 完成 -> {slug}.atom", file=sys.stderr)
    return md


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default="./data/lw_cache.json", help="仅用于定位 data 目录")
    ap.add_argument("--out", default="./data/feeds")
    ap.add_argument("--reports-dir", default="./data/reports")
    ap.add_argument("--base-url", default="")
    ap.add_argument("--slug", default="lingowhale-daily")
    ap.add_argument("--daily-id", default="", help="手动指定某一期")
    ap.add_argument("--no-push", dest="push", action="store_false")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不落盘不推送")
    ap.add_argument("--dump", action="store_true", help="打印接口返回的原始 JSON")
    args = ap.parse_args()

    lw2r.load_env_file()
    generate(
        cache_path=args.cache,
        out_dir=args.out,
        reports_dir=args.reports_dir,
        base_url=args.base_url,
        slug=args.slug,
        daily_id=args.daily_id,
        push=args.push,
        dry_run=args.dry_run,
        dump=args.dump,
    )


if __name__ == "__main__":
    main()
