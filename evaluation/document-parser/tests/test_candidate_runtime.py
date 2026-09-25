import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmark import read_json, write_json
from candidate_runtime import deny_python_network, inventory, lock_models, verify_models
from run_candidate import complete_rows, journal_rows, run_bounded


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache = self.root / "models"
        self.cache.mkdir()
        (self.cache / "weights.onnx").write_bytes(b"fake model, not an engine test")
        self.lock = self.root / "models.json"
        lock_models(self.cache, self.lock, "docling")

    def test_model_inventory_roundtrip(self):
        verify_models(self.cache, self.lock, "docling")
        self.assertEqual(len(inventory(self.cache)), 1)

    def test_wrong_engine_rejected(self):
        with self.assertRaises(ValueError):
            verify_models(self.cache, self.lock, "paddle")

    def test_modified_model_rejected(self):
        (self.cache / "weights.onnx").write_bytes(b"tampered")
        with self.assertRaises(ValueError):
            verify_models(self.cache, self.lock, "docling")

    def test_unlocked_model_rejected(self):
        (self.cache / "new.onnx").write_bytes(b"new")
        with self.assertRaisesRegex(ValueError, "unlocked"):
            verify_models(self.cache, self.lock, "docling")

    def test_missing_model_rejected(self):
        (self.cache / "weights.onnx").unlink()
        with self.assertRaises(FileNotFoundError):
            verify_models(self.cache, self.lock, "docling")

    def test_empty_model_directory_cannot_be_locked(self):
        (self.cache / "weights.onnx").unlink()
        with self.assertRaisesRegex(ValueError, "no model"):
            lock_models(self.cache, self.root / "empty.json", "docling")

    def test_python_network_attempts_rejected(self):
        for event in ("socket.connect", "socket.getaddrinfo", "socket.sendto"):
            with self.assertRaises(RuntimeError):
                deny_python_network(event, ())
        deny_python_network("open", ())

    def test_timeout_keeps_finished_and_failed_pages(self):
        pages = [{"id": "a", "sha256": "a"}, {"id": "b", "sha256": "b"}]
        rows = complete_rows(pages, [{"id": "a", "status": "succeeded"}], "timeout")
        self.assertEqual(rows[0]["status"], "succeeded")
        self.assertEqual(rows[1]["errorType"], "timeout")

    def test_unknown_worker_page_rejected(self):
        with self.assertRaises(ValueError):
            complete_rows([], [{"id": "a"}], "timeout")

    def test_truncated_journal_tail_does_not_lose_completed_page(self):
        path = self.root / "pages.jsonl"
        path.write_text('{"id":"a"}\n{"id":', encoding="utf-8")
        self.assertEqual(journal_rows(path), [{"id": "a"}])

    def test_corrupt_middle_journal_is_not_ignored(self):
        path = self.root / "pages.jsonl"
        path.write_text('broken\n{"id":"a"}\n', encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            journal_rows(path)

    def test_invalid_json_does_not_publish_file(self):
        path = self.root / "result.json"
        with self.assertRaises(ValueError):
            write_json(path, {"invalid": float("nan")})
        self.assertFalse(path.exists())
        self.assertFalse(list(self.root.glob(".json-*")))

    def test_existing_evidence_is_unchanged(self):
        with self.assertRaises(FileExistsError):
            write_json(self.lock, {})
        self.assertEqual(read_json(self.lock)["engine"], "docling")
        self.assertFalse(list(self.root.glob(".json-*")))

    def test_worker_timeout_really_stops_child(self):
        with (self.root / "worker.log").open("w") as log:
            code, reason = run_bounded([sys.executable, "-c", "import time; time.sleep(30)"], log, 0.1)
        self.assertEqual((code, reason), (-1, "timeout"))

    def test_worker_exit_code_is_preserved(self):
        with (self.root / "worker.log").open("w") as log:
            code, reason = run_bounded([sys.executable, "-c", "raise SystemExit(7)"], log, 10)
        self.assertEqual((code, reason), (7, "worker_exit"))


if __name__ == "__main__":
    unittest.main()
