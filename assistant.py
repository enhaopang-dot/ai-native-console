# -*- coding: utf-8 -*-
"""面板里的对话入口：用户说话，模型决定改什么，代码执行。

只开放白名单动作，全部落库留痕（chat_log），可回溯。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import llmcfg
import store

BASE = Path(__file__).parent
RULES = BASE / "data/rules.json"
STEPS = BASE / "data/next_steps.json"
PROMPT = BASE / "data/judge_prompt.md"

SYS = """你是庞恩浩的中台助理，可以帮他调整这个中台。

他说的每句话，你要判断：是提问，还是要改东西。

可执行动作（只能从这里选）：
- card_done     结案一张卡       {"op":"card_done","sid":"..."}
- card_skip     跳过一张卡       {"op":"card_skip","sid":"..."}
- card_level    改级别           {"op":"card_level","sid":"...","level":"P0~P5"}
- card_note     改备注/结论      {"op":"card_note","sid":"...","text":"..."}
- card_add      新建一张卡       {"op":"card_add","title":"...","level":"P2","note":"..."}
- ignore_add    不再收录某类     {"op":"ignore_add","text":"主播招聘"}
- prompt_add    调整判定口径     {"op":"prompt_add","text":"当天到期的承诺一律 P0"}
- step_add      给项目加未结事项 {"op":"step_add","project":"项目名","text":"..."}
- sched_cancel  取消一条日程     {"op":"sched_cancel","match":"11-09 客户B"}
- sched_add     新建一条日程     {"op":"sched_add","title":"...","start":"YYYY-MM-DD","end":"YYYY-MM-DD","place":"...","color":"#3aa0ff"}
- wx_search     去微信里搜原文   {"op":"wx_search","text":"郑庆博 明天 两点"}（可以只用这一次，拿到结果再回答）

规则：
- 拿不准就不改，只回答。宁可不做，不要瞎改。
- 改级别要有理由，写进 reply。
- 他如果说"以后这类别再放了"，用 ignore_add 或 prompt_add，不要一张张点。
- 一次最多 3 个动作。
- 问到具体时间、地点、某人的原话而你不确定时，直接返回 wx_search 动作去查；
  **不要在 reply 里说"我去查""稍后告诉你"**——你要么已经查到，要么这一轮就去查。
- 有「刚才聊过」就接着上文说，不要让用户重复。

