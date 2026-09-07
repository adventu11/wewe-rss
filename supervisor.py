#!/usr/bin/env python3
"""
supervisor —— 在容器里同时做两件事：
  1. 按固定时间点（默认 7/10/13/16/19/22 点，Asia/Shanghai）跑一次 lingowhale2rss
  2. 用内置 HTTP 服务器把生成的 .atom 文件暴露出去，给 Miniflux 订阅

只用标准库，不依赖系统 cron，避免 Alpine 装 cron 包和时区配置的额外坑。

本版改动:
  - 默认抓取频率从 3 次/天提到 6 次/天。配合 feed 现在从缓存出，
    夜里 22:00→次日 07:00 的空档不会再丢文章，但缩短窗口能让新文章更快进
    Miniflux，也让单轮的请求量更平均。
  - 新增 LW_PER_CHANNEL / LW_FEED_MAX / LW_ENTRY_TYPES 三个环境变量，
    LW_MAX_ITEMS 已废弃（它是整组共享配额，正是漏文章的根源）。
  - 分组自动同步。LW_AUTO_GROUPS=0 退回代码内置分组；
    LW_INCLUDE_UNGROUPED=0 不给未分组的号出 feed；
    LW_PRUNE_FEEDS=0 保留分组改名后遗留的旧 .atom。
"""

import base64
import gc
import http.server
import hmac
import os
import socketserver
import threading
import time
from datetime import datetime, timedelta

import lingowhale2rss as lw2r

# 日报模块是可选的。缺文件、或者 report.py 自身有语法错误时，
# 这里必须降级而不是抛异常——supervisor 挂掉会连 HTTP 服务和抓取一起带走，
# Miniflux 那边直接断粮，代价远大于"今天没有日报"。
try:
    import report as rpt
except Exception as _e:  # noqa: BLE001
    rpt = None
    print(f"[!] 日报模块不可用({_e})，本次仅抓取，不生成日报", flush=True)

try:
    import weekly as wkl
except Exception as _e:  # noqa: BLE001
    wkl = None
    print(f"[!] 周报模块不可用({_e})，不生成周报", flush=True)

try:
    import lw_daily as lwd
except Exception as _e:  # noqa: BLE001
    lwd = None
    print(f"[!] 语鲸日报模块不可用({_e})，不抓取语鲸日报", flush=True)

try:
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # noqa: BLE001
    TZ = None
    print("[!] zoneinfo 不可用，退回本地时区，请确认容器 TZ 设置正确", flush=True)

def parse_times(spec, default):
    """
    解析 "7,10,13:30,19" 这样的时间点列表，返回 [(hour, minute), ...]。

    支持纯小时("7")和"时:分"("7:50")两种写法混用。任何一项解析失败都整体
    退回 default，而不是让异常冒到模块顶层——这里一崩，容器就进重启循环
    （之前 LW_RUN_HOURS 就出过这个问题），一个环境变量填错不该有这个权力。
    """
    try:
        out = []
        for part in spec.split(","):
            part = part.strip()
            if not part:
                continue
            if ":" in part:
                h, m = part.split(":", 1)
                h, m = int(h), int(m)
            else:
                h, m = int(part), 0
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError(f"超出范围: {part}")
            out.append((h, m))
        return out
    except Exception as e:  # noqa: BLE001
        print(f"[!] 时间点解析失败 {spec!r}({e})，退回默认值 {default!r}", flush=True)
        return parse_times(default, default) if spec != default else []


