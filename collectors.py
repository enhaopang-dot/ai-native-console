# -*- coding: utf-8 -*-
"""中台数据采集器。只读本地文件，不做任何写入。

约定：采集器返回 (ok, data, note)。ok=False 时前端显示「待接入」，绝不编造数字。
"""
from __future__ import annotations

import json
import re
import socket
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path("D:/Claude/项目")
LIB = ROOT / "09 学习资料与数据库"
FDE = ROOT / "13 FDE/01-客户项目"
FDE_MORE = ROOT / "13 FDE/02-业务与获客"
MEDIA = Path(r"D:/Claude/项目/10 个人/自媒体")
SONGMU = FDE / "示例客户目录"
LULU = ROOT / "03 项目开发/软件工具/20260430-大LULU女装"
MINGLI = ROOT / "06 道/03-命/四柱八字/命理-私域/01-客户档案/2026年"
TODO_FILE = Path(__file__).parent / "data/todo.json"
STATE_FILE = Path(__file__).parent / "data/state.json"

# 内容生产工具链（存在即「可用」，都是本机已沉淀的东西）
CONTENT_TOOLS = [
    ("抖音下载转写", Path(""<你的本地路径>""), "抓流+ASR 一条命令"),
    ("微信提取", Path("D:/Claude/配置/.claude/skills/wechat-extraction"), "群聊/私聊/语音"),
    ("本地生图", Path("D:/Claude/配置/.claude/tools/local_image_gen.py"), "ComfyUI Qwen-Image"),
    ("视频生成", ROOT / "03 项目开发/软件工具/20260917-MiniMaxH3-本地部署", "MiniMax H3 本地"),
    ("网页视频下载", Path(""<你的本地路径>""), "CDP 抓包兜底"),
]


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _ago(ts: float) -> str:
    if not ts:
        return "—"
    d = datetime.now() - datetime.fromtimestamp(ts)
    if d < timedelta(minutes=1):
        return "刚刚"
    if d < timedelta(hours=1):
        return f"{int(d.total_seconds() // 60)} 分钟前"
    if d < timedelta(days=1):
        return f"{int(d.total_seconds() // 3600)} 小时前"
    return f"{d.days} 天前"


# ---------------- FDE ----------------
def collect_projects() -> tuple[bool, list, str]:
    if not FDE.exists() and not FDE_MORE.exists():
        return False, [], "客户项目目录不存在"
    rows = []
    date_re = re.compile(r"(20\d{2})-(\d{2})-(\d{2})")
    roots = [d for d in (FDE, FDE_MORE) if d.exists()]
    dirs = []
    for r in roots:
        for x in r.iterdir():
            if not x.is_dir() or x.name.startswith((".", "_")):
                continue
            # 02-业务与获客 下只认带日期前缀的项目目录，避免把说明/资料目录当项目
            if r == FDE_MORE and not re.match(r"^20\d{6}-", x.name):
                continue
            dirs.append(x)
    for p in dirs:
        if not p.is_dir() or p.name.startswith((".", "_")):
            continue
        files = list(p.glob("*.md")) + list(p.glob("*/*.md"))
        best_date, best_ts = None, 0.0
        for f in files:
            m = date_re.search(f.name)
            if m:
                try:
                    d = datetime.strptime(m.group(0), "%Y-%m-%d")
                    if best_date is None or d > best_date:
                        best_date = d
                except ValueError:
                    pass
            best_ts = max(best_ts, _mtime(f))
        if best_date:
            days = (datetime.now() - best_date).days
            meta = "今天" if days <= 0 else f"{days} 天前"
            ts = best_date.timestamp()
        else:
            ts = best_ts or _mtime(p)
            meta = _ago(ts)
        label = re.sub(r"^\d{6,8}[-_\s]*", "", p.name).strip() or p.name
        rows.append({"name": label, "raw": p.name, "ts": ts, "meta": meta})
    rows.sort(key=lambda r: r["ts"], reverse=True)
    return True, rows[:8], ""


# ---------------- 电商：客户B + 大 LULU ----------------
DOUYIN_DB = Path(r"D:/Claude/项目/14 内容管理/02-抖音开放平台-内容数据/03-数据/douyin.db")


