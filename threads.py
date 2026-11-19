# -*- coding: utf-8 -*-
"""把按天切的卡片，归并成「事项」。

卡片是证据层，不删不改；事项是呈现层。模型每轮巡检后跑一次归并，
判断每张还没归过的卡，是「已有某件事的进展」还是「另起一件」。

三条硬边界：
- 模型只许引用给它的卡片编号和事项编号，输出里出现别的编号一律丢弃。
- 模型挂了、输出坏了，事项层原地不动，面板退回按天列表，绝不把待办清空。
- 每次归并都写 thread_log，split() 能把一件事原样拆回卡片。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

import ai
import store

BASE = Path(__file__).parent
STATE = BASE / "data/auto_state.json"
CACHE = BASE / "data/thread_cache.json"

RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "P4": 4, "P5": 5}
# 停摆阈值：P0 急事两天没动就该催，P1 三天，其余不催
STALL_DAYS = {"P0": 2, "P1": 3}

SYS = """你是庞恩浩的助理。下面是他手上还没结的「事项」，和一批刚扫出来的「卡片」。

卡片是按天切的日志：同一件事聊了三天，就会有三张卡，每张都写"往来 N 条"。
你要判断每张卡属于哪件已有的事，还是该另起一件。

判断标准：
- 同一个客户/项目、指向同一个未结问题 → 同一件事，哪怕隔了几天、措辞不同。
- 同一个客户但不同的事（一个谈合作、一个催发票）→ 分开，别硬合并。
- 明显只是「进度往前推了一步」→ 一定归到已有那件事里。
- **同一个「会话」的卡，默认并进该会话已有的那件事**；只有当两张卡明显在说两件不相干的事时才另起。
  隔天同一会话又来了新卡，几乎都是同一件事往前推了一步，不要另开新事项。
- 拿不准就 NEW，宁可多开一件，不要合错。

严格输出 JSON，不要解释、不要代码块：
{"归并":[{"卡":["C1","C2"],"事":"T2"},{"卡":["C5"],"事":"NEW1"}],
 "新事项":[{"事":"NEW1","标题":"不超过18字","下一步":"不超过20字","级别":"P2"}],
 "刷新":[{"事":"T1","标题":"不超过18字","下一步":"不超过20字","我欠":"不超过16字"}]}

规则：
- 「卡」只能是下面列出的编号，不许编；同一件事的多张卡用数组一次写全。
- 「事」是已有事项编号，或 NEW1 / NEW2 这种新编号；新编号必须在「新事项」里给出标题。
- 同一件新的事，多张卡归到同一个 NEW 编号，不要一张卡开一件事。
- 每张卡最多出现一次。
- 「刷新」只列本轮挂上新卡的已有事项。标题写这件事**现在**到了哪一步，不是它当初怎么开始的；
  下一步写眼下最该做的那一个动作；我欠写我方还没兑现的话，已经办了的写空串。

