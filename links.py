# -*- coding: utf-8 -*-
"""把消息里的链接扒下来看内容。

微信里活动、报名、文档经常只发一个链接，正文一个字没有。
不抓页面，模型就永远不知道"明天下午两点"在哪。

结果按 URL 缓存，避免每轮重复请求。
"""
from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path

BASE = Path(__file__).parent
CACHE = BASE / "data/link_cache.json"

URL_RE = re.compile(r"https?://[^\s，。；、）)】\"']+")
_cache: dict = {}


def _load() -> None:
    global _cache
    try:
        _cache = json.loads(CACHE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        _cache = {}


def fetch(url: str, timeout: float = 8) -> str:
    """返回页面里最像"时间/地点/要点"的一段文字；失败返回空串。"""
    if url in _cache:
        return _cache[url]
    txt = ""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        html = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore")
        # 不剥 script：活动页常把正文塞在脚本里的 JSON 中，剥了反而什么都取不到
        flat = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))

        title = re.search(r"<title>(.*?)</title>", html, re.S)
        picks = []
        if title:
            picks.append(title.group(1).strip()[:60])
        # 优先抓"日期 + 时间 + 地点"这种整句
        pats = [
            r"[^。！？]{0,24}\d{1,2}\s*月\s*\d{1,2}\s*日[^。！？]{0,70}",
            r"[^。！？]{0,16}\d{1,2}:\d{2}[^。！？]{0,50}",
            r"[^。！？]{0,10}(?:地点|地址)[^。！？]{0,50}",
        ]
        for p in pats:
            for m in re.finditer(p, flat):
                seg = m.group(0).strip()
                if len(seg) > 8 and seg not in picks and not any(seg in x for x in picks):
                    picks.append(seg)
                if len(picks) >= 4:
                    break
            if len(picks) >= 4:
                break
        txt = " ｜ ".join(picks)[:400]
    except Exception:  # noqa: BLE001
        txt = ""
    _cache[url] = txt
    try:
        CACHE.write_text(json.dumps(_cache, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return txt


def previews(text: str, maxn: int = 2) -> str:
    urls = list(dict.fromkeys(URL_RE.findall(text or "")))[:maxn]
    out = []
    for u in urls:
        t = fetch(u)
        if t:
            out.append(f"[链接] {u} → {t}")
    return " ｜ ".join(out)


_load()
