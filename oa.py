# -*- coding: utf-8 -*-
"""客户B OA 生产数据的看数：断更、产量、质量、逾期。

只回答"哪里不正常"，不做建议——建议交给上游 judge。
数据来源：oa_sync.py 拉回本地的 data/oa.db。
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

BASE = Path(__file__).parent
DB = BASE / "data/oa.db"


def _q(con, sql: str, params: tuple = (), default=0):
    """表可能没同步过来（远端没这张表），查不到就给默认值，别把整页拖挂。"""
    try:
        return con.execute(sql, params).fetchone()
    except sqlite3.OperationalError:
        return default


def _con():
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=8)


def snapshot() -> dict:
    if not DB.exists():
        return {"ok": False, "why": "还没有 oa.db，先跑 oa_sync.py"}
    con = _con()
    try:
        today = date.today()
        last = con.execute("SELECT MAX(report_date) FROM production_daily_reports").fetchone()[0] or ""
        gap = (today - datetime.strptime(last, "%Y-%m-%d").date()).days if last else -1

        def win(days: int, offset: int = 0):
            a = (today - timedelta(days=days + offset)).isoformat()
            b = (today - timedelta(days=offset)).isoformat()
            return a, b

        a1, b1 = win(7)
        a0, b0 = win(7, 7)
        q_now = con.execute(
            "SELECT COALESCE(SUM(CAST(quantity AS REAL)),0) FROM production_daily_reports "
            "WHERE report_date >= ? AND report_date <= ?", (a1, b1)).fetchone()[0]
        q_prev = con.execute(
            "SELECT COALESCE(SUM(CAST(quantity AS REAL)),0) FROM production_daily_reports "
            "WHERE report_date >= ? AND report_date <= ?", (a0, b0)).fetchone()[0]

        rework, scrap = con.execute(
            "SELECT COALESCE(SUM(CAST(rework_quantity AS REAL)),0), "
            "COALESCE(SUM(CAST(scrap_quantity AS REAL)),0) "
            "FROM production_daily_reports WHERE report_date >= ? AND report_date <= ?",
            (a1, b1)).fetchone()

        row = _q(con,
                 "SELECT MAX(report_date), COALESCE(SUM(CAST(quantity AS REAL)),0) FROM finished_product_reports "
                 "WHERE report_date >= ?", (a1,), (None, 0))
        fin_last, fin_qty = row

        overdue = _q(con,
                     "SELECT COUNT(*) FROM tasks WHERE COALESCE(status,'') NOT IN ('done','cancelled','closed') "
                     "AND planned_end <> '' AND planned_end < ?", (today.isoformat(),), (0,))[0]

        active_proj = _q(con, "SELECT COUNT(*) FROM projects WHERE status='active'", (), (0,))[0]

        # 近 7 天谁没报（按车间）
        shops = [r[0] for r in con.execute(
            "SELECT DISTINCT workshop FROM production_daily_reports "
            "WHERE report_date >= ? AND workshop <> ''", (a1,))]
        idle = []
        for w in shops[:12]:
            n = con.execute(
                "SELECT COUNT(DISTINCT report_date) FROM production_daily_reports "
                "WHERE workshop=? AND report_date >= ?", (w, a1)).fetchone()[0]
            if n <= 2:
                idle.append((w, n))
    finally:
        con.close()

    return {
        "ok": True, "today": today.isoformat(), "last_report": last, "gap_days": gap,
        "qty_7d": round(q_now or 0, 1), "qty_prev_7d": round(q_prev or 0, 1),
        "rework_7d": round(rework or 0, 1), "scrap_7d": round(scrap or 0, 1),
        "fin_last": fin_last or "", "fin_qty_7d": round(fin_qty or 0, 1),
        "overdue_tasks": overdue, "active_projects": active_proj, "idle_workshops": idle,
    }


def anomalies(s: dict) -> list[dict]:
    """只报真异常，不报正常波动。"""
    if not s.get("ok"):
        return [{"key": "oa-missing", "level": "P2", "title": "OA 数据未同步",
                 "note": s.get("why", "")}]
    out = []
    if s["gap_days"] >= 2:
        out.append({"key": "oa-gap", "level": "P1", "title": f"生产日报断更 {s['gap_days']} 天",
                    "note": f"最后一条 {s['last_report']}，今天 {s['today']}"})

    prev = s["qty_prev_7d"]
    if prev and s["qty_7d"] < prev * 0.7:
        out.append({"key": "oa-drop", "level": "P2", "title": "近 7 天产量环比下降超三成",
                    "note": f"本周 {s['qty_7d']} / 上周 {prev}"})

    total = s["qty_7d"] or 1
    if (s["rework_7d"] + s["scrap_7d"]) / total > 0.05:
        out.append({"key": "oa-quality", "level": "P2", "title": "返工+报废占比超过 5%",
                    "note": f"返工 {s['rework_7d']}、报废 {s['scrap_7d']}，产量 {s['qty_7d']}"})

    if s["overdue_tasks"] >= 3:
        out.append({"key": "oa-task", "level": "P2", "title": f"OA 逾期任务 {s['overdue_tasks']} 条",
                    "note": "计划完成时间已过仍未关闭"})

    for w, n in s["idle_workshops"][:2]:
        out.append({"key": f"oa-idle-{w}", "level": "P3",
                    "title": f"{w} 近 7 天只报 {n} 天", "note": "可能是停产、也可能是漏报"})
    return out


if __name__ == "__main__":
    import json
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    s = snapshot()
    print(json.dumps(s, ensure_ascii=False, indent=1))
    print("--- 异常 ---")
    for a in anomalies(s):
        print(a["level"], a["title"], "|", a["note"])
