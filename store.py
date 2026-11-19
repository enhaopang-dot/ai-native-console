# -*- coding: utf-8 -*-
"""中台的结构化存储：data/console.db。

为什么不继续用 JSON：卡片、判定、比对三类数据要能按项目/级别/时间查，
还要能回答"模型判的和人最后做的是否一致"——这是调提示词的依据。
JSON 翻不动，SQL 一行就出来。

对外仍然导出一份 data/todo.json，方便肉眼查看和回滚。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
DB = BASE / "data/console.db"
TODO_JSON = BASE / "data/todo.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards(
  sid TEXT PRIMARY KEY,
  title TEXT, level TEXT, note TEXT, raw TEXT, source TEXT, project TEXT,
  reason TEXT, rule TEXT, media TEXT, url TEXT, link TEXT, ai TEXT,
  due TEXT, trace TEXT,
  created_at TEXT, updated_at TEXT,
  done INTEGER DEFAULT 0, done_at TEXT,
  skipped INTEGER DEFAULT 0, skipped_at TEXT
);
CREATE TABLE IF NOT EXISTS judge_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT, sid TEXT, title TEXT, level TEXT, need_me INTEGER,
  conclusion TEXT, actions TEXT, evidence TEXT, model TEXT
);
-- 事项层：卡片是按天切的日志，同一件事聊三天就三张卡。threads 把它们收成一件。
-- 卡片本身不删不改，thread_links 只记录「这张卡属于哪件事」。
CREATE TABLE IF NOT EXISTS threads(
  tid TEXT PRIMARY KEY,
  project TEXT, title TEXT, level TEXT, status TEXT DEFAULT 'open',
  next_step TEXT, first_at TEXT, last_at TEXT,
  created_at TEXT, updated_at TEXT, model TEXT, owed TEXT, title_at TEXT
);
CREATE TABLE IF NOT EXISTS thread_links(
  tid TEXT, sid TEXT, role TEXT, at TEXT,
  PRIMARY KEY(tid, sid)
);
CREATE TABLE IF NOT EXISTS thread_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT, action TEXT, tid TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS cross_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT, sid TEXT, text TEXT
);
-- 巡检看到了但判为「不用打扰」的，也要留痕，不然事后查不到这天说过什么
CREATE TABLE IF NOT EXISTS watch_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT, sid TEXT, project TEXT, title TEXT, level TEXT, why TEXT
);
CREATE TABLE IF NOT EXISTS scan_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT, found INTEGER, added INTEGER, auto INTEGER, payload TEXT
);
CREATE VIEW IF NOT EXISTS v_open AS
  SELECT sid, level, created_at, project, title, note, ai
  FROM cards WHERE done=0 AND skipped=0 ORDER BY created_at DESC;
-- 模型判定 vs 最终结果：把"模型说要我做但没人理"和"模型说不用但我留了"都暴露出来
CREATE VIEW IF NOT EXISTS v_judge_gap AS
  SELECT j.ts, j.sid, j.title, j.level AS ai_level, j.need_me,
         c.level AS final_level, c.done, c.skipped, j.conclusion
  FROM judge_log j LEFT JOIN cards c ON c.sid = j.sid
  ORDER BY j.ts DESC;
"""

FIELDS = ("sid title level note raw source project reason rule media url link ai due trace link_txt "
          "created_at updated_at done done_at skipped skipped_at").split()


def conn() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB, timeout=10)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    cols = {r[1] for r in c.execute("PRAGMA table_info(cards)")}
    if "link_txt" not in cols:
        c.execute("ALTER TABLE cards ADD COLUMN link_txt TEXT")
        c.commit()
    # 事项级别被人为改过就上锁，别让 _refresh 按卡片重新算回去
    tcols = {r[1] for r in c.execute("PRAGMA table_info(threads)")}
    if "lv_lock" not in tcols:
        c.execute("ALTER TABLE threads ADD COLUMN lv_lock INTEGER DEFAULT 0")
        c.commit()
    # 我方欠对方什么，由模型每轮归并时重写；空串=不欠。跟着事项走，事项销了它就没了。
    if "owed" not in tcols:
        c.execute("ALTER TABLE threads ADD COLUMN owed TEXT")
        c.commit()
    # 标题/下一步上一次由模型重写的时刻。last_at 比它新 = 事项往前走了、标签还停在旧说法，该重写。
    if "title_at" not in tcols:
        c.execute("ALTER TABLE threads ADD COLUMN title_at TEXT")
        c.commit()
    # 「跳过」= 这件事今天不碰，过一阵再冒出来。存到期时间，到点自动回到等你处理。
    if "snooze_until" not in tcols:
        c.execute("ALTER TABLE threads ADD COLUMN snooze_until TEXT")
        c.commit()
    return c


