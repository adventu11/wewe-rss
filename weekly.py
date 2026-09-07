#!/usr/bin/env python3
"""
weekly.py —— 每周新药获批汇总。

和日报(report.py)的关键差别:
  日报的单位是"文章"，一篇文章 = 报告里一条。
  周报的单位是"药品"，同一个药获批，药明康德/医药魔方/新浪医药都会写，
  按文章列会出现五六条重复。所以这里多了一步【按药品名合并去重】，
  一个药一行，来源列出所有报道过它的公众号。

三阶段:
  阶段一 粗筛: 标题+摘要判断"这篇是不是讲新药获批的"，一次判 20 条
  阶段二 抽取: 只对通过的文章取全文，抽成结构化记录
              (药名/企业/适应症/获批类型/日期)。一篇文章可能含多个药——
              比如"本周获批盘点"类文章，所以返回的是数组不是单条。
  阶段三 合并: 按通用名归一化后合并，来源累加。这步是纯 Python，
              不调模型——合并规则确定性的东西不该交给模型去猜。

产出:
  data/reports/weekly-YYYY-MM-DD.md      归档
  data/feeds/<WEEKLY_SLUG>.atom          Miniflux 订阅
  Server酱微信推送

单独跑:
    cd /app && python3 weekly.py --dry-run --cache /app/data/lw_cache.json
"""

import argparse
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape

import lingowhale2rss as lw2r
import report as rpt

# 表格列顺序
COLUMNS = ["药品名称", "企业", "适应症", "获批类型", "获批日期"]

SCREEN_PROMPT = """你在帮一位临床试验项目经理整理【每周新药获批汇总】。

现在判断每篇文章是否包含"药品获批"的实质信息。

【保留】
- 新药上市获批（NMPA/FDA/EMA 等任何监管机构）
- 已上市药品新增适应症获批
- 仿制药/生物类似药获批、通过一致性评价
- 附条件批准、优先审评、突破性疗法认定后的获批
- 多个获批消息的盘点/周报类文章

【过滤】
- 仅"递交上市申请""获受理""进入临床""IND 获批"——还没获批的一律不要
- 临床试验结果、数据读出、学术会议内容
- 融资、并购、股价、人事变动
- 政策法规解读（那是日报管的，不是本周报范围）
- 只提到药品名但没有获批事件的科普文章"""

EXTRACT_PROMPT = """你在从文章里抽取【药品获批】的结构化信息。

只输出 JSON 数组，不要解释、不要 markdown 代码块。
一篇文章可能包含多个药品获批（如盘点类文章），全部抽出来。
文章里没有任何获批信息就返回空数组 []。

每个元素:
{"药品名称": "通用名（有商品名就写成 通用名(商品名)）",
 "企业": "上市许可持有人/申报企业，写中文全称或通用简称",
 "适应症": "获批的适应症，30字以内，写具体病种不要写'某某领域'",
 "获批类型": "新药上市|新增适应症|仿制药|生物类似物|一致性评价|其他",
 "获批日期": "YYYY-MM-DD，原文只写到月就写 YYYY-MM，没写就留空字符串",
 "监管机构": "NMPA|FDA|EMA|PMDA|其他"}

严格要求:
- 只抽"已经获批"的，"递交申请""获受理""即将获批"一律不抽
- 字段拿不准就留空字符串，不要编造。企业和适应症尤其不要猜
- 药品名称必须填，没有明确药名的记录直接不要"""


