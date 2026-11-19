# -*- coding: utf-8 -*-
"""助理 Agent 的收件监控：微信告警 + Get笔记新录音 → 评估 → 进待办。

每 10 分钟跑一次。只用规则评分，不调大模型（零 token 成本）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import router

BASE = Path(__file__).parent
TODO_FILE = BASE / "data/todo.json"
MON_FILE = BASE / "data/monitor.json"
ALERTS = Path("D:/Claude/项目/01 参谋长/15-实时微信与录音监控/data/alerts.jsonl")

def log(msg: str) -> None:
    p = BASE / "logs" / "panel.log"
    p.parent.mkdir(exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write("[" + datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "] " + msg + chr(10))


NOTE_API = "https://openapi.biji.com/open/api/v1/resource/note/list?cursor=0"

# 微信监控运行时（只读调用，不改它的配置）
# 端口按 port_map.json 取，账号按 runtime 目录自动发现——加第三个微信不用改代码
PORT_MAP = Path(""<你的本地路径>"")
ACC_FILE = BASE / "data/accounts.json"
_WX_FALLBACK = {"wxid_xurchv26a0r822_4765": 5678, "wxid_vvzb7s8u7yro12_ca93": 5679}


def disabled_accounts() -> set:
    """data/accounts.json 里标了 enabled:false 的账号：不扫、不计数、不报警。"""
    try:
        d = json.loads(ACC_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return set()
    return {k for k, v in d.items() if isinstance(v, dict) and v.get("enabled") is False}


def wx_accounts() -> dict:
    """{账号目录名: 端口}，只含有消息文件且没被停用的账号。"""
    try:
        pm = json.loads(PORT_MAP.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pm = dict(_WX_FALLBACK)
    try:
        import wxreader
        for acc in wxreader.accounts():
            pm.setdefault(acc, 0)
    except Exception:  # noqa: BLE001
        pass
    off = disabled_accounts()
    return {k: v for k, v in pm.items() if k not in off}


WX_ACCOUNTS = wx_accounts()

# 规则评分：命中即升级
HIGH_WORDS = ["终止", "撤回", "投诉", "纠纷", "违约", "起诉", "律师", "索赔", "退款",
              "急", "今天", "明天", "截止", "涨价", "裁员", "离职", "住院", "手术"]
MID_WORDS = ["合同", "回款", "报价", "定金", "预算", "老板", "签约", "方案", "确认",
             "对接", "面谈", "复盘", "上线", "交付"]

MAX_PER_SCAN = 8

# 群消息必须点名我，才算"要我处理"；否则是群友互相说话
ME_WORDS = ("庞总", "庞老师", "恩浩", "庞恩浩", "庞恩民", "@庞", "庞工")


def addressed_to_me(chat: str, text: str, rule: dict | None) -> bool:
    """私聊直接算；群聊要点名我，除非该规则声明 group_mention=false。"""
    if "@chatroom" not in (chat or ""):
        return True
    if rule and rule.get("group_mention") is False:
        return True
    return any(w in text for w in ME_WORDS)


PAST_WORDS = ("之前", "已经", "昨天", "刚才", "做过", "弄过", "没时间", "没空", "还没")

WXID_PREFIX = re.compile(r"^wxid_[0-9A-Za-z]+[:：]\s*")

_COMMIT_RE = []


def _commit_re():
    """用 rules.json 里的时间词/动作词拼正则，改词表即生效。"""
    if _COMMIT_RE:
        return _COMMIT_RE[0]
    times = "|".join(router.w("commit_time", ["今天", "明天", "晚上"]))
    verbs = "|".join(router.w("commit_verb", ["看", "弄", "做"]))
    pat = (f"({times})[^，。！？]{{0,6}}({verbs})"
           f"|我(来|去|会)[^，。！？]{{0,4}}({verbs})")
    _COMMIT_RE.append(re.compile(pat))
    return _COMMIT_RE[0]


def is_commitment(text: str) -> bool:
    """我自己在对话里答应的事（我明天弄 / 那我今天看哈 / 回头我发你）。"""
    t = text.strip()
    if len(t) < 4 or len(t) > 80:
        return False
    if t.endswith(("吧", "吗", "呢", "?", "？")):
        return False
    m = _commit_re().search(t)
    if not m:
        return False
    # 只排除"承诺语之前紧挨着的过去式"——"之前那个已弄完，我今天看这个"仍算承诺
    head = t[:m.start()][-10:]
    return not any(x in head for x in router.w("past", PAST_WORDS))


def ignored(text: str) -> bool:
    """规则里声明的免读关键词（如主播招聘群），命中直接跳过。"""
    try:
        kws = router.load_rules().get("ignore", [])
    except Exception:  # noqa: BLE001
        kws = []
    return any(k and k in text for k in kws)


def _dm_noise(text: str, name: str = "") -> bool:
    """私聊全放行后，只挡纯应答和系统提示，不值得花一次模型调用。

    保守起见只挡「整句就是应答词」、系统号、加好友/删除好友这类提示，
    带任何内容的一律放行。
    """
    if name in ("微信团队", "文件传输助手", "微信支付", "腾讯新闻"):
        return True
    t = (text or "").strip().strip("，。！？~～…,.!?、 ")
    if t.startswith(("你已添加了", "以上是打招呼", "你已删除", "该消息类型暂不支持")):
        return True
    if len(t) < 3:
        return True
    acks = router.w("ack", ())
    return len(t) <= 6 and any(t == a or (t.startswith(a) and len(t) - len(a) <= 1)
                               for a in acks)


# ---------------- 状态 ----------------
def _read_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def _write_json(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def load_state() -> dict:
    return _read_json(MON_FILE, {"last_scan": 0, "seen": [], "stats": {}})


def save_state(st: dict) -> None:
    st["seen"] = st.get("seen", [])[-500:]
    _write_json(MON_FILE, st)


# ---------------- 微信告警 ----------------
def scan_wechat(since: float) -> list[dict]:
    if not ALERTS.exists():
        return []
    out = []
    try:
        for line in ALERTS.read_text(encoding="utf-8", errors="ignore").splitlines():
            if not line.strip():
                continue
            try:
                a = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if a.get("ts", 0) <= since:
                continue
            lv = a.get("level")
            if lv not in ("high", "mid"):
                continue
            if ignored(f"{a.get('label','')} {a.get('summary','')}"):
                continue
            out.append({
                "sid": f"wx-{a.get('id')}",
                "title": f"[微信] {a.get('label','')}"[:40],
                "note": (a.get("summary") or "").replace("\n", " ")[:120],
                "level": "P0" if lv == "high" else "P1",
                "source": "微信监控",
                "ts": a.get("ts", time.time()),
            })
    except Exception as e:  # noqa: BLE001
        print("wechat scan error", e)
    return out


def _wx(port: int, path: str, timeout: float = 6.0):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
        return json.load(r)


def scan_wechat_db(since: float) -> tuple[list[dict], dict]:
    """直读解密的微信库，覆盖全部会话（含新建群）。

    运行时接口只吐 26 个会话，新建的群根本看不见，所以这里绕开接口读库。
    端口探测保留，只用来判断运行时是否活着（它负责持续解密）。
    """
    import wxreader

    out, status = [], {}
    wx_last = {}
    rules = router.load_rules()
    # 每轮重算账号表：新登录一个微信，不用重启中台就能进来
    for acc, port in wx_accounts().items():
        port_ok = False
        try:
            if port:
                _wx(port, "/api/sessions", timeout=3)
            port_ok = True
        except Exception:  # noqa: BLE001
            port_ok = False
        # 端口通不等于这个微信登着：账号退出后本地监控进程照样占着端口。
        # 真在线要看库里还有没有新消息（7 天内有动静就算在线）
        last = 0
        try:
            last = wxreader.latest_ts(acc)
        except Exception:  # noqa: BLE001
            pass
        # 在线 = 这个号登着 且 原始库最近还在写盘。
        # 不看端口：多开副号扫不到密钥，端口永远不通，却一直被误判成离线。
        # 也不看「最近有没有新消息」：命理号可能一周没人私聊，但客户端明明登着。
        try:
            logged = bool(wxreader.login_state().get(acc))
            src_age = time.time() - wxreader.source_mtime(acc)
            src_hot = 0 <= src_age < 900          # 15 分钟内库被写过
            procs = wxreader.weixin_processes()   # 微信客户端进程数
        except Exception:  # noqa: BLE001
            logged, src_hot, procs = False, False, 0
        # 判定放宽：登录态说了算；登录态一时读不到，但客户端进程在跑（每个实例约 6 个）
        # 或者库刚被写过，都算在线。宁可误报在线，也别把登着的号标成掉线。
        client_up = procs >= 6 * max(1, len(WX_ACCOUNTS))
        fresh = bool(last) and (time.time() - last) < 3 * 86400
        alive = logged or src_hot or fresh or client_up
        wx_last[acc] = last
        try:
            msgs = wxreader.read_account(acc, since, limit_per_chat=200)
            status[acc] = alive
        except Exception as e:  # noqa: BLE001
            log(f"wxreader 读库失败 {acc}: {e}")
            status[acc] = False
            continue

        for m in msgs:
            txt = (m.get("text") or "").strip()
            if not txt:
                continue
            name = m.get("chat_name") or m.get("chat") or ""
            ts = int(m.get("ts") or 0)

            if m.get("is_self"):
                # 自己发的不算「别人要求」，但自己答应的事要跟进
                if is_commitment(txt):
                    out.append({
                        "sid": f"wxm-own-{acc[:8]}-{ts}-{hashlib.md5(txt.encode('utf-8')).hexdigest()[:8]}",
                        "title": f"[承诺] {name}"[:40],
                        "note": f"我说：{txt}"[:120].replace(chr(10), " "),
                        "raw": txt[:300].replace(chr(10), " "),
                        "sender": "我",
                        "source": "我的承诺",
                        "media": "",
                        "commit": True,
                        "ts": ts,
                    })
                continue

            blob = f"{name} {txt}"
            if ignored(blob):
                continue
            is_dm = "@chatroom" not in (m.get("chat") or "")
            r = router.route(blob)
            p0_hit = any(x in blob for x in router.w("p0", router.P0_WORDS))
            # 闸门：群聊只放行命中项目规则或高危词的；私聊全放行，要不要我做交给模型判。
            # 2026-11-11：客户C锦哲「咱俩一起设计个场景」因为项目没登记在 rules.json，
            # 整段被这行丢掉，模型根本没机会看。词表不该决定什么能进待办。
            if not is_dm and not r["rule_id"] and not p0_hit:
                continue
            if is_dm and not r["rule_id"] and _dm_noise(txt, name):
                continue
            if not addressed_to_me(m.get("chat"), txt, router.match_rule(blob, rules)):
                continue

            media = ""
            if "[文件]" in txt or "[卡片]" in txt or "[图片]" in txt:
                media = f"微信「{name}」"
            out.append({
                "sid": f"wxm-{acc[:8]}-{ts}-{hashlib.md5(txt.encode('utf-8')).hexdigest()[:8]}",
                "title": f"[微信] {name}"[:40],
                "note": ((m.get("sender") or "") + "：" + txt)[:120].replace(chr(10), " "),
                "raw": txt[:300].replace(chr(10), " "),
                "sender": m.get("sender") or "",
                "source": "微信消息",
                "media": media,
                "ts": ts,
            })
    return out, status, wx_last


# ---------------- Get笔记 ----------------
def scan_getnote(since: float) -> tuple[list[dict], bool]:
    key = os.environ.get("GET_BIJI_API_KEY")
    cid = os.environ.get("GET_BIJI_CLIENT_ID", "")
    if not key:
        return [], False
    try:
        req = urllib.request.Request(NOTE_API, headers={"Authorization": key, "X-Client-ID": cid})
        data = json.load(urllib.request.urlopen(req, timeout=20))
        notes = ((data.get("data") or {}).get("notes")) or []
    except Exception as e:  # noqa: BLE001
        print("getnote error", e)
        return [], False

    out = []
    for n in notes:
        try:
            ts = datetime.strptime(n.get("created_at", ""), "%Y-%m-%d %H:%M:%S").timestamp()
        except Exception:  # noqa: BLE001
            continue
        if ts <= since:
            continue
        title = n.get("title") or "未命名录音"
        if ignored(title):
            continue
        tags = " ".join(t.get("name", "") for t in (n.get("tags") or []))
        # 正文开头是「录音信息（时间/时长/人数/类型）」+ 图片，真正的分析在「录音总结」之后。
        # 直接取前 600 字会把元数据当成分析（2026-10-23 修正）
        raw_c = n.get("content") or ""
        mm = re.search(r"####\s*录音总结\s*(.*)", raw_c, re.S)
        seg = mm.group(1) if mm else raw_c
        seg = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", seg)

        # 录音可能一直开着，一段里装了好几件不相干的事（项目推进 + 孩子在学校）。
        # 按总结里的小节标题拆开，一节一条候选，后面按项目各自归位。
        sections = _split_sections(seg)
        nid = n.get("note_id") or n.get("id")
        for idx, (head, text) in enumerate(sections):
            # 拆出多节时，首段「总述」是其余各节的概括，留着会让两个主题又混回一张卡
            if len(sections) > 1 and not head:
                continue
        secs = []
        for idx, (head, text) in enumerate(sections):
            # 拆出多节时，首段「总述」是其余各节的概括，留着会让两个主题又混回一张卡
            if len(sections) > 1 and not head:
                continue
            body = re.sub(r"\s+", " ", re.sub(r"[#*>`]", " ", text)).strip()
            if len(body) < 8:
                continue
            label = f"{title}｜{head}" if head else title
            lv = score(f"{label} {tags} {body}")
            if not lv:
                continue
            secs.append({"idx": idx, "label": label, "body": body, "level": lv, "head": head,
                         "topic": topic_of(f"{head} {body[:200]}") if len(sections) > 1 else ""})
        # 一类明显占多数（六成以上）时，把其余小节并进去，避免一段面试裂成两张卡；
        # 但「家庭」与工作类永不合并——孩子的事不能被并进项目卡
        real = [s["topic"] for s in secs if s["topic"]]
        if real:
            cnt = {}
            for t in real:
                cnt[t] = cnt.get(t, 0) + 1
            top, top_n = max(cnt.items(), key=lambda kv: kv[1])
            dominant = top_n >= max(2, len(secs) * 0.6)
            for s in secs:
                if not s["topic"]:
                    s["topic"] = top
                elif dominant and s["topic"] != top:
                    # 家庭与工作类互不吞并；其余按多数归拢
                    if "家庭" in (s["topic"], top):
                        continue
                    s["topic"] = top
        for s in secs:
            out.append({
                "sid": f"note-{nid}" + (f"-{s['idx']}" if len(sections) > 1 else ""),
                "title": f"[录音] {s['label']}"[:40],
                "group_title": f"[录音] {title}｜{s['topic'] or '其他'}"[:40],
                "note": s["body"][:120],
                "level": s["level"],
                "source": "Get笔记",
                "ts": ts,
                "head": s["head"],
                "topic": s["topic"],
                "sec_head": s["head"],
                "url": n.get("note_url", ""),
                "media": n.get("note_url", "") if "![" in raw_c else "",
            })
    return out, True


# 一段录音里常混着几件不相干的事。按主题词把同一个小节归到一类，
# 同一录音同一类只出一张卡，避免"拆开后又变成七八张碎卡"
TOPICS = {
    "项目": ("项目", "预算", "落地", "退出", "决策", "投标", "评审", "申报", "客户",
             "合同", "合作", "团队", "交付", "报价"),
    "家庭": ("孩子", "老师", "家长", "学校", "念念", "情绪", "医院", "确诊", "罚站", "班主任"),
    "招聘": ("面试", "招聘", "候选人", "薪资", "入职", "简历"),
    "财务": ("报销", "发票", "回款", "对账", "付款"),
}


def topic_of(text: str) -> str:
    best, hit = "", 0
    for name, kws in TOPICS.items():
        n = sum(1 for k in kws if k in text)
        if n > hit:
            best, hit = name, n
    return best


def _split_sections(md: str) -> list[tuple[str, str]]:
    """把录音总结按 `**小节标题**` 拆开。返回 [(标题, 正文)]，首段标题为空。"""
    out, head, buf = [], "", []
    for ln in md.split("\n"):
        h = re.match(r"^\s*\*\*(.+?)\*\*\s*$", ln)
        if h:
            if "".join(buf).strip():
                out.append((head, "\n".join(buf)))
            head, buf = h.group(1).strip(), []
        else:
            buf.append(ln)
    if "".join(buf).strip():
        out.append((head, "\n".join(buf)))
    return out[:8]


def score(text: str) -> str:
    """命中高分词 → P0；命中中分词 → P1；都不中 → 不入待办。"""
    hits_hi = [w for w in HIGH_WORDS if w in text]
    if hits_hi:
        return "P0"
    hits_mid = [w for w in MID_WORDS if w in text]
    if hits_mid:
        return "P1"
    return ""


# ---------------- 写入待办 ----------------
def load_todos() -> list[dict]:
    """主存是 data/console.db（可 SQL 查改），同时导出 todo.json 方便肉眼看。"""
    import store
    items = store.load_todos()
    for i, it in enumerate(items):
        it.setdefault("sid", f"local-{i}")
        it.setdefault("done", False)
        it.setdefault("source", "手工")
    return items


def save_todos(items: list[dict]) -> None:
    import store
    store.save_todos(items)


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime('%m-%d')


def group_candidates(routed: list[tuple[dict, dict]]) -> list[dict]:
    """把同一项目、同一天的微信往来与录音合成一条。

    一条消息一张卡会把面板搞得又长又碎（2026-10-23 实测：回扫 10 天出 13 张卡）。
    助理该干的是把当天这件事读完再说话：
      - 同一项目 + 同一天 = 一件事
      - 录音的智能总结当"分析"，微信往来当"过程"
      - 原文按时间线完整保留，可回溯
    """
    groups: dict[tuple, list] = {}
    for f, r in routed:
        proj = r.get("rule_name") or ""
        # 录音按「录音名+主题」归组：一段录音里的项目、家事各出一张卡
        base = f.get("group_title") or f.get("title", "")
        key = (proj or base, _day(f["ts"]))
        groups.setdefault(key, []).append((f, r))

    out = []
    for (gkey, day), lst in groups.items():
        lst.sort(key=lambda x: x[0]["ts"])
        # 注意：分组键里的 gkey 在未归属时是条目标题，不能直接当项目名用
        proj = next((r.get("rule_name") for _, r in lst if r.get("rule_name")), "")
        lv = min((r["level"] for _, r in lst), key=lambda x: router.RANK.get(x, 9))
        top = min(lst, key=lambda x: router.RANK.get(x[1]["level"], 9))[1]
        recs = [(f, r) for f, r in lst if f.get("source") == "Get笔记"]
        msgs = [(f, r) for f, r in lst if f.get("source") != "Get笔记"]

        # 分析：有承诺就先亮承诺，其次录音总结，再次最后一句
        commit_item = next((f for f, _ in lst if f.get("commit")), None)
        if commit_item:
            analysis = "我方承诺：" + (commit_item.get("note") or "")[:100]
        elif recs:
            analysis = "；".join((f.get("note") or "").strip()[:80] for f, _ in recs[:4])
        else:
            analysis = (msgs[-1][0].get("note") or "")[:110] if msgs else ""

        # 过程：时间线，带说话人和时间，最多 10 条
        # 只留 10 条时，容易把带链接的海报、我自己承诺的那句截掉。
        # 先保「我承诺的」和「带链接的」，再补最近的
        keep = []
        for group in ([x for x in lst if x[0].get("commit")],
                      [x for x in lst if "http" in (x[0].get("note") or "")],
                      lst):
            for x in group:
                if x not in keep:
                    keep.append(x)
                if len(keep) >= 10:
                    break
            if len(keep) >= 10:
                break
        keep.sort(key=lambda x: x[0]["ts"])
        lines = []
        for f, _ in keep:
            tag = ("录音" if f.get("source") == "Get笔记"
                   else ("生产" if f.get("source") == "OA·生产" else "微信"))
            lines.append(f"{_day(f['ts'])} {datetime.fromtimestamp(f['ts']):%H:%M} [{tag}] {f.get('note','')[:70]}")
        raw = " ｜ ".join(lines)

        srcs = {f.get("source", "") for f, _ in lst}
        is_oa = srcs == {"OA·生产"}
        parts = []
        if is_oa:
            parts.append(f"生产异常 {len(lst)} 项")
        elif msgs:
            parts.append(f"往来 {len(msgs)} 条")
        if recs:
            parts.append(f"录音 {len(recs)} 段")
        label = "＋".join(parts)

        card = {
            "sid": "agg-" + hashlib.md5(f"{gkey}|{day}".encode()).hexdigest()[:10],
            "title": (f"[{proj}] {day} · {label}" if proj else
                      _plain_title(lst, day, label))[:40],
            "level": lv,
            "note": analysis or f"当日 {label}",
            "raw": raw[:600],
            "source": "OA·生产" if is_oa else
                      ("整合·" + ("录音+微信" if recs and msgs else ("录音" if recs else "微信"))),
            "project": proj,
            "project_path": top.get("project", ""),
            "reason": f"当日 {label} 合为一条，取组内最高级 {lv}；"
                      + top.get("why", "").split("→")[0][:56],
            "rule": top.get("rule_id", ""),
            "media": next((f.get("media") for f, _ in lst if f.get("media")), ""),
            "url": next((f.get("url") for f, _ in lst if f.get("url")), ""),
            "ts": lst[0][0]["ts"],
            "escalate": any(r["escalate"] for _, r in lst),
            "commit": any(f.get("commit") for f, _ in lst),
            "count": len(lst),
        }
        try:
            import links
            card["link_txt"] = links.previews(card.get("raw", ""))
        except Exception:  # noqa: BLE001
            card["link_txt"] = ""
        card["link"] = _cross_link(card)
        out.append(card)
    # 模型判断：要不要我做、几级、一句话结论（规则降级为兜底）
    _apply_judge(out)
    # 模型分析按级别优先：先 P0 再 P1，预算用完为止；展示顺序仍按时间
    out.sort(key=lambda c: (router.RANK.get(c["level"], 9), -c["ts"]))
    for c in out:
        c["ai"] = c.get("ai") or _ai_analyze(c)
    out.sort(key=lambda x: -x["ts"])
    return out


def _steps_of(project: str) -> list[dict]:
    try:
        d = json.loads((BASE / "data/next_steps.json").read_text(encoding="utf-8"))
        return d.get("projects", {}).get(project, [])
    except Exception:  # noqa: BLE001
        return []


_JUDGE_BUDGET = 12      # 单轮最多让模型判几组（按项目、承诺、条数优先）

P0_WORDS_LOCAL = None


def _apply_judge(cards: list[dict]) -> None:
    """模型的判断覆盖规则：要不要我做、几级、一句话结论。

    规则降级为兜底：模型不可用/超时/返回值不对时，沿用规则算出来的级别。
    P0 词命中时不许降到 P0 以下（这是安全阀，不看模型脸色）。
    """
    if not cards:
        return
    try:
        import ai
        import judge
        import router as _r
        if not ai.ready():
            return
    except Exception:  # noqa: BLE001
        return

    p0 = router.w("p0", router.P0_WORDS)
    order = sorted(cards, key=lambda c: (-(1 if c.get("project") else 0),
                                         -(1 if c.get("commit") else 0),
                                         -c.get("count", 0)))
    used = 0
    for c in order:
        if used >= _JUDGE_BUDGET:
            break
        link_txt = c.get("link_txt") or ""
        try:
            out = judge.judge(c["sid"], c["raw"], _steps_of(c.get("project", "")),
                              extra=((c.get("note", "") or "")[:200]
                                     + ("\n链接内容：" + link_txt if link_txt else "")))
            used += 1
        except Exception as e:  # noqa: BLE001
            log(f"judge 失败 {c.get('title','')[:16]}: {e}")
            continue

        lv = out.get("级别", "")
        if lv in router.RANK:
            if any(w in (c.get("raw", "") + c.get("note", "")) for w in p0):
                lv = "P0"                      # 安全阀：高危词命中不许降
            c["level"] = lv
        c["escalate"] = bool(out.get("要我做"))
        if out.get("结论"):
            c["note"] = out["结论"]
        c["judge"] = out
        try:
            import store, llmcfg
            store.log_judge(c["sid"], c.get("title",""), out, llmcfg.resolve().get("model",""))
        except Exception:  # noqa: BLE001
            pass
        acts = [a for a in (out.get("动作") or []) if a][:2]
        if acts:
            c["ai"] = "下一步：" + "；".join(acts) + (
                f"（依据：{out['依据']}）" if out.get("依据") else "")
        c["reason"] = f"模型判定：{'要我做' if c['escalate'] else '不用我做'}，{lv}；{out.get('依据','')}"[:110]


def _cross_link(card: dict) -> str:
    """把卡片内容与项目里已有的文档、未结事项做交叉比对。"""
    path = card.get("project_path") or ""
    if not path:
        return ""
    try:
        import context
        txt = context.cross(f"{card.get('note','')} {card.get('raw','')}", path, card.get("project", ""))
        try:
            import store
            store.log_cross(card.get("sid",""), txt)
        except Exception:  # noqa: BLE001
            pass
        return txt
    except Exception as e:  # noqa: BLE001
        log(f"cross link 失败 {card.get('title','')[:20]}: {e}")
        return ""


_AI_BUDGET = [0]        # 单轮扫描最多问几次模型，防一次回填补齐把账单打爆


def _ai_analyze(card: dict) -> str:
    """P0/P1 且有归属的卡，交给模型读项目文档后给结论。失败降级为空，不阻断。"""
    if card.get("level") not in ("P0", "P1"):
        return ""
    path = card.get("project_path") or ""
    if not path or _AI_BUDGET[0] >= 4:
        return ""
    try:
        import ai
        import context
        if not ai.ready():
            return ""
        steps = []
        try:
            d = json.loads((BASE / "data/next_steps.json").read_text(encoding="utf-8"))
            steps = d.get("projects", {}).get(card.get("project", ""), [])
        except Exception:  # noqa: BLE001
            pass
        _AI_BUDGET[0] += 1
        return ai.line_of(ai.analyze(card, context.corpus(path), steps))
    except Exception as e:  # noqa: BLE001
        log(f"ai 分析失败 {card.get('title','')[:18]}: {e}")
        return ""


def _plain_title(lst: list, day: str, label: str) -> str:
    """未归属卡片的标题。

    拆过主题的录音不能再用录音原名——原名常把两件事都写进去
    （「项目推进意愿确认与孩子校园问题沟通」），光看标题就以为还是一张混卡。
    改用「主题｜首个小节名」。
    """
    comm = "承诺 · " if any(f.get("commit") for f, _ in lst) else ""
    topic = next((f.get("topic") for f, _ in lst if f.get("topic")), "")
    if topic:
        head = next((f.get("sec_head") for f, _ in lst if f.get("sec_head")), "")
        base = f"{topic}｜{head}" if head else topic
    else:
        base = re.sub(r"^\[[^\]]*\]\s*", "",
                      next((f.get("group_title") for f, _ in lst if f.get("group_title")), "")
                      or lst[0][0].get("title") or "未归属")
    return f"{comm}{base} · {day} · {label}"


def route_all(found: list[dict]) -> list[tuple[dict, dict]]:
    """先给每条候选定级，再交给 group_candidates 合并。"""
    out = []
    for f in found:
        if f.get("fixed_level"):
            r = {"level": f["fixed_level"], "rule_id": "", "rule_name": f.get("project", ""),
                 "project": f.get("project_path", ""), "escalate": True,
                 "trace": bool(f.get("project_path")), "trace_ok": True,
                 "why": f.get("why", "生产数据异常")}
            out.append((f, r))
            continue
        if f.get("commit"):
            # 承诺也要认项目：先按原文路由拿到归属，再把级别定为 P2、置为要处理。
            # 不认项目的话，同一件事会裂成「承诺卡」和「项目卡」两张
            r = dict(router.route(f"{f.get('title', '')} {f.get('note', '')}"))
            r.update({"level": "P2", "escalate": True, "trace_ok": True,
                      "why": "我方在对话里答应要做（我的消息）→ P2，需要跟进"})
        else:
            r = router.route(f"{f.get('title', '')} {f.get('note', '')}")
        out.append((f, r))
    return out


def push(items: list[dict], routed: list[tuple[dict, dict]], seen: set, cap: int = None,
         min_level: str = None) -> dict:
    """按「项目 + 当天」合成卡片后分流。

    min_level：补拉时设 "P1"，微信只捞 P0/P1；录音不降级（整段会议，一句 P2 可能就是全部结论）。
    已结案或已跳过的卡片不会被重新激活——用户在面板上关掉的，不该第二天又冒出来。
    """
    groups = group_candidates(routed)
    by_sid = {it.get("sid"): it for it in items}
    added = auto = fresh = 0

    for g in groups:
        old = by_sid.get(g["sid"])
        # 去重按「卡片 + 内容指纹」：只按 sid 记的话，同一天早先以 P4 扫过、
        # 后来变成 P2 的卡会被永远跳过（2026-10-23 郑庆博「我明天过去」就是这么丢的）
        skey = f"{g['sid']}|{hashlib.md5(g['raw'].encode('utf-8')).hexdigest()[:8]}"
        if skey in seen:
            continue
        fresh += 1
        old = by_sid.get(g["sid"])
        if old is not None:
            # 同一天后来又有消息：补进原卡，不新建；已结案的不动
            if not old.get("done") and not old.get("skipped") and g["raw"] != old.get("raw"):
                old.update({"note": g["note"], "raw": g["raw"], "level": g["level"],
                            "reason": g["reason"], "title": g["title"],
                            "link_txt": g.get("link_txt", ""),
                            "link": g.get("link", ""), "ai": g.get("ai", ""),
                            "updated_at": datetime.now().strftime("%m-%d %H:%M")})
            seen.add(skey)
            continue
        if added >= (cap or MAX_PER_SCAN):
            break
        seen.add(skey)

        # 降噪门槛：录音组、含我方承诺的组都不降级（承诺是要兑现的，级别低也得报）
        is_rec = "录音" in g.get("source", "")
        too_low = (bool(min_level)
                   and router.RANK.get(g["level"], 9) > router.RANK.get(min_level, 1)
                   and not is_rec and not g.get("commit"))

        routed_item = {"level": g["level"], "rule_id": g.get("rule", ""),
                       "rule_name": g.get("project", ""), "project": g.get("project_path", ""),
                       "escalate": g["escalate"], "trace": bool(g.get("project_path")), "trace_ok": True}
        traced = ""
        if g.get("project_path") and g["escalate"]:
            traced = router.append_trace(g, routed_item)
        router.log_action(g, routed_item, traced)

        if g.get("project"):
            for c in auto_close_steps(g["project"], f"{g['title']} {g['note']}", g["note"]):
                log(f"auto-close step: {g['project']} · {c[:30]}")

        if g["escalate"] and not too_low:
            items.append({
                "sid": g["sid"],
                "title": g["title"],
                "level": g["level"],
                "note": g["note"],
                "due": "",
                "done": False,
                "source": g["source"],
                "project": g.get("project", ""),
                "reason": g["reason"],
                "rule": g.get("rule", ""),
                "trace": traced,
                "media": g.get("media", ""),
                "raw": g["raw"],
                "link": g.get("link", ""),
                "ai": g.get("ai", ""),
                "link_txt": g.get("link_txt", ""),
                "created_at": datetime.fromtimestamp(g["ts"]).strftime("%m-%d %H:%M"),
                "url": g.get("url", ""),
            })
            by_sid[g["sid"]] = items[-1]
            added += 1
        else:
            auto += 1
            # 不进待办，但留一条痕：这轮看到了什么、为什么不动它
            try:
                _why = "录音组降级" if is_rec else ("补拉窗口外的低级别" if too_low else "未达升级门槛")
                import store as _st
                _c = _st.conn()
                _c.execute("INSERT INTO watch_log(ts,sid,project,title,level,why) VALUES(?,?,?,?,?,?)",
                           (datetime.now().strftime("%Y-%m-%d %H:%M"), g["sid"],
                            g.get("project", ""), g["title"][:60], g["level"], _why))
                _c.commit(); _c.close()
            except Exception:  # noqa: BLE001
                pass
    return {"added": added, "auto": auto, "groups": len(groups), "fresh": fresh}


OA_STALE_H = 6


def scan_oa() -> tuple[list[dict], bool]:
    """客户B OA 生产数据：过期就同步，再把异常变成候选。"""
    import time as _t
    import oa
    import oa_sync
    ok = True
    try:
        db = oa_sync.DB
        if not db.exists() or (_t.time() - db.stat().st_mtime) > OA_STALE_H * 3600:
            oa_sync.sync()
    except Exception as e:  # noqa: BLE001
        log(f"oa 同步失败：{e}")
        ok = False
    try:
        snap = oa.snapshot()
    except Exception as e:  # noqa: BLE001
        log(f"oa 读库失败：{e}")
        return [], ok

    now = _t.time()
    out = []
    for a in oa.anomalies(snap):
        out.append({
            "sid": f"oa-{snap.get('today','')}-{a['key']}",
            "title": f"[生产] {a['title']}"[:40],
            "note": a.get("note", ""),
            "raw": a.get("note", ""),
            "source": "OA·生产",
            "project": "客户B家居·生产",
            "project_path": "D:/Claude/项目/13 FDE/01-客户项目/20260721-客户B家居",
            "fixed_level": a.get("level", "P2"),
            "why": f"OA 生产数据判定：{a['title']}",
            "ts": now,
        })
    return out, ok


def run_scan(lookback_days: int = 7) -> dict:
    """跑一轮扫描：拉新 → 评分 → 进待办。返回本轮统计。

    首次运行（还没有 last_scan）回看 lookback_days 天，不是 24 小时。
    2026-10-23 教训：中台晚上线，10-21 的「项目路演和考察见面事项沟通」录音
    落在首跑 24 小时窗口外，之后再也不会被扫到，路演安排整条漏掉。
    """
    st = load_state()
    since = st.get("last_scan", 0) or (time.time() - lookback_days * 86400)
    live, wx_status, wx_last = scan_wechat_db(since)
    oa_items, oa_ok = scan_oa()
    notes, note_live = scan_getnote(since)
    found = scan_wechat(since) + notes + live + oa_items
    found.sort(key=lambda x: x["ts"], reverse=True)   # 补拉时优先处理最新的

    # 距上次扫描超过 30 分钟（关机/休眠）就进补拉模式：上限放宽
    gap_min = int((time.time() - since) / 60)
    backfill = gap_min > 30
    cap = 60 if backfill else MAX_PER_SCAN

    items = load_todos()
    seen = set(st.get("seen", []))
    routed = route_all(found)
    res = push(items, routed, seen, cap, min_level="P1" if backfill else None)
    added, auto, new_n = res["added"], res["auto"], res.get("fresh", 0)
    save_todos(items)

    # 归并成事项：挂在巡检后面跑，没有新卡就不调模型。
    # 失败只记日志，绝不因为归并挂了把待办搞空。
    th = {}
    try:
        import threads
        th = threads.consolidate()
    except Exception as e:  # noqa: BLE001
        log(f"consolidate skipped: {e}")
        th = {"ok": False, "error": str(e)[:80]}

    st["last_scan"] = time.time()
    st["last_ok"] = time.time()
    st.pop("last_error", None)
    st.pop("last_error_at", None)
    st["seen"] = sorted(seen)
    st["stats"] = {
        "found": len(found),
        "new": new_n,
        "added": added,
        "auto": auto,
        "wx_live": any(wx_status.values()) if wx_status else False,
        "wx_accounts": wx_status,
        "wx_last": {k: datetime.fromtimestamp(v).strftime("%m-%d %H:%M") if v else "" for k, v in wx_last.items()},
        "note_live": note_live,
        "oa_ok": oa_ok,
        "at": datetime.now().strftime("%m-%d %H:%M"),
        "backfill_min": gap_min if backfill else 0,
        "pending": sum(1 for i in items if not i.get("done")),
        "threads": th.get("threads", 0),
        "merged": th.get("merged", 0),
    }
    save_state(st)
    try:
        import store
        store.log_scan(st["stats"])
    except Exception:  # noqa: BLE001
        pass
    return st["stats"]


def auto_close_steps(project_name: str, text: str, snippet: str) -> list[str]:
    """新消息命中某条建议的关键词（≥2 个）就自动结案，并记证据。"""
    import json as _json
    f = BASE / "data/next_steps.json"
    try:
        d = _json.loads(f.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    closed = []
    for x in d.get("projects", {}).get(project_name, []):
        if x.get("done"):
            continue
        kws = x.get("keywords") or []
        if len(kws) >= 2 and sum(1 for k in kws if k and k in text) >= 2:
            x["done"] = True
            x["auto"] = True
            x["evidence"] = snippet[:80]
            x["done_at"] = datetime.now().strftime("%m-%d %H:%M")
            closed.append(x.get("text", ""))
    if closed:
        f.write_text(_json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    return closed


def skip(sid: str) -> bool:
    """跳过：不结案，但不再出现在待办里。"""
    items = load_todos()
    hit = False
    for it in items:
        if it.get("sid") == sid:
            it["skipped"] = True
            it["skipped_at"] = datetime.now().strftime("%m-%d %H:%M")
            hit = True
    if hit:
        save_todos(items)
    return hit


def complete_step(project: str, sid: str) -> bool:
    f = BASE / "data/next_steps.json"
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False
    hit = False
    for x in d.get("projects", {}).get(project, []):
        if x.get("id") == sid:
            x["done"] = True
            x["auto"] = False
            x["done_at"] = datetime.now().strftime("%m-%d %H:%M")
            hit = True
    if hit:
        f.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    return hit


_AUTOCLOSE_BUDGET = 4


def _chat_name_of(title: str) -> str:
    """承诺卡的标题形如「承诺 · 💟 · 11-16 · 往来 1 条」，取出中间那个会话名。"""
    parts = [p.strip() for p in (title or "").split("·")]
    return parts[1] if len(parts) > 2 else ""


_NOISE_CHAT = ("主播", "通告", "招募", "兼职", "羊毛", "福利", "团购", "秒杀", "砍价",
               "红包", "资源群", "招聘", "模特", "约拍", "带货群", "返点")


def _noise_chat(name: str) -> bool:
    return any(k in (name or "") for k in _NOISE_CHAT)


def _msgs_after(chat_name: str, since_ts: float, hours: int = 24) -> list[dict]:
    """承诺之后我发出去的消息（跨会话）。

    兑现经常发生在另一个窗口：在 💟 里答应「8 点前发了就行」，
    实际是发到「夏老师」那个会话里。只看同一个会话会漏判。
    """
    import wxreader
    end = since_ts + hours * 3600
    out = []
    for acc in wxreader.accounts():
        try:
            for m in wxreader.read_account(acc, since_ts, limit_per_chat=200):
                t = m.get("ts") or 0
                if not (since_ts < t <= end):
                    continue
                # 招人广告群会把判断淹掉，兑现与否只看人话：
                # 只留私聊（以及承诺发生的那个会话），群里@所有人式的通知不算兑现
                is_group = "@chatroom" in (m.get("chat") or "")
                if is_group and (m.get("chat_name") or "") != chat_name:
                    continue
                if _noise_chat(m.get("chat_name") or "") and (m.get("chat_name") or "") != chat_name:
                    continue
                out.append(m)
        except Exception:  # noqa: BLE001
            continue
    out.sort(key=lambda x: x.get("ts") or 0)
    return out               # 不在这里截断：调用方按时间窗/会话筛，截断会把早期那几条挤掉


PS_C = ("你是庞恩浩的助理。判断一件事：他之前在微信里答应的事，**后来到底做了没有**。\n"
        "算兑现的情形：他自己发出了文件/图片/消息（例如在对方或别的窗口发「[文件] …请假…」），"
        "或他说「发了/已发/发过去了/弄好了/转你了」，或对方回「收到/谢谢」。\n"
        "不算兑现：对方在催、他自己只重复承诺、内容对不上（比如答应发请假条，结果发的是别的）。\n"
        "拿不准就判 false。\n"
        "严格输出 JSON，不要解释：{\"兑现\": true 或 false, \"证据\": \"引用他发出东西的那条原话，不超过40字\"}")


INCOMING_HINT = re.compile(r"等.{0,8}发|说明天发|对方.{0,6}承诺|发你材料|发我材料|发给我")


def _finish_card(sid: str, title: str, detail: str) -> None:
    """结卡 + 关线程 + 写 autoclose 流水，三处一起改，避免只动一半。"""
    complete(sid)
    try:
        import store as _st
        con = _st.conn()
        tid = (con.execute("SELECT tid FROM thread_links WHERE sid=?", (sid,)).fetchone() or [None])[0]
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        if tid:
            con.execute("UPDATE threads SET status='done',updated_at=? WHERE tid=?", (now, tid))
            con.execute("INSERT INTO thread_log(ts,action,tid,detail) VALUES(?,?,?,?)",
                        (now, "autoclose", tid, f"{(title or '')[:30]}｜证据：{detail}"))
        con.commit(); con.close()
    except Exception:  # noqa: BLE001
        pass
    log(f"autoclose {(title or '')[:20]} · {detail[:40]}")


def auto_close_incoming() -> list[dict]:
    """对方答应发的东西已经收到了，就把「等对方」这类卡自动结掉。

    与「我的承诺」相反：这里认的是**对方**后面真的发来的文件/图片/正文。
    没有证据就保持原样，不猜。
    """
    items = load_todos()
    todo = [i for i in items if not i.get("done") and not i.get("skipped")
            and INCOMING_HINT.search(f"{i.get('raw','')} {i.get('note','')}")
            and "承诺 ·" not in (i.get("title") or "")]      # 只处理对方欠我的
    closed = []
    for it in todo:
        name = (it.get("title") or "").split("·")[0].strip()
        if not name:
            continue
        try:
            ts = datetime.strptime(it.get("created_at", ""), "%m-%d %H:%M").replace(
                year=datetime.now().year).timestamp()
        except Exception:  # noqa: BLE001
            continue
        msgs = _msgs_after(name, ts, hours=72)
        hits = []
        for m in msgs:
            if m.get("is_self") or (m.get("chat_name") or "") != name:
                continue
            if (m.get("ts") or 0) <= ts + 60:
                continue
            t = m.get("text") or ""
            # 只认「真的发来了东西」：文件/图片/链接，或明说已发。
            # 「明天发你材料」是承诺本身，不能当证据（第一版就是栽在这）
            if ("[文件]" in t or "[图片]" in t or "发过去了" in t
                    or t.startswith("已发") or "给你发了" in t):
                hits.append(m)
                break
        if not hits:
            continue
        ev = (hits[0].get("text") or "")[:60]
        _finish_card(it["sid"], it.get("title") or "", f"对方已发来：{ev}")
        closed.append({"sid": it["sid"], "title": it.get("title"), "evidence": ev})
    return closed


def auto_close_promises() -> list[dict]:
    """承诺卡：后续微信里已经兑现的，自动结案并留下证据。

    只处理标题带「承诺」或「待兑现」的未结卡片；每轮最多问模型 4 次，
    判不出来的保持原样（宁可留给你点，也不乱关）。
    """
    try:
        import ai
        if not ai.ready():
            return []
    except Exception:  # noqa: BLE001
        return []

    # 每轮扫描都问一次太贵：一小时最多判一轮
    st = load_state()
    if time.time() - float(st.get("autoclose_at") or 0) < 3600:
        return []
    st["autoclose_at"] = time.time()
    save_state(st)

    items = load_todos()
    todo = [i for i in items if not i.get("done") and not i.get("skipped")
            and ("承诺" in (i.get("title") or "") or "待兑现" in (i.get("title") or ""))]
    seen = st.setdefault("autoclose_seen", {})
    closed = []
    for it in todo[: _AUTOCLOSE_BUDGET]:
        name = _chat_name_of(it.get("title") or "")
        if not name:
            continue
        try:
            ts = datetime.strptime(it.get("created_at", ""), "%m-%d %H:%M").replace(
                year=datetime.now().year).timestamp()
        except Exception:  # noqa: BLE001
            continue
        msgs = _msgs_after(name, ts)
        mine = [m for m in msgs if m.get("is_self") and (m.get("ts") or 0) > ts + 60]
        if not mine:
            continue                       # 承诺之后他自己一个字没说，谈不上兑现
        # 这段对话没再变过就别重复问模型（同一张卡每小时问一次纯浪费）
        last_ts = msgs[-1].get("ts") or 0
        if seen.get(it["sid"]) == last_ts:
            continue
        seen[it["sid"]] = last_ts
        # 兑现通常发生在答应之后的十几分钟里。窗口收在前后 3 小时内，
        # 既不把那一刻挤掉，也不让半夜的无关对话稀释判断
        near = [m for m in msgs if ts - 600 <= (m.get("ts") or 0) <= ts + 3 * 3600][:40]
        lines = []
        for m in near:
            who = "我" if m.get("is_self") else (m.get("sender") or "对方")
            lines.append(f"{datetime.fromtimestamp(m['ts']):%m-%d %H:%M} [{m.get('chat_name','')[:10]}] "
                         f"{who}：{(m.get('text') or '')[:70]}")
        transcript = "\n".join(lines)
        try:
            raw = ai.chat(PS_C,
                          f"【原本答应的事】{(it.get('note') or '')[:120]}\n"
                          f"【原话】{(it.get('raw') or '')[:160]}\n"
                          f"【之后的往来】\n{transcript}", timeout=90, max_tokens=800)
            m = re.search(r"\{.*\}", raw or "", re.S)
            out = json.loads(m.group(0)) if m else {}
        except Exception as e:  # noqa: BLE001
            log(f"autoclose judge failed: {e}")
            continue
        if not out.get("兑现"):
            continue
        evidence = (out.get("证据") or "").strip()[:60]
        # 只有证据确实出现在他自己的话里，才认
        if evidence and not any(evidence[:12] in (x.get("text") or "") for x in mine):
            continue
        _finish_card(it["sid"], it.get("title") or "", evidence or "已发")
        closed.append({"sid": it["sid"], "title": it.get("title"), "evidence": evidence})
        log(f"autoclose {it.get('title','')[:20]} · {evidence[:30]}")
    st["autoclose_seen"] = seen
    save_state(st)
    return closed


def record_error(msg: str) -> None:
    st = load_state()
    st["last_error"] = msg
    st["last_error_at"] = datetime.now().strftime("%m-%d %H:%M")
    save_state(st)


def complete(sid: str) -> bool:
    items = load_todos()
    hit = False
    for it in items:
        if it.get("sid") == sid:
            it["done"] = True
            it["done_at"] = datetime.now().strftime("%m-%d %H:%M")
            hit = True
    if hit:
        save_todos(items)
    return hit


if __name__ == "__main__":
    import sys
    # 用法：python monitor.py                 增量一轮
    #      python monitor.py --backfill 30   把最近 30 天重扫一遍（幂等）
    #      python monitor.py --dry 3         只看合并结果，不写盘
    if len(sys.argv) >= 3 and sys.argv[1] == "--dry":
        days = int(sys.argv[2])
        since = time.time() - days * 86400
        live, _ = scan_wechat_db(since)
        notes, _ = scan_getnote(since)
        groups = group_candidates(route_all(scan_wechat(since) + notes + live))
        print(f"候选 {len(live) + len(notes)} 条 → 合并 {len(groups)} 张卡\n")
        for g in groups:
            if not g["escalate"]:
                continue
            print(f"{g['level']} {g['title']}")
            print(f"    分析：{g['note'][:90]}")
            print(f"    原文：{g['raw'][:150]}")
            print()
    elif len(sys.argv) >= 3 and sys.argv[1] == "--backfill":
        days = int(sys.argv[2])
        st = load_state()
        st["last_scan"] = time.time() - days * 86400
        save_state(st)
        print(json.dumps(run_scan(), ensure_ascii=False))
    else:
        print(json.dumps(run_scan(), ensure_ascii=False))