def _migrate(con: sqlite3.Connection) -> None:
    """首次运行把 todo.json 导进来。"""
    n = con.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
    if n or not TODO_JSON.exists():
        return
    try:
        items = json.loads(TODO_JSON.read_text(encoding="utf-8")).get("items", [])
    except Exception:  # noqa: BLE001
        return
    upsert(con, items)


def upsert(con: sqlite3.Connection, items: list[dict]) -> None:
    rows = []
    for it in items:
        rows.append((
            str(it.get("sid") or ""), it.get("title", ""), it.get("level", ""),
            it.get("note", ""), it.get("raw", ""), it.get("source", ""), it.get("project", ""),
            it.get("reason", ""), it.get("rule", ""), it.get("media", ""), it.get("url", ""),
            it.get("link", ""), it.get("ai", ""), it.get("due", ""), it.get("trace", ""),
            it.get("link_txt", ""),
            it.get("created_at", ""), it.get("updated_at", ""),
            1 if it.get("done") else 0, it.get("done_at", ""),
            1 if it.get("skipped") else 0, it.get("skipped_at", ""),
        ))
    con.executemany(
        "INSERT INTO cards(" + ",".join(FIELDS) + ") VALUES(" + ",".join("?" * len(FIELDS)) + ") "
        "ON CONFLICT(sid) DO UPDATE SET " +
        ",".join(f"{f}=excluded.{f}" for f in FIELDS if f != "sid"),
        rows)
    con.commit()


def load_todos() -> list[dict]:
    con = conn()
    _migrate(con)
    cur = con.execute("SELECT " + ",".join(FIELDS) + " FROM cards ORDER BY created_at DESC")
    out = []
    for r in cur.fetchall():
        d = dict(zip(FIELDS, r))
        d["done"] = bool(d["done"])
        d["skipped"] = bool(d["skipped"])
        out.append({k: v for k, v in d.items() if v not in ("", None, 0, False)})
    con.close()
    return out


def save_todos(items: list[dict]) -> None:
    con = conn()
    _migrate(con)
    keep = {str(i.get("sid") or "") for i in items}
    exist = {r[0] for r in con.execute("SELECT sid FROM cards")}
    for sid in exist - keep:
        con.execute("DELETE FROM cards WHERE sid=?", (sid,))
    upsert(con, items)
    con.close()
    # 仍然导出一份 JSON，方便肉眼看与回滚
    TODO_JSON.write_text(
        json.dumps({"updated": datetime.now().strftime("%Y-%m-%d"), "items": items},
                   ensure_ascii=False, indent=1), encoding="utf-8")


def log_judge(sid: str, title: str, out: dict, model: str = "") -> None:
    con = conn()
    con.execute(
        "INSERT INTO judge_log(ts,sid,title,level,need_me,conclusion,actions,evidence,model) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), sid, title,
         out.get("级别", ""), 1 if out.get("要我做") else 0,
         out.get("结论", ""), json.dumps(out.get("动作") or [], ensure_ascii=False),
         out.get("依据", ""), model))
    con.commit()
    con.close()


def log_cross(sid: str, text: str) -> None:
    if not text:
        return
    con = conn()
    con.execute("INSERT INTO cross_log(ts,sid,text) VALUES(?,?,?)",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), sid, text))
    con.commit()
    con.close()


def log_scan(stats: dict) -> None:
    con = conn()
    con.execute("INSERT INTO scan_log(ts,found,added,auto,payload) VALUES(?,?,?,?,?)",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 stats.get("found", 0), stats.get("added", 0), stats.get("auto", 0),
                 json.dumps(stats, ensure_ascii=False)))
    con.commit()
    con.close()


def q(sql: str, limit: int = 200, params: tuple = ()) -> list[tuple]:
    """临时查询用：python store.py "SELECT * FROM v_open LIMIT 5\""""
    con = conn()
    try:
        cur = con.execute(sql, params)
        return cur.fetchmany(limit) if cur.description else []
    finally:
        con.close()


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sql = sys.argv[1] if len(sys.argv) > 1 else "SELECT * FROM v_open LIMIT 5"
    rows = q(sql)
    for r in rows:
        print(" | ".join(str(x) for x in r))
    print(f"-- {len(rows)} 行")
