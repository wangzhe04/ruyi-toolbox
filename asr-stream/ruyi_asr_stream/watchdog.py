"""父进程看门狗（组件登记约定 §2.2）。与 asr-shim 同一份逻辑，只改了日志名。

如意被强杀／崩溃时来不及杀子进程；这条看门狗保证我们不会变成孤儿。
Windows 上判 pid 存活【不许】解析 `tasklist`：用 `OpenProcess` + `WaitForSingleObject`，内核自己的答案。
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from typing import Callable

LOG = logging.getLogger("ruyi_asr_stream.watchdog")

_WIN = sys.platform == "win32"
_SYNCHRONIZE = 0x00100000
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_WAIT_TIMEOUT = 0x00000102
_ERROR_ACCESS_DENIED = 5
_ERROR_INVALID_PARAMETER = 87


def _is_zombie(pid: int) -> bool:
    """Linux：进程已退出、只是还没被它的父进程收尸（状态 Z）时 os.kill(pid, 0) 仍然成功，
    但它已经不会再做任何事了，看门狗该把它当死。读不到 /proc（macOS 等）就当不是僵尸。"""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            stat = f.read()
    except OSError:
        return False
    # 第 2 列是带括号的进程名（里面可能有空格与括号），状态字在最后一个 ')' 之后。
    tail = stat[stat.rfind(b")") + 1:].split()
    return bool(tail) and tail[0] == b"Z"


def pid_alive(pid: int) -> bool:
    """那个 pid 还在不在。判不准的时候一律当作「还在」—— 宁可多活一会儿，不可误杀自己。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if _WIN:
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
        return not _is_zombie(pid)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True


def _pid_alive_windows(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.WaitForSingleObject.restype = wintypes.DWORD
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k32.OpenProcess(_SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        err = ctypes.get_last_error()
        if err == _ERROR_INVALID_PARAMETER:
            return False
        if err == _ERROR_ACCESS_DENIED:
            return True
        LOG.debug("OpenProcess(%d) 失败 err=%d，保守当作还活着", pid, err)
        return True
    try:
        return k32.WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT
    finally:
        k32.CloseHandle(handle)


class ParentWatchdog:
    """给了 RUYI_TOOLBOX_PARENT_PID 就每 poll_sec 秒看一眼；父没了就回调 on_dead。"""

    def __init__(self, parent_pid: int, on_dead: Callable[[], None], poll_sec: float = 3.0):
        self.parent_pid = int(parent_pid)
        self.poll_sec = max(0.05, min(5.0, float(poll_sec)))  # 约定要求 ≤ 5 秒
        self._on_dead = on_dead
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.fired = False

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="asr-stream-parent-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _loop(self) -> None:
        while True:
            if not pid_alive(self.parent_pid):
                self.fired = True
                LOG.warning("父进程 %d 已经不在了，本服务自行退出（不留孤儿）", self.parent_pid)
                try:
                    self._on_dead()
                except Exception:
                    LOG.exception("看门狗回调出错")
                return
            if self._stop.wait(self.poll_sec):
                return


def watchdog_from_env(on_dead: Callable[[], None], env=None) -> ParentWatchdog | None:
    env = os.environ if env is None else env
    raw = str(env.get("RUYI_TOOLBOX_PARENT_PID", "")).strip()
    if not raw:
        return None
    try:
        pid = int(raw)
    except ValueError:
        LOG.warning("RUYI_TOOLBOX_PARENT_PID 不是数字，忽略")
        return None
    if pid <= 0:
        return None
    poll = 3.0
    raw_poll = str(env.get("RUYI_TOOLBOX_PARENT_POLL_SEC", "")).strip()
    if raw_poll:
        try:
            poll = float(raw_poll)
        except ValueError:
            pass
    return ParentWatchdog(pid, on_dead, poll_sec=poll)