示例：C1、C3 是上海科洋同一件事的两次往来，C2 是新冒出来的杭师大培训，
上海科洋那边刚把方案发出去了，标题就不能还叫"上海科洋跟进"：
{"归并":[{"卡":["C1","C3"],"事":"T1"},{"卡":["C2"],"事":"NEW1"}],
 "新事项":[{"事":"NEW1","标题":"杭师大培训排期","下一步":"确认开课日期","级别":"P4"}],
 "刷新":[{"事":"T1","标题":"科洋方案已发，等对方回","下一步":"周三前问反馈","我欠":"演示环境还没给"}]}"""


# ---------------- 基础 ----------------
def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _full_at(s: str) -> str:
    """卡片的 created_at 是 '10-27 16:42'，没有年份，补当年。"""
    s = (s or "").strip()
    if re.match(r"^\d{4}-", s):
        return s[:16]
    m = re.match(r"^(\d{2})-(\d{2})\s+(\d{2}):(\d{2})$", s)
    if m:
        y = datetime.now().year
        return f"{y}-{m.group(1)}-{m.group(2)} {m.group(3)}:{m.group(4)}"
    return _now()


def _days_since(at: str) -> int:
    try:
        d = datetime.strptime(at[:16], "%Y-%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        return 0
    return max(0, (datetime.now() - d).days)


def _best_level(a: str, b: str) -> str:
    if not a:
        return b or "P3"
    if not b:
        return a
    return a if RANK.get(a, 9) <= RANK.get(b, 9) else b


def _read_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _write_state(d: dict) -> None:
    tmp = dict(_read_state())
    tmp.update(d)
    tmp["_updated"] = _now()
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(tmp, ensure_ascii=False, indent=1), encoding="utf-8")


def auto_state() -> dict:
    return _read_state()


# ---------------- 读 ----------------
def open_cards(con) -> list[dict]:
    rows = con.execute(
        "SELECT sid,title,level,note,raw,source,project,created_at,due "
        "FROM cards WHERE done=0 AND skipped=0 ORDER BY created_at DESC").fetchall()
    keys = ("sid", "title", "level", "note", "raw", "source", "project", "created_at", "due")
    return [dict(zip(keys, r)) for r in rows]


def _links(con) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for tid, sid, role, at in con.execute(
            "SELECT l.tid,l.sid,l.role,l.at FROM thread_links l "
            "JOIN cards c ON c.sid=l.sid WHERE c.done=0 AND c.skipped=0"):
        out.setdefault(tid, []).append({"sid": sid, "role": role, "at": at})
    return out


def open_threads(con) -> list[dict]:
    links = _links(con)
    out = []
    for tid, project, title, level, next_step, first_at, last_at, owed, snooze in con.execute(
            "SELECT tid,project,title,level,next_step,first_at,last_at,owed,snooze_until FROM threads "
            "WHERE status='open' ORDER BY last_at DESC"):
        if tid not in links:          # 卡都被结案了，事项自然空
            continue
        out.append({"tid": tid, "project": project or "", "title": title or "",
                    "level": level or "P3", "next_step": next_step or "",
                    "first_at": first_at or "", "last_at": last_at or "",
                    "owed": owed or "", "snooze_until": snooze or "", "links": links[tid]})
    return out


def list_threads() -> list[dict]:
    """给面板用。带停摆天数、时间线、证据。"""
    con = store.conn()
    try:
        cards = {c["sid"]: c for c in open_cards(con)}
        out = []
        for t in open_threads(con):
            days = _days_since(t["last_at"])
            limit = STALL_DAYS.get(t["level"])
            stalled = days if (limit is not None and days >= limit) else 0
            timeline = []
            for lk in sorted(t["links"], key=lambda x: x["at"]):
                c = cards.get(lk["sid"])
                if not c:
                    continue
                timeline.append({
                    "name": f"{lk['at'][5:10]} {lk['role']}",
                    "value": c.get("level", ""),
                    "sub": (c.get("note") or c.get("title") or "")[:80],
                    "src": c.get("source", ""),           # 消息从哪个渠道来的，谁说的
                    "at": lk["at"],
                    "sid": lk["sid"],
                })
            out.append({**t, "stalled": stalled, "timeline": timeline,
                        "n": len(timeline)})
        return out
    finally:
        con.close()


# ---------------- 归并 ----------------
def _unlinked(con, cards: list[dict]) -> list[dict]:
    done = {r[0] for r in con.execute("SELECT sid FROM thread_links")}
    return [c for c in cards if c["sid"] not in done]


def _chat_key(title: str) -> str:
    """从卡片标题里取会话名：'承诺 · 💟 · 11-16 · …' → 💟；'齐老师🎀 · 11-15 · …' → 齐老师🎀。

    项目卡（[杭师大…]）和 OA 卡没有会话名，返回空串，交回给模型归并。
    """
    t = (title or "").strip()
    if not t or t.startswith("["):
        return ""
    parts = [p.strip() for p in t.split("·")]
    if not parts:
        return ""
    key = parts[1] if parts[0] == "承诺" and len(parts) > 1 else parts[0]
    # 单个 emoji（💟 这种）也是合法会话名，只挡日期残片
    if not key or re.fullmatch(r"\d{1,2}-\d{1,2}", key):
        return ""
    return key[:24]


def _auto_link(con, todo: list[dict], ths: list[dict], cards: list[dict]) -> tuple[int, list[dict]]:
    """同一会话、7 天内的新卡直接并回它的事项，不劳模型。

    2026-11-17 实证：齐老师 11-15 说「明天发你材料」、11-16 真发了，
    两张卡被模型拆成两件事，旧事项的「最后进展」永远停在 11-15。
    这里只在**该会话只有一个未结事项**时自动并，多义的情况仍交给模型。
    """
    by_sid = {c["sid"]: c for c in cards}
    chat_threads: dict[str, list[dict]] = {}
    for t in ths:
        keys = {_chat_key(by_sid[l["sid"]]["title"]) for l in t["links"]
                if l["sid"] in by_sid and by_sid[l["sid"]].get("title")}
        keys.discard("")
        for k in keys:
            chat_threads.setdefault(k, []).append(t)

    linked, rest, touched = 0, [], set()
    for c in todo:
        key = _chat_key(c.get("title") or "")
        cands = chat_threads.get(key) or []
        t = cands[0] if len(cands) == 1 else None
        if t and t.get("last_at") and _days_since(t["last_at"]) <= 7:
            con.execute("INSERT OR IGNORE INTO thread_links(tid,sid,role,at) VALUES(?,?,?,?)",
                        (t["tid"], c["sid"], "承诺" if c.get("due") else "进展",
                         _full_at(c.get("created_at"))))
            con.execute("UPDATE threads SET last_at=?,updated_at=? WHERE tid=?",
                        (_full_at(c.get("created_at")), _now(), t["tid"]))
            con.execute("INSERT INTO thread_log(ts,action,tid,detail) VALUES(?,?,?,?)",
                        (_now(), "autolink", t["tid"], f"并入同会话卡片 {c['sid']}"))
            linked += 1
            touched.add(t["tid"])
        else:
            rest.append(c)
    if linked:
        con.commit()
    return linked, rest


def _parse(raw: str) -> dict:
    if not raw:
        return {}
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {}
    s = re.sub(r",\s*([}\]])", r"\1", m.group(0))
    try:
        d = json.loads(s)
    except Exception:  # noqa: BLE001
        return {}
    return d if isinstance(d, dict) else {}


def _salvage(raw: str) -> list[dict]:
    """模型被 thinking 挤断时，JSON 是半截的。把每一条已经写全的对象抠出来。

    只认带「事」且能对上事项编号的完整对象，宁少不多——
    半截的字段不如不给，给了会把标题写成半句话。
    """
    out = []
    for m in re.finditer(r"\{[^{}]*\}", raw or ""):
        try:
            d = json.loads(re.sub(r",\s*([}\]])", r"\1", m.group(0)))
        except Exception:  # noqa: BLE001
            continue
        if isinstance(d, dict) and str(d.get("事") or ""):
            out.append(d)
    return out


def _new_tid(project: str, title: str) -> str:
    h = hashlib.md5(f"{project}|{title}".encode("utf-8")).hexdigest()[:10]
    return "t-" + h


def consolidate(force: bool = False) -> dict:
    """把没归过的未结卡片归到事项里。返回本轮统计。"""
    con = store.conn()
    try:
        cards = open_cards(con)
        todo = _unlinked(con, cards)
        if not todo:
            # 没有新卡要归并，但标题可能已经跟不上最后一条进展了——单独补一趟
            con.close()
            rt = refresh_titles()
            res = {"ok": True, "skipped": "没有待归并的卡", "retitle": rt.get("refreshed", 0),
                   "at": _now()}
            _write_state({"consolidate": {**res, "merged": 0, "new": 0}})
            return res

        ths = open_threads(con)
        n_auto, todo = _auto_link(con, todo, ths, cards)
        if n_auto:
            ths = open_threads(con)
            if not todo:
                con.close()
                rt = refresh_titles()
                res = {"ok": True, "autolink": n_auto, "retitle": rt.get("refreshed", 0),
                       "merged": n_auto, "new": 0, "at": _now()}
                _write_state({"consolidate": res})
                return res
        tline = "\n".join(
            f"- {t['tid']} | {t['project'] or '未归类'} | {t['title']} | "
            f"最后 {t['last_at'][5:10]} | {len(t['links'])} 条进展" for t in ths) or "（暂无）"
        cline = "\n".join(
            f"- C{i + 1} | {c.get('project') or c.get('source') or '未归类'} | "
            f"会话 {_chat_key(c.get('title') or '') or '—'} | "
            f"{(c.get('created_at') or '')[:5]} | {(c.get('title') or '')[:30]} | "
            f"{(c.get('note') or c.get('title') or '')[:70]}"
            for i, c in enumerate(todo))

        user = f"【已有事项】\n{tline}\n\n【待归并卡片】\n{cline}"
        # max_tokens 留足：这个模型会先吐一段 thinking，给少了正文就被挤没
        raw = ai.chat(SYS, user, timeout=120, max_tokens=2600)
        out = _parse(raw)
        if not out:
            raw = ai.chat(SYS, user + "\n\n（直接输出 JSON，不要思考）", timeout=120, max_tokens=2600)
            out = _parse(raw)
        if not out:
            res = {"ok": False, "error": "模型输出无法解析", "at": _now()}
            _write_state({"consolidate": {**res, "merged": 0, "new": 0}})
            return res

        idx = {f"C{i + 1}": c for i, c in enumerate(todo)}
        valid_tid = {t["tid"] for t in ths}
        news = {str(n.get("事") or ""): n for n in (out.get("新事项") or [])
                if isinstance(n, dict) and n.get("事")}

        merged = created = 0
        touched: list[str] = []
        for row in (out.get("归并") or []):
            if not isinstance(row, dict):
                continue
            keys = row.get("卡")
            keys = [keys] if isinstance(keys, str) else list(keys or [])
            tk = str(row.get("事") or "")
            group = [idx[k] for k in (str(x) for x in keys) if k in idx]
            if not group:                     # 模型编的编号，整行丢弃
                continue
            if tk in valid_tid:
                tid = tk
            elif tk.startswith("NEW"):
                spec = news.get(tk) or {}
                first = group[0]
                title = str(spec.get("标题") or first.get("title") or "")[:24]
                tid = _new_tid(first.get("project") or "", title)
                existed = con.execute("SELECT 1 FROM threads WHERE tid=?", (tid,)).fetchone()
                con.execute(
                    "INSERT INTO threads(tid,project,title,level,status,next_step,"
                    "first_at,last_at,created_at,updated_at,model,title_at) "
                    "VALUES(?,?,?,?,'open',?,?,?,?,?,'',?) ON CONFLICT(tid) DO NOTHING",
                    (tid, first.get("project") or "", title, spec.get("级别") or first["level"],
                     str(spec.get("下一步") or ""), _full_at(first.get("created_at")),
                     _full_at(first.get("created_at")), _now(), _now(), _now()))
                if not existed:
                    created += 1
            else:
                continue
            for card in group:
                role = "承诺" if card.get("due") else "进展"
                at = _full_at(card.get("created_at"))
                cur = con.execute(
                    "INSERT OR IGNORE INTO thread_links(tid,sid,role,at) VALUES(?,?,?,?)",
                    (tid, card["sid"], role, at))
                merged += cur.rowcount or 0
            touched.append(tid)

        # 本轮挂上新卡的事项，标题/下一步/我欠一律按最新进展重写。
        # 这三样只写在建事项那一天，就是用户说的"短语一成不变"——事项往前走了，标签还停在起点。
        refreshed = _apply_refresh(con, out.get("刷新") or [], set(touched))

        _refresh(con)
        con.execute("INSERT INTO thread_log(ts,action,tid,detail) VALUES(?,?,?,?)",
                    (_now(), "consolidate", "-",
                     json.dumps({"merged": merged, "new": created, "refreshed": refreshed},
                                ensure_ascii=False)))
        con.commit()
        n_threads = len(open_threads(con))
        # 归并那一趟模型漏掉的、以及本来就没新卡的过期事项，再从过期队列里补一遍
        pending = _stale(con)
        con.close()
        if pending:
            refreshed += refresh_titles(pending).get("refreshed", 0)
        res = {"ok": True, "merged": merged, "new": created, "refreshed": refreshed,
               "threads": n_threads, "at": _now()}
        _write_state({"consolidate": res})
        try:
            res["rename"] = rename()
        except Exception:  # noqa: BLE001
            pass
        return res
    finally:
        con.close()


def _apply_refresh(con, rows: list, allowed: set[str]) -> int:
    """把模型给的「标题 / 下一步 / 我欠」写回事项。

    只认本轮真的挂上新卡的事项（allowed），模型报了别的一律丢——
    否则一次跑偏能把整列待办的标签全改掉。
    """
    n = 0
    for r in rows:
        if not isinstance(r, dict):
            continue
        tid = str(r.get("事") or "")
        if tid not in allowed:
            continue
        cur = con.execute("SELECT title,next_step,owed FROM threads WHERE tid=?",
                          (tid,)).fetchone()
        if not cur:
            continue
        title = (str(r.get("标题") or "").strip() or cur[0] or "")[:24]
        step = (str(r.get("下一步") or "").strip() or cur[1] or "")[:30]
        owed = str(r.get("我欠") or "").strip()[:24]   # 空串是有意义的：说明这笔还完了
        if (title, step, owed) == (cur[0] or "", cur[1] or "", cur[2] or ""):
            # 说法没变也得记一笔：不然它永远算"过期"，每轮巡检都白调一次模型
            con.execute("UPDATE threads SET title_at=? WHERE tid=?", (_now(), tid))
            continue
        con.execute("UPDATE threads SET title=?,next_step=?,owed=?,title_at=?,updated_at=? WHERE tid=?",
                    (title, step, owed, _now(), _now(), tid))
        con.execute("INSERT INTO thread_log(ts,action,tid,detail) VALUES(?,?,?,?)",
                    (_now(), "refresh", tid,
                     json.dumps({"从": [cur[0], cur[1], cur[2]],
                                 "到": [title, step, owed]}, ensure_ascii=False)))
        n += 1
    return n


TITLE_SYS = """你是庞恩浩的助理。下面每件「事项」都有个旧标题，和它最近的几条往来。
旧标题是上一次写的，可能已经跟不上进展了。