def collect_douyin() -> tuple[bool, dict, str]:
    """读抖音作品库。数据来自创作者中心导出后解析入库，不是 API 实时拉取，
    所以每条都要标「截止日」，不能当实时数据用。"""
    if not DOUYIN_DB.exists():
        return False, {}, "未找到 douyin.db"
    con = sqlite3.connect(f"file:{DOUYIN_DB}?mode=ro", uri=True, timeout=5)
    try:
        by = {}
        for acc, n, plays, mx in con.execute(
                "SELECT account, COUNT(*), COALESCE(SUM(CAST(play AS INTEGER)),0), MAX(publish_time) "
                "FROM works GROUP BY account"):
            by[acc] = {"works": n, "plays": plays, "last": (mx or "")[:10]}
        cut = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        for acc, n, plays, cr in con.execute(
                "SELECT account, COUNT(*), COALESCE(SUM(CAST(play AS INTEGER)),0), "
                "AVG(CAST(complete_rate AS REAL)) FROM works WHERE publish_time >= ? GROUP BY account",
                (cut,)):
            by.setdefault(acc, {}).update({"m_works": n, "m_plays": plays,
                                           "m_comp": round((cr or 0) * 100, 1)})
        latest = con.execute("SELECT MAX(publish_time) FROM works").fetchone()[0] or ""
        # 近 7 天：锚在库里的最新发布日期，不是 today()——导出是快照，按 today 算会一天天滑成 0
        cut7 = ""
        if latest:
            try:
                cut7 = (datetime.strptime(latest[:19], "%Y-%m-%d %H:%M:%S")
                        - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                cut7 = ""
        if cut7:
            for acc, n, plays in con.execute(
                    "SELECT account, COUNT(*), COALESCE(SUM(CAST(play AS INTEGER)),0) "
                    "FROM works WHERE publish_time >= ? GROUP BY account", (cut7,)):
                by.setdefault(acc, {}).update({"w_works": n, "w_plays": plays})
    finally:
        con.close()
    return True, {"by_account": by, "latest": latest[:10], "cut7": cut7[:10]}, ""


def collect_ecom() -> tuple[bool, dict, str]:
    facts = {}
    md = SONGMU / "CLAUDE.md"
    if md.exists():
        text = md.read_text(encoding="utf-8", errors="ignore")
        m = re.search(r"在册\s*(\d+)\s*人", text)
        if m:
            facts["staff"] = int(m.group(1))
        m = re.search(r"([\d,]{3,})\s*元/月", text)
        if m:
            facts["fee"] = m.group(1)
        if "天猫旗舰店" in text:
            facts["channels"] = "天猫 · 亚马逊 · 小红书"
    # 客户B每日产量汇报：数最近有几次
    daily = SONGMU / "07-数据源/管理群每日产量汇报"
    if daily.exists():
        days = sorted([d.name for d in daily.iterdir() if d.is_dir() and d.name.isdigit()])
        facts["daily_days"] = len(days)
        facts["daily_last"] = days[-1] if days else ""
        if days:
            facts["daily_meta"] = _ago(_mtime(daily / days[-1]))

    lulu_ok = LULU.exists()
    if lulu_ok:
        files = list(LULU.rglob("*.csv")) + list(LULU.rglob("*.xlsx"))
        facts["lulu_files"] = len(files)

    ok = bool(facts)
    return ok, facts, "" if ok else f"未找到 {SONGMU}"


# ---------------- 内容工具链 ----------------
def collect_tools() -> tuple[bool, list, str]:
    rows = []
    for name, path, desc in CONTENT_TOOLS:
        ok = path.exists()
        rows.append({
            "name": name,
            "value": "可用" if ok else "缺失",
            "sub": desc,
            "delta": "",
            "dir": "up" if ok else "down",
            "ok": ok,
        })
    return True, rows, ""


# ---------------- 命理 ----------------
REVENUE_FILE = Path(__file__).parent.parent.parent / "06 道/03-命/四柱八字/命理-私域" / \
    "00-客户运营中台/app/data/revenue_monthly.json"


def _load_revenue() -> dict:
    """读 build_revenue.py 产出的月度收入。口径：只算客户转账，剔除本号与主号内部划转。"""
    try:
        return json.loads(REVENUE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def collect_mingli() -> tuple[bool, dict, str]:
    if not MINGLI.exists():
        return False, {}, f"目录不存在：{MINGLI}"
    clients = [d for d in MINGLI.glob("*/*") if d.is_dir()]
    months = sorted([d.name for d in MINGLI.iterdir() if d.is_dir()],
                    key=lambda s: int(s.replace("月", "") or 0))
    latest_month = months[-1] if months else ""
    latest = [c for c in clients if c.parent.name == latest_month]
    newest = max(clients, key=_mtime) if clients else None
    rev = _load_revenue()
    cur = rev.get("cur") or {}
    prev = rev.get("prev") or {}
    return True, {
        "total": len(clients),
        "month": latest_month,
        "month_new": len(latest),
        "newest": newest.name[:18] if newest else "—",
        "newest_ago": _ago(_mtime(newest)) if newest else "—",
        "rev_cur": cur.get("income"),
        "rev_cur_n": cur.get("n", 0),
        "rev_cur_month": cur.get("month", ""),
        "rev_cur_day": cur.get("day", ""),
        "rev_prev": prev.get("income"),
        "rev_prev_n": prev.get("n", 0),
        "rev_prev_month": prev.get("month", ""),
        "rev_mom": rev.get("mom_pct"),
        "rev_at": rev.get("generated_at", ""),
    }, ""


# ---------------- 知识库（保留，不上面板） ----------------
def collect_library() -> tuple[bool, dict, str]:
    if not LIB.exists():
        return False, {}, f"目录不存在：{LIB}"
    total = week = 0
    cutoff = (datetime.now() - timedelta(days=7)).timestamp()
    for f in LIB.rglob("*.md"):
        if "_归档" in str(f):
            continue
        total += 1
        if _mtime(f) >= cutoff:
            week += 1
    return True, {"total": total, "week": week}, ""


MON_FILE = Path(__file__).parent / "data/monitor.json"
NEXT_FILE = Path(__file__).parent / "data/next_steps.json"


def next_steps_for(name: str) -> dict:
    """返回 {"pending":[...], "done":[...]}"""
    try:
        d = json.loads(NEXT_FILE.read_text(encoding="utf-8"))
        raw = d.get("projects", {}).get(name, [])
    except Exception:  # noqa: BLE001
        return {"pending": [], "done": []}
    norm = []
    for i, x in enumerate(raw):
        if isinstance(x, str):
            norm.append({"id": f"s{i}", "text": x, "done": False, "keywords": []})
        else:
            norm.append(x)
    return {"pending": [x for x in norm if not x.get("done")],
            "done": [x for x in norm if x.get("done")]}


def project_path(name: str) -> str:
    """按项目名从 rules.json 找回路径。"""
    try:
        rules = json.loads((Path(__file__).parent / "data/rules.json").read_text(encoding="utf-8"))
        for r in rules.get("rules", []):
            if r.get("name") == name:
                return r.get("project", "")
    except Exception:  # noqa: BLE001
        pass
    return ""


def project_activity(name: str, todos: list[dict]) -> dict:
    """某项目今天做了什么、还欠什么、有没有留痕。"""
    today = datetime.now().strftime("%Y-%m-%d")
    done_today = [t for t in todos
                  if t.get("done") and t.get("project") == name
                  and str(t.get("done_at", "")).startswith(datetime.now().strftime("%m-%d"))]
    pending = [t for t in todos if not t.get("done") and t.get("project") == name]
    traces, recent = [], []
    path = project_path(name)
    if path:
        folder = Path(path) / "沟通记录"
        if folder.exists():
            f = folder / f"沟通记录-{today}.md"
            if f.exists():
                try:
                    txt = f.read_text(encoding="utf-8", errors="ignore")
                    traces = [ln.strip("- ").strip() for ln in txt.splitlines()
                              if ln.startswith("- 来源：") or ln.startswith("- 处理：")]
                except Exception:  # noqa: BLE001
                    pass
            files = sorted([x.name for x in folder.glob("沟通记录-*.md")], reverse=True)[:3]
            recent = files
    # 目录结构与最近文件
    dirs, recent_files, open_items = [], [], []
    if path:
        root = Path(path)
        if root.exists():
            dirs = sorted([f"{d.name}" for d in root.iterdir() if d.is_dir() and not d.name.startswith("_")])
            files = [f for f in root.rglob("*") if f.is_file() and f.suffix in (".md", ".xlsx", ".pdf", ".docx")]
            files.sort(key=_mtime, reverse=True)
            recent_files = [f"{f.name}（{_ago(_mtime(f))}）" for f in files[:6]]
            # 从 CLAUDE.md 与项目记忆里抽“待确认/待办/⏳”的句子
            srcs = [root / "CLAUDE.md"] + list((root / "项目记忆").glob("*.md")) if (root / "项目记忆").exists() else [root / "CLAUDE.md"]
            for f in srcs:
                if not f.exists():
                    continue
                try:
                    for ln in f.read_text(encoding="utf-8", errors="ignore").splitlines():
                        t = ln.strip(" -|").lstrip("⏳⚠️✅🔴➡️·•　 ")
                        if any(k in t for k in ("待确认", "待定", "待约", "待推进", "⏳", "待处理", "待跟")):
                            open_items.append(t[:70])
                except Exception:  # noqa: BLE001
                    pass
    # 去重保序
    seen, opens = set(), []
    for x in open_items:
        if x not in seen:
            seen.add(x); opens.append(x)

    # 项目定位：CLAUDE.md 第一段正文
    brief, next_hints, timeline = "", [], []
    if path:
        root = Path(path)
        cm = root / "CLAUDE.md"
        if cm.exists():
            try:
                lines = [l.strip() for l in cm.read_text(encoding="utf-8", errors="ignore").splitlines()]
                for l in lines:
                    if l and not l.startswith("#") and not l.startswith(">") and len(l) > 12:
                        brief = l[:110]
                        break
            except Exception:  # noqa: BLE001
                pass
        # 下一步线索：含计划类词的句子
        KEY = ("明天", "下周", "本周", "路演", "PPT", "ppt", "截止", "下一步",
               "待办", "计划安排", "复盘会", "待约", "待推进")
        BAD = ("禁止", "一律", "命名", "示例", "`", "目录结构", "规则")
        for f in ([cm] if cm.exists() else []) + list((root / "项目记忆").glob("*.md"))                  + sorted((root / "迭代记录").glob("*.md"))[-2:]                  + sorted((root / "沟通记录").glob("*.md"))[-2:]:
            try:
                for ln in f.read_text(encoding="utf-8", errors="ignore").splitlines():
                    t = ln.strip(" -|#>")
                    if 8 < len(t) <= 72 and any(k in t for k in KEY) and not any(b in t for b in BAD):
                        next_hints.append(t[:80])
            except Exception:  # noqa: BLE001
                continue
        # 近期动态：文件名时间线
        for folder_name in ("沟通记录", "迭代记录", "项目记忆"):
            d = root / folder_name
            if not d.exists():
                continue
            fs = sorted([x for x in d.glob("*.md")], key=_mtime, reverse=True)[:3]
            for f in fs:
                timeline.append({"kind": folder_name, "name": f.name, "ago": _ago(_mtime(f))})
        timeline.sort(key=lambda x: x["name"], reverse=True)

    seen2, hints = set(), []
    for x in next_hints:
        if x not in seen2:
            seen2.add(x); hints.append(x)

    return {"done_today": done_today, "pending": pending, "traces": traces,
            "recent": recent, "path": path, "dirs": dirs,
            "recent_files": recent_files, "open_items": opens[:8],
            "brief": brief, "next_hints": hints[:4], "timeline": timeline[:8],
            "next": next_steps_for(name)}


def collect_monitor() -> dict:
    try:
        return json.loads(MON_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _ceo_log() -> list:
    """今天 CEO 层到底干了什么：归并、改名、调级、结案，逐条列出来。"""
    out = []
    try:
        import store
        t = datetime.now().strftime("%Y-%m-%d")
        # 同一件事只留最后一条动作，避免一整屏都是同一件在反复改名
        seen = set()
        for ts, act, tid, detail in store.q(
                "SELECT ts,action,tid,detail FROM thread_log WHERE ts LIKE ? ORDER BY id DESC", 60, (t + "%",)):
            if tid in seen:
                continue
            seen.add(tid)
            out.append({"ts": (ts or "")[11:16], "act": act, "tid": tid, "detail": detail or ""})
            if len(out) >= 16:
                break
        for done_at, title in store.q(
                "SELECT done_at,title FROM cards WHERE done=1 AND done_at LIKE ? ORDER BY rowid DESC",
                8, (t + "%",)):
            out.append({"ts": "", "act": "结案", "tid": "", "detail": title or ""})
    except Exception:  # noqa: BLE001
        pass
    return out


def _ecom_writebacks_today() -> int:
    """电商 Agent 今天的「出手」= 千川/罗盘数据写回次数（刷新日志里数）。"""
    try:
        f = Path(__file__).parent / "logs" / "client_a_refresh.log"
        day = datetime.now().strftime("%Y-%m-%d")
        n = 0
        with open(f, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith(f"[{day}") and "写回" in line:
                    n += 1
        return n
    except Exception:  # noqa: BLE001
        return 0


def _scan_rounds() -> int:
    """今天真正跑了几轮巡检。以前显示的是「面板刷新了几次」，虚高。"""
    try:
        import store
        t = datetime.now().strftime("%Y-%m-%d")
        return int(store.q("SELECT COUNT(*) FROM scan_log WHERE ts LIKE ?", 1, (t + "%",))[0][0])
    except Exception:  # noqa: BLE001
        return 0


def _ceo_actions() -> int:
    """今天 CEO 层真正动过几次手：归并、改名、调级、结案都算。"""
    try:
        import store
        t = datetime.now().strftime("%Y-%m-%d")
        # 按「碰过几件事」算，不按日志行数：同一件事改名+调级会产生两行，重复算就把数字吹大了
        n = int(store.q("SELECT COUNT(DISTINCT tid) FROM thread_log WHERE ts LIKE ?", 1, (t + "%",))[0][0])
        n += int(store.q("SELECT COUNT(*) FROM cards WHERE done=1 AND done_at LIKE ?", 1, (t + "%",))[0][0])
        return n
    except Exception:  # noqa: BLE001
        return 0


def _port_alive(port: int, host: str = "127.0.0.1") -> bool:
    """端口通不通 = 这个工作台在不在跑。300ms 超时，不拖慢面板。"""
    if not port:
        return False
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except Exception:  # noqa: BLE001
        return False


def collect_workbenches() -> tuple[bool, list, str]:
    """本地工作台清单，点一下在浏览器里打开。"""
    f = Path(__file__).parent / "data/workbenches.json"
    if not f.exists():
        return False, [], "还没有 data/workbenches.json"
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return False, [], f"workbenches.json 解析失败：{e}"
    items = d.get("items", [])
    for it in items:
        it["alive"] = _port_alive(int(it.get("port") or 0))
    return True, items, ""


def collect_schedule() -> tuple[bool, list, str]:
    """右侧日程。数据来自 data/schedule.json，日期一律真实，不确定就留空标待定。"""
    p = Path(__file__).parent / "data/schedule.json"
    if not p.exists():
        return False, [], "还没有 data/schedule.json"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return False, [], f"schedule.json 解析失败：{e}"
    today = datetime.now().strftime("%Y-%m-%d")
    out = []
    for it in d.get("items", []):
        s = (it.get("start") or "").strip()
        e = (it.get("end") or s).strip()
        if not s:
            status, days = "待定", 0
        else:
            if s <= today <= e:
                status = "进行中" if s != e else "今天"
            elif e < today:
                status = "已过"
            else:
                status = "未来"
            try:
                days = (datetime.strptime(e, "%Y-%m-%d")
                        - datetime.strptime(s, "%Y-%m-%d")).days + 1
            except ValueError:
                days = 1
        out.append({**it, "start": s, "end": e, "status": status,
                    "days": days, "multi": days > 1})
    out.sort(key=lambda x: (x["start"] or "9999-12-31", x.get("title", "")))
    return True, out, ""


def collect_auto_closed(limit: int = 12) -> list[dict]:
    """系统自己结案的记录：哪件事、什么时候、凭什么关的。"""
    try:
        import store
        con = store.conn()
        rows = con.execute(
            "SELECT l.ts, t.title, l.detail FROM thread_log l "
            "LEFT JOIN threads t ON t.tid=l.tid WHERE l.action='autoclose' "
            "ORDER BY l.id DESC LIMIT ?", (limit,)).fetchall()
        con.close()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for ts, title, detail in rows:
        d = detail or ""
        ev = d.split("证据：", 1)[1] if "证据：" in d else d
        out.append({"at": ts, "title": (title or d.split("｜")[0])[:40], "evidence": ev[:80]})
    return out


def _wx_list(stats: dict) -> list:
    """按账号逐个报状态。两个微信都登录时会各出一盏灯，不能只显示 1/2。"""
    try:
        accs = json.loads((Path(__file__).parent / "data/accounts.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        accs = {}
    names = {k: (v.get("label") or k[-8:]) for k, v in accs.items() if isinstance(v, dict)}
    on = stats.get("wx_accounts") or {}
    out = [{"label": names.get(k, k[-8:]), "ok": bool(v)} for k, v in on.items()]
    # 停用/还没数据的号也列出来，标离线，别让它凭空消失
    for k, lab in names.items():
        if k not in on:
            out.append({"label": lab, "ok": False})
    return out


def collect_rules(ths: list, th_err: str, n_cards: int) -> dict:
    """「规则」抽屉的数据：每条自动化在干什么、上次跑出什么结果。"""
    try:
        import threads as _th
        st = _th.auto_state()
    except Exception:  # noqa: BLE001
        st = {}
    c = st.get("consolidate") or {}
    over = sum(1 for t in ths if (t.get("owed") or "").strip())
    stalled = sum(1 for t in ths if t["stalled"])
    try:
        p_at = datetime.fromtimestamp(
            (Path(__file__).parent / "data/judge_prompt.md").stat().st_mtime)
        p_txt = f"{p_at:%m-%d %H:%M}"
    except Exception:  # noqa: BLE001
        p_txt = "—"
    state = ("通" if c.get("ok") else ("断 · " + str(c.get("error", ""))[:30] if c else "还没跑过"))
    items = [
        {"name": "同事项归并", "desc": "每轮巡检后把新卡归到已有事项，识别“只是进度推进”",
         "state": state, "last": c.get("at", "—"),
         "hits": f"合并 {c.get('merged', 0)} · 新开 {c.get('new', 0)}"},
        {"name": "停摆提醒", "desc": "P0 超 2 天 / P1 超 3 天没进展，在卡上飘红",
         "state": "开", "last": "随时算", "hits": f"当前 {stalled} 件"},
        {"name": "我方欠账", "desc": "每轮归并时由模型重写「我方还欠对方什么」，跟着事项走，事项销了自动消失",
         "state": "开", "last": "每轮巡检", "hits": f"{over} 件欠着"},
        {"name": "判据提示词", "desc": "data/judge_prompt.md，改了就下一轮生效",
         "state": "热加载", "last": p_txt, "hits": "模型判定口径"},
        {"name": "进化建议", "desc": "每天 19:00 出一份“下一步该改什么”，落 data/evolve/",
         "state": "开", "last": "19:00", "hits": "见下方"},
    ]
    try:
        import evolve as _ev
        rep = _ev.today_report()
    except Exception:  # noqa: BLE001
        rep = ""
    return {"items": items, "evolve": rep, "err": th_err,
            "cards": n_cards, "threads": len(ths)}


def _asst_items(ths: list, todos: list, th_err: str = "") -> list:
    """助理卡列表：每件事一行。

    事项层没建成（模型挂了 / 还没跑过）就退回按天的卡片——
    宁可显示老的碎卡片，也不能因为归并失败把待办显示成空的。
    """
    items = []
    for t in ths:
        sub = f"{t['n']} 条进展 · 最后 {t['last_at'][5:10]}"
        owed = (t.get("owed") or "").strip()
        if owed:                       # 我方还欠对方的话，就顶在副标上，不另立一张台账
            sub = f"欠：{owed[:16]} · {sub}"
        items.append({
            "name": (t["title"] or "未命名")[:18],
            "value": t["level"],
            "sub": sub,
            "delta": f"停 {t['stalled']} 天" if t["stalled"] else ("欠着" if owed else ""),
            "dir": "down" if (t["stalled"] or owed) else "",
            "children": t["timeline"] or [{"name": "—", "value": "", "sub": "还没有挂卡片"}],
            "tid": t["tid"],
            "owed": owed,
        })
    if items:
        return items
    return [{"name": t.get("title", "")[:18], "value": t.get("level", ""),
             "sid": t.get("sid", ""),
             "sub": f"{t.get('project') or t.get('source', '')} · {t.get('created_at', '')}",
             "delta": "", "dir": "dn"}
            for t in todos[:8]]


def collect_todo() -> tuple[bool, list, str]:
    """主存是 data/console.db；读不到再回退 todo.json。"""
    try:
        import store
        items = [i for i in store.load_todos() if not i.get("skipped")]
        return True, items, ""
    except Exception:  # noqa: BLE001
        pass
    if not TODO_FILE.exists():
        return False, [], "还没有 data/todo.json"
    try:
        data = json.loads(TODO_FILE.read_text(encoding="utf-8"))
        return True, data.get("items", []), ""
    except Exception as e:  # noqa: BLE001
        return False, [], f"todo.json 解析失败：{e}"


def collect_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def bump_state(key: str) -> None:
    st = collect_state()
    st[key] = st.get(key, 0) + 1
    st["last_beat"] = time.time()
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------- 逐条诊断：现状 / 问题 / 建议 ----------------
def diagnose(agent_key: str, it: dict) -> dict:
    name = str(it.get("name", ""))
    val = str(it.get("value", ""))
    sub = str(it.get("sub", ""))

    if agent_key == "ecom":
        if "客户B家居" in name:
            return {"s": "正常", "i": "首月尾款到账待确认，发票 2 张已开", "a": "催财务对账，确认对公到账"}
        if "产量日报" in name:
            return {"s": "异常", "i": "管理群产量日报已断更，最新一条停留在 91 天前", "a": "找徐爱萍恢复日报；断更超一周直接上门"}
        if "LULU" in name:
            return {"s": "正常", "i": "库存报表吃多平台导出，口径还没统一", "a": "先钉死抖店/小红书/微信小店三条口径再谈自动化"}
        return {"s": "关注", "i": "京东已退店，亚马逊是主力渠道", "a": "把亚马逊销量接进面板"}

    if agent_key == "content":
        if name == "公众号":
            try:
                m = json.loads((Path(__file__).parent / "data/wxmp.json").read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                m = {}
            if not m.get("ok"):
                return {"s": "关注", "i": "公众号数据未接入", "a": "跑一次 公众号取数.py 刷新快照"}
            cdu = m.get("常读用户") or {}
            return {"s": "关注",
                    "i": f"停更：最近发文 {m.get('last_post_at')}，近 30 天阅读 {m.get('read_30d')} 人",
                    "a": f"定复更节奏；先用 {cdu.get('count', '—')} 个常读用户做选题验证"}
        # 原来只认 val=="可用"，但内容卡的值是「几个号」，于是每一行都被判成异常，
        # 真丢工具和真接上数据长得一模一样。改成先查工具在不在，再看这一行有没有实时数。
        missing = [n for n, p, _ in CONTENT_TOOLS if not Path(p).exists()]
        if missing:
            return {"s": "异常", "i": "工具缺失：" + "、".join(missing)[:40],
                    "a": "检查安装位置，改 CONTENT_TOOLS 路径"}
        if "限流" in name:
            return {"s": "异常", "i": sub[:40], "a": "看这条为什么没进推荐池"}
        if "实时" in sub or "数据已接入" in sub or "正在跑" in sub:
            return {"s": "正常", "i": "已接平台后台，数据实时", "a": "看限流告警与低播放条目"}
        if "待接入" in sub or "未通" in sub:
            return {"s": "关注", "i": sub[:40] or "数据未接入", "a": "接该平台后台数据"}
        return {"s": "正常", "i": "工具就绪", "a": "保持每日 08:00 抓取"}

    if agent_key == "fde":
        days = None
        if "天前" in val:
            try:
                days = int(val.replace("天前", "").strip())
            except ValueError:
                days = None
        if days is None:
            return {"s": "正常", "i": "近期有动作", "a": "保持跟进"}
        if days <= 2:
            return {"s": "正常", "i": f"最近 {days} 天内有改动", "a": "按当前节奏推进"}
        if days <= 7:
            return {"s": "关注", "i": f"已 {days} 天没动静，节点可能被拖", "a": "今天发一条进展确认，别等对方问"}
        return {"s": "异常", "i": f"已 {days} 天没动，等于停摆", "a": "要么定下周动作，要么标暂停，别挂着占位"}

    if agent_key == "mingli":
        if "新增" in name:
            return {"s": "关注", "i": "本月只新增 1 位，获客端偏弱", "a": "复用最近一次成交话术做一轮私域触达"}
        if "最近建档" in name:
            return {"s": "正常", "i": "昨天刚建档，跟进节奏正常", "a": "48 小时内发第一次解读"}
        return {"s": "正常", "i": "63 位档案已归档，无缺档", "a": "每月底跑一次 sync_archives.py"}

    if agent_key == "asst":
        lv = str(it.get("value", ""))
        if lv == "P0":
            return {"s": "紧急", "i": "P0 事项，挂着就是在烧时间", "a": "今天必须给结论，不给结论也要给截止时间"}
        if lv == "P1":
            return {"s": "关注", "i": "P1，影响本周节奏", "a": "本周内定，超过 3 天升级为 P0"}
        return {"s": "正常", "i": "P2，可排期", "a": "下周统一过一遍"}

    return {"s": "正常", "i": "", "a": ""}


# 千川账户 → 店铺。日报里的 accounts 目前为空，靠这张表把账户挂到店铺上，
# 否则所有消耗都会堆进「未标注店铺」，电商卡片点不进具体店。
ADV_SHOP = {"0000000000000000": "示例店铺（抖音）"}
ADV_NAME = {"0000000000000000": "01-商品卡"}


def collect_qianchuan() -> tuple[bool, dict, str]:
    """千川投放数据：读项目落地的日报台账。观察期（2026-10-25 起一周）只展示不报警。"""
    p = Path(r"D:/Claude/项目/14 内容管理/01-抖音电商API接入/05-数据/千川日报.json")
    if not p.exists():
        return False, {}, f"未找到 {p.name}"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return False, {}, f"解析失败：{e}"

    daily = d.get("daily") or {}
    days = sorted(daily)
    if not days:
        return False, {}, "台账无数据"

    today = days[-1]
    rows = daily.get(today) or {}
    cost = round(sum(v.get("cost", 0) for v in rows.values()), 2)
    settle = round(sum(v.get("settle", 0) for v in rows.values()), 2)
    orders = sum(v.get("orders", 0) for v in rows.values())
    roi = round(settle / cost, 2) if cost else 0

    # 近 7 日基线（观察期用，之后定阈值靠它）
    recent = days[-7:]
    hist_cost = [round(sum(v.get("cost", 0) for v in (daily[k] or {}).values()), 2) for k in recent]
    hist_roi = []
    for k in recent:
        c = sum(v.get("cost", 0) for v in (daily[k] or {}).values())
        s = sum(v.get("settle", 0) for v in (daily[k] or {}).values())
        hist_roi.append(round(s / c, 2) if c else 0)
    avg_cost = round(sum(hist_cost) / len(hist_cost), 2) if hist_cost else 0
    avg_roi = round(sum(hist_roi) / len(hist_roi), 2) if hist_roi else 0

    name_of = {str(a["adv_id"]): a.get("name", "") for a in d.get("accounts", [])}
    shop_of = {str(a["adv_id"]): (a.get("shop") or "") for a in d.get("accounts", [])}
    by_shop = {}
    for adv_id, v in rows.items():
        sh = shop_of.get(adv_id, "") or ADV_SHOP.get(str(adv_id), "") or "未标注店铺"
        b = by_shop.setdefault(sh, {"cost": 0.0, "settle": 0.0, "orders": 0, "accounts": []})
        b["cost"] += v.get("cost", 0) or 0
        b["settle"] += v.get("settle", 0) or 0
        b["orders"] += v.get("orders", 0) or 0
        if (v.get("cost") or v.get("orders")):
            b["accounts"].append({
                "name": name_of.get(adv_id) or ADV_NAME.get(str(adv_id), adv_id),
                "cost": round(v.get("cost", 0) or 0, 2),
                "roi": v.get("roi", 0) or 0,
                "orders": v.get("orders", 0) or 0,
                "settle": round(v.get("settle", 0) or 0, 2),
            })
    for b in by_shop.values():
        b["cost"] = round(b["cost"], 2)
        b["settle"] = round(b["settle"], 2)
        b["roi"] = round(b["settle"] / b["cost"], 2) if b["cost"] else 0
    items = []
    for adv_id, v in sorted(rows.items(), key=lambda kv: -kv[1].get("cost", 0))[:5]:
        if not (v.get("cost") or v.get("orders")):
            continue
        items.append({
            "name": name_of.get(adv_id) or ADV_NAME.get(str(adv_id), adv_id),
            "value": f"{v.get('cost', 0)} 元",
            "sub": f"ROI {v.get('roi', 0)} · 净成交 {v.get('settle', 0)} · {v.get('orders', 0)} 单",
            "delta": "", "dir": "",
        })
    if not items:
        items.append({"name": "今日暂无投放", "value": "0 元", "sub": f"台账更新 {d.get('updated','')}",
                      "delta": "", "dir": ""})

    facts = {
        "day": today, "cost": cost, "settle": settle, "orders": orders, "roi": roi,
        "avg_cost": avg_cost, "avg_roi": avg_roi, "days": len(days),
        "accounts": len(d.get("accounts") or []), "shops": len(d.get("shops") or []),
        "updated": d.get("updated", ""),
    }
    return True, {"facts": facts, "items": items, "by_shop": by_shop}, ""


YALIDA_DATA = Path(__file__).parent / "data/client_a_dashboard.json"


def collect_client_a() -> dict | None:
    """客户A店铺详情页数据（由项目侧 82_build_dashboard_data.py 生成）。"""
    try:
        return json.loads(YALIDA_DATA.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _fans_num(s) -> float:
    s = str(s or "").replace(" ", "").replace("　", "")
    if not s:
        return 0.0
    try:
        if s.endswith("万"):
            return float(s[:-1]) * 10000
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def _content_ident(acc: dict) -> str:
    """按发布身份分两块：九离命理 / 九离AI。"""
    d = acc.get("dir") or ""
    n = acc.get("name") or ""
    if "九离 AI" in n or "庞恩浩" in n or d.startswith("AI") or "Excel" in d:
        return "ai"
    return "mingli"


def _client_a_feed(n: int = 12) -> list[dict]:
    """读客户A刷新日志，给左右 Agent 侧栏用。"""
    p = Path(__file__).parent / "logs/client_a_refresh.log"
    out = []
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines()[-n:]:
        line = line.strip()
        if not line.startswith("[") or "] " not in line:
            continue
        try:
            ts, msg = line[1:].split("] ", 1)
        except ValueError:
            continue
        out.append({"at": ts[11:16] if len(ts) >= 16 else ts, "msg": msg})
    return out


def build_payload() -> dict:
    ok_p, projects_raw, note_p = collect_projects()
    ok_e, ecom, _ = collect_ecom()
    ok_t, tools, _ = collect_tools()
    ok_m, ml, note_m = collect_mingli()
    ok_todo, todos_all, note_t = collect_todo()
    ok_qc, qc_pack, note_qc = collect_qianchuan()
    projects = {}
    for r in projects_raw:
        projects[r["name"]] = project_activity(r["name"], todos_all)

    todos = [t for t in todos_all if not t.get("done") and not t.get("skipped")]
    # ---- 事项层：卡片归并成「一件事」，我方欠什么挂在事项上 ----
    ths, th_err = [], ""
    try:
        import threads as _th
        ths = _th.list_threads()
    except Exception as e:  # noqa: BLE001
        th_err = str(e)[:80]
    th_n = len(ths)
    stalled_n = sum(1 for t in ths if t["stalled"])
    owed_n = sum(1 for t in ths if (t.get("owed") or "").strip())
    asst_items = _asst_items(ths, todos, th_err)
    mon = collect_monitor()
    st = collect_state()

    # ---- 电商：以店铺为实体，渠道挂在下面 ----
    by_shop = qc_pack.get("by_shop", {}) if ok_qc else {}
    ecom_items = []
    for shop, d in sorted(by_shop.items(), key=lambda kv: -kv[1].get("cost", 0)):
        if not d.get("cost") and not d.get("orders") and "未标注" in shop:
            continue
        kids = []
        for ac in d.get("accounts", [])[:6]:
            kids.append({"name": f"千川·{ac['name']}", "value": f"¥{ac['cost']}",
                         "sub": f"ROI {ac['roi']} · 净成交 {ac['settle']} · {ac['orders']} 单"})
        if not kids:
            kids.append({"name": "千川", "value": "¥0", "sub": "今日该店无投放"})
        kids.append({"name": "万相台", "value": "待接入", "sub": "等店铺后台数据"})
        kids.append({"name": "店铺经营", "value": "待接入", "sub": "访客/加购/成交"})
        ecom_items.append({
            "name": shop if len(shop) <= 16 else shop[:15] + "…",
            "value": f"¥{d.get('cost', 0)}",
            "sub": f"千川 ROI {d.get('roi', 0)} · {d.get('orders', 0)} 单 · {len(d.get('accounts', []))} 个投放账户",
            "children": kids,
            "shop": "client_a" if "客户A" in shop else "",
        })
    if ok_e:
        ecom_items.append({
            "name": "客户B家居（天猫）",
            "value": "待接入",
            "sub": ecom.get("channels", "天猫 · 亚马逊 · 小红书"),
            "shop": "client_b",
            "children": [
                {"name": "万相台", "value": "待接入", "sub": "阿里妈妈后台"},
                {"name": "店铺经营", "value": "待接入", "sub": "访客/加购/成交（1015 批次滞后）"},
                {"name": "生产日报", "value": f"{ecom.get('daily_days', '—')} 天",
                 "sub": f"管理群汇报 · 最新 {ecom.get('daily_meta', '—')}"},
            ],
        })
    ecom_items.append({
        "name": "大 LULU 女装", "value": "待接入",
        "sub": "抖店 · 小红书 · 微信小店",
        "shop": "lulu",
        "children": [{"name": "补货自动化", "value": f"{ecom.get('lulu_files', 0)} 份", "sub": "已有文件，未接经营数据"}],
    })

    ecom_shops = [
        {"key": "client_a", "name": "示例店铺（抖音）", "platform": "抖音",
         "status": "已接入", "sub": "千川 + 罗盘 · 26 场", "has": True},
        {"key": "client_b", "name": "客户B家居（天猫）", "platform": "天猫",
         "status": "待接入", "sub": "万相台与店铺经营未接", "has": False},
        {"key": "lulu", "name": "大 LULU 女装", "platform": "抖店 · 小红书 · 微信小店",
         "status": "待接入", "sub": "只有补货文件", "has": False},
    ]

    # ---- 内容：以自媒体账号为实体 ----
    content_items = []
    ok_dy, dy, note_dy = collect_douyin()
    dy_acc = (dy or {}).get("by_account", {})
    # 实时抓的优先：douyin.db 是人工导出的快照，按天变旧，而且没有审核状态
    try:
        import content_live as _cl
        cl = _cl.load()
    except Exception:  # noqa: BLE001
        cl = {}
    dy_live = cl.get("douyin") or {}
    tx_live = cl.get("tencent") or {}
    xhs_live = cl.get("xhs") or {}
    # 公众号后台需扫码，数据是手动刷新落盘的快照，不是实时；缺文件就当没接
    try:
        mp_live = json.loads((Path(__file__).parent / "data/wxmp.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        mp_live = {}
    wan = lambda n: f"{n / 10000:.1f}万" if n >= 10000 else str(n)
    norm = lambda x: str(x).replace(" ", "").replace("　", "")
    try:
        ma = json.loads((Path(__file__).parent / "data/media_accounts.json").read_text(encoding="utf-8"))
        groups: dict[str, list] = {}
        for acc in ma.get("accounts", []):
            groups.setdefault(acc.get("platform") or "未分平台", []).append(acc)

        # 限流告警排最前面：这是唯一能直接回答「这条是不是被判了」的地方
        if dy_live.get("ok") and dy_live.get("alerts"):
            al = dy_live["alerts"]
            content_items.append({
                "name": "疑似限流", "value": f"{len(al)} 条",
                "sub": f"基线中位数 {dy_live.get('baseline', 0)} 播放 · 抓于 {cl.get('at', '')}",
                "delta": f"限流 {len(al)}", "dir": "down",
                "children": [{"name": w["pub"][5:16], "value": f"{w['play']} 播放",
                              "sub": w["why"]} for w in al[:8]],
            })

        for plat, accs in groups.items():
            kids, tot, last, has = [], 0, "", 0
            for acc in accs:
                name = acc.get("name", "")
                fans = acc.get("fans") or ""
                # 抖音优先用实时抓的；抓不到才退回人工导出的快照库
                dst = None
                # 实时数据只属于抓的那个号，别拿九离的数去标佐易和九离 AI
                lv = None
                if plat == "公众号" and mp_live.get("ok") and norm(name) == norm(mp_live.get("account", "")):
                    m = mp_live
                    cdu = m.get("常读用户") or {}
                    kids.append({
                        "name": name,
                        "value": f"粉丝 {m.get('fans') or fans or '—'}",
                        "sub": (f"{m.get('状态', '').split('。')[0]}"
                                f" · 近 30 天阅读 {m.get('read_30d', '—')} 人"
                                f" · 常读 {cdu.get('count', '—')} 人（{cdu.get('ratio', '—')}）"
                                f" · 最近发文 {m.get('last_post_at', '—')}"),
                    })
                    has += 1
                    last = max(last, m.get("last_post_at") or "")
                    continue
                if plat == "抖音" and dy_live.get("ok") and norm(name) == norm(dy_live.get("account", "九离")):
                    lv = dy_live
                elif plat == "小红书" and xhs_live.get("ok") and norm(name) == norm(xhs_live.get("account", "九离")):
                    lv = xhs_live
                if lv:
                    lw = lv.get("works") or []
                    if lw:
                        kids.append({
                            "name": name,
                            "value": f"粉丝 {fans or '—'}",
                            "sub": (f"实时 · 中位 {lv.get('baseline', 0)} 播放"
                                    f" · 最近 {lw[0]['pub'][:10]} {lw[0]['play']} 播"
                                    f" · 偏低 {len(lv.get('lows') or [])} 条"),
                        })
                        has += 1
                        tot += sum(w["play"] for w in lw)
                        last = max(last, lw[0]["pub"])
                        continue
                if plat == "视频号" and not tx_live.get("ok"):
                    kids.append({
                        "name": name,
                        "value": f"粉丝 {fans or '—'}",
                        "sub": f"实时抓取未通：{tx_live.get('error') or '未跑过'}",
                    })
                    continue
                if plat == "抖音":
                    dst = (next((v for k, v in dy_acc.items() if norm(k) == norm(name)), None)
                           or dy_acc.get(name))
                if dst:
                    has += 1
                    tot += dst["plays"]
                    last = max(last, dst.get("last") or "")
                    kids.append({
                        "name": name,
                        "value": f"粉丝 {fans or '—'}",
                        "sub": (f"近7天 {wan(dst.get('w_plays', 0))} · 累计 {wan(dst['plays'])}"
                                f" · {dst['works']} 条 · 最近 {dst['last']}"),
                    })
                else:
                    kids.append({
                        "name": name,
                        "value": f"粉丝 {fans or '—'}",
                        "sub": f"{acc.get('state') or '数据待接入'} · {acc.get('dir', '')}",
                    })
            if plat == "公众号" and mp_live.get("ok"):
                cdu = mp_live.get("常读用户") or {}
                sub = (f"抓于 {mp_live.get('at', '')} · 累计 {mp_live.get('fans', '—')} 粉"
                       f" · 常读 {cdu.get('count', '—')} 人（{cdu.get('ratio', '—')}，月净增 {cdu.get('net_change', '—')}）")
            elif plat == "抖音" and dy_live.get("ok"):
                sub = (f"实时 {cl.get('at', '')} · 中位 {dy_live.get('baseline', 0)} 播放"
                       f" · 告警 {len(dy_live.get('alerts') or [])} 条")
            elif plat == "小红书" and xhs_live.get("ok"):
                sub = (f"实时 {cl.get('at', '')} · 中位 {xhs_live.get('baseline', 0)} 播放"
                       f" · 告警 {len(xhs_live.get('alerts') or [])} 条")
            else:
                sub = (f"合计播放 {wan(tot)} · 最近 {last}"
                       + (f" · {has}/{len(accs)} 个号有数据" if has < len(accs) else "")
                       if has else (accs[0].get("state") or "数据待接入"))
            content_items.append({
                "name": plat, "value": f"{len(accs)} 个号", "sub": sub, "children": kids,
            })
    except Exception as e:  # noqa: BLE001
        pass  # 账号表缺失时内容卡为空，不阻断面板
    # 内容 Agent 只留两个身份块：九离命理 / 九离AI，账号明细挂下面
    ident = {
        "mingli": {"key": "mingli", "name": "九离命理", "accounts": []},
        "ai": {"key": "ai", "name": "九离AI", "accounts": []},
    }
    for acc in (ma.get("accounts") or []):
        ident[_content_ident(acc)]["accounts"].append(acc)
    for v in ident.values():
        accs = v["accounts"]
        v["total_fans"] = sum(_fans_num(a.get("fans")) for a in accs)
        v["platforms"] = sorted({a.get("platform") or "" for a in accs if a.get("platform")})
    content_items = []
    for v in ident.values():
        accs = v["accounts"]
        if not accs:
            continue
        kids = [{
            "name": f"{a.get('name', '')} · {a.get('platform', '')}",
            "value": f"粉丝 {a.get('fans') or '—'}",
            "sub": f"{a.get('dir', '')} · {a.get('state', '')}",
        } for a in accs]
        content_items.append({
            "name": v["name"],
            "value": f"{len(accs)} 个号",
            "sub": f"合计粉丝 {wan(v['total_fans'])} · {'/'.join(v['platforms'])}",
            "children": kids,
            "content": v["key"],
        })
    content_shops = [{
        "key": v["key"], "name": v["name"],
        "platform": " · ".join(v["platforms"]),
        "status": "在跑" if v["accounts"] else "待接入",
        "sub": f"{len(v['accounts'])} 个号 · 合计粉丝 {wan(v['total_fans'])}",
        "has": bool(v["accounts"]),
    } for v in ident.values()]
    content_identities = {
        v["key"]: {
            "key": v["key"], "name": v["name"],
            "platforms": v["platforms"], "total_fans": v["total_fans"],
            "accounts": [{
                k: a.get(k) for k in ("name", "platform", "no", "fans", "fans_at", "dir", "state")
            } for a in v["accounts"]],
        } for v in ident.values()
    }
    content_live_pack = {
        "at": cl.get("at", ""),
        "douyin": {"ok": bool(dy_live.get("ok")), "account": dy_live.get("account", ""),
                   "baseline": dy_live.get("baseline", 0),
                   "alerts": len(dy_live.get("alerts") or [])},
        "xhs": {"ok": bool(xhs_live.get("ok")), "account": xhs_live.get("account", ""),
                "baseline": xhs_live.get("baseline", 0),
                "alerts": len(xhs_live.get("alerts") or [])},
        "tencent": {"ok": bool(tx_live.get("ok")), "error": tx_live.get("error", "")},
        "mp": {"ok": bool(mp_live.get("ok")), "fans": mp_live.get("fans"),
               "last_post_at": mp_live.get("last_post_at")},
    }
    content_rail = []
    if cl.get("at"):
        content_rail.append({
            "at": cl["at"][11:16] if len(cl["at"]) >= 16 else cl["at"],
            "title": "数据刷新",
            "detail": (f"抖音中位 {dy_live.get('baseline', 0)} 播放 · "
                       f"小红书中位 {xhs_live.get('baseline', 0)} 播放 · "
                       f"告警 {len(dy_live.get('alerts') or [])} 条"),
        })
    if mp_live.get("ok"):
        content_rail.append({
            "at": (mp_live.get("at") or "")[11:16],
            "title": "公众号快照",
            "detail": f"{mp_live.get('fans', '—')} 粉 · 最近发文 {mp_live.get('last_post_at', '—')}",
        })
    content_rail.append({
        "at": "", "title": "账号清单",
        "detail": f"九离命理 {len(ident['mingli']['accounts'])} 个号 · 九离AI {len(ident['ai']['accounts'])} 个号",
    })

    try:
        media_n = sum(len(v["accounts"]) for v in ident.values())
    except Exception:  # noqa: BLE001
        media_n = len(content_items)

    # ---- 内容工具入口：外部应用挂在这里，点一下直接在面板里打开 ----
    content_items.append({
        "name": "混剪工具", "value": "MoneyPrinterTurbo",
        "sub": "D:/AI/MoneyPrinterTurbo · 点开启动",
        "url": "http://127.0.0.1:8501",
        "children": [
            {"name": "启动方式", "value": "本地点开", "sub": "D:/AI/MoneyPrinterTurbo/启动-MoneyPrinterTurbo.bat"},
            {"name": "能干什么", "value": "文案→成片", "sub": "自动搜素材、配音、字幕、合成竖屏短视频"},
            {"name": "协议", "value": "MIT", "sub": "可商用"},
        ],
    })

    # ---- FDE：项目 + 顾问费（顾问费属于 FDE 收入，不是店铺经营） ----
    fee = ecom.get("fee", "") if ok_e else ""
    fde_items = []
    for r in projects_raw:
        pa = projects.get(r["name"], {})
        kids = [
            {"name": "最近改动", "value": r["meta"], "sub": "按文件名日期"},
            {"name": "今日已处理", "value": str(len(pa.get("done_today", []))), "sub": "来自助理卡"},
            {"name": "未结待办", "value": str(len(pa.get("pending", []))), "sub": "等拍板"},
        ]
        if "客户B" in r["name"] and fee:
            kids.insert(0, {"name": "顾问费", "value": f"¥{fee}/月", "sub": "月度结算"})
        fde_items.append({
            "name": r["name"][:16], "value": r["meta"],
            "sub": f"今日处理 {len(pa.get('done_today', []))} · 待办 {len(pa.get('pending', []))}",
            "delta": "", "dir": "", "project": r["name"], "children": kids,
        })

    ml_items = []
    if ok_m:
        ml_items = [{"name": "客户档案", "value": f"{ml['total']}", "sub": "2026 年 · 已归档"}]
        if ml.get("rev_cur") is not None:
            mom = ml.get("rev_mom")
            ml_items.append({
                "name": "本月收入",
                "value": f"¥{ml['rev_cur']:,.0f}",
                "sub": f"{ml['rev_cur_month']} · 截至 {ml['rev_cur_day']} 日 · {ml['rev_cur_n']} 笔",
                "delta": (f"{mom:+.1f}%" if mom is not None else ""),
                "dir": "up" if (mom or 0) >= 0 else "dn",
            })
            ml_items.append({
                "name": "上月收入",
                "value": f"¥{ml['rev_prev']:,.0f}",
                "sub": f"{ml['rev_prev_month']} · {ml['rev_prev_n']} 笔",
            })
        ml_items += [
            {"name": f"{ml['month']}新增", "value": f"{ml['month_new']}", "sub": "建档客户"},
            {"name": "最近建档", "value": ml["newest_ago"], "sub": ml["newest"]},
        ]

    qc_f = qc_pack.get("facts", {}) if ok_qc else {}

    agents = [
        {
            "key": "ecom", "name": "电商 Agent", "icon": "▣", "color": "#ff8a3d",
            "ok": bool(ok_qc or ok_e),
            "value": f"¥{qc_f.get('cost', '—')}" if ok_qc else "—",
            "unit": "今日消耗" if ok_qc else "待接入",
            "meta": (f"净成交 ROI {qc_f.get('roi', '—')} · 渠道 1/4 有数（千川）"
                     if ok_qc else "等店铺数据接入"),
            "badge": ("观察期不报警", f"台账 {qc_f.get('days', '—')} 天" if ok_qc else "未接"),
            "items": ecom_items,
            "hint": "",
        },
        {
            "key": "content", "name": "内容 Agent", "icon": "▶", "color": "#3aa0ff",
            "ok": media_n > 0,
            "value": "2" if content_items else "—",
            "unit": "个内容身份" if content_items else "待接入",
            "meta": (f"{media_n} 个账号 · 实时 {cl.get('at', '')} · 中位 {dy_live.get('baseline', 0)} 播放"
                     if dy_live.get("ok")
                     else (f"{media_n} 个账号 · 人工快照截至 {(dy or {}).get('latest', '—')}"
                           if media_n else "等抖音/视频号数据")),
            "badge": ("每天 08:00 抓一次", f"疑似限流 {len(dy_live.get('alerts') or [])} 条"),
            "items": content_items or [{"name": "暂无在产内容", "value": "—", "sub": MEDIA.name}],
            "hint": "",
        },
        {
            "key": "fde", "name": "FDE Agent", "icon": "◇", "color": "#ff5c8a",
            "ok": ok_p,
            "value": str(len(projects_raw)) if ok_p else "—",
            "unit": "个项目在跑" if ok_p else "待接入",
            "meta": (f"顾问费 客户B ¥{fee}/月" if fee else "扫描 01-客户项目"),
            "badge": (f"派活 {len(todos)} 件", f"日报 {datetime.now():%H:%M}"),
            "items": fde_items,
            "hint": "",
        },
        {
            "key": "mingli", "name": "术数 Agent", "icon": "☯", "color": "#9b7bff",
            "ok": ok_m,
            "value": str(ml.get("total", "—")) if ok_m else "—",
            "unit": "位当前客户" if ok_m else "待接入",
            "meta": (f"{ml.get('month', '')}新增 {ml.get('month_new', '—')} 位 · 收入仅计客户转账"
                     if ok_m and ml.get("rev_cur") is not None
                     else (f"{ml.get('month', '')}新增 {ml.get('month_new', '—')} 位" if ok_m else note_m)),
            "badge": ("每晚沉淀", f"本月 {ml.get('month_new', 0)} 位" if ok_m else "入库 —"),
            "items": ml_items,
            "hint": "",
        },
        {
            "key": "asst", "name": "助理 Agent", "icon": "★", "color": "#ffc93c",
            "ok": ok_todo, "value": str(th_n or len(todos)) if ok_todo else "—",
            "unit": ("件事" if th_n else "件待拍板") if ok_todo else "待接入",
            "meta": (f"{len(todos)} 张卡已归并 · 我欠 {owed_n} 件" if th_n
                     else "data/todo.json") if ok_todo else note_t,
            "badge": ("每 10 分钟巡检", f"停摆 {stalled_n} 件" if stalled_n else "无停摆"),
            "items": asst_items,
            "hint": "",
        },
    ]

    _r = _scan_rounds()
    _mm = datetime.now().strftime("%m-%d")
    _ecom_acts = _ecom_writebacks_today()
    for _a in agents:                       # 每张卡也挂上「几轮 · 出手几次」
        _a["rounds"] = _r
        if _a.get("key") == "ecom":
            # 电商卡的条目是店铺行，没有日期，用「今日数据写回次数」当出手，别恒显示 0
            _a["acts"] = _ecom_acts
        else:
            # 没有「带日期的进展」这类数据源的卡，出手记成 None（界面显示 —），别用 0 冒充「没干活」
            _dated = sum(1 for _i in _a.get("items", []) if _mm in str(_i.get("sub", "")))
            _a["acts"] = _dated if _dated else None

    for t in todos:
        t["project_path"] = project_path(t.get("project", "")) if t.get("project") else ""


    for a in agents:
        for it in a["items"]:
            d = diagnose(a["key"], it)
            it["state"], it["issue"], it["action"] = d["s"], d["i"], d["a"]

    # 巡检健康度：超过 15 分钟没成功扫描，或有微信账号掉线，都算异常
    now = time.time()
    stats = mon.get("stats") or {}
    age_min = int((now - mon.get("last_ok", 0)) / 60) if mon.get("last_ok") else -1
    wx_acc = stats.get("wx_accounts") or {}
    wx_down = [k for k, v in wx_acc.items() if not v]
    health = {
        "ok": bool(mon.get("last_ok")) and age_min <= 15 and not wx_down,
        "age_min": age_min,
        "wx_down": len(wx_down),
        "wx_total": len(wx_acc),
        "error": (mon.get("last_error", "") if age_min >= 0 and age_min <= 30 else ""),
        "error_at": (mon.get("last_error_at", "") if age_min >= 0 and age_min <= 30 else ""),
    }

    try:
        import llmcfg
        model_txt = llmcfg.describe()
        model_ok = bool(llmcfg.resolve().get("token"))
    except Exception:  # noqa: BLE001
        model_txt, model_ok = "未接入", False

    monitor = {
        "wx_ok": health["wx_total"] - health["wx_down"],
        "wx_total": health["wx_total"],
        "note_ok": bool(stats.get("note_live")),
        "oa_ok": bool(stats.get("oa_ok")),
        "model": model_txt,
        "model_ok": model_ok,
        "at": stats.get("at", ""),
        "wx_list": _wx_list(stats),
    }

    # ---- 左右 Agent 侧栏：等你处理 / CEO 已处理 / 电商已处理 / 动作流 ----
    pending_rail = []
    _now_txt = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for t in ths:
        if (t.get("snooze_until") or "") > _now_txt:      # 「跳过」没过期的，先不占用你的注意力
            continue
        if t.get("level") in ("P0", "P1"):
            pending_rail.append({
                "tid": t.get("tid", ""),
                "at": (t.get("last_at") or "")[11:16],
                "title": t.get("title", ""),
                "project": t.get("project", ""),
                "level": t.get("level", ""),
                "next": t.get("next_step", ""),
                "owed": t.get("owed", ""),
            })
    pending_rail = pending_rail[:12]

    ceo_act = {"consolidate": "归并事项", "rename": "改名", "relevel": "调级",
               "skip": "跳过", "close": "结案", "done": "结案", "merge": "归并"}
    proj_of = {t.get("tid", ""): t.get("project", "") for t in ths}
    ceo_rail = []
    for x in _ceo_log():
        act = x.get("act") or ""
        detail = (x.get("detail") or "").strip()
        if detail.startswith("{"):
            try:
                j = json.loads(detail)
                if act == "consolidate":
                    detail = f"归并 {j.get('merged', 0)} 组 · 新增 {j.get('new', 0)} 条"
            except Exception:  # noqa: BLE001
                detail = detail[:60]
        ceo_rail.append({
            "at": x.get("ts") or "",
            "act": ceo_act.get(act, act),
            "detail": detail[:90],
            "project": proj_of.get(x.get("tid", ""), ""),
        })

    ecom_rail = []
    seen_ecom = set()
    for x in reversed(_client_a_feed(30)):
        msg = x.get("msg", "")
        title = ""
        if "写回完成" in msg:
            title = "千川账户日报刷新"
            detail = msg.split("：", 1)[-1][:120]
        elif "场景拆分" in msg:
            title = "场景拆分检查"
            detail = "接口异常，保留历史拆分" if ("不可用" in msg or "异常" in msg) else msg[:100]
        if not title or title in seen_ecom:
            continue
        seen_ecom.add(title)
        ecom_rail.append({"at": x["at"], "title": title, "detail": detail})
        if len(ecom_rail) >= 4:
            break
    if ok_qc:
        ecom_rail.append({"at": "", "title": "千川账户汇总",
                          "detail": f"今日消耗 ¥{qc_f.get('cost', 0)} · 净成交 ¥{qc_f.get('settle', 0)} · "
                                    f"净单 {qc_f.get('orders', 0)} · ROI {qc_f.get('roi', 0)}"})
    ecom_rail = ecom_rail[:5]

    # 待办页自己的动作：近 3 天已结案的待办
    now = datetime.now()
    cutoff = now - timedelta(days=3)
    todo_rail = []
    for t in todos_all:
        if not t.get("done"):
            continue
        da = t.get("done_at") or ""
        try:
            done_at = datetime.strptime(f"{now.year}-{da}", "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        if done_at < cutoff:
            continue
        todo_rail.append({
            "at": da[6:11] if len(da) >= 11 else da,
            "title": t.get("title", ""),
            "detail": f"已结案 · {t.get('project', '')}",
        })
    todo_rail = todo_rail[:8]

    # FDE 页自己的动作：今天各项目已处理的条目
    fde_rail = []
    for r in projects_raw:
        pa = projects.get(r["name"], {})
        for d in (pa.get("done_today") or []):
            da = d.get("done_at") or ""
            fde_rail.append({
                "at": da[11:16] if len(da) >= 16 else da,
                "title": r["name"],
                "detail": d.get("title", ""),
            })
    fde_rail = fde_rail[:8]
    if not fde_rail:
        for r in projects_raw[:6]:
            fde_rail.append({"at": "", "title": r["name"], "detail": f"最近改动 {r.get('meta', '')}"})

    # 术数页自己的动作：档案与收入状态
    mingli_rail = []
    if ok_m:
        mingli_rail.append({"at": "", "title": "客户档案",
                            "detail": f"{ml.get('total', '—')} 位 · 本月新增 {ml.get('month_new', '—')} 位"})
        if ml.get("rev_cur") is not None:
            mingli_rail.append({"at": "", "title": "本月收入",
                                "detail": f"¥{ml['rev_cur']:,.0f} · {ml.get('rev_cur_n', '—')} 笔"})
        mingli_rail.append({"at": "", "title": "最近建档", "detail": ml.get("newest", "—")})

    activity = []
    for x in ecom_rail[:4]:
        activity.append({"at": x.get("at", ""), "who": "电商", "title": x.get("title", ""),
                         "detail": x.get("detail", ""), "level": ""})
    for x in ceo_rail[:4]:
        activity.append({"at": x.get("at", ""), "who": "CEO", "title": x.get("act", ""),
                         "detail": x.get("detail", ""), "level": ""})
    for x in pending_rail[:3]:
        activity.append({"at": x.get("at", ""), "who": "你", "title": "待拍板 · " + x.get("title", ""),
                         "detail": x.get("next") or x.get("project", ""), "level": x.get("level", "")})
    activity.sort(key=lambda x: x.get("at") or "", reverse=True)

    rails = {"pending": pending_rail, "ceo": ceo_rail, "ecom": ecom_rail,
             "content": content_rail, "todo": todo_rail, "fde": fde_rail,
             "mingli": mingli_rail, "activity": activity}

    return {
        "projects": projects,
        "rails": rails,
        "contentShops": content_shops,
        "contentIdentities": content_identities,
        "contentLive": content_live_pack,
        "on_top": True,
        "monitor": monitor,
        "health": health,
        "time": datetime.now().strftime("%H:%M:%S"),
        "date": datetime.now().strftime("%m-%d"),
        "me": {"name": "庞恩浩", "short": "庞", "role": "创始人 · 拍板定方向",
               "pending": (th_n or len(todos)) if ok_todo else 0},
        "ceo": {"rounds": _scan_rounds(), "actions": _ceo_actions(),
                "log": _ceo_log(),
                "status": "正常", "interval": "每 10 分钟"},
        "agents": agents,
        "agents_online": sum(1 for a in agents if a["ok"]),
        "ecomShops": ecom_shops,
        "client_a": collect_client_a(),
        "todo": todos,
        "schedule": collect_schedule()[1],
        "benches": collect_workbenches()[1],
        "today": datetime.now().strftime("%Y-%m-%d"),
        "threads": [{"tid": t["tid"], "title": t["title"], "project": t["project"],
                     "level": t["level"], "stalled": t["stalled"], "n": t["n"],
                     "next_step": t["next_step"], "last_at": t["last_at"],
                     "first_at": t["first_at"], "owed": t.get("owed") or "",
                     "timeline": t["timeline"],
                     "sids": [x["sid"] for x in t["timeline"]]} for t in ths],
        "auto_closed": collect_auto_closed(),
        "rules": collect_rules(ths, th_err, len(todos)),
    }


if __name__ == "__main__":
    import io
    io.open("_test.json", "w", encoding="utf-8").write(
        json.dumps(build_payload(), ensure_ascii=False, indent=1))
    print("written _test.json")
