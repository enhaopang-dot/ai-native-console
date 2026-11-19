# -*- coding: utf-8 -*-
"""把卡片内容与项目里已有的文档做交叉比对。

只做三件可核查的事，不猜：
  1. 实体核对——卡片里的日期/金额/人名，项目文档里有没有出现过
  2. 方向核对——卡片说"撤回/终止"，项目文档还写着"推进中"，就是矛盾，要摆出来
  3. 未结事项——卡片内容与 data/next_steps.json 里未关闭的建议是否相关
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
STEPS = BASE / "data/next_steps.json"

MAX_FILES = 24
MAX_CHARS = 20000

_cache: dict[str, tuple[float, list[tuple[str, str]]]] = {}

# 方向相反的词对：见到就比对，不做语义推理
NEGATIVE = ("撤回", "终止", "停止推进", "取消", "不做了", "退出")
POSITIVE = ("推进", "申报", "进行中", "组织实施", "建设中")

DATE_RE = re.compile(r"(\d{1,2}\s*月\s*\d{0,2}\s*[日号]?|\d{1,2}\s*月初|1[0-2]\s*月)")
MONEY_RE = re.compile(r"(\d+(?:\.\d+)?\s*(?:万|万元|元))")

# 引用时优先引这几类文档：纪要与沟通记录是"上次说到哪"，申报书是"对外承诺了什么"
PRIORITY = ("纪要", "沟通记录", "项目记忆", "申报书", "方案")


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="ignore")[:MAX_CHARS]
    except Exception:  # noqa: BLE001
        return ""


def corpus(project_path: str) -> list[tuple[str, str]]:
    """项目目录里的 Markdown（申报书、方案、纪要、沟通记录、记忆）。带 mtime 缓存。"""
    if not project_path:
        return []
    root = Path(project_path)
    if not root.exists():
        return []
    try:
        files = sorted(root.rglob("*.md"))[:MAX_FILES]
        stamp = max((f.stat().st_mtime for f in files), default=0)
    except OSError:
        return []
    hit = _cache.get(project_path)
    if hit and hit[0] == stamp:
        return hit[1]
    out = [(f.name, _read(f)) for f in files]
    _cache[project_path] = (stamp, out)
    return out


def _entities(text: str) -> set[str]:
    return set(DATE_RE.findall(text)) | set(MONEY_RE.findall(text))


def cross(text: str, project_path: str, project_name: str = "") -> str:
    """返回一句话的关联结论；没东西可说就返回空串。"""
    docs = corpus(project_path)
    if not docs:
        return ""
    docs = sorted(docs, key=lambda d: -sum(1 for k in PRIORITY if k in d[0]))

    bits = []
    hits = 0
    seen_dates = set()
    for name, body in docs:
        ents = [e for e in _entities(text) if e in body]
        if ents:
            hits += 1
            seen_dates.update(ents[:2])
    if hits:
        sample = "、".join(list(seen_dates)[:3])
        bits.append(f"项目文档 {len(docs)} 篇中 {hits} 篇提到相同实体（{sample}）" if sample
                    else f"项目文档 {len(docs)} 篇中 {hits} 篇有对应内容")

    # 方向核对：卡片说撤、文档还在推进
    neg = [w for w in NEGATIVE if w in text]
    if neg:
        pos_docs = [n for n, b in docs if any(w in b for w in POSITIVE)]
        if pos_docs:
            bits.append(f"与「{pos_docs[0][:18]}」方向冲突（本次「{neg[0]}」）→ 需同步更新材料")

    # 未结事项
    try:
        d = json.loads(STEPS.read_text(encoding="utf-8"))
        items = d.get("projects", {}).get(project_name, [])
    except Exception:  # noqa: BLE001
        items = []
    for it in items:
        if it.get("done"):
            continue
        kws = [k for k in (it.get("keywords") or []) if k and k in text]
        if len(kws) >= 2:
            bits.append(f"关联未结事项「{it.get('text','')[:24]}」")
            break

    return "；".join(bits)


def summary(project_path: str) -> str:
    """项目文档家底一句话，卡片上展示用。"""
    docs = corpus(project_path)
    if not docs:
        return ""
    return f"{len(docs)} 篇：" + "、".join(n[:12] for n, _ in docs[:4]) + ("…" if len(docs) > 4 else "")


if __name__ == "__main__":
    import sys
    path = sys.argv[1]
    txt = sys.argv[2] if len(sys.argv) > 2 else ""
    print("家底：", summary(path))
    if txt:
        print("关联：", cross(txt, path) or "（无可关联内容）")