def normalize_drug(name):
    """
    药名归一化，用于跨文章合并同一个药。

    各家写法差异很大: "泽布替尼胶囊"/"泽布替尼(百悦泽)"/"BTK抑制剂泽布替尼"，
    这里把剂型、商品名括号、注册符号、空格都剥掉，只留核心名做匹配键。
    宁可合并失败(多一行)也不要误合并(把两个药并成一个)，所以规则保守。
    """
    s = (name or "").strip()
    if not s:
        return ""
    s = re.sub(r"[®™©]", "", s)
    # 去掉括号及其内容(通常是商品名或英文名)
    s = re.sub(r"[（(\[【][^）)\]】]*[）)\]】]", "", s)
    # 去掉常见剂型后缀
    s = re.sub(
        r"(注射液|注射用|片剂|胶囊剂|胶囊|片|颗粒|干混悬剂|混悬液|滴眼液|软膏|凝胶|"
        r"喷雾剂|吸入剂|贴剂|口服液|散剂|冻干粉针|粉针剂)$",
        "",
        s,
    )
    s = re.sub(r"\s+", "", s)
    return s.lower()


def screen_approvals(items, batch_size=20, max_tokens=6000, audit=None):
    """粗筛: 挑出讲获批的文章。返回通过的 entry_id 集合。"""
    kept = set()
    sys_msg = (
        SCREEN_PROMPT
        + "\n\n只输出 JSON 数组，不要解释、不要 markdown 代码块。"
        + '\n每个元素: {"i": 序号, "keep": true/false, "reason": "12字以内理由"}'
    )
    for start in range(0, len(items), batch_size):
        chunk = items[start : start + batch_size]
        lines = [
            f"{i}. 【{it.get('channel') or '未知'}】{it.get('title') or ''}"
            + (f" —— {(it.get('abstract') or '')[:120]}" if it.get("abstract") else "")
            for i, it in enumerate(chunk)
        ]
        try:
            out = rpt.llm_chat(
                [
                    {"role": "system", "content": sys_msg},
                    {"role": "user", "content": "\n".join(lines)},
                ],
                max_tokens=max_tokens,
            )
            arr = rpt.parse_json_loose(out)
        except Exception as e:  # noqa: BLE001
            print(f"  [!] 粗筛失败(第 {start // batch_size + 1} 批): {e}", file=sys.stderr)
            if audit is not None:
                for it in chunk:
                    audit.append({**_audit_row(it), "keep": None, "reason": "本批粗筛调用失败"})
            continue

        by_idx = {}
        for r in arr if isinstance(arr, list) else []:
            try:
                by_idx[int(r.get("i"))] = r
            except (TypeError, ValueError):
                continue
        for i, it in enumerate(chunk):
            r = by_idx.get(i)
            if r is None:
                if audit is not None:
                    audit.append({**_audit_row(it), "keep": None, "reason": "模型未返回判定"})
                continue
            keep = bool(r.get("keep"))
            if audit is not None:
                audit.append(
                    {**_audit_row(it), "keep": keep, "reason": (r.get("reason") or "")[:40]}
                )
            if keep:
                kept.add(it["entry_id"])
        print(
            f"  粗筛 {start + 1}-{start + len(chunk)}: 通过 "
            f"{sum(1 for it in chunk if it['entry_id'] in kept)} 条",
            file=sys.stderr,
        )
        time.sleep(1)
    return kept


def _audit_row(it):
    return {
        "entry_id": it["entry_id"],
        "title": it.get("title") or "",
        "channel": it.get("channel") or "",
    }


