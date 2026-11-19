# -*- coding: utf-8 -*-
"""模型端点解析。优先级从高到低：

1. `data/llm.json`——面板自己的显式配置，想钉死模型就写这里
2. CC Switch 配置库 `~/.cc-switch/cc-switch.db` 里 `is_current=1` 的 claude 供应商
3. `~/.claude/settings.json` 的 env 块

直读 CC Switch 是为了绕开一个老坑：切换供应商会覆盖 settings.json 的 env，
面板跟着就 401（2026-09-25 踩过）。
密钥只在内存里用，不打印、不落盘、不进日志。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path

BASE = Path(__file__).parent
LOCAL = BASE / "data/llm.json"
CCDB = Path(os.path.expanduser("~/.cc-switch/cc-switch.db"))
SETTINGS = Path("D:/Claude/配置/.claude/settings.json")


def _from_local() -> dict:
    try:
        d = json.loads(LOCAL.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return normalise(d)


def _from_ccswitch(name_hint: str = "") -> dict:
    try:
        con = sqlite3.connect(f"file:{CCDB}?mode=ro", uri=True, timeout=5)
        rows = con.execute(
            "SELECT name, settings_config FROM providers "
            "WHERE app_type='claude' ORDER BY (is_current='1') DESC, sort_index").fetchall()
        con.close()
    except Exception:  # noqa: BLE001
        return {}
    for name, raw in rows:
        if name_hint and name_hint.lower() not in (name or "").lower():
            continue
        try:
            env = (json.loads(raw) or {}).get("env", {}) or {}
        except Exception:  # noqa: BLE001
            continue
        if env.get("ANTHROPIC_AUTH_TOKEN") and env.get("ANTHROPIC_BASE_URL"):
            return normalise({
                "base": env.get("ANTHROPIC_BASE_URL", ""),
                "token": env.get("ANTHROPIC_AUTH_TOKEN", ""),
                "model": env.get("ANTHROPIC_MODEL", ""),
                "source": f"cc-switch:{name}",
            })
    return {}


def _from_settings() -> dict:
    try:
        env = (json.loads(SETTINGS.read_text(encoding="utf-8")) or {}).get("env", {}) or {}
    except Exception:  # noqa: BLE001
        return {}
    return normalise({
        "base": env.get("ANTHROPIC_BASE_URL", ""),
        "token": env.get("ANTHROPIC_AUTH_TOKEN", ""),
        "model": env.get("ANTHROPIC_MODEL", ""),
        "source": "settings.json",
    })


def normalise(d: dict) -> dict:
    model = re.sub(r"\[.*?\]", "", str(d.get("model") or "")).strip()
    return {
        "base": str(d.get("base") or "").rstrip("/"),
        "token": str(d.get("token") or ""),
        "model": model or "deepseek-flash",
        "source": d.get("source", ""),
    }


def resolve() -> dict:
    for fn in (_from_local,
               lambda: _from_ccswitch("deepseek"),
               _from_ccswitch,
               _from_settings):
        d = fn()
        if d.get("token") and d.get("base"):
            return d
    return {"base": "", "token": "", "model": "", "source": "none"}


def describe() -> str:
    d = resolve()
    if not d["token"]:
        return "未接入"
    return f"{d['model']} · {d['source']}"


def chat(system: str, user: str, timeout: float = 90, max_tokens: int = 900) -> str:
    import urllib.request
    d = resolve()
    if not d["token"]:
        raise RuntimeError("no llm config")
    body = json.dumps({
        "model": d["model"],
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }).encode()
    req = urllib.request.Request(
        f"{d['base']}/v1/messages", data=body,
        headers={"content-type": "application/json",
                 "x-api-key": d["token"],
                 "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
    return "".join(b.get("text", "") for b in data.get("content", [])
                   if b.get("type") == "text").strip()


def check() -> tuple[bool, str]:
    d = resolve()
    if not d["token"]:
        return False, "未找到可用的模型配置"
    try:
        out = chat("只回四个字", "回答：连接正常", timeout=30, max_tokens=20)
        return True, f"{describe()}｜返回：{out[:20]}"
    except Exception as e:  # noqa: BLE001
        return False, f"{describe()}｜失败：{type(e).__name__}: {str(e)[:80]}"


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ok, msg = check()
    print(("OK  " if ok else "FAIL") + " " + msg)
