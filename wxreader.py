# -*- coding: utf-8 -*-
"""直读微信 4.x 解密库，拿到全部会话（含新建群）。

比运行时接口强的地方：接口只吐 26 个会话，这里能覆盖全部 374+ 个会话。
微信 4.x 结构：
  message_message_N.db  每会话一张 Msg_<md5(会话名)> 表
  Name2Id(rowid, user_name)              发送者 id → wxid
  WCDB_CT_message_content=4 表示 zstd 压缩
联系人名在 contact_contact.db 的 contact 表（remark / nick_name）
群名在 contact 表的 chat_room 相关字段或 group 表
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

try:
    import zstandard as zstd
    _ZSTD = zstd.ZstdDecompressor()
except Exception:  # noqa: BLE001
    _ZSTD = None

RUNTIME = Path(""<你的本地路径>"")
# 微信 4.x 的原始数据根目录（解密的源），按顺序找
SRC_ROOTS = [Path("D:/RJ/Weixinjilu/xwechat_files"),
             Path.home() / "Documents" / "xwechat_files"]

_QUOTE_PREFIX = re.compile(r"^(?:wxid_[0-9A-Za-z]+|\d+@chatroom)[:：]\s*")


def accounts() -> list[str]:
    """扫描 runtime 目录发现所有已登录账号（不写死，加第三个微信自动纳入）。"""
    try:
        names = [d.name for d in RUNTIME.iterdir()
                 if (d / "decrypted" / "_monitor_cache").exists()]
    except OSError:
        return []
    return sorted(names)


def login_state() -> dict:
    """监控进程写的登录态：{账号目录: 是否登着}。
    extract 成功 = 微信在跑且登着这个号，比"最近有没有人发消息"可靠。"""
    try:
        return json.loads((RUNTIME / "login_state.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def source_mtime(acc: str) -> float:
    """该账号原始库里最新的一次写盘时间。
    号登着就算没人找你，微信也在写库（收包、读回执、索引）；这个数骗不了人。"""
    for root in SRC_ROOTS:
        d = root / acc / "db_storage" / "message"
        if not d.exists():
            continue
        try:
            return max((f.stat().st_mtime for f in d.glob("*.db")), default=0.0)
        except OSError:
            return 0.0
    return 0.0


def weixin_processes() -> int:
    """微信客户端进程数。4.x 一个实例大约 6 个进程，多开就是 12 个。
    账号在不在线，先看客户端有没有在跑，比"最近有没有人发消息"实在。"""
    try:
        import subprocess
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=8,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout or ""
        return sum(1 for ln in out.splitlines() if "Weixin.exe" in ln)
    except Exception:  # noqa: BLE001
        return 0


def self_wxid(acc: str) -> str:
    """目录名形如 `wxid_<11位>_<4位后缀>`，剥掉最后一个下划线段就是本机 wxid。

    后缀不一定是数字（有 `_4765` 也有 `_ca93`），所以按段剥而不是按 `_\\d+$`。
    """
    parts = acc.split("_")
    return "_".join(parts[:2]) if len(parts) >= 3 else acc


ACCOUNTS = accounts()


def _cache_dir(acc: str) -> Path:
    return RUNTIME / acc / "decrypted" / "_monitor_cache"


def _open(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)


def _decode(blob, cflag) -> str:
    if blob is None:
        return ""
    if isinstance(blob, bytes):
        if cflag == 4 and _ZSTD is not None:
            try:
                blob = _ZSTD.decompress(blob)
            except Exception:  # noqa: BLE001
                pass
        for enc in ("utf-8", "gbk", "latin-1"):
            try:
                return blob.decode(enc, errors="ignore")
            except Exception:  # noqa: BLE001
                continue
        return ""
    return str(blob)


_LEAD_JUNK = re.compile(r"^[\s -​　\x00-\x1f﻿]+")
_XML_HEAD = re.compile(r"^\s*<\?xml[^>]*\?>\s*")


def _clean(body: str) -> str:
    """去控制字符 / 系统消息 / 引用头，把 appmsg 卡片换成 [文件] 标题。

    层层剥：引用头（"wxid_x:"）后面可能再跟一段 <?xml>，剥完要回头再剥一次，
    所以用循环直到稳定，不能只做一遍。
    """
    for _ in range(4):
        body = _LEAD_JUNK.sub("", _XML_HEAD.sub("", _LEAD_JUNK.sub("", body)))
        lines = body.split("\n")
        if len(lines) > 1:
            first = lines[0].strip()
            if (len(first) <= 40 and first.endswith((":", "："))
                    and not re.search(r"[，。！？!?]", first)):
                body = "\n".join(lines[1:])
                continue
        break
    body = body.strip()
    if body.startswith("<sysmsg"):
        return ""
    if body.startswith("<msg") or body.startswith("<appmsg"):
        m = re.search(r"<title>(.*?)</title>", body, re.S)
        return f"[文件] {m.group(1).strip()}" if m else "[卡片]"
    return body


def load_names(acc: str) -> dict:
    """username → 显示名（备注优先，其次昵称）。"""
    out = {}
    p = _cache_dir(acc) / "contact_contact.db"
    if not p.exists():
        return out
    try:
        con = _open(p)
        for un, remark, nick in con.execute(
                "SELECT username, remark, nick_name FROM contact"):
            out[un] = (remark or nick or un)
        con.close()
    except Exception:  # noqa: BLE001
        pass
    return out


def table_map(cur) -> dict:
    """Msg_<md5> → 会话 username。"""
    try:
        names = [(r[1], hashlib.md5(r[1].encode()).hexdigest())
                 for r in cur.execute("SELECT rowid, user_name FROM Name2Id")]
    except Exception:  # noqa: BLE001
        return {}
    h2n = {h: n for n, h in names}
    out = {}
    for (tab,) in cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"):
        un = h2n.get(tab[4:])
        if un:
            out[tab] = un
    return out


def read_account(acc: str, since: float, limit_per_chat: int = 50) -> list[dict]:
    """读某账号 since 之后的所有消息。"""
    d = _cache_dir(acc)
    if not d.exists():
        return []
    names_map = load_names(acc)
    me = self_wxid(acc)
    rows: list[dict] = []

    for db in sorted(d.glob("message_message_*.db")):
        try:
            con = _open(db)
            cur = con.cursor()
            id2user = {r[0]: r[1] for r in cur.execute("SELECT rowid, user_name FROM Name2Id")}
            for tab, chat in table_map(cur).items():
                try:
                    q = (f"SELECT real_sender_id, local_type, create_time, "
                         f"message_content, WCDB_CT_message_content FROM '{tab}' "
                         f"WHERE create_time >= ? ORDER BY create_time DESC LIMIT ?")
                    for sid, ltype, ct, content, cflag in cur.execute(q, (int(since), limit_per_chat)):
                        sender = id2user.get(sid, "")
                        is_self = (sender == me)
                        body = _clean(_decode(content, cflag))
                        if not body:
                            continue
                        rows.append({
                            "acc": acc,
                            "chat": chat,
                            "chat_name": names_map.get(chat, chat),
                            "sender": "我" if is_self else names_map.get(sender, sender),
                            "is_self": is_self,
                            "type": ltype,
                            "ts": int(ct or 0),
                            "text": body,
                        })
                except Exception:  # noqa: BLE001
                    continue
            con.close()
        except Exception:  # noqa: BLE001
            continue
    rows.sort(key=lambda x: x["ts"])
    return rows


def latest_ts(acc: str) -> float:
    """该账号最后一条消息的时间戳。判断账号是不是真在线，比端口可靠：
    端口开着只说明本地解密进程在跑，微信早退了它照样开着。"""
    d = _cache_dir(acc)
    mx = 0
    if not d.exists():
        return 0
    for db in sorted(d.glob("message_message_*.db")):
        try:
            con = _open(db)
            cur = con.cursor()
            for (tab,) in cur.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"):
                try:
                    r = cur.execute(f'SELECT MAX(create_time) FROM "{tab}"').fetchone()
                    mx = max(mx, int(r[0] or 0))
                except Exception:  # noqa: BLE001
                    continue
            con.close()
        except Exception:  # noqa: BLE001
            continue
    return mx


def search(keyword: str, days: int = 7) -> list[dict]:
    """按关键词搜全部会话（调试用）。"""
    since = datetime.now().timestamp() - days * 86400
    out = []
    for acc in ACCOUNTS:
        for m in read_account(acc, since, limit_per_chat=500):
            if keyword in m["text"] or keyword in m["chat_name"]:
                out.append(m)
    return out


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    kw = sys.argv[1] if len(sys.argv) > 1 else ""
    if kw:
        hits = search(kw, days=7)
        print(f"命中 {len(hits)} 条")
        for m in hits[:10]:
            t = datetime.fromtimestamp(m["ts"]).strftime("%m-%d %H:%M")
            print(f"  {t} | {m['chat_name'][:18]} | {'我' if m['is_self'] else m['sender']}: {m['text'][:40]}")
    else:
        now = datetime.now().timestamp()
        allm = []
        for acc in ACCOUNTS:
            allm += read_account(acc, now - 86400)
        print(f"24h 消息 {len(allm)} 条，会话 {len({m['chat'] for m in allm})} 个")
