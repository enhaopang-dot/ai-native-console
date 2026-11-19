# -*- coding: utf-8 -*-
"""中台启动引导：开机自启走这里，失败也能留下证据。

为什么单独一个文件：pythonw 没有控制台，app.py 在 import 阶段就挂掉、
或者窗口没建出来就卡住，外面什么都看不到，表现就是「开机没自动起」。
这里做三件事：
  1. 拉起之前先写一行日志，出问题能追到哪一步；
  2. 开机时桌面和 WebView2 还没就绪，先等 --delay 秒；
  3. 子进程起完 90 秒仍没有窗口，杀掉重来一次，并把结果写进 boot.log。
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
LOG = BASE / "logs" / "boot.log"
TITLE = "AI Native Console"


def _w(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")


def _window_up() -> bool:
    try:
        return bool(ctypes.windll.user32.FindWindowW(None, TITLE))
    except Exception:  # noqa: BLE001
        return False


def main() -> None:
    args = list(sys.argv[1:])
    delay = 0
    if "--delay" in args:
        i = args.index("--delay")
        try:
            delay = int(args[i + 1])
            del args[i:i + 2]
        except (IndexError, ValueError):
            pass

    _w(f"bootstrap pid={__import__('os').getpid()} delay={delay} args={args}")
    if delay > 0:
        time.sleep(delay)

    target = str(BASE / "app.py")
    for attempt in (1, 2):
        proc = subprocess.Popen([sys.executable, target] + args, cwd=str(BASE))
        for _ in range(45):                    # 90 秒还没窗口就当没起来
            time.sleep(2)
            if _window_up():
                _w(f"window up (attempt {attempt})")
                return
            if proc.poll() is not None:
                _w(f"process exited rc={proc.returncode} (attempt {attempt})")
                break
        else:
            _w(f"no window in 90s, kill (attempt {attempt})")
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        if attempt == 2:
            _w("give up after 2 attempts")
            return
        _w("retry in 5s")
        time.sleep(5)


if __name__ == "__main__":
    main()
