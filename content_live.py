# -*- coding: utf-8 -*-
"""内容平台实时取数：从创作者后台抓每条作品的播放与审核状态。

为什么要这个：面板原来读的是 douyin.db，那是人工从创作者中心导出后入库的快照，
按天越来越旧，也拿不到「审核中 / 仅自己可见 / 违规」这类状态。
所以「这条是不是被限流了」这个问题，面板原来根本答不了。

判定口径（逐条算，不按账号平均）：
- 状态不是「已发布」且已过 3 小时  → 卡审
- 发布超 24 小时，播放不到该号近 30 天中位数的 15% → 疑似限流
- 同上但落在 15%–40% → 偏低，只提示不告警

落 data/content_live.json，collectors 优先读它，读不到再退回 douyin.db。
"""
from __future__ import annotations

import asyncio
import io
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
OUT = BASE / "data/content_live.json"
CK = Path(r"D:/Claude/项目/14 内容管理/05-发布与复盘/自动发布工具/social-auto-upload/cookies")
CK_DOUYIN = CK / "douyin_jiuliai.json"
CK_TENCENT = CK / "tencent_pangenh.json"

DOUYIN_URL = "https://creator.douyin.com/creator-micro/content/manage"
TENCENT_URL = "https://channels.weixin.qq.com/platform/post/list"

GOOD_STATE = ("已发布", "发布成功")

# 必须取挂载在文档里的节点的 innerText：cloneNode 出来的节点没有排版，
# innerText 会退化成 textContent，换行全丢，正则一条也匹配不上
TEXT_JS = """() => (document.body ? document.body.innerText : '')"""

# 日期 → 状态 → 播放 → 点赞/评论/分享/收藏，顺序固定
ROW = re.compile(
    r"(20\d{2})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2}):(\d{2})\n"
    r"([^\n]{1,12})\n"
    r"播放\n([^\n]+)\n"
    r"(?:点赞\n([^\n]+)\n)?"
    r"(?:评论\n([^\n]+)\n)?"
    r"(?:分享\n([^\n]+)\n)?"
    r"(?:收藏\n([^\n]+)\n)?")


def _num(s) -> float:
    """13.8万 → 138000；1.2亿 → 120000000"""
    s = str(s or "").strip().replace(",", "")
    m = re.match(r"^([\d.]+)\s*(万|亿|w|W)?$", s)
    if not m:
        return 0.0
    v = float(m.group(1))
    u = m.group(2)
    if u in ("万", "w", "W"):
        v *= 10000
    elif u == "亿":
        v *= 100000000
    return v


def parse_works(text: str) -> list[dict]:
    """从整页正文里按固定顺序切作品。

    不去 DOM 里找卡片容器：创作者后台的卡片嵌套七八层，
    按容器切不是切出半个页面，就是把 87 个父容器全算成作品。
    """
    out, seen = [], set()
    for m in ROW.finditer(text or ""):
        y, mo, d, hh, mi, state, play, like, cmt, shr, sav = m.groups()
        try:
            pub = datetime(int(y), int(mo), int(d), int(hh), int(mi))
        except ValueError:
            continue
        w = {
            "pub": pub.strftime("%Y-%m-%d %H:%M"),
            "state": (state or "").strip() or "—",
            "play": int(_num(play)),
            "like": int(_num(like)),
            "comment": int(_num(cmt)),
            "share": int(_num(shr)),
            "save": int(_num(sav)),
        }
        k = (w["pub"], w["play"], w["state"])
        if k in seen:
            continue
        seen.add(k)
        out.append(w)
    # 页面顺序是「置顶在前」，不是按时间；不排一下，最新一条会被当成置顶那个旧的
    out.sort(key=lambda w: w["pub"], reverse=True)
    return out


