# -*- coding: utf-8 -*-
"""让模型来判断"这条要不要我做"，而不是查词表。

规则只做三件事：把消息按会话/项目/天归组、兜底模型不可用、P0 词命中不许被降级。
其余交给模型：结合当天全部往来与录音转写，判断这件事的性质、级别、要不要打扰我。

成本控制：按「组」调用（一天十几组），按内容指纹缓存，单轮设上限。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import ai  # 复用同一个端点和密钥读取方式

BASE = Path(__file__).parent
CACHE = BASE / "data/judge_cache.json"
PROMPT = BASE / "data/judge_prompt.md"
_cache: dict = {}

SYS = """你是庞恩浩的私人助理。他只想知道一件事：这件事要不要他现在管。

给你一段当天的微信往来或会议记录（可能很长、可能夹杂客套与无关对话）。
判断标准：
- 需要他本人处理的：别人向他提的要求/问题、他自己答应的事、有截止日或约好的时间、会影响钱/合同/交付/关系的事。
- 不需要的：客套、通报、群里别人的互相协调、他自己已经做完的事、纯闲聊。
- 他自己说的话里答应的事，一律算"需要"，这是最容易被漏的一类。

严格输出 JSON，不要解释、不要代码块：
{"要我做": true 或 false,
 "级别": "P0|P1|P2|P3|P4|P5",
 "结论": "一句话说清这件事是什么，不超过35字",
 "动作": ["他要做的具体动作，1-2条，每条不超过20字"],
 "依据": "为什么这么判，引用原话片段，不超过30字"}

级别口径：P0 今天就得处理/会造成损失；P1 合同回款报价签约交付；P2 本周内要办；
P3 记着就行；P4 纯记录；P5 可丢。"""


def _parse(raw: str) -> dict:
    if not raw:
        return {}
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {}
    s = m.group(0)
    # 不替换中文引号：它常在字符串内容里（“发我”），改成英文引号会把 JSON 打断
    s = re.sub(r",\s*([}\]])", r"\1", s)
    try:
        d = json.loads(s)
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(d, dict) or "要我做" not in d:
        return {}
    d["动作"] = d.get("动作") or []
    d.setdefault("级别", "P3")
    d.setdefault("结论", "")
    return d


def system_prompt() -> str:
    """判定口径放在 data/judge_prompt.md，用户可直接改，下一轮生效。"""
    try:
        t = PROMPT.read_text(encoding="utf-8").strip()
        if len(t) > 80:
            return t
    except Exception:  # noqa: BLE001
        pass
    return SYS


def judge(key: str, text: str, steps: list[dict], extra: str = "") -> dict:
    """key=组标识（用来缓存），text=当天全部往来，steps=项目未结事项。"""
    sig = hashlib.md5(text.encode("utf-8")).hexdigest()[:12]
    hit = _cache.get(key)
    if hit and hit.get("sig") == sig:
        return hit["out"]

    undone = [s.get("text", "") for s in steps if not s.get("done")][:6]
    user = (
        ("【项目未结事项】\n" + "\n".join("- " + u for u in undone) + "\n\n" if undone else "")
        + ("【项目背景】\n" + extra + "\n\n" if extra else "")
        + "【当天往来/记录】\n" + text[:2600]
    )
    sp = system_prompt()
    out = _parse(ai.chat(sp, user, timeout=90, max_tokens=800))
    if not out:
        out = _parse(ai.chat(sp, user + "\n\n（只输出 JSON）", timeout=90, max_tokens=800))
    if not out:
        raise RuntimeError("judge bad output")

    _cache[key] = {"sig": sig, "out": out}
    try:
        CACHE.write_text(json.dumps(_cache, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return out


def load_cache() -> None:
    global _cache
    try:
        _cache = json.loads(CACHE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        _cache = {}


load_cache()
