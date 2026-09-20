"""父进程看门狗（组件登记约定 §2.2）。

如意被强杀／崩溃时来不及杀子进程；这条看门狗保证我们不会变成占着显存的孤儿。

Windows 上判 pid 存活【不许】解析 `tasklist` 的输出：它慢、要起子进程、本地化的表头与截断的
镜像名都咬不准，而且进程刚退时还会留在表里一会儿。这里用 `OpenProcess` + `WaitForSingleObject`
——内核自己的答案，没有歧义。
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from typing import Callable

LOG = logging.getLogger("ruyi_asr_shim.watchdog")

_WIN = sys.platform == "win32"

# Windows 常量
_SYNCHRONIZE = 0x00100000
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_WAIT_OBJECT_0 = 0x00000000
_WAIT_TIMEOUT = 0x00000102
_ERROR_ACCESS_DENIED = 5
_ERROR_INVALID_PARAMETER = 87


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
        os.kill(pid, 0)  # 不发信号，只做存在性与权限检查
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 进程在，只是不归我们管
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
            return False  # 没有这个 pid
        if err == _ERROR_ACCESS_DENIED:
            return True  # 进程在（多半是更高权限的），只是打不开
        LOG.debug("OpenProcess(%d) 失败 err=%d，保守当作还活着", pid, err)
        return True
    try:
        # 超时 0 = 只问现状：已退出的进程句柄是 signaled 的。
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
        self._thread = threading.Thread(target=self._loop, name="asr-parent-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _loop(self) -> None:
        # 先立刻看一眼：如意可能在我们起来之前就没了。
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
    """没设 RUYI_TOOLBOX_PARENT_PID 就回 None（用户手动起的，不该被看门狗管）。"""
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