你要按**最新的那条往来**重写三样：
- 标题：**一律以主体开头，再接现在到了哪一步**，不超过18字。「张洁批件已发」合格，
  「已发出」「暂无进度」这种不合格——离了上下文看不出说的是谁。写现在，不写起点。
  主体从项目名、旧标题、方括号里的卡片标题（形如「承诺 · 张洁 · 10-27」，
  点号后面那个就是对方）、往来正文里出现的名字中取。
  **旧标题里已有的人名或项目名必须保留**，只换它后面描述进展的那半句。
- 下一步：眼下最该做的那一个动作，不超过20字，同样要带主体。
- 我欠：我方还没兑现的话（答应了要发、要给、要问回话而没做的），不超过16字；
  已经办了、或者我方本来就不欠的，写空串。

别扭的意思别硬造。往来里看不出变化，就把旧标题原样抄回来。

严格输出 JSON，不要解释、不要代码块：
{"刷新":[{"事":"T1","标题":"...","下一步":"...","我欠":""}]}

规则：
- 「事」原样抄下面列出的 T1 / T2 编号，不许自己另编或改写。
- 每件事都在「刷新」里给一条，不许漏。"""


def _stale(con) -> list[str]:
    """最后一条进展比标题新 = 标签停在旧说法上。没写过的（title_at 为空）一律算过期。"""
    return [r[0] for r in con.execute(
        "SELECT tid FROM threads WHERE status='open' "
        "AND COALESCE(last_at,'') > COALESCE(title_at,'') ORDER BY last_at DESC")]


def refresh_titles(only: list[str] | None = None) -> dict:
    """把跟不上的标题 / 下一步 / 我欠重写一遍。

    只对过期的事项调模型：没新进展的不动，省得每轮都把标签洗一遍，
    用户看到的是"它自己在跟着事情走"，不是"每次刷新都换一种说法"。
    """
    con = store.conn()
    try:
        stale = only if only is not None else _stale(con)
        if not stale:
            return {"ok": True, "skipped": "没有跟不上的事项", "refreshed": 0}
        cards = {c["sid"]: c for c in open_cards(con)}
        lines, alias = [], {}
        for tid in stale:
            row = con.execute("SELECT title,next_step,owed FROM threads WHERE tid=?",
                              (tid,)).fetchone()
            if not row:
                continue
            links = con.execute(
                "SELECT l.sid,l.at FROM thread_links l JOIN cards c ON c.sid=l.sid "
                "WHERE l.tid=? AND c.done=0 AND c.skipped=0 ORDER BY l.at DESC LIMIT 4",
                (tid,)).fetchall()
            body = "\n".join(
                f"    {(cards.get(sid) or {}).get('created_at', '')[:5]} "
                f"[{(cards.get(sid) or {}).get('title', '')[:24]}] "
                f"{((cards.get(sid) or {}).get('note') or '')[:80]}"
                for sid, _at in links) or "    （没有挂卡片）"
            # 给模型的是 T1/T2 这种短号，不是 tid：让它照抄 tid 它总会自己编一套
            key = f"T{len(alias) + 1}"
            alias[key] = tid
            proj = con.execute("SELECT project FROM threads WHERE tid=?", (tid,)).fetchone()[0] or ""
            lines.append(f"- {key} | 项目「{proj or '未归类'}」| 现在叫「{row[0] or ''}」"
                         f"| 下一步「{row[1] or ''}」| 我欠「{row[2] or ''}」\n{body}")
        if not lines:
            return {"ok": True, "skipped": "过期事项没挂着卡", "refreshed": 0}

        user = "【事项】\n" + "\n".join(lines)
        raw = ai.chat(TITLE_SYS, user, timeout=120, max_tokens=2600)
        out = _parse(raw)
        if not out:
            raw = ai.chat(TITLE_SYS, user + "\n\n（直接输出 JSON，不要思考）",
                          timeout=90, max_tokens=1200)
            out = _parse(raw)
        if not out.get("刷新"):
            # 输出被 thinking 挤断时 JSON 是半截的，逐个抠完整的对象救回来
            salvaged = _salvage(raw)
            out = {"刷新": salvaged} if salvaged else out
        if not out.get("刷新"):
            return {"ok": False, "error": "模型输出无法解析", "refreshed": 0}

        rows = []
        for r in (out.get("刷新") or []):
            if not isinstance(r, dict):
                continue
            tid = alias.get(str(r.get("事") or "").strip())
            if tid:
                rows.append({**r, "事": tid})
        n = _apply_refresh(con, rows, set(stale))
        con.execute("INSERT INTO thread_log(ts,action,tid,detail) VALUES(?,?,?,?)",
                    (_now(), "retitle", "-",
                     json.dumps({"refreshed": n, "stale": len(stale)}, ensure_ascii=False)))
        con.commit()
        return {"ok": True, "refreshed": n, "stale": len(stale), "at": _now()}
    finally:
        con.close()


def _refresh(con) -> None:
    """按挂上来的卡，重算事项的级别与最后时间。

    标题、下一步、我欠不在这里动——那三样是模型按最新进展重写的，
    每轮按卡片算回去就等于把"事项往前走了一步"这件事抹掉了。
    """
    for tid, lock in con.execute("SELECT tid,COALESCE(lv_lock,0) FROM threads WHERE status='open'"):
        rows = con.execute(
            "SELECT c.level,c.created_at FROM thread_links l JOIN cards c ON c.sid=l.sid "
            "WHERE l.tid=? AND c.done=0 AND c.skipped=0", (tid,)).fetchall()
        if not rows:
            con.execute("UPDATE threads SET status='done',updated_at=? WHERE tid=?",
                        (_now(), tid))
            continue
        lv = "P5"
        for r in rows:
            lv = _best_level(lv, r[0] or "P3")
        times = sorted(_full_at(r[1]) for r in rows)
        if lock:                       # 人说过"改成 P3"，就别再按卡片算回 P2
            con.execute("UPDATE threads SET first_at=?,last_at=?,updated_at=? WHERE tid=?",
                        (times[0], times[-1], _now(), tid))
        else:
            con.execute("UPDATE threads SET level=?,first_at=?,last_at=?,updated_at=? WHERE tid=?",
                        (lv, times[0], times[-1], _now(), tid))


def set_level(tid: str, level: str) -> bool:
    """手动定级别，并上锁，防止下一轮归并又按卡片算回去。"""
    if level not in RANK:
        return False
    con = store.conn()
    try:
        cur = con.execute("UPDATE threads SET level=?,lv_lock=1,updated_at=? WHERE tid=?",
                          (level, _now(), tid))
        con.execute("INSERT INTO thread_log(ts,action,tid,detail) VALUES(?,?,?,?)",
                    (_now(), "level", tid, level))
        con.commit()
        return (cur.rowcount or 0) > 0
    finally:
        con.close()


SYS_RENAME = """你在给庞恩浩的待办改标题、校准级别。