严格输出 JSON，不要解释：
{"reply":"对他说的话，不超过60字","actions":[...]}"""


def _cards_for_prompt(limit: int = 8) -> str:
    """带原文片段：只给结论的话，模型只会回"看不到原始记录"——用户就是这么被噎住的。"""
    rows = store.q(
        "SELECT sid,level,created_at,title,note,raw,link_txt FROM cards WHERE done=0 AND skipped=0 "
        "ORDER BY created_at DESC LIMIT ?", params=(limit,))
    out = []
    for sid, lv, at, t, n, raw, lt in rows:
        out.append(f"- {sid} | {lv} | {at} | {t}")
        out.append(f"    结论：{(n or '')[:60]}")
        if raw:
            out.append(f"    原文：{raw[:260]}")
        if lt:
            out.append(f"    链接内容：{lt[:200]}")
    return "\n".join(out)


def _wx_search(kw: str, days: int = 7) -> str:
    """去微信里搜原文。模型说"看不到原始记录"时，用它补。"""
    import wxreader
    since = datetime.now().timestamp() - days * 86400
    seen, lines = set(), []
    for term in [x for x in re.split(r"[\s·,，]+", kw or "") if len(x) >= 2][:3]:
        for acc in wxreader.accounts():
            try:
                ms = wxreader.read_account(acc, since, 300)
            except Exception:  # noqa: BLE001
                continue
            for m in ms:
                if term in (m["text"] or "") or term in (m["chat_name"] or ""):
                    k = (m["chat"], m["ts"], (m["text"] or "")[:20])
                    if k in seen:
                        continue
                    seen.add(k)
                    who = "我" if m["is_self"] else m["sender"]
                    lines.append(f"{datetime.fromtimestamp(m['ts']):%m-%d %H:%M} "
                                 f"{m['chat_name'][:14]} {who}: {(m['text'] or '')[:60]}")
    lines.sort()
    return "\n".join(lines[-15:]) or "（没搜到）"


def _parse(raw: str) -> dict:
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return {}
    s = re.sub(r",\s*([}\]])", r"\1", m.group(0))
    try:
        d = json.loads(s)
    except Exception:  # noqa: BLE001
        return {}
    return d if isinstance(d, dict) and d.get("reply") is not None else {}



def _schedule_path():
    from pathlib import Path as _P
    return _P(__file__).parent / "data/schedule.json"


def _sched_cancel(match: str) -> str:
    """按关键词取消日程。匹不到就原样返回空，不乱删。"""
    import json as _json
    from pathlib import Path as _P
    p = _schedule_path()
    try:
        d = _json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return ""
    keys = [k for k in match.replace("-", " ").split() if k]
    hit, keep = [], []
    for it in d.get("items", []):
        blob = f"{it.get('title','')} {it.get('start','')} {it.get('place','')} {it.get('note','')}"
        if keys and all(k in blob for k in keys):
            hit.append(it.get("title", ""))
        else:
            keep.append(it)
    if not hit:
        return ""
    d["items"] = keep
    _P(p).write_text(_json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    return "sched_cancel:" + "、".join(hit)


def _sched_add(act: dict) -> str:
    """新建日程。日期必须真实存在，写空就排到待定，不猜。"""
    import json as _json
    p = _schedule_path()
    try:
        d = _json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        d = {"items": []}
    title = str(act.get("title") or "").strip()
    if not title:
        return ""
    d.setdefault("items", []).append({
        "title": title,
        "project": act.get("project") or "",
        "start": act.get("start") or "",
        "end": act.get("end") or act.get("start") or "",
        "place": act.get("place") or "",
        "color": act.get("color") or "#3aa0ff",
        "note": act.get("note") or "对话里加的",
    })
    p.write_text(_json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    return "sched_add:" + title


def _apply(act: dict) -> str:
    op = act.get("op", "")
    try:
        if op == "sched_cancel":
            return _sched_cancel(str(act.get("match") or act.get("text") or ""))
        if op == "sched_add":
            return _sched_add(act)
        if op in ("card_done", "card_skip", "card_level", "card_note"):
            sid = act.get("sid", "")
            if not sid:
                return ""
            con = store.conn()
            now = datetime.now().strftime("%m-%d %H:%M")
            if op == "card_done":
                con.execute("UPDATE cards SET done=1, done_at=? WHERE sid=?", (now, sid))
            elif op == "card_skip":
                con.execute("UPDATE cards SET skipped=1, skipped_at=? WHERE sid=?", (now, sid))
            elif op == "card_level":
                lv = act.get("level", "")
                if lv not in ("P0", "P1", "P2", "P3", "P4", "P5"):
                    con.close()
                    return ""
                con.execute("UPDATE cards SET level=?, updated_at=? WHERE sid=?", (lv, now, sid))
            else:
                con.execute("UPDATE cards SET note=?, updated_at=? WHERE sid=?",
                            (act.get("text", ""), now, sid))
            con.commit()
            con.close()
            return op

        if op == "card_add":
            con = store.conn()
            store.upsert(con, [{
                "sid": "manual-" + datetime.now().strftime("%Y%m%d%H%M%S"),
                "title": act.get("title", "手工添加"),
                "level": act.get("level", "P2"),
                "note": act.get("note", ""),
                "source": "对话添加",
                "created_at": datetime.now().strftime("%m-%d %H:%M"),
            }])
            con.close()
            return op

        if op == "ignore_add":
            t = act.get("text", "").strip()
            if not t:
                return ""
            d = json.loads(RULES.read_text(encoding="utf-8"))
            if t not in d.setdefault("ignore", []):
                d["ignore"].append(t)
                RULES.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
            return op

        if op == "prompt_add":
            t = act.get("text", "").strip()
            if not t:
                return ""
            with PROMPT.open("a", encoding="utf-8") as f:
                f.write(f"\n- {t}\n")
            return op

        if op == "step_add":
            proj, t = act.get("project", "").strip(), act.get("text", "").strip()
            if not (proj and t):
                return ""
            d = json.loads(STEPS.read_text(encoding="utf-8"))
            d.setdefault("projects", {}).setdefault(proj, []).append({
                "id": "chat-" + datetime.now().strftime("%H%M%S"),
                "text": t, "done": False, "keywords": [], "auto": False,
                "done_at": "", "evidence": "",
            })
            STEPS.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
            return op
    except Exception as e:  # noqa: BLE001
        return f"err:{op}:{e}"
    return ""


PROJ_ROOT = Path("D:/Claude/项目")
# 说话提到这些词，就去对应项目目录里捞资料，别只靠待办卡片答
PROJ_HINTS = [
    (re.compile(r"念念|孩子|儿子|医院|复查|挂号|抽动|家长会|夏老师|鲍老师"),
     PROJ_ROOT / "01 参谋长/08-家族/念念"),
    (re.compile(r"杭师大|就业班|成班|公开课"), PROJ_ROOT / "13 FDE/02-业务与获客/20261107-杭师大学校培训"),
    (re.compile(r"客户C|锦哲|千问|益然恩"), PROJ_ROOT / "13 FDE/01-客户项目/20261107-上海客户CFDE项目"),
    (re.compile(r"千川|客户A"), PROJ_ROOT / "14 内容管理/01-抖音电商API接入"),
    (re.compile(r"断食"), PROJ_ROOT / "01 参谋长/05-学习规划/身体提升/36小时断食"),
    (re.compile(r"客户B"), PROJ_ROOT / "13 FDE/01-客户项目/20260721-客户B家居"),
]


def _recall(text: str, limit: int = 800) -> str:
    """从项目目录的 Markdown 里捞与这句话最相关的片段。"""
    try:
        import context
    except Exception:  # noqa: BLE001
        return ""
    # \u7528\u4e8c\u5b57\u6ed1\u7a97\u505a\u5173\u952e\u8bcd\uff1a\u6574\u53e5\u5207\u8bcd\u5bb9\u6613\u5207\u51fa\u300c\u53bb\u533b\u9662\u590d\u300d\u8fd9\u79cd\u67e5\u4e0d\u5230\u7684\u4e32
    zh = "".join(re.findall(r"[\u4e00-\u9fa5]", text))
    words = {zh[i:i + 2] for i in range(len(zh) - 1)}
    stop = {"\u4ec0\u4e48", "\u600e\u4e48", "\u8fd9\u4e2a", "\u90a3\u4e2a", "\u660e\u5929", "\u4eca\u5929", "\u4e00\u4e0b", "\u6211\u4eec", "\u4ed6\u4eec", "\u8981\u5e26", "\u9700\u8981"}
    words = [w for w in words if w not in stop]
    hits = []
    for pat, path in PROJ_HINTS:
        if not pat.search(text) or not path.exists():
            continue
        for name, body in context.corpus(str(path)):
            score = sum(body.count(w) for w in words)
            if score:
                hits.append((score, name, body))
    hits.sort(key=lambda x: -x[0])
    out, used = [], 0
    for score, name, body in hits:
        seg = re.sub(r"\s+", " ", body)[:400]
        if not seg:
            continue
        out.append(f"〔{name}〕{seg}")
        used += len(seg)
        if used >= limit or len(out) >= 3:
            break
    return "\n".join(out)


def _schedule_brief() -> str:
    """把最近的日程塞进上下文，问'明天有什么'能直接答。"""
    try:
        d = json.loads((BASE / "data/schedule.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return ""
    today = datetime.now().strftime("%Y-%m-%d")
    rows = [i for i in d.get("items", []) if (i.get("end") or i.get("start") or "") >= today]
    rows.sort(key=lambda x: x.get("start") or "9999")
    return "\n".join(f"{i.get('start')} {i.get('title','')} {i.get('note','')}" for i in rows[:10])


def handle(text: str, sid: str = "") -> dict:
    """返回 {reply, done:[...]}"""
    ctx = _cards_for_prompt()
    recall = _recall(text)
    sched = _schedule_brief()
    hist = ""
    try:
        rows = store.q("SELECT user,reply FROM chat_log ORDER BY id DESC LIMIT 3")
        for u, r in reversed(rows):
            hist += f"他：{u}\n你：{r}\n"
    except Exception:  # noqa: BLE001
        pass
    user = (f"【刚才聊过】\n{hist}\n" if hist else "") + \
           (f"【近期日程】\n{sched}\n\n" if sched else "") + \
           (f"【项目资料摘录】\n{recall}\n（只作背景，别据此编日期和数字）\n\n" if recall else "") + \
           (f"【当前待办】\n{ctx}\n\n" if ctx else "") + \
           (f"【他正指着的卡】{sid}\n\n" if sid else "") + \
           f"【他说】{text}"

    out = {}
    for _ in range(3):
        raw = llmcfg.chat(SYS, user, timeout=60, max_tokens=700)
        out = _parse(raw) or {"reply": raw[:120] or "我没听懂，再说一遍？", "actions": []}
        want = [a for a in out.get("actions") or [] if a.get("op") == "wx_search"]
        if not want:
            break
        user += "\n\n【微信原文】\n" + _wx_search(want[0].get("text", ""))

    done = []
    for act in [a for a in (out.get("actions") or []) if a.get("op") != "wx_search"][:3]:
        r = _apply(act)
        if r:
            done.append(r)

    try:
        con = store.conn()
        con.execute(
            "CREATE TABLE IF NOT EXISTS chat_log(id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " ts TEXT, user TEXT, reply TEXT, actions TEXT, sid TEXT)")
        con.execute("INSERT INTO chat_log(ts,user,reply,actions,sid) VALUES(?,?,?,?,?)",
                    (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), text,
                     out.get("reply", ""), json.dumps(done, ensure_ascii=False), sid))
        con.commit()
        con.close()
    except Exception:  # noqa: BLE001
        pass

    return {"reply": out.get("reply", ""), "done": done}


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(json.dumps(handle(sys.argv[1] if len(sys.argv) > 1 else "现在有什么要紧的？",
                            sys.argv[2] if len(sys.argv) > 2 else ""),
                     ensure_ascii=False, indent=1))
