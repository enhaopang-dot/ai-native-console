# -*- coding: utf-8 -*-
"""每天 19:00 给自己出一份进化建议：这套系统下一步该往哪优化。

思路：不写死方向。把系统眼下的真实状态（每个 Agent 通不通、多少事项停摆、
多少承诺逾期、最近动过哪些文件、上一份建议说了什么）交给模型，让它自己找
「今天最值得改的一件事」，并且必须说清改哪个文件、怎么验收。

产出落 data/evolve/YYYY-MM-DD.md，面板「规则」抽屉里直接看。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import ai
import store

BASE = Path(__file__).parent
OUT = BASE / "data/evolve"
HOUR = 19          # 每天几点出这份建议

SYS = """你是庞恩浩的 CEO Agent——不是写日报的秘书，是跟他一起把这套 AI Native 中台做出来的搭档。

这套系统的目标只有一个：**AI 主导、人为辅**。现在它偏统计、偏展示，判断和关联做得不够。
你要找的是「今天最值得动的一件事」，不是列一堆待办。

给你的材料是系统当前的真实状态。请按下面的顺序想：
1. 五张 Agent 卡里，哪张还停在"摆数字"？它缺的那一步具体是什么？
2. 事项层、承诺、停摆这些判断链，哪里还是死的（靠规则、靠人看）？
3. 哪个动作能让别的模块一起变好（杠杆最大）？
4. 有没有新的外部做法值得搬进来？

输出 markdown，严格这个结构，全文不超过 400 字：

## 今天最值得改的一件事
一句话说清改什么。

## 为什么是它
两到三条，每条都要引用给你的材料里的具体数字或事实，不许空谈。

## 怎么改
具体到文件、函数、要加的那一层。三到五步。

## 怎么算成了
可验收的判据，比如"面板上 X 从 13 条变成 8 件"这种，不要写"体验更好"。

## 顺带记一笔
一句话，今天不值得单独做但要记住的事；没有就写"无"。

不许输出这些：泛泛的"加强数据接入""提升智能化""优化用户体验"。
每条建议必须落到这个仓库里真实存在的文件或模块上。"""


def _recent_changes(days: int = 3) -> str:
    """最近改过什么：迭代记录 + 代码文件 mtime。"""
    out = []
    for p in sorted((BASE.parent).glob("迭代记录-*.md"))[-2:]:
        out.append(f"- {p.name}（{datetime.fromtimestamp(p.stat().st_mtime):%m-%d %H:%M}）")
    hot = []
    for py in BASE.glob("*.py"):
        try:
            hot.append((py.stat().st_mtime, py.name))
        except OSError:
            continue
    hot.sort(reverse=True)
    if hot:
        out.append("- 最近动过的代码：" + "、".join(n for _, n in hot[:5]))
    return "\n".join(out) or "（无记录）"


def _context() -> str:
    parts = []
    # Agent 通不通
    try:
        import collectors
        p = collectors.build_payload()
        rows = [f"- {a['name']}：{'通' if a['ok'] else '断'} · {a.get('value','')}{a.get('unit','')} · {a.get('meta','')}"
                for a in p["agents"]]
        parts.append("【五张 Agent 卡】\n" + "\n".join(rows))
        th = [i for i in (p.get("rules") or {}).get("items", [])]
        if th:
            parts.append("【自动化规则状态】\n" + "\n".join(th))
    except Exception as e:  # noqa: BLE001
        parts.append(f"【Agent 卡】读取失败：{e}")

    # 事项 / 停摆 / 承诺
    try:
        import threads as _th
        tl = _th.list_threads()
        owed = [t for t in tl if (t.get("owed") or "").strip()]
        parts.append(
            f"【事项层】未结 {len(tl)} 件，其中停摆 {sum(1 for t in tl if t['stalled'])} 件，"
            f"我方欠着 {len(owed)} 件\n"
            + "\n".join(f"- {t['title']}（{t['level']}，{t['n']} 条进展，停 {t['stalled']} 天"
                        + (f"，欠：{t['owed']}" if t.get("owed") else "") + "）"
                        for t in tl[:10]))
    except Exception as e:  # noqa: BLE001
        parts.append(f"【事项层】读取失败：{e}")

    # 数据健康
    try:
        st = json.loads((BASE / "data/monitor.json").read_text(encoding="utf-8"))
        s = st.get("stats") or {}
        parts.append(f"【巡检】新发现 {s.get('found', '?')} · 进待办 {s.get('added', '?')} · "
                     f"微信 {sum(1 for v in (s.get('wx_accounts') or {}).values() if v)}/{len(s.get('wx_accounts') or {})} 在线 · "
                     f"录音 {'通' if s.get('note_live') else '断'} · OA {'通' if s.get('oa_ok') else '断'}")
    except Exception:  # noqa: BLE001
        pass

    parts.append("【最近改动】\n" + _recent_changes())
    last = _last_report()
    if last:
        parts.append(f"【上一份建议（不要重复同一件事）】\n{last[:300]}")
    return "\n\n".join(parts)


def _path(day: str | None = None) -> Path:
    return OUT / f"{(day or datetime.now().strftime('%Y-%m-%d'))}.md"


def _last_report() -> str:
    files = sorted(OUT.glob("*.md"))
    if not files:
        return ""
    try:
        return files[-1].read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return ""


def today_report() -> str:
    p = _path()
    try:
        return p.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return ""


def generate(force: bool = False) -> dict:
    p = _path()
    if p.exists() and not force:
        return {"ok": True, "skipped": "今天已经出过", "path": str(p)}
    OUT.mkdir(parents=True, exist_ok=True)
    # 推理型模型会先吃掉一大截 token 想事情，预算给小了正文就空。给够，且短了再来一次。
    txt, last_err = "", ""
    for budget in (2600, 4000):
        try:
            txt = ai.chat(SYS, _context(), timeout=180, max_tokens=budget).strip()
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {str(e)[:100]}"
            continue
        if len(txt) >= 60:
            break
        last_err = f"模型输出过短（{len(txt)} 字，预算 {budget}）"
    if len(txt) < 60:
        return {"ok": False, "error": last_err or "模型输出过短"}
    head = f"# {datetime.now():%Y-%m-%d} 进化建议\n\n"
    p.write_text(head + txt + "\n", encoding="utf-8")
    return {"ok": True, "path": str(p), "chars": len(txt)}


def maybe_daily() -> dict:
    """每 10 分钟被叫一次：过了 19 点、今天还没出，就出一份。"""
    if datetime.now().hour < HOUR:
        return {"ok": True, "skipped": "未到点"}
    if _path().exists():
        return {"ok": True, "skipped": "今天已经出过"}
    return generate()


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(json.dumps(generate(force="--force" in sys.argv), ensure_ascii=False, indent=1))
