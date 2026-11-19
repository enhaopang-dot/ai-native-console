# -*- coding: utf-8 -*-
"""把客户B OA 的生产数据同步到本地，供中台只读使用。

为什么不在服务器上算：中台是本机程序，读本地快照最稳，也不碰生产库的写锁。
做法：ssh 到 ECS，用 sqlite3 以 CSV 导出需要的表，拉回来灌进 data/oa.db。

只读、只拉、不改生产库。
"""
from __future__ import annotations

import csv
import io
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
DB = BASE / "data/oa.db"
SSH_KEY = Path.home() / ".ssh" / "id_ed25519_caa_oa"
SERVER = "root@121.41.116.103"
REMOTE_DB = "/opt/client_b-oa/backend/data/company.db"

# 只拉这几张：产量、成品、项目、任务
TABLES = ["production_daily_reports", "finished_product_reports", "projects", "tasks"]


def _ssh_exe() -> str:
    """优先用系统自带 OpenSSH。

    PATH 里的 ssh 常被 Git 的 MSYS 版抢先；由 pythonw 悄悄拉起时它会报 0xc0000142
    （DLL 初始化失败），开机就弹一个「应用程序错误」框。系统自带的没这毛病。
    """
    for cand in (r"C:\Windows\System32\OpenSSH\ssh.exe",
                 r"C:\Program Files\Git\usr\bin\ssh.exe"):
        if Path(cand).exists():
            return cand
    return "ssh"


def _ssh(cmd: str, timeout: int = 120) -> str:
    r = subprocess.run(
        [_ssh_exe(), "-i", str(SSH_KEY), "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
         "-o", "ConnectTimeout=15", SERVER, cmd],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        capture_output=True, text=True, encoding="utf-8", errors="ignore", timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "ssh failed").strip()[:200])
    return r.stdout


def sync() -> dict:
    """拉全量（表都不大），覆盖写本地库。返回各表行数。"""
    DB.parent.mkdir(parents=True, exist_ok=True)
    tmp = DB.with_suffix(".tmp")
    if tmp.exists():
        tmp.unlink()
    con = sqlite3.connect(tmp)
    counts = {}

    for t in TABLES:
        try:
            out = _ssh(f"sqlite3 -header -csv 'file:{REMOTE_DB}?mode=ro' \"SELECT * FROM {t}\"")
        except Exception as e:  # noqa: BLE001
            counts[t] = f"ERR {str(e)[:60]}"
            continue
        if not out.strip():
            counts[t] = 0
            continue
        rdr = csv.DictReader(io.StringIO(out))
        cols = rdr.fieldnames or []
        rows = list(rdr)
        con.execute(f'DROP TABLE IF EXISTS "{t}"')
        con.execute(f'CREATE TABLE "{t}" (' + ",".join(f'"{c}" TEXT' for c in cols) + ")")
        if rows:
            con.executemany(
                f'INSERT INTO "{t}" VALUES(' + ",".join("?" * len(cols)) + ")",
                [[r.get(c, "") for c in cols] for r in rows])
        con.commit()
        counts[t] = len(rows)

    con.execute("CREATE TABLE IF NOT EXISTS _sync (at TEXT)")
    con.execute("DELETE FROM _sync")
    con.execute("INSERT INTO _sync VALUES(?)", (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),))
    con.commit()
    con.close()
    tmp.replace(DB)
    return counts


def last_sync() -> str:
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        r = con.execute("SELECT at FROM _sync").fetchone()
        con.close()
        return r[0] if r else ""
    except Exception:  # noqa: BLE001
        return ""


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(sync())