RUN_HOURS = parse_times(os.environ.get("LW_RUN_HOURS", "7,10,13,16,19,22"), "7,10,13,16,19,22")
# 日报时间点。留空则不生成日报。默认 8 点——排在 7 点抓取之后，隔一小时够跑完
REPORT_HOURS = parse_times(os.environ.get("LW_REPORT_HOURS", "8"), "8")
# 周报时间点。LW_WEEKLY_DAY: 0=周一 ... 6=周日，默认周一 9:00。
# 留空 LW_WEEKLY_HOURS 则不生成周报。
WEEKLY_HOURS = parse_times(os.environ.get("LW_WEEKLY_HOURS", "9"), "9")
# 抓取语鲸自己生成的日报。语鲸通常清晨出前一日日报，默认 9:30 抓
LWDAILY_HOURS = parse_times(os.environ.get("LW_LWDAILY_HOURS", "9:30"), "9:30")
try:
    WEEKLY_DAY = int(os.environ.get("LW_WEEKLY_DAY", "0"))
    if not 0 <= WEEKLY_DAY <= 6:
        raise ValueError(WEEKLY_DAY)
except Exception:  # noqa: BLE001
    print("[!] LW_WEEKLY_DAY 无效，退回 0（周一）", flush=True)
    WEEKLY_DAY = 0
DATA_DIR = os.environ.get("LW_DATA_DIR", "/app/data")
FEEDS_DIR = os.path.join(DATA_DIR, "feeds")
CACHE_PATH = os.path.join(DATA_DIR, "lw_cache.json")
REPORTS_DIR = os.path.join(DATA_DIR, "reports")
PORT = int(os.environ.get("PORT", 8080))


def now():
    return datetime.now(TZ) if TZ else datetime.now()


AUTH_USER = os.environ.get("LW_HTTP_USER", "")
AUTH_PASS = os.environ.get("LW_HTTP_PASS", "")


def check_auth(header_value):
    """校验 Authorization: Basic xxx，使用 compare_digest 防时序攻击"""
    if not header_value or not header_value.startswith("Basic "):
        return False
    try:
        raw = base64.b64decode(header_value[6:]).decode("utf-8")
        user, _, pw = raw.partition(":")
    except Exception:  # noqa: BLE001
        return False
    return hmac.compare_digest(user, AUTH_USER) and hmac.compare_digest(pw, AUTH_PASS)


def serve():
    """在 FEEDS_DIR 上起一个只读静态文件服务，可选 HTTP Basic Auth"""
    os.makedirs(FEEDS_DIR, exist_ok=True)
    auth_enabled = bool(AUTH_USER and AUTH_PASS)
    if auth_enabled:
        print("HTTP Basic Auth 已启用", flush=True)
    else:
        print("[!] 未设置 LW_HTTP_USER/LW_HTTP_PASS，域名当前公开无密码", flush=True)

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=FEEDS_DIR, **kw)

        def log_message(self, fmt, *args):
            print(f"[http] {self.address_string()} {fmt % args}", flush=True)

        def do_GET(self):
            if self.path == "/healthz":
                body = b"ok"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if auth_enabled and not check_auth(self.headers.get("Authorization")):
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="lingowhale-feeds"')
                self.end_headers()
                return
            super().do_GET()

    with socketserver.ThreadingTCPServer(("0.0.0.0", PORT), Handler) as httpd:
        print(f"HTTP 服务已启动: 0.0.0.0:{PORT} -> {FEEDS_DIR}", flush=True)
        httpd.serve_forever()


