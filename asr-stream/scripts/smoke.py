r"""真机冒烟：起一个 asr-stream（真模型），把一段 WAV 按 250 ms 切块送过去，打印每块的 partial 与句尾 final 及耗时。

用法：.venv\Scripts\python.exe scripts\smoke.py <wav 16k mono> [--model-dir D] [--port P] [--chunk-ms 250] [--hotword 词] [--realtime]
不进 git 的样例音频可用 ../asr-shim/scripts/make-sample.ps1 造。
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import subprocess
import sys
import time
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("wav")
    ap.add_argument("--model-dir", default=os.environ.get("RUYI_ASR_STREAM_MODEL_DIR", ""))
    ap.add_argument("--port", type=int, default=18791)
    ap.add_argument("--chunk-ms", type=int, default=250)
    ap.add_argument("--hotword", action="append", default=[])
    ap.add_argument("--realtime", action="store_true", help="按真实时间送（否则尽快送）")
    ns = ap.parse_args()

    with wave.open(ns.wav, "rb") as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2, "要 16 kHz 单声道 16 bit"
        pcm = w.readframes(w.getnframes())
    total_s = len(pcm) / 2 / 16000

    env = dict(os.environ, RUYI_ASR_STREAM_PORT=str(ns.port), RUYI_ASR_STREAM_MODEL_DIR=ns.model_dir,
               RUYI_TOOLBOX_PARENT_PID=str(os.getpid()), PYTHONUTF8="1", PYTHONUNBUFFERED="1")
    t0 = time.time()
    proc = subprocess.Popen([sys.executable, "-m", "ruyi_asr_stream"], cwd=ROOT, env=env, stdin=subprocess.DEVNULL)

    def req(method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", ns.port, timeout=30)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, (json.loads(data) if data else None)

    try:
        while time.time() - t0 < 60:
            try:
                st, h = req("GET", "/health")
                if st == 200:
                    break
            except OSError:
                time.sleep(0.2)
        print("ready in %.2fs: %s" % (time.time() - t0, h), flush=True)
        st, s = req("POST", "/v1/stream/sessions", json.dumps({"hotwords": ns.hotword}).encode(), {"Content-Type": "application/json"})
        sid = s["id"]
        step = ns.chunk_ms * 16 * 2
        sent = 0
        finals = []
        last_partial = ""
        t_start = time.time()
        while sent < len(pcm):
            chunk = pcm[sent:sent + step]
            sent += len(chunk)
            t1 = time.time()
            st, r = req("POST", "/v1/stream/sessions/%s/audio" % sid, chunk, {"Content-Type": "audio/L16; rate=16000"})
            dt = (time.time() - t1) * 1000
            pos = sent / 2 / 16000
            if r["partial"] != last_partial:
                print("  %5.2fs  (%3.0f ms)  partial: %s" % (pos, dt, r["partial"]), flush=True)
                last_partial = r["partial"]
            for f in r["finals"]:
                print("  %5.2fs  (%3.0f ms)  FINAL  [%d-%d ms]: %s" % (pos, dt, f["startMs"], f["endMs"], f["text"]), flush=True)
                finals.append(f["text"])
            if ns.realtime:
                time.sleep(max(0, ns.chunk_ms / 1000 - (time.time() - t1)))
        t1 = time.time()
        st, r = req("POST", "/v1/stream/sessions/%s/finish" % sid, b"")
        for f in r["finals"]:
            print("  finish (%3.0f ms)  FINAL  [%d-%d ms]: %s" % ((time.time() - t1) * 1000, f["startMs"], f["endMs"], f["text"]), flush=True)
            finals.append(f["text"])
        print("audio %.2fs, wall %.2fs, finals: %s" % (total_s, time.time() - t_start, " | ".join(finals)))
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
