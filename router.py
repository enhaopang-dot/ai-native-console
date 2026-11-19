# -*- coding: utf-8 -*-
"""规则路由：判断一条消息归哪个项目、什么级别、要不要打扰人、往哪留痕。

规则全在 data/rules.json，用户逐步给，改文件即生效。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
RULES_FILE = BASE / "data/rules.json"
LOG_FILE = BASE / "data/auto_log.jsonl"

# 六档：P0 最高
LEVELS = ["P0", "P1", "P2", "P3", "P4", "P5"]
RANK = {lv: i for i, lv in enumerate(LEVELS)}

# P0 只放"会造成损失"的词；时间词不算
P0_WORDS = ["终止", "撤回", "投诉", "纠纷", "诉讼", "起诉", "律师", "索赔", "违约",
            "退款", "裁员", "离职", "住院", "手术", "断供", "封号"]
P1_WORDS = ["合同", "回款", "报价", "定金", "预算", "签约", "面谈", "复盘", "交付", "涨价"]

# 只有"像任务"的消息才升级到 P2；客套话一律不打扰
# 注意：这里只放"请求/疑问"词。今天/明天这类时间词单独不构成打扰理由
# （否则"昨天日报未写""明天的人员调上来"这种汇报全会变成待办）
TASK_HINTS = ("?", "？", "请您", "请你", "请问", "麻烦", "能否", "能不能", "需要",
              "安排", "什么时候", "尽快", "确认", "发一下", "给一下", "帮我", "帮忙",
              "看一下", "是否", "要不", "能不能给", "催一下")
# 句尾疑问助词也算请求（"你明天来的吧""几点方便呢"）
QUESTION_TAIL = ("吧", "吗", "呢", "么")
ACK_WORDS = ("好的", "好", "收到", "嗯", "谢谢", "行", "可以", "OK", "ok", "辛苦了", "谢谢老板")


_W = {}


def words() -> dict:
    """词表从 rules.json 读，改文件即生效；读不到就用内置默认。"""
    global _W
    if _W:
        return _W
    try:
        import json as _j
        _W = _j.loads(RULES_FILE.read_text(encoding="utf-8")).get("words", {})
    except Exception:  # noqa: BLE001
        _W = {}
    return _W


def w(key: str, default: list) -> list:
    return words().get(key) or default


def reload_words() -> None:
    global _W
    _W = {}


def load_rules() -> dict:
    try:
        return json.loads(RULES_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"rules": [], "defaults": {"escalate_at": "P2", "trace": True}}


def match_rule(text: str, rules: dict) -> dict | None:
    """先看组合词（要同时出现），再看单个关键词。

    组合词用于"路演/答辩"这类通用词：只有同时出现项目词才算这个项目，
    否则黑客松的路演也会被归进来。
    """
    for r in rules.get("rules", []):
        for a, b in r.get("match_pairs", []):
            if a in text and b in text:
                return r
        for kw in r.get("match", []):
            if kw and kw in text:
                return r
    return None


def level_of(text: str, rule: dict | None) -> str:
    """词决定上限，项目归属决定能不能用这个上限。没有归属最多 P2。"""
    has_p0 = any(x in text for x in w("p0", P0_WORDS))
    has_p1 = any(x in text for x in w("p1", P1_WORDS))
    if has_p0:
        return "P0" if rule else "P2"
    if has_p1:
        return "P1" if rule else "P2"
    core = text.strip()
    # 纯客套话 → P5，连留痕都不写
    if len(core) <= 12 and any(core.endswith(a) or core == a for a in w("ack", ACK_WORDS)):
        return "P5"
    if rule:
        tail = core.rstrip("。！!，,")[-1:]
        if any(h in text for h in w("task", TASK_HINTS)) or (
                tail and any(tail == q for q in QUESTION_TAIL)):
            return "P2"
        return "P4"      # 有归属但只是日常同步 → 只记日志，不写客户档案
    return "P4"


def route(text: str) -> dict:
    """返回 {level, rule, project, project_name, escalate, trace}"""
    rules = load_rules()
    rule = match_rule(text, rules)
    level = level_of(text, rule)
    esc_at = (rule or {}).get("escalate_at") or rules.get("defaults", {}).get("escalate_at", "P2")
    escalate = RANK.get(level, 9) <= RANK.get(esc_at, 2)
    why = []
    hit_p0 = [x for x in w("p0", P0_WORDS) if x in text]
    hit_p1 = [x for x in w("p1", P1_WORDS) if x in text]
    hit_task = [h for h in w("task", TASK_HINTS) if h in text]
    if rule:
        why.append(f"命中项目「{rule.get('name')}」（关键词：{[k for k in rule.get('match',[]) if k in text][:2]}）")
    else:
        why.append("没有匹配到任何项目规则")
    if hit_p0:
        why.append(f"含高危词{hit_p0[:3]} → P0")
    elif hit_p1:
        why.append(f"含事务词{hit_p1[:3]} → P1")
    elif rule and hit_task:
        why.append(f"含请求词{hit_task[:3]} → P2，需要处理")
    elif rule:
        why.append("有项目归属但不像任务 → 只留痕")

    trace_ok = RANK.get(level, 9) <= RANK["P3"]
    return {
        "level": level,
        "trace_ok": trace_ok,
        "why": "；".join(why),
        "rule_id": (rule or {}).get("id", ""),
        "rule_name": (rule or {}).get("name", ""),
        "project": (rule or {}).get("project", ""),
        "escalate": escalate,
        "trace": bool((rule or {}).get("trace", rules.get("defaults", {}).get("trace", True))),
    }


def append_trace(item: dict, routed: dict) -> str:
    """往对应项目的《沟通记录-YYYY-MM-DD.md》追加一条，返回落盘路径。"""
    project = routed.get("project")
    if not project:
        return ""
    root = Path(project)
    if not root.exists():
        return ""
    day = datetime.now().strftime("%Y-%m-%d")
    folder = root / "沟通记录"
    folder.mkdir(exist_ok=True)
    f = folder / f"沟通记录-{day}.md"
    if not f.exists():
        f.write_text(f"# 沟通记录 · {day}\n\n", encoding="utf-8")

    ts = datetime.now().strftime("%H:%M")
    action = "进待办等拍板" if routed["escalate"] else "自动留痕，未打扰"
    block = (
        f"\n## [中台自动] {ts} · {item.get('source','')}\n"
        f"- 级别：{routed['level']}"
        + (f"（规则 {routed['rule_id']} · {routed['rule_name']}）" if routed.get("rule_id") else "")
        + "\n"
        f"- 来源：{item.get('title','')}\n"
        f"- 摘要：{item.get('note','')}\n"
        f"- 处理：{action}\n"
    )
    with f.open("a", encoding="utf-8") as fp:
        fp.write(block)
    return str(f)


def log_action(item: dict, routed: dict, traced_to: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sid": item.get("sid", ""),
        "title": item.get("title", ""),
        "level": routed["level"],
        "rule": routed.get("rule_id", ""),
        "escalate": routed["escalate"],
        "trace": traced_to,
    }
    with LOG_FILE.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(rec, ensure_ascii=False) + "\n")


def recent_actions(n: int = 20) -> list[dict]:
    try:
        lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for line in lines[-n:]:
        try:
            out.append(json.loads(line))
        except Exception:  # noqa: BLE001
            pass
    return out