def run_job():
    print(f"[{now()}] 开始抓取", flush=True)
    if os.environ.get("LW_MAX_ITEMS"):
        print(
            "[!] LW_MAX_ITEMS 已废弃(整组共享配额是漏文章的根源)，"
            "请改用 LW_PER_CHANNEL / LW_FEED_MAX",
            flush=True,
        )
    try:
        entry_types = {
            int(x)
            for x in os.environ.get("LW_ENTRY_TYPES", "7").split(",")
            if x.strip()
        }
        lw2r.run(
            out=FEEDS_DIR,
            base_url=os.environ.get("LW_BASE_URL", ""),
            per_channel=int(os.environ.get("LW_PER_CHANNEL", "10")),
            feed_max=int(os.environ.get("LW_FEED_MAX", "120")),
            limit=int(os.environ.get("LW_LIMIT", "10")),
            delay=float(os.environ.get("LW_DELAY", "1.0")),
            no_content=os.environ.get("LW_NO_CONTENT") == "1",
            link_only=os.environ.get("LW_LINK_ONLY") == "1",
            cache_path=CACHE_PATH,
            cache_max_age_days=int(os.environ.get("LW_CACHE_MAX_AGE_DAYS", "14")),
            notify_days=int(os.environ.get("LW_NOTIFY_DAYS", "1")),
            entry_types=entry_types,
            auto_groups=os.environ.get("LW_AUTO_GROUPS", "1") != "0",
            include_ungrouped=os.environ.get("LW_INCLUDE_UNGROUPED", "1") != "0",
            prune_feeds=os.environ.get("LW_PRUNE_FEEDS", "1") != "0",
        )
        print(f"[{now()}] 抓取完成", flush=True)
    except SystemExit as e:
        # build_headers()/check_token_expiry() 配置有误时用 sys.exit() 报错。
        # 现在跟 supervisor 同一个进程，必须拦下来，否则会连 HTTP 服务一起被杀掉。
        print(f"[{now()}] 抓取因配置问题中止: {e}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[{now()}] 抓取异常: {e}", flush=True)
    gc.collect()


def report_job():
    """生成 AI 日报。失败只记日志，绝不能影响抓取和 HTTP 服务。"""
    if rpt is None:
        print(f"[{now()}] 日报模块未加载，跳过", flush=True)
        return
    if not os.environ.get("LLM_API_KEY"):
        print(f"[{now()}] 未设置 LLM_API_KEY，跳过日报", flush=True)
        return
    print(f"[{now()}] 开始生成日报", flush=True)
    try:
        rpt.generate(
            cache_path=CACHE_PATH,
            out_dir=FEEDS_DIR,
            reports_dir=REPORTS_DIR,
            base_url=os.environ.get("LW_BASE_URL", ""),
            window_hours=int(os.environ.get("LW_REPORT_WINDOW_HOURS", "26")),
            max_articles=int(os.environ.get("LW_REPORT_MAX_ARTICLES", "25")),
            max_chars=int(os.environ.get("LW_REPORT_MAX_CHARS", "3000")),
            screen_batch=int(os.environ.get("LW_REPORT_SCREEN_BATCH", "20")),
            screen_max_tokens=int(os.environ.get("LW_REPORT_SCREEN_MAX_TOKENS", "6000")),
            digest_batch=int(os.environ.get("LW_REPORT_DIGEST_BATCH", "3")),
            digest_max_tokens=int(os.environ.get("LW_REPORT_DIGEST_MAX_TOKENS", "5000")),
            report_slug=os.environ.get("LW_REPORT_SLUG", "daily-report"),
            push=os.environ.get("LW_REPORT_PUSH", "1") != "0",
        )
        print(f"[{now()}] 日报完成", flush=True)
    except SystemExit as e:
        print(f"[{now()}] 日报因配置问题中止: {e}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[{now()}] 日报异常: {e}", flush=True)
    gc.collect()


def weekly_job():
    """生成新药获批周报。失败只记日志，不影响其他任务。"""
    if wkl is None:
        print(f"[{now()}] 周报模块未加载，跳过", flush=True)
        return
    if not os.environ.get("LLM_API_KEY"):
        print(f"[{now()}] 未设置 LLM_API_KEY，跳过周报", flush=True)
        return
    print(f"[{now()}] 开始生成周报", flush=True)
    try:
        wkl.generate(
            cache_path=CACHE_PATH,
            out_dir=FEEDS_DIR,
            reports_dir=REPORTS_DIR,
            base_url=os.environ.get("LW_BASE_URL", ""),
            window_hours=int(os.environ.get("LW_WEEKLY_WINDOW_HOURS", "168")),
            max_articles=int(os.environ.get("LW_WEEKLY_MAX_ARTICLES", "40")),
            max_chars=int(os.environ.get("LW_WEEKLY_MAX_CHARS", "3000")),
            screen_batch=int(os.environ.get("LW_WEEKLY_SCREEN_BATCH", "20")),
            screen_max_tokens=int(os.environ.get("LW_WEEKLY_SCREEN_MAX_TOKENS", "6000")),
            extract_batch=int(os.environ.get("LW_WEEKLY_EXTRACT_BATCH", "2")),
            extract_max_tokens=int(os.environ.get("LW_WEEKLY_EXTRACT_MAX_TOKENS", "5000")),
            weekly_slug=os.environ.get("LW_WEEKLY_SLUG", "weekly-approvals"),
            push=os.environ.get("LW_WEEKLY_PUSH", "1") != "0",
        )
        print(f"[{now()}] 周报完成", flush=True)
    except SystemExit as e:
        print(f"[{now()}] 周报因配置问题中止: {e}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[{now()}] 周报异常: {e}", flush=True)
    gc.collect()


