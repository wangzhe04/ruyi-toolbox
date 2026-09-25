"""Run one candidate in a bounded worker, keeping raw output and page accounting."""
import argparse
from datetime import datetime, timezone
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

from benchmark import digest, local_file, read_json, validate, write_json
from candidate_runtime import build_engine, convert, lock_models, verify_models


def selected(manifest, selection):
    # Holdout is deliberately not selectable here: nothing tuned on it, nothing run on it yet.
    return [p for p in manifest["pages"] if (p["pilot"] if selection == "pilot" else p["split"] == "dev")]


def complete_rows(pages, rows, reason):
    by_id = {row["id"]: row for row in rows}
    if len(by_id) != len(rows) or set(by_id) - {p["id"] for p in pages}:
        raise ValueError("invalid worker page accounting")
    return [by_id.get(p["id"], {"id": p["id"], "sourceSha256": p["sha256"],
                              "status": "failed", "errorType": reason}) for p in pages]


def journal_rows(path):
    if not path.exists():
        return []
    rows = []
    lines = path.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            # A killed worker may leave one incomplete trailing record, never a corrupt middle record.
            if i != len(lines) - 1:
                raise
    return rows


def run_bounded(command, log, timeout):
    process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
    try:
        return process.wait(timeout=timeout), "worker_exit"
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
        reason = "timeout" if isinstance(error, subprocess.TimeoutExpired) else "cancelled"
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, timeout=10)
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        return -1, reason


def worker(args):
    if args.prepare:
        build_engine(args.engine, args.cache, args.threads, prepare=True, profile=args.profile)
        lock_models(args.cache, args.output / "models.json", args.engine)
        return
    verify_models(args.cache, args.model_lock, args.engine)
    manifest = validate(args.manifest)
    pages = selected(manifest, args.selection)
    started = time.perf_counter()
    backend = build_engine(args.engine, args.cache, args.threads, profile=args.profile)
    write_json(args.output / "initialization.json", {"elapsedSec": time.perf_counter() - started})
    with (args.output / "pages.jsonl").open("x", encoding="utf-8") as journal:
        for page in pages:
            started = time.perf_counter()
            row = {"id": page["id"], "sourceSha256": page["sha256"], "page": 1}
            try:
                source = local_file(args.manifest.parent, page["file"])
                if digest(source) != page["sha256"]:
                    raise ValueError("source changed")
                raw = convert(args.engine, backend, source)
                raw_path = args.output / f"{page['id']}.raw.json"
                write_json(raw_path, raw)
                row.update(status="succeeded", rawFile=raw_path.name, rawSha256=digest(raw_path))
            except Exception as error:
                row.update(status="failed", errorType=type(error).__name__)
                # Full exceptions can include input contents. Metadata only in routine logs.
            row["elapsedSec"] = time.perf_counter() - started
            try:
                import psutil
                info = psutil.Process().memory_info()
                row["processPeakWorkingSetBytes"] = getattr(info, "peak_wset", None)
            except ImportError:
                row["processPeakWorkingSetBytes"] = None
            journal.write(json.dumps(row, ensure_ascii=False) + "\n")
            journal.flush()
            os.fsync(journal.fileno())
            print(json.dumps(row), flush=True)


def main(args):
    if args.threads < 1 or args.timeout < 1:
        raise ValueError("threads and timeout must be positive")
    if not args.prepare and (not args.manifest or not args.model_lock):
        raise ValueError("inference requires manifest and model lock")
    # Validate before creating output; no accidentally consumed evidence directory on bad input.
    pages = [] if args.prepare else selected(validate(args.manifest), args.selection)
    if not args.prepare:
        verify_models(args.cache, args.model_lock, args.engine)
    args.output.mkdir(parents=True, exist_ok=False)
    command = [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:], "--worker"]
    # Hash the code at launch: an edit during a long run must not be credited to it.
    code_hashes = {"runnerSha256": digest(Path(__file__)),
                   "adapterSha256": digest(Path(__file__).with_name("candidate_runtime.py"))}
    started = time.perf_counter()
    with (args.output / "worker.log").open("w", encoding="utf-8") as log:
        code, failure = run_bounded(command, log, args.timeout)
    rows = complete_rows(pages, journal_rows(args.output / "pages.jsonl"), failure)
    report = {"schemaVersion": 1, "engine": args.engine, "prepare": args.prepare,
              "createdAt": datetime.now(timezone.utc).isoformat(), "python": platform.python_version(),
              "threads": args.threads, "selection": None if args.prepare else args.selection, "profile": args.profile, "elapsedSec": time.perf_counter() - started, "exitCode": code,
              "status": "succeeded" if code == 0 and all(r["status"] == "succeeded" for r in rows) else "failed",
              "offlineInferenceVerified": False, "pythonNetworkGuard": not args.prepare,
              **code_hashes,
              "qualityScored": False, "pages": rows,
              "packages": {d.metadata["Name"]: d.version for d in importlib.metadata.distributions()}}
    if not args.prepare:
        report.update(manifestSha256=digest(args.manifest), modelLockSha256=digest(args.model_lock))
    write_json(args.output / "run.json", report)
    print(json.dumps({k: v for k, v in report.items() if k not in ("packages", "pages")}, indent=2))
    return 0 if report["status"] == "succeeded" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("engine", choices=("docling", "paddle"))
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--model-lock", type=Path)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--selection", choices=("pilot", "dev"), default="pilot")
    parser.add_argument("--profile", choices=("default", "light"), default="default",
                        help="paddle only; use a separate --cache per profile so model locks stay distinct")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    options = parser.parse_args()
    if options.worker:
        worker(options)
    else:
        raise SystemExit(main(options))
