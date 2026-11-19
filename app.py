# -*- coding: utf-8 -*-
"""AI Native 中台 · 桌面壳（pywebview + WebView2 + 托盘）

行为：
  · 启动后落在副屏（非主屏里最宽的那块），无边框铺满
  · 顶栏可一键在主屏 / 副屏之间切换；切到主屏时给一个居中大窗口
  · 最小化、点 ✕、托盘菜单都能收起，不退出进程
  · 真正退出只在托盘菜单里

用法：
    python app.py            默认
    python app.py --main     直接开在主屏（调试）
    python app.py --no-top   不置顶
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

import webview

from collectors import build_payload, bump_state
from monitor import run_scan, complete as complete_todo, record_error, complete_step, skip as skip_todo

BASE = Path(__file__).parent
_SCHED = None          # 日程抽取模块，主循环里按天调用一次
UI = BASE / "ui/index.html"
LOG = BASE / "logs"
LOG.mkdir(parents=True, exist_ok=True)

TITLE = "AI Native Console"
MONITORS: list[dict] = []
_MUTEX = None


def already_running() -> bool:
    """单实例：已有进程就直接把它叫到前台，然后自己退出。"""
    global _MUTEX
    kernel32 = ctypes.windll.kernel32
    _MUTEX = kernel32.CreateMutexW(None, False, r"Global\AI_NATIVE_CONSOLE_MUTEX")
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        hwnd = ctypes.windll.user32.FindWindowW(None, TITLE)
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 9)      # SW_RESTORE
            ctypes.windll.user32.SetForegroundWindow(hwnd)
        log("another instance already running, exit")
        return True
    return False


def log(msg: str) -> None:
    with open(LOG / "panel.log", "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")


# ---------------- 显示器 ----------------
class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


def list_monitors() -> list[dict]:
    result: list[dict] = []
    user32 = ctypes.windll.user32
    proc_t = ctypes.WINFUNCTYPE(
        ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
        ctypes.POINTER(wintypes.RECT), ctypes.c_double,
    )

    def cb(hmon, hdc, lprect, data):
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            r = mi.rcMonitor
            wr = mi.rcWork          # 可用区：扣掉任务栏
            result.append({
                "x": r.left, "y": r.top,
                "w": r.right - r.left, "h": r.bottom - r.top,
                "wx": wr.left, "wy": wr.top,
                "ww": wr.right - wr.left, "wh": wr.bottom - wr.top,
                "primary": bool(mi.dwFlags & 1),
            })
        return 1

    user32.EnumDisplayMonitors(0, 0, proc_t(cb), 0)
    return result


def secondary() -> dict:
    ms = list_monitors()
    sec = [m for m in ms if not m["primary"]]
    if sec:
        return max(sec, key=lambda m: m["w"])
    return ms[0] if ms else {"x": 0, "y": 0, "w": 1280, "h": 720, "primary": True}


def primary() -> dict:
    ms = list_monitors()
    for m in ms:
        if m["primary"]:
            return m
    return ms[0] if ms else {"x": 0, "y": 0, "w": 1600, "h": 900, "primary": True}


# ---------------- 窗口控制 ----------------
def hwnd_of() -> int:
    return ctypes.windll.user32.FindWindowW(None, TITLE)


def rect_of() -> tuple[int, int, int, int] | None:
    h = hwnd_of()
    if not h:
        return None
    r = wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(h, ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


class Shell:
    def __init__(self, window, monitors=None):
        self.window = window
        # 显示器几何只在启动时量一次：窗口跨不同 DPI 的屏之后，
        # EnumDisplayMonitors 的返回值会带上缩放系数，实时读会算错。
        self.monitors = monitors or list_monitors()
        self._mon_count = len(self.monitors)
        sec = [m for m in self.monitors if not m["primary"]]
        self.sec = max(sec, key=lambda m: m["w"]) if sec else self.monitors[0]
        self.pri = next((m for m in self.monitors if m["primary"]), self.monitors[0])
        self.mode = "secondary"      # secondary | primary
        self.expected = None         # 归位守卫用的目标矩形
        self.rounds = 0
        self.hidden = False
        self.on_top = False

    # 把窗口摆到某个显示器上
    def refresh_monitors(self):
        """重新枚举显示器。副屏被关掉/拔掉后列表会变，缓存不可用。"""
        ms = list_monitors()
        if not ms:
            return self.sec
        if len(ms) != getattr(self, "_mon_count", 0):
            log(f"monitors changed: {self._mon_count} -> {len(ms)}")
            self._mon_count = len(ms)
        self.monitors = ms
        sec = [m for m in ms if not m["primary"]]
        self.sec = max(sec, key=lambda m: m["w"]) if sec else None
        self.pri = next((m for m in ms if m["primary"]), ms[0])
        return self.sec

    def place(self, monitor: dict, full: bool) -> None:
        self.expected = None
        want_w = monitor["w"]
        want_h = monitor["h"]
        if not full:
            want_w = min(1600, monitor["w"] - 80)
            want_h = min(900, monitor["h"] - 80)
        x = monitor["x"] + (monitor["w"] - want_w) // 2
        y = monitor["y"] + (monitor["h"] - want_h) // 2 if not full else monitor["y"]

        # pywebview 的 move/resize 会按主屏缩放系数换算，位置会被推到屏外。
        # 这里直接对 HWND 用物理像素 SetWindowPos，绕开换算。
        SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010
        trace = []
        # 先隐藏再摆位再显示：跨屏移动时旧屏不会留下残影
        try:
            self.window.hide()
            time.sleep(0.08)
        except Exception:  # noqa: BLE001
            pass
        for _ in range(3):
            hwnd = hwnd_of()
            if not hwnd:
                trace.append("no-hwnd")
                break
            ctypes.windll.user32.SetWindowPos(
                hwnd, 0, x, y, want_w, want_h, SWP_NOZORDER | SWP_NOACTIVATE
            )
            time.sleep(0.12)
            r = rect_of()
            trace.append(str(r))
            if r and abs(r[2] - want_w) <= 2 and abs(r[3] - want_h) <= 2 and r[0] == x and r[1] == y:
                break
        try:
            self.window.show()
            time.sleep(0.15)
            self.window.evaluate_js("window.refit && window.refit()")
        except Exception:  # noqa: BLE001
            pass
        self.expected = (x, y, want_w, want_h)
        log(f"place {self.mode} want=({x},{y},{want_w},{want_h}) trace={trace}")

    def show_secondary(self) -> bool:
        sec = self.refresh_monitors()
        if not sec:
            log("secondary display not found, fallback to primary")
            self.mode = "primary"
            self.window.show()
            self.hidden = False
            self.place(self.pri, full=True)
            return False
        self.mode = "secondary"
        self.window.show()
        self.hidden = False
        self.place(sec, full=True)
        return True

    def show_primary(self) -> bool:
        # 切主屏 = 最大化到可用区（扣掉任务栏），铺满整块屏会挡住底部的任务栏
        self.refresh_monitors()
        self.mode = "primary"
        self.window.show()
        self.hidden = False
        m = dict(self.pri)
        if m.get("ww"):
            m = {"x": m["wx"], "y": m["wy"], "w": m["ww"], "h": m["wh"], "primary": True}
        self.place(m, full=True)
        return True

    def toggle_max(self) -> bool:
        """最大化到当前屏的可用区（扣掉任务栏），再点一次还原。

        屏的完整高度含任务栏，铺满会把底部挡住，所以用 rcWork 而不是 rcMonitor。
        """
        try:
            u = ctypes.windll.user32
            u.SetWindowPos.argtypes = [wintypes.HWND, ctypes.c_void_p,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_uint]
            u.SetWindowPos.restype = wintypes.BOOL
            hwnd = hwnd_of()
            if not hwnd:
                return False
            SWP_NOZORDER, SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x0004, 0x0010, 0x0040
            cur = rect_of()
            if not getattr(self, "_prev_rect", None):
                # 第一次点：记下当前尺寸，铺到可用区
                self._prev_rect = cur
                mi = MONITORINFO()
                mi.cbSize = ctypes.sizeof(MONITORINFO)
                MONITOR_DEFAULTTONEAREST = 2
                mon = u.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
                if mon and u.GetMonitorInfoW(mon, ctypes.byref(mi)):
                    r = mi.rcWork
                    u.SetWindowPos(hwnd, None, r.left, r.top, r.right - r.left,
                                   r.bottom - r.top, SWP_NOZORDER | SWP_NOACTIVATE)
                    self.expected = (r.left, r.top, r.right - r.left, r.bottom - r.top)
                    log(f"maximize work={self.expected}")
                    try:
                        self.window.evaluate_js("window.refit && window.refit()")
                    except Exception:  # noqa: BLE001
                        pass
                    self.window.show()
                    return True
                self._prev_rect = None
                return False
            # 第二次点：还原
            x, y, w, h = self._prev_rect
            self._prev_rect = None
            u.SetWindowPos(hwnd, None, x, y, w, h, SWP_NOZORDER | SWP_NOACTIVATE)
            self.expected = (x, y, w, h)
            log(f"restore {self.expected}")
            try:
                self.window.evaluate_js("window.refit && window.refit()")
            except Exception:  # noqa: BLE001
                pass
            return True
        except Exception as e:  # noqa: BLE001
            log(f"toggle_max failed: {e}")
            return False

    def toggle_screen(self) -> bool:
        if self.mode == "secondary":
            return self.show_primary()
        return self.show_secondary()

    def hide(self) -> None:
        self.hidden = True
        self.window.hide()

    def toggle_top(self) -> bool:
        """窗口置顶开关：HWND_TOPMOST / HWND_NOTOPMOST。

        必须显式声明 argtypes：64 位下不声明，HWND 会被截成 32 位，
        SetWindowPos 直接返回 0（ERROR_INVALID_WINDOW_HANDLE），置顶关不掉。
        """
        self.on_top = not getattr(self, "on_top", True)
        # 不要在这里碰 self.window.on_top：pywebview 的属性 setter 会回主线程，
        # 从 js_api 线程调用会卡死整个窗口（2026-11-03 用户实测）。
        try:
            hwnd = hwnd_of()
            if hwnd:
                u = ctypes.windll.user32
                u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                           ctypes.c_uint]
                u.SetWindowPos.restype = wintypes.BOOL
                HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
                SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
                ok = u.SetWindowPos(
                    hwnd,
                    ctypes.c_void_p(HWND_TOPMOST if self.on_top else HWND_NOTOPMOST),
                    0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE,
                )
                ex = u.GetWindowLongW(hwnd, -20)
                log(f"on_top={self.on_top} ok={bool(ok)} exstyle={hex(ex)}")
            else:
                log(f"on_top={self.on_top} no-hwnd")
        except Exception as e:  # noqa: BLE001
            log(f"on_top={self.on_top} failed: {e}")
        return self.on_top

    def toggle_visible(self) -> None:
        if self.hidden:
            self.window.show()
            self.hidden = False
            self.place(self.sec if self.mode == "secondary" else self.pri,
                       full=(self.mode == "secondary"))
        else:
            self.hide()


# ---------------- JS 侧 API ----------------
class Api:
    def __init__(self):
        self._shell: Shell | None = None

    def chat(self, text: str, sid: str = "") -> str:
        """面板底部对话：模型决定改什么，assistant 执行，返回给他看的话。"""
        try:
            import assistant
            return json.dumps(assistant.handle(text, sid), ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            return json.dumps({"reply": f"出错了：{e}", "done": []}, ensure_ascii=False)

    def get_data(self) -> str:
        """返回缓存里的快照。

        以前每次调用都现场 build_payload()，它要跑微信库、千川、罗盘一圈，
        慢的时候十几秒，把 WebView 的消息循环堵死，表现就是整个面板「未响应」。
        现在由后台线程每 20 秒重算一次，这里直接给缓存；有动作刚发生（dirty）时
        最多等 8 秒拿新的，拿不到就先给旧的。
        """
        if self._shell:
            self._shell.rounds += 1
            bump_state("rounds")
        dirty_at = _CACHE.get("dirty")
        if dirty_at:
            t0 = time.time()
            while time.time() - t0 < 8 and (_CACHE.get("ts") or 0) <= dirty_at:
                time.sleep(0.2)
        txt = _CACHE.get("json") or ""
        if not txt:
            t0 = time.time()
            while time.time() - t0 < 45 and not (_CACHE.get("json") or ""):
                time.sleep(0.2)
            txt = _CACHE.get("json") or ""
        if not txt:
            return json.dumps({"time": time.strftime("%H:%M:%S"), "err": _CACHE.get("err", "首次取数还没回来")},
                              ensure_ascii=False)
        payload = json.loads(txt)
        if self._shell:
            payload["screen"] = self._shell.mode
            payload["on_top"] = bool(getattr(self._shell, "on_top", True))
        return json.dumps(payload, ensure_ascii=False)

    def toggle_top(self) -> str:
        try:
            if self._shell:
                return json.dumps({"on_top": self._shell.toggle_top()})
        except Exception as e:  # noqa: BLE001
            return json.dumps({"on_top": True, "error": str(e)})
        return json.dumps({"on_top": True})

    def open_when_ready(self, url: str, port: int = 0) -> str:
        """本地服务还在启动时：等它真的能出页面了再开浏览器，免得弹一个白屏让人以为坏了。"""
        try:
            url = str(url or "")
            if not url.startswith(("http://", "https://")):
                return json.dumps({"ok": False, "error": "地址不对"}, ensure_ascii=False)
            p = int(port or 0)

            def _waiter() -> None:
                import socket
                import urllib.request
                t0 = time.time()
                while time.time() - t0 < 90:
                    ok = False
                    if p:
                        try:
                            with socket.create_connection(("127.0.0.1", p), timeout=0.6):
                                ok = True
                        except Exception:  # noqa: BLE001
                            ok = False
                    else:
                        ok = True
                    if ok:
                        try:
                            req = urllib.request.Request(url, headers={"User-Agent": "ai-native/1.0"})
                            with urllib.request.urlopen(req, timeout=5) as r:
                                if 200 <= r.status < 400:
                                    break
                        except Exception:  # noqa: BLE001
                            pass
                    time.sleep(2)
                import webbrowser
                try:
                    webbrowser.open(url)
                except Exception:  # noqa: BLE001
                    pass

            threading.Thread(target=_waiter, daemon=True).start()
            return json.dumps({"ok": True, "waiting": True}, ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            return json.dumps({"ok": False, "error": str(e)[:100]}, ensure_ascii=False)

    def bench_alive(self) -> str:
        """只探端口，不重算整份数据。工作台页每隔几秒刷新用它，别让 get_data 把界面拖死。"""
        try:
            from collectors import collect_workbenches
            ok, items, _ = collect_workbenches()
            return json.dumps({"ok": ok, "items": [{"name": i.get("name"), "port": i.get("port"),
                                                     "alive": bool(i.get("alive"))} for i in items]},
                              ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            return json.dumps({"ok": False, "error": str(e)[:120]}, ensure_ascii=False)

    def open_url(self, url: str) -> str:
        """在工作台里点名字：交给系统默认浏览器，比 webview 的 window.open 稳。"""
        try:
            if not url or not str(url).startswith(("http://", "https://")):
                return json.dumps({"ok": False, "error": "地址不对"}, ensure_ascii=False)
            import webbrowser
            webbrowser.open(str(url))
            return json.dumps({"ok": True})
        except Exception as e:  # noqa: BLE001
            return json.dumps({"ok": False, "error": str(e)[:100]}, ensure_ascii=False)

    def bench_start(self, path: str) -> str:
        """一键启动：直接拉起来，不占中台的进程。"""
        try:
            if not path or not Path(path).exists():
                return json.dumps({"ok": False, "error": "启动脚本不存在"}, ensure_ascii=False)
            # BENCH_NOOPEN：从面板启动时不弹浏览器标签，页面用「点名字打开」按需开
            # CREATE_NO_WINDOW：脚本在后台跑，不弹黑窗口；要开窗的脚本自己 start cmd
            env = dict(os.environ, BENCH_NOOPEN="1")
            flags = 0x08000000 if os.name == "nt" else 0
            subprocess.Popen(["cmd", "/c", str(path)], shell=False, env=env,
                             creationflags=flags)
            return json.dumps({"ok": True})
        except Exception as e:  # noqa: BLE001
            return json.dumps({"ok": False, "error": str(e)[:100]}, ensure_ascii=False)

    def _bench_stop_worker(self, port: int) -> None:
        """真正的关停动作放后台线程：netstat + taskkill 可能几秒，不能卡住界面。"""
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                                 timeout=15, creationflags=flags).stdout
            pids = {ln.split()[-1] for ln in out.splitlines()
                    if f":{port} " in ln and "LISTENING" in ln.upper()}
            for pid in pids:
                subprocess.run(["taskkill", "/F", "/T", "/PID", pid], capture_output=True,
                               timeout=25, creationflags=flags)
        except Exception:  # noqa: BLE001
            pass

    def bench_stop(self, port: int) -> str:
        """一键关闭：按端口找进程，连子进程一起杀，显存和内存一起放。
        立刻返回，杀进程在后台做；状态由工作台 10 秒刷新如实反映。"""
        try:
            ports = [int(x) for x in str(port or "").replace("，", ",").split(",") if x.strip().isdigit()]
            if not ports:
                return json.dumps({"ok": False, "error": "没配端口，不知道该关谁"}, ensure_ascii=False)
            for pt in ports:
                threading.Thread(target=self._bench_stop_worker, args=(pt,), daemon=True).start()
            return json.dumps({"ok": True, "async": True}, ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            return json.dumps({"ok": False, "error": str(e)[:100]}, ensure_ascii=False)

    def toggle_max(self) -> str:
        if self._shell:
            return json.dumps({"ok": self._shell.toggle_max()})
        return json.dumps({"ok": False})

    def toggle_screen(self) -> str:
        if self._shell:
            ok = self._shell.toggle_screen()
            return json.dumps({"mode": self._shell.mode, "ok": bool(ok)})
        return json.dumps({"mode": "secondary", "ok": True})

    def complete_todo(self, sid: str) -> str:
        ok = complete_todo(sid)
        return json.dumps({"ok": ok})

    def skip_todo(self, sid: str) -> str:
        r = skip_todo(sid)
        kick_refresh()
        return json.dumps({"ok": r})

    def complete_step(self, project: str, sid: str) -> str:
        ok = complete_step(project, sid)
        return json.dumps({"ok": ok})

    def run_consolidate(self) -> str:
        """手动跑一次归并（规则抽屉里的按钮）。"""
        kick_refresh()
        try:
            import threads
            return json.dumps(threads.consolidate(force=True), ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            return json.dumps({"ok": False, "error": str(e)[:120]}, ensure_ascii=False)

    def close_thread(self, tid: str, skip: bool = False) -> str:
        """整件事结案 / 跳过（总览点开事项就能直接办，不用进专项页）。"""
        kick_refresh()
        if not str(tid or "").strip():
            return json.dumps({"ok": False, "error": "没有选中事项（这个按钮没绑到具体那件）"}, ensure_ascii=False)
        try:
            import threads
            return json.dumps(threads.close(tid, bool(skip)), ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            return json.dumps({"ok": False, "error": str(e)[:120]}, ensure_ascii=False)

    def snooze_thread(self, tid: str, hours: int = 24) -> str:
        """跳过：这件事先放着，过几小时再回到「等你处理」。"""
        kick_refresh()
        try:
            import threads
            return json.dumps(threads.snooze(tid, int(hours or 24)), ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            return json.dumps({"ok": False, "error": str(e)[:120]}, ensure_ascii=False)

    def set_thread_level(self, tid: str, level: str) -> str:
        kick_refresh()
        try:
            import threads
            return json.dumps({"ok": threads.set_level(tid, level)}, ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            return json.dumps({"ok": False, "error": str(e)[:120]}, ensure_ascii=False)

    def split_thread(self, tid: str) -> str:
        """合错了把一件事拆回卡片。"""
        kick_refresh()
        try:
            import threads
            return json.dumps({"ok": threads.split(tid)}, ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            return json.dumps({"ok": False, "error": str(e)[:120]}, ensure_ascii=False)

    def run_evolve(self) -> str:
        """立刻出一份进化建议（不等 19:00）。"""
        try:
            import evolve
            return json.dumps(evolve.generate(force=True), ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            return json.dumps({"ok": False, "error": str(e)[:120]}, ensure_ascii=False)

    def copy_text(self, text: str) -> str:
        """把文本塞进 Windows 剪贴板（64 位指针必须声明 restype，否则会截断）。"""
        try:
            u = ctypes.windll.user32
            k = ctypes.windll.kernel32
            k.GlobalAlloc.restype = ctypes.c_void_p
            k.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
            k.GlobalLock.restype = ctypes.c_void_p
            k.GlobalLock.argtypes = [ctypes.c_void_p]
            k.GlobalUnlock.argtypes = [ctypes.c_void_p]
            u.SetClipboardData.restype = ctypes.c_void_p
            u.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]

            data = text.encode("utf-16-le") + bytes(2)
            if not u.OpenClipboard(0):
                raise RuntimeError("OpenClipboard failed")
            try:
                u.EmptyClipboard()
                h = k.GlobalAlloc(0x0042, len(data))
                if not h:
                    raise RuntimeError("GlobalAlloc failed")
                ptr = k.GlobalLock(h)
                if not ptr:
                    raise RuntimeError("GlobalLock failed")
                ctypes.memmove(ptr, data, len(data))
                k.GlobalUnlock(h)
                u.SetClipboardData(13, h)      # CF_UNICODETEXT
            finally:
                u.CloseClipboard()
            return json.dumps({"ok": True, "len": len(text)})
        except Exception as e:  # noqa: BLE001
            log(f"clipboard error {e}")
            return json.dumps({"ok": False, "error": str(e)})

    def hide_window(self) -> None:
        if self._shell:
            self._shell.hide()

    def quit_app(self) -> None:
        if self._shell:
            self._shell.window.destroy()


# ---------------- 托盘 ----------------
def make_icon():
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=(10, 20, 34, 255), outline=(79, 216, 232, 255), width=3)
    d.ellipse((20, 20, 44, 44), fill=(255, 138, 61, 255))
    return img


def start_tray(shell: Shell) -> None:
    import pystray

    def on_show(icon, item):
        shell.window.show()
        shell.hidden = False
        shell.place(shell.sec if shell.mode == "secondary" else shell.pri,
                    full=(shell.mode == "secondary"))

    def on_sec(icon, item):
        shell.show_secondary()

    def on_pri(icon, item):
        shell.show_primary()

    def on_toggle(icon, item):
        shell.toggle_screen()

    def on_quit(icon, item):
        icon.stop()
        shell.window.destroy()

    menu = pystray.Menu(
        pystray.MenuItem("显示 / 隐藏", on_show, default=True),
        pystray.MenuItem("切到副屏（长条常挂）", on_sec),
        pystray.MenuItem("切到主屏（细看）", on_pri),
        pystray.MenuItem("在两屏之间切换", on_toggle),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", on_quit),
    )
    icon = pystray.Icon("ai_native", make_icon(), "AI Native 中台", menu)
    threading.Thread(target=icon.run, daemon=True).start()
    return icon


CMD_FILE = BASE / "data/cmd.json"


WX_RUNTIME = Path(""<你的本地路径>"")
_wx_last_spawn = [0.0]


def _port_open(port: int) -> bool:
    import socket
    sk = socket.socket()
    sk.settimeout(0.6)
    try:
        sk.connect(("127.0.0.1", port))
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        sk.close()


def ensure_wx_runtime() -> None:
    """微信运行时掉线就自己拉起来（关机/重启后常见）。3 分钟内不重复拉。"""
    if not WX_RUNTIME.exists():
        return
    from monitor import wx_accounts
    ports = [p for p in wx_accounts().values() if p]
    down = [p for p in ports if not _port_open(p)]
    if not down:
        return
    if time.time() - _wx_last_spawn[0] < 180:
        return
    _wx_last_spawn[0] = time.time()
    try:
        subprocess.Popen(
            [sys.executable, str(WX_RUNTIME), "daemon"],
            cwd=str(WX_RUNTIME.parent),
            creationflags=0x08000000,          # CREATE_NO_WINDOW
        )
        log(f"wx runtime down {down}, restarted")
    except Exception as e:  # noqa: BLE001
        log(f"wx restart failed: {e}")


def set_aumid() -> None:
    """给进程一个独立身份，任务栏才不会跟 pythonw 挤在一起用它的图标。"""
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PangEnhao.AINative.Console")
    except Exception:  # noqa: BLE001
        pass


def apply_icon() -> None:
    """给窗口换上自己的图标。

    pywebview 在 Windows 上不吃 icon 参数，只能自己发 WM_SETICON。
    按标题找窗口不可靠（主窗口可能只是其中一层），改成按进程号枚举顶层窗口，
    每个都设一遍，并且隔一会儿再设一次——WebView 初始化完可能把它重置回去。
    """
    try:
        ico = str(BASE / "ui/icons/app.ico")
        if not Path(ico).exists():
            return
        u = ctypes.windll.user32
        kernel = ctypes.windll.kernel32
        LR_LOADFROMFILE, IMAGE_ICON = 0x0010, 1
        pid_mine = kernel.GetCurrentProcessId()
        handles = {}

        def _load(size):
            if size not in handles:
                handles[size] = u.LoadImageW(0, ico, IMAGE_ICON, size, size, LR_LOADFROMFILE)
            return handles[size]

        EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        for attempt in range(4):
            time.sleep(2 if attempt == 0 else 4)
            found = []

            def cb(hwnd, _):
                pid = ctypes.c_ulong()
                u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value == pid_mine and u.IsWindowVisible(hwnd):
                    found.append(hwnd)
                return True

            u.EnumWindows(EnumProc(cb), 0)
            for hwnd in found:
                for which, size in ((0, 16), (1, 32)):
                    h = _load(size)
                    if h:
                        u.SendMessageW(hwnd, 0x0080, which, h)      # WM_SETICON
            log(f"icon attempt {attempt+1}: {len(found)} 个窗口")
        log("icon applied")
    except Exception as e:  # noqa: BLE001
        log(f"icon failed: {e}")


def watch_health() -> None:
    """每分钟检查一次微信运行时，掉线自动拉起。"""
    set_aumid()
    apply_icon()
    time.sleep(20)
    while True:
        try:
            ensure_wx_runtime()
        except Exception as e:  # noqa: BLE001
            log(f"health-error {e}")
        time.sleep(60)


def watch_inbox() -> None:
    """每 10 分钟扫一次微信告警与 Get笔记新录音。"""
    global _SCHED
    try:
        import importlib.util
        _spec = importlib.util.spec_from_file_location(
            "extract_schedule", BASE / "_构建脚本" / "extract_schedule.py")
        _SCHED = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_SCHED)
    except Exception as e:  # noqa: BLE001
        _SCHED = None
        log(f"schedule module load failed: {e}")
    time.sleep(5)
    while True:
        try:
            st = run_scan()
            log(f"scan found={st['found']} added={st['added']} auto={st.get('auto',0)} pending={st['pending']}")
            try:
                from monitor import auto_close_promises, auto_close_incoming
                for c in auto_close_promises():
                    log(f"autoclose {c['title'][:24]} · {c['evidence'][:30]}")
                for c in auto_close_incoming():
                    log(f"autoclose-in {c['title'][:24]} · {c['evidence'][:30]}")
            except Exception as e:  # noqa: BLE001
                log(f"autoclose-error {e}")
        except Exception as e:  # noqa: BLE001
            log(f"scan-error {e}")
            try:
                record_error(str(e))
            except Exception:  # noqa: BLE001
                pass
        # 每天 19 点后补一份进化建议；没到点或今天已出就直接返回
        try:
            import evolve
            r = evolve.maybe_daily()
            if r.get("ok") and not r.get("skipped"):
                log(f"evolve {r.get('path')}")
        except Exception as e:  # noqa: BLE001
            log(f"evolve-error {e}")
        # 每天 8 点后从微信 + 录音里补一次未来日程（证据回查，抽不出就不写）
        try:
            r = _SCHED.maybe_daily() if _SCHED else {}
            if r.get("ok"):
                log(f"schedule-extract {r.get('at')}")
        except Exception as e:  # noqa: BLE001
            log(f"schedule-extract-error {e}")
        # 每天 8 点后抓一次内容平台的作品数据，判这一批有没有被限流
        try:
            import content_live
            r = content_live.maybe_daily()
            if not r.get("skipped"):
                log(f"content {r.get('at')} works={r.get('works')} alerts={r.get('alerts')}")
        except Exception as e:  # noqa: BLE001
            log(f"content-error {e}")
        time.sleep(600)


def guard_position(shell: Shell) -> None:
    """窗口被拖走或尺寸跑偏时自动摆回原位。"""
    SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010
    while True:
        time.sleep(2)
        try:
            if not shell.hidden:
                sec = shell.refresh_monitors()
                if shell.mode == "secondary" and not sec:
                    log("guard: secondary lost, switching to primary")
                    shell.show_primary()
                    continue
            exp = shell.expected
            if not exp or shell.hidden:
                continue
            r = rect_of()
            if not r:
                continue
            if abs(r[0]-exp[0]) > 3 or abs(r[1]-exp[1]) > 3 or                abs(r[2]-exp[2]) > 3 or abs(r[3]-exp[3]) > 3:
                hwnd = hwnd_of()
                if hwnd:
                    ctypes.windll.user32.SetWindowPos(
                        hwnd, 0, exp[0], exp[1], exp[2], exp[3],
                        SWP_NOZORDER | SWP_NOACTIVATE)
                    log(f"guard: {r} -> {exp}")
        except Exception as e:  # noqa: BLE001
            log(f"guard-error {e}")


def watch_commands(shell: Shell) -> None:
    """外部可用写 data/cmd.json 控制面板，例如 {"cmd":"primary"}。
    支持：primary / secondary / toggle / hide / show / quit。
    """
    last = 0.0
    while True:
        try:
            if CMD_FILE.exists():
                mtime = CMD_FILE.stat().st_mtime
                if mtime > last:
                    last = mtime
                    cmd = json.loads(CMD_FILE.read_text(encoding="utf-8")).get("cmd", "")
                    log(f"cmd={cmd}")
                    if cmd == "primary":
                        shell.show_primary()
                    elif cmd == "secondary":
                        shell.show_secondary()
                    elif cmd == "toggle":
                        shell.toggle_screen()
                    elif cmd == "hide":
                        shell.hide()
                    elif cmd == "show":
                        shell.window.show()
                        shell.hidden = False
                    elif cmd == "probe":
                        r = shell.window.evaluate_js(
                            "JSON.stringify({v:String(typeof view==='undefined'?'?':view),"
                            "lc:(document.getElementById('leftcol')||{style:{}}).style.display||'(empty)',"
                            "sc:(document.getElementById('sched')||{style:{}}).style.display||'(empty)'})")
                        log(f"probe {r}")
                    elif cmd == "diag":
                        r = shell.window.evaluate_js(
                            "JSON.stringify({iw:innerWidth,ih:innerHeight,"
                            "tf:document.getElementById('stage').style.transform,"
                            "sw:document.getElementById('stage').style.width,"
                            "sh:document.getElementById('stage').style.height,"
                            "ag:document.querySelectorAll('.ag-card').length,"
                            "raw:!!window.__RAW})")
                        log(f"diag {r}")
                    elif cmd == "proj":
                        v = json.loads(CMD_FILE.read_text(encoding='utf-8')).get('value','')
                        shell.window.evaluate_js(f"setView('project', '{v}')")
                        log(f"proj={v}")
                    elif cmd == "mask":
                        shell.window.evaluate_js("window.toggleMask && toggleMask()")
                        log("mask toggled")
                    elif cmd == "schedmask":
                        shell.window.evaluate_js("window.toggleSchedMask && toggleSchedMask()")
                        log("schedmask toggled")
                    elif cmd == "view":
                        v = json.loads(CMD_FILE.read_text(encoding='utf-8')).get('value','all')
                        shell.window.evaluate_js(f"window.setView && setView('{v}')")
                        log(f"view={v}")
                    elif cmd == "cptest":
                        shell.window.evaluate_js("window.pywebview.api.copy_text('CLIP_TEST_123')")
                        log("cptest fired")
                    elif cmd == "projs":
                        r = shell.window.evaluate_js(
                            "JSON.stringify([...document.querySelectorAll('.sub')].filter(d=>d.textContent.includes('客户B')).map(d=>{const r=d.getBoundingClientRect();return {x:Math.round(r.left+r.width/2),y:Math.round(r.top+r.height/2)};}))")
                        log(f"projs {r}")
                    elif cmd == "cprect":
                        r = shell.window.evaluate_js(
                            "(()=>{const b=document.getElementById('cpBtn');"
                            "if(!b) return 'none';const r=b.getBoundingClientRect();"
                            "return JSON.stringify({x:Math.round(r.left+r.width/2),y:Math.round(r.top+r.height/2)});})()")
                        log(f"cprect {r}")
                    elif cmd == "api":
                        r = shell.window.evaluate_js("JSON.stringify(Object.keys(window.pywebview.api))")
                        log(f"api {r}")
                    elif cmd == "rects":
                        r = shell.window.evaluate_js(
                            "JSON.stringify([...document.querySelectorAll('.donebtn,.procbtn')].map(b=>{"
                            "const r=b.getBoundingClientRect();"
                            "return {c:b.className,sid:b.dataset.sid,x:Math.round(r.left+r.width/2),y:Math.round(r.top+r.height/2)};}))")
                        log(f"rects {r}")
                    elif cmd == "mask":
                        shell.window.evaluate_js("window.toggleMask && window.toggleMask()")
                    elif cmd == "quit":
                        shell.window.destroy()
                        return
        except Exception as e:  # noqa: BLE001
            log(f"cmd-error {e}")
        time.sleep(1)


_CACHE: dict = {"json": "", "ts": 0.0, "err": "", "dirty": None}


def _refresh_loop() -> None:
    """后台每 20 秒重算一次快照；算挂了也只是数据旧，不会把界面拖死。"""
    while True:
        try:
            payload = build_payload()
            _CACHE["json"] = json.dumps(payload, ensure_ascii=False)
            _CACHE["ts"] = time.time()
            _CACHE["err"] = ""
            if _CACHE.get("dirty") and _CACHE["ts"] > _CACHE["dirty"]:
                _CACHE["dirty"] = None
        except Exception as e:  # noqa: BLE001
            _CACHE["err"] = f"{type(e).__name__}: {str(e)[:120]}"
            log(f"refresh-error {_CACHE['err']}")
        time.sleep(20)


def kick_refresh() -> None:
    """动作之后调一下，下一次取数会等新快照（最多 8 秒）。"""
    _CACHE["dirty"] = time.time()


def main() -> None:
    global MONITORS
    log(f"main enter argv={sys.argv[1:]}")
    # 声明 per-monitor DPI 感知。主屏是 1.5 倍缩放，不声明的话切到主屏文字会被
    # 位图拉伸发虚；位置一律走 SetWindowPos 给物理像素，绕开 pywebview move() 的换算。
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:  # noqa: BLE001
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:  # noqa: BLE001
            pass

    if already_running():
        return

    MONITORS = list_monitors()
    for _ in range(20):                      # 开机瞬间 EnumDisplayMonitors 可能还是空的
        if MONITORS:
            break
        time.sleep(1)
        MONITORS = list_monitors()
    if not MONITORS:
        log("no monitor detected, fallback 1920x1080")
        MONITORS = [{"x": 0, "y": 0, "w": 1920, "h": 1080,
                     "wx": 0, "wy": 0, "ww": 1920, "wh": 1080, "primary": True}]
    no_top = "--no-top" in sys.argv
    force_main = "--main" in sys.argv
    force_main = "--main" in sys.argv

    api = Api()
    start_mon = (next((m for m in MONITORS if m['primary']), MONITORS[0])
                 if force_main else
                 max([m for m in MONITORS if not m['primary']] or MONITORS, key=lambda m: m['w']))
    window = webview.create_window(
        TITLE,
        url=f"{UI}?v={int(time.time())}",   # 防 WebView 缓存旧页面
        width=start_mon["w"],
        height=start_mon["h"],
        x=start_mon["x"],
        y=start_mon["y"],
        frameless=True,
        easy_drag=False,
        on_top=not no_top,
        background_color="#0a1422",
        js_api=api,
    )
    shell = Shell(window, MONITORS)
    shell.mode = "primary" if force_main else "secondary"
    api._shell = shell

    def on_loaded():
        shell.place(start_mon, full=not force_main)
        start_tray(shell)
        threading.Thread(target=watch_commands, args=(shell,), daemon=True).start()
        threading.Thread(target=guard_position, args=(shell,), daemon=True).start()
        threading.Thread(target=watch_inbox, daemon=True).start()
        threading.Thread(target=watch_health, daemon=True).start()

    def on_closing():
        # 点 ✕ 收进托盘，不退出
        shell.hide()
        return False

    threading.Thread(target=_refresh_loop, daemon=True).start()
    window.events.loaded += on_loaded
    window.events.closing += on_closing
    log(f"start target={start_mon}")
    webview.start(debug=False, private_mode=False)


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        import traceback
        (LOG).mkdir(parents=True, exist_ok=True)
        with open(LOG / "crash.log", "a", encoding="utf-8") as f:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            f.write("[" + stamp + "]" + chr(10) + traceback.format_exc() + chr(10))
        raise