def extract_approvals(items, batch_size=2, max_tokens=5000):
    """
    抽取: 从全文里抠出结构化获批记录。

    batch_size 默认比日报精读还小(2)，因为这里要求模型输出结构化多字段、
    一篇还可能出多条记录，输出体量比日报大，批大了极易被截断。
    """
    records = []
    for start in range(0, len(items), batch_size):
        chunk = items[start : start + batch_size]
        blocks = [
            f"### id: {it['entry_id']}\n"
            f"标题: {it.get('title') or ''}\n"
            f"来源公众号: {it.get('channel') or ''}\n"
            f"正文:\n{it.get('_text') or it.get('abstract') or ''}"
            for it in chunk
        ]
        try:
            res = rpt.llm_chat(
                [
                    {"role": "system", "content": EXTRACT_PROMPT},
                    {"role": "user", "content": "\n\n".join(blocks)},
                ],
                max_tokens=max_tokens,
            )
            arr = rpt.parse_json_loose(res)
        except Exception as e:  # noqa: BLE001
            print(f"  [!] 抽取失败(第 {start // batch_size + 1} 批): {e}", file=sys.stderr)
            arr = []

        # 模型可能不回 id，这批只有 1-2 篇，来源按整批归属即可
        src = {
            "channels": [it.get("channel") or "" for it in chunk],
            "urls": {(it.get("channel") or ""): it.get("orig_url") or "" for it in chunk},
        }
        for r in arr if isinstance(arr, list) else []:
            if not isinstance(r, dict):
                continue
            name = (r.get("药品名称") or "").strip()
            if not name or not normalize_drug(name):
                continue
            records.append(
                {
                    "药品名称": name,
                    "企业": (r.get("企业") or "").strip(),
                    "适应症": (r.get("适应症") or "").strip(),
                    "获批类型": (r.get("获批类型") or "").strip(),
                    "获批日期": (r.get("获批日期") or "").strip(),
                    "监管机构": (r.get("监管机构") or "").strip(),
                    "_sources": src["channels"],
                    "_urls": src["urls"],
                }
            )
        print(f"  抽取 {start + 1}-{start + len(chunk)}: 累计 {len(records)} 条记录", file=sys.stderr)
        time.sleep(1)
    return records


def merge_records(records):
    """
    按归一化药名合并同一个药的多篇报道。

    合并策略: 先到的记录占主，空字段用后来的补齐(不覆盖已有值)——
    因为不同公众号写的详略不同，取并集信息最全。来源累加去重。
    """
    merged = {}
    order = []
    for r in records:
        key = normalize_drug(r["药品名称"])
        if key not in merged:
            r = dict(r)
            r["_sources"] = list(dict.fromkeys(r["_sources"]))
            merged[key] = r
            order.append(key)
            continue
        cur = merged[key]
        for col in ("企业", "适应症", "获批类型", "获批日期", "监管机构"):
            if not cur.get(col) and r.get(col):
                cur[col] = r[col]
        # 药名取更长的那个(通常带商品名，信息更全)
        if len(r["药品名称"]) > len(cur["药品名称"]):
            cur["药品名称"] = r["药品名称"]
        cur["_sources"] = list(dict.fromkeys(cur["_sources"] + r["_sources"]))
        cur["_urls"] = {**r.get("_urls", {}), **cur.get("_urls", {})}
    out = [merged[k] for k in order]
    # 按获批类型排序，新药上市排最前
    rank = {"新药上市": 0, "新增适应症": 1, "生物类似物": 2, "仿制药": 3, "一致性评价": 4}
    out.sort(key=lambda x: rank.get(x.get("获批类型"), 5))
    return out


def render_markdown(period, rows, stats):
    out = [f"# 新药获批周报 {period}", ""]
    if not rows:
        out.append("本周没有筛出新药获批信息。")
    else:
        out.append(f"本周共 **{len(rows)}** 项获批。")
        out.append("")
        out.append("| " + " | ".join(COLUMNS) + " | 来源 |")
        out.append("|" + "---|" * (len(COLUMNS) + 1))
        for r in rows:
            src = "、".join(r["_sources"][:3]) or "—"
            cells = [(r.get(c) or "—").replace("|", "/") for c in COLUMNS]
            out.append("| " + " | ".join(cells) + f" | {src} |")
        out.append("")
        out.append("## 明细")
        out.append("")
        for r in rows:
            機 = r.get("监管机构") or ""
            head = f"**{r['药品名称']}**"
            if 機:
                head += f"（{機}）"
            out.append(head)
            out.append(
                f"- 企业：{r.get('企业') or '原文未提'}　"
                f"适应症：{r.get('适应症') or '原文未提'}"
            )
            out.append(
                f"- 类型：{r.get('获批类型') or '未分类'}　"
                f"日期：{r.get('获批日期') or '原文未提'}"
            )
            links = [
                f"[{ch}]({url})" for ch, url in (r.get("_urls") or {}).items() if url and ch
            ]
            if links:
                out.append("- 来源：" + "　".join(links[:3]))
            out.append("")
    out.append("---")
    out.append(
        f"<sub>候选 {stats['total']} 篇 → 获批相关 {stats['screened']} 篇 → "
        f"合并后 {len(rows)} 项，由 {rpt.LLM_MODEL} 生成，以官方公告为准。</sub>"
    )
    return "\n".join(out)


