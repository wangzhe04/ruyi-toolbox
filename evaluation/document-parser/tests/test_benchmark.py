import copy
import sys
from pathlib import Path
import unittest
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmark import normalize, score_page, metrics, local_file, CATEGORIES, digest, write_json, validate, score


class ScoringTests(unittest.TestCase):
    def setUp(self):
        self.cell = {"row": 1, "col": 2, "rowSpan": 1, "colSpan": 1}
        self.truth = {"blocks": [{"id": "a", "text": "(-12.50)元", "keyNumber": True, "cell": self.cell},
                                  {"id": "b", "text": "", "cell": {**self.cell, "col": 3}}]}
        self.result = {"status": "succeeded", "blocks": [
            {"goldId": b["id"], "text": b["text"], "cell": b["cell"], "order": i,
             "sourceSha256": "hash", "page": 1, "locationVerified": True}
            for i, b in enumerate(self.truth["blocks"])]}

    def test_perfect(self):
        result = metrics(score_page(self.truth, self.result, "hash"))
        self.assertEqual(result["cellAccuracy"], 1)
        self.assertEqual(result["characterErrorRate"], 0)

    def test_missing_page_keeps_denominators(self):
        result = metrics(score_page(self.truth, {"status": "missing"}, "hash"))
        self.assertEqual(result["cells"], 2)
        self.assertEqual(result["cellAccuracy"], 0)
        self.assertEqual(result["numberAccuracy"], 0)
        self.assertEqual(result["characterErrorRate"], 1)

    def test_failed_page_ignores_partial_blocks(self):
        self.result["status"] = "failed"
        self.assertEqual(score_page(self.truth, self.result, "hash")["correctCells"], 0)

    def test_numbers_are_exact(self):
        for bad in ("(12.50)元", "(-1250)元", "(-12.50)", "12.50", "(-12.50)元补充"):
            with self.subTest(bad=bad):
                self.result["blocks"][0]["text"] = bad
                self.assertEqual(score_page(self.truth, self.result, "hash")["correctNumbers"], 0)

    def test_inline_numbers_must_appear_whole(self):
        truth = {"blocks": [{"id": "p", "text": "共 925 页，金额 48,781.88 元，同比 +1.5%", "cell": None,
                             "numbers": ["925", "48,781.88", "+1.5%"]}]}
        cases = {"共925页，金额48,781.88元，同比+1.5%": 3, "共 9250 页，金额 48,781.8 元，同比 +1.5%": 1,
                 "共９２５页，金额４８，７８１．８８元，同比＋１．５％": 3, "共 925 页": 1}
        for text, correct in cases.items():
            with self.subTest(text=text):
                result = {"status": "succeeded", "blocks": [{"goldId": "p", "text": text, "order": 0}]}
                counts = score_page(truth, result, "hash")
                self.assertEqual((counts["numbers"], counts["correctNumbers"]), (3, correct))

    def test_inserted_column_fails_strict_cells_but_keeps_text_and_neighbours(self):
        def c(row, col):
            return {"row": row, "col": col, "rowSpan": 1, "colSpan": 1}
        truth = {"blocks": [{"id": f"{r}{k}", "text": f"{r}{k}", "cell": c(r, k)} for r in range(2) for k in range(2)]}
        shifted = {"status": "succeeded", "blocks": [{"goldId": b["id"], "text": b["text"], "order": i,
                                                      "cell": c(b["cell"]["row"], b["cell"]["col"] + 1)}
                                                     for i, b in enumerate(truth["blocks"])]}
        report = metrics(score_page(truth, shifted, "hash"))
        self.assertEqual((report["cellAccuracy"], report["cellTextAccuracy"], report["cellNeighbourAccuracy"]), (0, 1, 1))
        shifted["blocks"][1]["cell"] = c(1, 2)  # one cell dropped a row: breaks two neighbour pairs
        self.assertEqual(metrics(score_page(truth, shifted, "hash"))["cellNeighbourAccuracy"], 0.5)

    def test_missing_empty_cell_is_not_correct(self):
        self.result["blocks"].pop()
        self.assertEqual(score_page(self.truth, self.result, "hash")["correctCells"], 1)

    def test_wrong_merged_span_fails(self):
        self.result["blocks"][0]["cell"] = {**self.cell, "colSpan": 2}
        self.assertEqual(score_page(self.truth, self.result, "hash")["correctCells"], 1)

    def test_source_requires_hash_page_and_review(self):
        for key, value in (("sourceSha256", "wrong"), ("page", 2), ("locationVerified", False)):
            result = copy.deepcopy(self.result)
            result["blocks"][0][key] = value
            self.assertEqual(score_page(self.truth, result, "hash")["correctSources"], 1)

    def test_reversed_reading_order(self):
        self.result["blocks"][0]["order"] = 10
        self.assertEqual(score_page(self.truth, self.result, "hash")["correctOrderPairs"], 0)

    def test_interleaved_columns_caught_by_all_pairs_only(self):
        truth = {"blocks": [{"id": i, "text": i, "cell": None} for i in ("L1", "L2", "L3", "R1", "R2", "R3")]}
        engine_order = {"L1": 0, "R1": 1, "L2": 2, "R2": 3, "L3": 4, "R3": 5}
        result = {"status": "succeeded", "blocks": [{"goldId": k, "text": k, "order": v, "cell": None}
                                                    for k, v in engine_order.items()]}
        report = metrics(score_page(truth, result, "hash"))
        self.assertEqual(report["orderAccuracy"], 0.8)
        self.assertEqual(report["allPairsOrderAccuracy"], 12 / 15)

    def test_proposed_location_counts_only_when_provisional(self):
        for block in self.result["blocks"]:
            block.update(locationVerified=False, locationProposed=True)
        self.assertEqual(score_page(self.truth, self.result, "hash")["correctSources"], 0)
        self.assertEqual(score_page(self.truth, self.result, "hash", provisional=True)["correctSources"], 2)

    def test_duplicate_alignment_rejected(self):
        self.result["blocks"].append(self.result["blocks"][0])
        with self.assertRaises(ValueError):
            score_page(self.truth, self.result, "hash")

    def test_unknown_alignment_rejected(self):
        self.result["blocks"][0]["goldId"] = "unknown"
        with self.assertRaises(ValueError):
            score_page(self.truth, self.result, "hash")

    def test_normalization_does_not_strip_punctuation(self):
        self.assertEqual(normalize(" （－１２．５０） 元 \n"), "(-12.50)元")
        self.assertNotEqual(normalize("12.50%"), normalize("1250"))

    def test_path_traversal_rejected(self):
        with self.assertRaises(ValueError):
            local_file(Path(__file__).parent, "../benchmark.py")


class CorpusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "source.pdf").write_bytes(b"fake input for hash tests")
        write_json(self.root / "gold.json", {"blocks": [{"id": "b1", "text": "12.5", "keyNumber": True,
                                                          "bbox": [0, 0, 1, 1]}]})
        self.pages = [{"id": f"{cat}-{i}", "category": cat, "page": 1,
                       "split": "dev" if i < (6 if c < 2 else 7) else "holdout", "pilot": i < 2,
                       "file": "source.pdf", "sha256": digest(self.root / "source.pdf"),
                       "annotation": "gold.json", "annotationSha256": digest(self.root / "gold.json"),
                       "rights": "test fixture"} for c, cat in enumerate(CATEGORIES) for i in range(10)]
        self.manifest = self.root / "manifest.json"
        write_json(self.manifest, {"schemaVersion": 1, "pages": self.pages})

    def rewrite_manifest(self):
        self.manifest.unlink()
        write_json(self.manifest, {"schemaVersion": 1, "pages": self.pages})

    def test_valid_shape(self):
        self.assertEqual(len(validate(self.manifest)["pages"]), 60)

    def test_source_change_rejected(self):
        (self.root / "source.pdf").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            validate(self.manifest)

    def test_annotation_change_rejected(self):
        (self.root / "gold.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            validate(self.manifest)

    def test_pilot_cannot_include_holdout(self):
        self.pages[-1]["pilot"] = True
        self.rewrite_manifest()
        with self.assertRaisesRegex(ValueError, "pilot"):
            validate(self.manifest)

    def test_duplicate_page_rejected(self):
        self.pages[-1]["id"] = self.pages[0]["id"]
        self.rewrite_manifest()
        with self.assertRaisesRegex(ValueError, "duplicate"):
            validate(self.manifest)

    def run_score(self, rows, reviewed=True, manifest_hash=None, provisional=False):
        results = self.root / "results.json"
        write_json(results, {"manifestSha256": manifest_hash or digest(self.manifest),
                             "alignmentReviewed": reviewed, "pages": rows})
        return score(self.manifest, results, provisional=provisional)

    def test_empty_results_report_all_twelve_failures(self):
        report = self.run_score([])
        self.assertEqual(len(report["failures"]), 12)
        self.assertEqual(report["accountedPages"], 0)
        self.assertFalse(report["releaseQualified"])

    def test_unreviewed_rejected(self):
        with self.assertRaisesRegex(ValueError, "reviewed"):
            self.run_score([], reviewed=False)

    def test_unreviewed_provisional_is_labelled(self):
        report = self.run_score([], reviewed=False, provisional=True)
        self.assertTrue(report["provisional"])
        self.assertFalse(report["alignmentReviewed"])
        self.assertFalse(report["releaseQualified"])
        self.assertIn("PROVISIONAL", report["note"])

    def test_reviewed_run_is_not_provisional(self):
        self.assertFalse(self.run_score([], provisional=True)["provisional"])

    def test_different_manifest_rejected(self):
        with self.assertRaisesRegex(ValueError, "different corpus"):
            self.run_score([], manifest_hash="wrong")

    def test_split_leakage_rejected(self):
        with self.assertRaisesRegex(ValueError, "split leakage"):
            self.run_score([{"id": self.pages[-1]["id"], "status": "succeeded"}])

    def test_new_results_never_overwrite(self):
        with self.assertRaises(FileExistsError):
            write_json(self.manifest, {})


if __name__ == "__main__":
    unittest.main()
