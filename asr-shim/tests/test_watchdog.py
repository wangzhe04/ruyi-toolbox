"""父进程看门狗（组件登记约定 §2.2）：如意没了，我们不许变成占着显存的孤儿。

判 pid 存活必须用内核自己的答案（Windows: OpenProcess+WaitForSingleObject），
绝不解析 tasklist —— 慢、要起子进程、本地化表头与截断镜像名都咬不准。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import unittest

from ruyi_asr_shim.watchdog import ParentWatchdog, pid_alive, watchdog_from_env


class TestPidAlive(unittest.TestCase):
    def test_self_is_alive(self):
        self.assertTrue(pid_alive(os.getpid()))

    def test_bogus_pids(self):
        self.assertFalse(pid_alive(0))
        self.assertFalse(pid_alive(-1))
        self.assertFalse(pid_alive("abc"))
        self.assertFalse(pid_alive(None))

    def test_dead_child_is_reported_dead(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait(timeout=30)
        # 句柄已经被 Popen 收掉，内核里那个 pid 应当已经不在。
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and pid_alive(proc.pid):
            time.sleep(0.05)
        self.assertFalse(pid_alive(proc.pid), "已退出的子进程不该被判成活着")

    def test_live_child_is_reported_alive(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            self.assertTrue(pid_alive(proc.pid))
        finally:
            proc.kill()
            proc.wait(timeout=30)


class TestWatchdog(unittest.TestCase):
    def test_fires_within_10s_after_parent_exits(self):
        """起一个短命子进程当「父」，它退出之后看门狗必须在 10 秒内动手。"""
        parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1.0)"])
        fired = threading.Event()
        wd = ParentWatchdog(parent.pid, fired.set, poll_sec=0.2)
        t0 = time.monotonic()
        wd.start()
        try:
            self.assertTrue(fired.wait(10), "父进程退出后 10 秒内没有触发自退")
            elapsed = time.monotonic() - t0
            self.assertLess(elapsed, 10.0)
            self.assertTrue(wd.fired)
        finally:
            wd.stop()
            parent.wait(timeout=30)

    def test_does_not_fire_while_parent_alive(self):
        parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        fired = threading.Event()
        wd = ParentWatchdog(parent.pid, fired.set, poll_sec=0.1)
        wd.start()
        try:
            self.assertFalse(fired.wait(1.5), "父还活着就不许自退")
        finally:
            wd.stop()
            parent.kill()
            parent.wait(timeout=30)

    def test_poll_interval_capped_at_5s(self):
        wd = ParentWatchdog(os.getpid(), lambda: None, poll_sec=600)
        self.assertLessEqual(wd.poll_sec, 5.0, "约定要求每 ≤5 秒看一眼")

    def test_stop_ends_the_thread(self):
        wd = ParentWatchdog(os.getpid(), lambda: None, poll_sec=0.05)
        wd.start()
        wd.stop()
        wd.join(5)
        self.assertFalse(wd.fired)


class TestFromEnv(unittest.TestCase):
    def test_absent_env_means_no_watchdog(self):
        self.assertIsNone(watchdog_from_env(lambda: None, env={}))

    def test_garbage_env_means_no_watchdog(self):
        self.assertIsNone(watchdog_from_env(lambda: None, env={"RUYI_TOOLBOX_PARENT_PID": "nope"}))
        self.assertIsNone(watchdog_from_env(lambda: None, env={"RUYI_TOOLBOX_PARENT_PID": "0"}))

    def test_valid_env_builds_watchdog(self):
        wd = watchdog_from_env(lambda: None, env={"RUYI_TOOLBOX_PARENT_PID": str(os.getpid())})
        self.assertIsNotNone(wd)
        self.assertEqual(wd.parent_pid, os.getpid())
        self.assertLessEqual(wd.poll_sec, 5.0)


if __name__ == "__main__":
    unittest.main()