def render_html(period, rows, stats):
    out = [f"<h2>新药获批周报 {period}</h2>"]
    if not rows:
        out.append("<p>本周没有筛出新药获批信息。</p>")
    else:
        out.append(f"<p>本周共 <b>{len(rows)}</b> 项获批。</p>")
        out.append('<table border="1" cellpadding="6" cellspacing="0">')
        out.append("<tr>" + "".join(f"<th>{escape(c)}</th>" for c in COLUMNS) + "<th>来源</th></tr>")
        for r in rows:
            tds = "".join(f"<td>{escape(r.get(c) or '—')}</td>" for c in COLUMNS)
            links = [
                f'<a href="{escape(u)}">{escape(ch)}</a>'
                for ch, u in (r.get("_urls") or {}).items()
                if u and ch
            ]
            out.append(f"<tr>{tds}<td>{'、'.join(links[:3]) or '—'}</td></tr>")
        out.append("</table>")
    out.append(
        f"<hr><small>候选 {stats['total']} 篇 → 获批相关 {stats['screened']} 篇 → "
        f"合并后 {len(rows)} 项，由 {escape(rpt.LLM_MODEL)} 生成，以官方公告为准。</small>"
    )
    return "".join(out)


def generate(
    cache_path="./data/lw_cache.json",
    out_dir="./data/feeds",
    reports_dir="./data/reports",
    base_url="",
    window_hours=168,  # 7 天
    max_articles=40,
    max_chars=3000,
    screen_batch=20,
    screen_max_tokens=6000,
    extract_batch=2,
    extract_max_tokens=5000,
    weekly_slug="weekly-approvals",
    weekly_keep=26,  # 半年
    push=True,
    dry_run=False,
    tz_offset=8,
):
    tz = timezone(timedelta(hours=tz_offset))
    now = datetime.now(tz)
    date_str = now.strftime("%Y-%m-%d")
    period = f"{(now - timedelta(hours=window_hours)).strftime('%m-%d')} ~ {now.strftime('%m-%d')}"

    data_dir = os.path.dirname(os.path.abspath(cache_path))
    cache = lw2r.load_json(cache_path)
    if not cache:
        print("[周报] 缓存为空，跳过", file=sys.stderr)
        return None

    # 周报不看日报的 reported 账本——两者目的不同，日报报过不代表周报不该收录。
    # 去重靠的是"按药品名合并"，不是"按文章去重"。
    since = time.time() - window_hours * 3600
    cands = [
        v
        for v in cache.values()
        if v.get("orig_url") and (v.get("pub_time") or 0) >= since
    ]
    cands.sort(key=lambda x: -(x.get("pub_time") or 0))
    print(f"[周报] 候选 {len(cands)} 篇（最近 {window_hours} 小时）", file=sys.stderr)
    if not cands:
        return None

    audit = []
    kept_ids = screen_approvals(
        cands, batch_size=screen_batch, max_tokens=screen_max_tokens, audit=audit
    )
    kept = [it for it in cands if it["entry_id"] in kept_ids]
    if len(kept) > max_articles:
        print(f"[周报] 获批相关 {len(kept)} 篇，截到 {max_articles} 篇", file=sys.stderr)
        kept = kept[:max_articles]
    print(f"[周报] 获批相关 {len(kept)} 篇，开始抽取", file=sys.stderr)

    if not kept:
        rows, stats = [], {"total": len(cands), "screened": 0}
    else:
        headers = None
        if any(not it.get("html") for it in kept):
            try:
                headers = lw2r.build_headers()
            except SystemExit:
                print("[周报] 无语鲸令牌，退化为用摘要抽取", file=sys.stderr)
        for it in kept:
            it["_text"] = rpt.get_article_text(headers, it, max_chars)

        records = extract_approvals(
            kept, batch_size=extract_batch, max_tokens=extract_max_tokens
        )
        rows = merge_records(records)
        print(f"[周报] 抽取 {len(records)} 条 → 合并后 {len(rows)} 项", file=sys.stderr)
        stats = {"total": len(cands), "screened": len(kept)}

    md = render_markdown(period, rows, stats)
    html = render_html(period, rows, stats)

    if dry_run:
        print(md)
        return md

    os.makedirs(reports_dir, exist_ok=True)
    with open(
        os.path.join(reports_dir, f"weekly-{date_str}.md"), "w", encoding="utf-8"
    ) as f:
        f.write(md)
    with open(
        os.path.join(reports_dir, f"weekly-{date_str}-audit.md"), "w", encoding="utf-8"
    ) as f:
        f.write(rpt.render_audit(f"周报粗筛 {period}", audit, len(cands)))

    hist_path = os.path.join(data_dir, "lw_weekly_history.json")
    hist = lw2r.load_json(hist_path, [])
    if not isinstance(hist, list):
        hist = []
    hist = [h for h in hist if h.get("date") != date_str]
    hist.insert(
        0,
        {
            "date": date_str,
            "entry_id": f"weekly-{date_str}",
            "title": f"新药获批周报 {period}（{len(rows)} 项）",
            "pub_time": time.time(),
            "html": html,
            "channel": "AI 周报",
            "author": "lingowhale2rss",
            "orig_url": f"{base_url.rstrip('/')}/reports/weekly-{date_str}.md"
            if base_url
            else "",
        },
    )
    hist = hist[:weekly_keep]
    lw2r.save_json(hist_path, hist)

    os.makedirs(out_dir, exist_ok=True)
    self_url = f"{base_url.rstrip('/')}/{weekly_slug}.atom" if base_url else ""
    xml = lw2r.build_atom("新药获批周报", self_url, hist)
    with open(os.path.join(out_dir, f"{weekly_slug}.atom"), "w", encoding="utf-8") as f:
        f.write(xml)

    if push:
        body = md if len(md) < 30000 else md[:30000] + "\n\n…（内容过长已截断）"
        lw2r.send_wechat_notify(f"新药获批周报 {period}（{len(rows)} 项）", body)

    print(f"[周报] 完成: {len(rows)} 项 -> {weekly_slug}.atom", file=sys.stderr)
    return md


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default="./data/lw_cache.json")
    ap.add_argument("--out", default="./data/feeds")
    ap.add_argument("--reports-dir", default="./data/reports")
    ap.add_argument("--base-url", default="")
    ap.add_argument("--window-hours", type=int, default=168, help="回看窗口，默认 7 天")
    ap.add_argument("--max-articles", type=int, default=40)
    ap.add_argument("--max-chars", type=int, default=3000)
    ap.add_argument("--screen-batch", type=int, default=20)
    ap.add_argument("--screen-max-tokens", type=int, default=6000)
    ap.add_argument("--extract-batch", type=int, default=2, help="抽取每批文章数")
    ap.add_argument("--extract-max-tokens", type=int, default=5000)
    ap.add_argument("--weekly-slug", default="weekly-approvals")
    ap.add_argument("--no-push", dest="push", action="store_false")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    lw2r.load_env_file()
    generate(
        cache_path=args.cache,
        out_dir=args.out,
        reports_dir=args.reports_dir,
        base_url=args.base_url,
        window_hours=args.window_hours,
        max_articles=args.max_articles,
        max_chars=args.max_chars,
        screen_batch=args.screen_batch,
        screen_max_tokens=args.screen_max_tokens,
        extract_batch=args.extract_batch,
        extract_max_tokens=args.extract_max_tokens,
        weekly_slug=args.weekly_slug,
        push=args.push,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