def lwdaily_job():
    """抓取语鲸自己生成的个人专属日报。不依赖 LLM，只要语鲸令牌有效即可。"""
    if lwd is None:
        print(f"[{now()}] 语鲸日报模块未加载，跳过", flush=True)
        return
    print(f"[{now()}] 开始抓取语鲸日报", flush=True)
    try:
        lwd.generate(
            cache_path=CACHE_PATH,
            out_dir=FEEDS_DIR,
            reports_dir=REPORTS_DIR,
            base_url=os.environ.get("LW_BASE_URL", ""),
            slug=os.environ.get("LW_LWDAILY_SLUG", "lingowhale-daily"),
            push=os.environ.get("LW_LWDAILY_PUSH", "1") != "0",
        )
        print(f"[{now()}] 语鲸日报完成", flush=True)
    except SystemExit as e:
        print(f"[{now()}] 语鲸日报因配置问题中止: {e}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[{now()}] 语鲸日报异常: {e}", flush=True)
    gc.collect()


def next_event(t):
    """把抓取、日报、周报、语鲸日报几套时刻表合成一条时间线，同一时刻的事情一起做。"""
    best, kinds = None, set()

    def consider(c, kind):
        nonlocal best, kinds
        if best is None or c < best:
            best, kinds = c, {kind}
        elif c == best:
            kinds.add(kind)

    for times, kind in (
        (RUN_HOURS, "fetch"),
        (REPORT_HOURS, "report"),
        (LWDAILY_HOURS, "lwdaily"),
    ):
        for h, m in times:
            c = t.replace(hour=h, minute=m, second=0, microsecond=0)
            if c <= t:
                c += timedelta(days=1)
            consider(c, kind)

    # 周报只在指定星期几触发，所以要先算出"下一个该星期几"再对齐时刻
    for h, m in WEEKLY_HOURS:
        c = t.replace(hour=h, minute=m, second=0, microsecond=0)
        delta = (WEEKLY_DAY - c.weekday()) % 7
        c += timedelta(days=delta)
        if c <= t:
            c += timedelta(days=7)
        consider(c, "weekly")

    return best, kinds


def scheduler():
    # 启动时先抓一次，避免部署后要空等到下一个整点才有数据。
    # 日报不在启动时跑——容器重启一次就推一条微信太吵，
    # 想手动补一份用 LW_REPORT_ON_START=1。
    run_job()
    if os.environ.get("LW_REPORT_ON_START") == "1":
        report_job()

    while True:
        t = now()
        nxt, kinds = next_event(t)
        wait = (nxt - t).total_seconds()
        print(
            f"下次任务: {nxt} [{'+'.join(sorted(kinds))}] (等待 {wait / 3600:.1f} 小时)",
            flush=True,
        )
        time.sleep(max(wait, 1))
        if "fetch" in kinds:
            run_job()
        if "report" in kinds:
            report_job()
        if "weekly" in kinds:
            weekly_job()
        if "lwdaily" in kinds:
            lwdaily_job()


if __name__ == "__main__":
    threading.Thread(target=serve, daemon=True).start()
    scheduler()