给你的每件事，都有编号、当前标题、级别，和它下面真实的往来记录。
要做两件事：

1. 重写标题：必须说清这件事到底是什么，用记录里出现的人名、事由、当下的卡点。
   - 不超过 14 字，不要出现「往来 N 条」「待查看」这种从日志里抄来的空话。
   - 记录里没出现过的事，不许写进标题。
2. 校准级别：按「现在」的紧急程度重判，不按历史最高。
   - P0 今天不动就出事（截止今天、有人等回话、钱/合同在走）
   - P1 本周内要办；P2 排得开；P3 记着就行；P4 纯记录；P5 可以丢
   - 只是别人随口提一句、或者只是发了个文件让你看看 → P3 或 P4，不要给 P0

严格输出 JSON，不要解释：
{"事项":[{"id":"t-xxx","标题":"不超过14字","级别":"P3","理由":"不超过20字"}]}"""


def rename(force: bool = False) -> dict:
    """按最新往来重写事项标题、校准级别。级别被人手动改过的（lv_lock）不动。"""
    ths = list_threads()
    if not ths:
        return {"ok": True, "skipped": "没有事项"}
    st = _read_state().get("rename") or {}
    if not force and st.get("at", "")[:13] == _now()[:13]:
        return {"ok": True, "skipped": "这一小时已经改过"}
    lines = []
    for t in ths:
        ev = " / ".join(f"{x['name']} {x['sub'][:34]}" for x in t["timeline"][-4:])
        lines.append(f"- {t['tid']} | 现名：{t['title']} | 现级别：{t['level']} | 记录：{ev}")
    user = "【事项】\n" + "\n".join(lines)
    raw = ai.chat(SYS_RENAME, user, timeout=120, max_tokens=2400)
    out = _parse(raw)
    if not out or not out.get("事项"):
        raw = ai.chat(SYS_RENAME, user + "\n\n（只输出 JSON）", timeout=120, max_tokens=2400)
        out = _parse(raw)
    if not out or not out.get("事项"):
        _write_state({"rename": {"ok": False, "at": _now(), "error": "输出无法解析"}})
        return {"ok": False, "error": "模型输出无法解析"}
    con = store.conn()
    n_t = n_l = 0
    try:
        for row in out["事项"]:
            tid = str(row.get("id") or "")
            th = next((x for x in ths if x["tid"] == tid), None)
            if not th:
                continue
            title = str(row.get("标题") or "").strip()[:24]
            lv = str(row.get("级别") or "").strip().upper()
            lock = con.execute("SELECT COALESCE(lv_lock,0) FROM threads WHERE tid=?",
                               (tid,)).fetchone()
            lock = bool(lock and lock[0])
            if title and title != th["title"]:
                con.execute("UPDATE threads SET title=?,updated_at=? WHERE tid=?", (title, _now(), tid))
                con.execute("INSERT INTO thread_log(ts,action,tid,detail) VALUES(?,?,?,?)",
                            (_now(), "rename", tid, f"{th['title']} -> {title}"))
                n_t += 1
            if lv in RANK and not lock and lv != th["level"]:
                con.execute("UPDATE threads SET level=?,updated_at=? WHERE tid=?", (lv, _now(), tid))
                con.execute("INSERT INTO thread_log(ts,action,tid,detail) VALUES(?,?,?,?)",
                            (_now(), "relevel", tid, f"{th['level']} -> {lv}"))
                n_l += 1
        con.commit()
    finally:
        con.close()
    res = {"ok": True, "renamed": n_t, "releveled": n_l, "at": _now()}
    _write_state({"rename": res})
    return res


def snooze(tid: str, hours: int = 24) -> dict:
    """跳过：这件事先不处理，过 N 小时再回到「等你处理」。事项不结案，卡不动。"""
    from datetime import datetime, timedelta
    until = (datetime.now() + timedelta(hours=max(1, int(hours or 24)))).strftime("%Y-%m-%d %H:%M:%S")
    con = store.conn()
    try:
        con.execute("UPDATE threads SET snooze_until=?, updated_at=? WHERE tid=?", (until, _now(), tid))
        con.execute("INSERT INTO thread_log(ts,action,tid,detail) VALUES(?,?,?,?)",
                    (_now(), "snooze", tid, f"到 {until}"))
        con.commit()
    finally:
        con.close()
    return {"ok": True, "until": until}


def close(tid: str, skip: bool = False) -> dict:
    """整件事结案 / 跳过：把它挂着的卡按同样方式处理一遍。"""
    con = store.conn()
    try:
        sids = [r[0] for r in con.execute("SELECT sid FROM thread_links WHERE tid=?", (tid,))]
    finally:
        con.close()
    import monitor
    n = 0
    for s in sids:
        try:
            if (monitor.skip if skip else monitor.complete)(s):
                n += 1
        except Exception:  # noqa: BLE001
            continue
    con = store.conn()
    try:
        con.execute("UPDATE threads SET status='done',updated_at=? WHERE tid=?", (_now(), tid))
        con.execute("INSERT INTO thread_log(ts,action,tid,detail) VALUES(?,?,?,?)",
                    (_now(), "skip" if skip else "close", tid, f"{n} 张卡"))
        con.commit()
    finally:
        con.close()
    return {"ok": n > 0, "cards": n}


def split(tid: str) -> bool:
    """把一件事拆回卡片：删掉关联，事项归档，卡片回到按天列表。"""
    con = store.conn()
    try:
        n = con.execute("SELECT COUNT(*) FROM thread_links WHERE tid=?", (tid,)).fetchone()[0]
        con.execute("DELETE FROM thread_links WHERE tid=?", (tid,))
        con.execute("UPDATE threads SET status='done',updated_at=? WHERE tid=?", (_now(), tid))
        con.execute("INSERT INTO thread_log(ts,action,tid,detail) VALUES(?,?,?,?)",
                    (_now(), "split", tid, f"拆回 {n} 张卡"))
        con.commit()
        return n > 0
    finally:
        con.close()




if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(json.dumps(consolidate(force=True), ensure_ascii=False, indent=1))
    for t in list_threads():
        print(f"[{t['level']}] {t['title']} · {t['n']} 条 · 停 {t['stalled']} 天")