def _median(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def judge(works: list[dict], now: datetime | None = None) -> dict:
    """逐条判。基线取该号已发布且有播放的作品中位数。"""
    now = now or datetime.now()
    base = _median([w["play"] for w in works if w["state"] in GOOD_STATE and w["play"] > 0])
    alerts, lows = [], []
    for w in works:
        try:
            age = (now - datetime.strptime(w["pub"], "%Y-%m-%d %H:%M")).total_seconds() / 3600
        except ValueError:
            continue
        w["age_h"] = round(age, 1)
        w["ratio"] = round(w["play"] / base, 3) if base else 0.0
        if w["state"] not in GOOD_STATE and age >= 3:
            alerts.append({**w, "why": f"发布 {age:.0f} 小时仍是「{w['state']}」"})
        elif age >= 6 and base and w["ratio"] < 0.05:
            # 六小时连中位数的 5% 都不到，基本等于没进推荐池
            alerts.append({**w, "why": f"发布 {age:.0f} 小时只有 {w['play']} 播放，"
                                       f"是该号中位数 {int(base)} 的 {w['ratio']*100:.1f}%"})
        elif age >= 24 and base and w["ratio"] < 0.15:
            alerts.append({**w, "why": f"播放 {w['play']}，只有该号中位数 {int(base)} 的 {w['ratio']*100:.0f}%"})
        elif age >= 24 and base and w["ratio"] < 0.40:
            lows.append({**w, "why": f"播放 {w['play']}，该号中位数 {int(base)}"})
    return {"baseline": int(base), "alerts": alerts, "lows": lows}


async def _douyin() -> dict:
    if not CK_DOUYIN.exists():
        return {"ok": False, "error": "没有抖音 cookie"}
    from patchright.async_api import async_playwright
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        try:
            ctx = await b.new_context(storage_state=str(CK_DOUYIN), locale="zh-CN",
                                      viewport={"width": 1440, "height": 1200})
            pg = await ctx.new_page()
            await pg.goto(DOUYIN_URL, wait_until="domcontentloaded", timeout=60000)
            await pg.wait_for_timeout(11000)
            if "passport" in pg.url or "login" in pg.url:
                return {"ok": False, "error": "抖音 cookie 失效，要重新扫码"}
            works = parse_works(await pg.evaluate(TEXT_JS))
            if not works:
                return {"ok": False, "error": "作品页没解析出条目，多半是页面改版"}
            return {"ok": True, "account": "九离 AI", "works": works[:20], **judge(works)}
        finally:
            await b.close()


async def _tencent() -> dict:
    """视频号只取账号级数据：作品列表在子页里，且入口元素常不可见。

    首页稳定可读：账号昵称、视频号ID、视频总数、关注者数。
    逐条播放与审核状态等接口或页面改版后再补。
    """
    if not CK_TENCENT.exists():
        return {"ok": False, "error": "没有视频号 cookie"}
    from patchright.async_api import async_playwright
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        try:
            ctx = await b.new_context(storage_state=str(CK_TENCENT), locale="zh-CN",
                                      viewport={"width": 1440, "height": 1200})
            pg = await ctx.new_page()
            await pg.goto("https://channels.weixin.qq.com/platform",
                          wait_until="domcontentloaded", timeout=90000)
            await pg.wait_for_timeout(6000)
            if "login" in pg.url or "passport" in pg.url:
                return {"ok": False, "error": "视频号 cookie 失效，要重新扫码"}
            txt = await pg.evaluate(TEXT_JS)
            if "视频号ID" not in txt:
                return {"ok": False, "error": "没读到视频号后台首页"}
            m_v = re.search(r"视频(\d+)", txt)
            m_f = re.search(r"关注者(\d+)", txt)
            m_id = re.search(r"视频号ID:\s*(\S+)", txt)
            return {
                "ok": True,
                "account": "庞恩浩",
                "video_id": (m_id.group(1) if m_id else ""),
                "video_count": int(m_v.group(1)) if m_v else None,
                "fans": int(m_f.group(1)) if m_f else None,
                "works": [],
                "note": "账号级数据；作品列表在子页，逐条播放待接口",
            }
        finally:
            await b.close()


XHS_JSON = BASE / "data/xhs_notes.json"
XHS_PULL = Path(r"D:/Claude/项目/14 内容管理/05-发布与复盘/自动发布工具/xhs-auto/pull_xhs_data.py")


async def _xiaohongshu() -> dict:
    """小红书走 RedBookSkills 的创作者接口，落 data/xhs_notes.json。

    跟抖音、视频号不一样：这里不自己开浏览器，交给 xhs-auto/pull_xhs_data.py，
    它复用小红书发布那套 Chrome 登录态，省一次扫码。数据超过 12 小时就重拉。
    """
    fresh = False
    if XHS_JSON.exists():
        try:
            age = time.time() - XHS_JSON.stat().st_mtime
            fresh = age < 12 * 3600
        except OSError:
            fresh = False
    if not fresh and XHS_PULL.exists():
        try:
            subprocess.run(
                [sys.executable, "-X", "utf8", str(XHS_PULL), "--account", "jiuliai"],
                capture_output=True, timeout=600, check=False,
            )
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"[:120]}
    if not XHS_JSON.exists():
        return {"ok": False, "error": "没有小红书数据，先跑 pull_xhs_data.py"}
    raw = json.loads(XHS_JSON.read_text(encoding="utf-8"))
    works = raw.get("works") or []
    if not works:
        return {"ok": False, "error": "小红书数据为空"}
    return {"ok": True, "account": raw.get("account") or "九离 AI",
            "works": works[:20], **judge(works)}


async def collect() -> dict:
    out = {"at": datetime.now().strftime("%Y-%m-%d %H:%M")}
    for key, fn in (("douyin", _douyin), ("tencent", _tencent), ("xhs", _xiaohongshu)):
        try:
            out[key] = await fn()
        except Exception as e:  # noqa: BLE001
            out[key] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:120]}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def load() -> dict:
    try:
        return json.loads(OUT.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


HOUR = 8      # 早上八点后抓一次，跟前一天的数对得上


def maybe_daily() -> dict:
    """巡检每 10 分钟叫一次：过了 8 点、今天还没抓过，就跑一趟。

    平台后台是浏览器页面，跑一次要开无头浏览器，所以一天只抓一次，
    不做高频轮询——播放量在头 24 小时变化快，但判限流看的是它有没有进推荐池，
    一天一次足够看出「六小时了还不到中位数 5%」这种。
    """
    if datetime.now().hour < HOUR:
        return {"ok": True, "skipped": "未到点"}
    today = datetime.now().strftime("%Y-%m-%d")
    if (load().get("at") or "")[:10] == today:
        return {"ok": True, "skipped": "今天已经抓过"}
    r = asyncio.run(collect())
    dy = r.get("douyin") or {}
    return {"ok": bool(dy.get("ok")), "at": r.get("at"),
            "works": len(dy.get("works") or []), "alerts": len(dy.get("alerts") or []),
            "error": dy.get("error")}


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    r = asyncio.run(collect())
    brief = {}
    for k, v in r.items():
        if k == "at":
            brief[k] = v
            continue
        brief[k] = {"ok": v.get("ok"), "err": v.get("error"),
                    "n": len(v.get("works") or []),
                    "base": v.get("baseline"),
                    "alerts": len(v.get("alerts") or []),
                    "lows": len(v.get("lows") or [])}
    print(json.dumps(brief, ensure_ascii=False, indent=1))
