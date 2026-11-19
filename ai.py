# -*- coding: utf-8 -*-
"""让模型读卡片 + 项目文档，回答"这条对项目意味着什么"。

密钥纪律：只从 settings.json 的 env 块读，不打印、不落盘、不写日志。
调用失败一律降级为规则版结论，不阻断巡检。
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
SETTINGS = Path("D:/Claude/配置/.claude/settings.json")
CACHE = BASE / "data/ai_cache.json"

_cache: dict = {}


def _env() -> dict:
    """兼容旧调用：真正解析交给 llmcfg（本地配置 > CC Switch > settings.json）。"""
    import llmcfg
    d = llmcfg.resolve()
    return {"token": d["token"], "base": d["base"], "model": d["model"]}


def ready() -> bool:
    return bool(_env()["token"])


def chat(system: str, user: str, timeout: float = 90, max_tokens: int = 900) -> str:
    import llmcfg
    return llmcfg.chat(system, user, timeout=timeout, max_tokens=max_tokens)


def _chat_unused(system: str, user: str, timeout: float = 90, max_tokens: int = 900) -> str:
    c = _env()
    if not c["token"]:
        raise RuntimeError("no token")
    body = json.dumps({
        "model": c["model"],
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }).encode()
    req = urllib.request.Request(
        f"{c['base']}/v1/messages", data=body,
        headers={"content-type": "application/json",
                 "x-api-key": c["token"],
                 "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
    parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
    return "".join(parts).strip()


def _grams(t: str) -> set:
    t = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", t)
    return {t[i:i + 2] for i in range(len(t) - 1)}


def pick_docs(text: str, docs: list[tuple[str, str]], n: int = 3, size: int = 800) -> str:
    """按二字重叠挑最相关的几篇，各截一段。"""
    g = _grams(text)
    scored = sorted(docs, key=lambda d: -len(g & _grams(d[1])))
    out = []
    for name, body in scored[:n]:
        if not (g & _grams(body)):
            continue
        out.append(f"## {name}\n{body[:size]}")
    return "\n\n".join(out)


SYS = (
    "你是庞恩浩的项目助理。给你一张从微信/录音里整理出来的卡片，以及这个项目已有的文档摘录。"
    "只做判断，不写套话。输出严格 JSON，不要代码块，字段：\n"
    '{"意味":"这条内容对项目意味着什么，一句话，不超过40字",'
    '"关系":"推进|矛盾|新信息|无关",'
    '"冲突":"若与现有记录矛盾，写明是哪份文档的什么说法，没有就空字符串",'
    '"动作":["下一步该做什么，1-3条，每条不超过25字"],'
    '"置信":"high|mid|low"}'
)


def _parse(raw: str) -> dict:
    """容忍代码块、前后解释、字段缺失。"""
    if not raw:
        return {}
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {}
    try:
        d = json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        # 常见坏法：尾逗号、中文引号
        s = m.group(0)
        # 不替换中文引号：它常在字符串内容里，改成英文引号会把 JSON 打断
        s = re.sub(r",\s*([}\]])", r"\1", s)
        try:
            d = json.loads(s)
        except Exception:  # noqa: BLE001
            return {}
    if not isinstance(d, dict) or not d.get("意味"):
        return {}
    d.setdefault("关系", "")
    d["动作"] = d.get("动作") or []
    return d


def analyze(card: dict, docs: list[tuple[str, str]], steps: list[dict]) -> dict:
    """返回 {意味, 关系, 冲突, 动作, 置信}；失败抛异常由调用方降级。"""
    text = f"{card.get('title','')}\n{card.get('note','')}\n{card.get('raw','')}"
    sig = str(hash(text))
    key = card.get("sid", "")
    hit = _cache.get(key)
    if hit and hit.get("sig") == sig:
        return hit["out"]

    undone = [s.get("text", "") for s in steps if not s.get("done")][:6]
    user = (
        f"项目：{card.get('project') or '未归属'}\n"
        f"日期：{card.get('created_at','')}　级别：{card.get('level','')}\n\n"
        f"【卡片】\n{text[:1200]}\n\n"
        f"【项目未结事项】\n" + ("\n".join("- " + u for u in undone) or "（无）") +
        f"\n\n【项目文档摘录】\n{pick_docs(text, docs) or '（无相关文档）'}"
    )
    raw = chat(SYS, user)
    out = _parse(raw)
    if not out:                      # 模型偶尔不按 JSON 回，重试一次
        raw = chat(SYS, user + "\n\n（只输出 JSON，不要任何解释）")
        out = _parse(raw)
    if not out:
        raise RuntimeError(f"bad output: {raw[:60]}")

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


def line_of(out: dict) -> str:
    """把结果压成面板上的一行。"""
    if not out:
        return ""
    bits = [out.get("意味", "")]
    if out.get("冲突"):
        bits.append(f"冲突：{out['冲突']}")
    acts = [a for a in (out.get("动作") or []) if a][:2]
    if acts:
        bits.append("下一步：" + "；".join(acts))
    return "｜".join(b for b in bits if b)


load_cache()
