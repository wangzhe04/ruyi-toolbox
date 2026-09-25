from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from align_candidate import _TableHTML, best_span, docling_blocks, paddle_blocks, propose_page

CELL = {"row": 0, "col": 1, "rowSpan": 1, "colSpan": 1}


def gold(id, text, cell=None, bbox=(0.1, 0.1, 0.5, 0.2)):
    return {"id": id, "text": text, "cell": cell, "bbox": list(bbox)}


def block(text, cell=None, bbox=(0.05, 0.05, 0.6, 0.25), ref="e"):
    return {"text": text, "cell": cell, "bbox": list(bbox), "engineRef": ref}


class SpanTests(unittest.TestCase):
    def test_exact_substring(self):
        self.assertEqual(best_span("右栏1", "左栏1右栏1左栏2"), (0, 3, 6))

    def test_substitution_cost(self):
        self.assertEqual(best_span("12.50", "合计12.60元"), (1, 2, 7))


class ProposalTests(unittest.TestCase):
    def test_text_comes_from_engine_not_gold(self):
        aligned, _, _ = propose_page({"blocks": [gold("b1", "金额 12.50")]}, [block("金额 12.60")], "h", 0)
        self.assertEqual(aligned[0]["text"], "金额 12.60")
        self.assertFalse(aligned[0]["locationVerified"])

    def test_merged_block_split_into_engine_spans_in_engine_order(self):
        truth = {"blocks": [gold("l1", "左栏第1段"), gold("l2", "左栏第2段"), gold("r1", "右栏第1段")]}
        aligned, _, _ = propose_page(truth, [block("左栏第1段 右栏第1段 左栏第2段")], "h", 0)
        by_id = {a["goldId"]: a for a in aligned}
        self.assertEqual(by_id["r1"]["text"], "右栏第1段")
        self.assertLess(by_id["r1"]["order"], by_id["l2"]["order"])
        self.assertTrue(all(not a["wholeBlock"] for a in aligned))

    def test_line_inside_paragraph_block_is_located_but_not_inside_a_huge_block(self):
        truth = {"blocks": [gold("a", "第一行", bbox=(0.1, 0.1, 0.5, 0.12))]}
        paragraph = block("第一行第二行", bbox=(0.1, 0.1, 0.5, 0.2))
        self.assertTrue(propose_page(truth, [paragraph], "h", 0)[0][0]["locationProposed"])
        page_blob = block("第一行第二行", bbox=(0.05, 0.05, 0.95, 0.95))
        self.assertFalse(propose_page(truth, [page_blob], "h", 0)[0][0]["locationProposed"])

    def test_engine_cell_box_may_be_much_larger_than_cell_text(self):
        truth = {"blocks": [gold("c", "58", CELL, bbox=(0.47, 0.37, 0.49, 0.38))]}
        cell_box = block("58", CELL, bbox=(0.37, 0.36, 0.5, 0.39))
        self.assertTrue(propose_page(truth, [cell_box], "h", 0)[0][0]["locationProposed"])

    def test_rotated_page_uses_observed_box(self):
        truth = {"blocks": [{**gold("a", "文本", bbox=(0.1, 0.1, 0.2, 0.12)), "observedBbox": [0.6, 0.6, 0.7, 0.62]}]}
        near_observed = block("文本", bbox=(0.58, 0.58, 0.72, 0.64))
        self.assertTrue(propose_page(truth, [near_observed], "h", 3)[0][0]["locationProposed"])
        near_upright = block("文本", bbox=(0.08, 0.08, 0.22, 0.14))
        self.assertFalse(propose_page(truth, [near_upright], "h", 3)[0][0]["locationProposed"])

    def test_short_cell_cannot_steal_from_running_text(self):
        truth = {"blocks": [gold("line", "合计 98.41 元，同比 -4.23%"), gold("pct", "%", {**CELL, "col": 1})]}
        aligned, _, _ = propose_page(truth, [block("合计 98.41 元，同比 -4.23%")], "h", 0)
        self.assertEqual([a["goldId"] for a in aligned], ["line"])
        standalone = propose_page(truth, [block("合计 98.41 元，同比 -4.23%"), block("%")], "h", 0)[0]
        self.assertEqual({a["goldId"]: a["engineRef"] for a in standalone}.keys(), {"line", "pct"})

    def test_repeated_phrase_falls_back_to_next_free_span(self):
        truth = {"blocks": [gold("a", "甲组待抽样复核后关闭。乙组"), gold("b", "样复核后关闭。")]}
        aligned, _, _ = propose_page(truth, [block("甲组待抽样复核后关闭。乙组待抽样复核后关闭。")], "h", 0)
        by_id = {a["goldId"]: a for a in aligned}
        self.assertEqual(by_id["b"]["text"], "样复核后关闭。")
        self.assertGreater(by_id["b"]["span"][0], by_id["a"]["span"][1] - 1)

    def test_identical_phrases_resolved_by_position(self):
        truth = {"blocks": [gold("top", "体进度。", bbox=(0.1, 0.1, 0.2, 0.12)),
                            gold("low", "体进度。", bbox=(0.1, 0.8, 0.2, 0.82))]}
        engine = [block("不影响整体进度。", bbox=(0.05, 0.75, 0.6, 0.85), ref="low"),
                  block("不影响整体进度。", bbox=(0.05, 0.05, 0.6, 0.15), ref="top")]
        aligned, _, _ = propose_page(truth, engine, "h", 0)
        self.assertEqual({a["goldId"]: a["engineRef"] for a in aligned}, {"top": "top", "low": "low"})

    def test_spans_do_not_overlap(self):
        truth = {"blocks": [gold("a", "核对"), gold("b", "核对")]}
        aligned, review, _ = propose_page(truth, [block("核对")], "h", 0)
        self.assertEqual(len(aligned), 1)
        self.assertEqual(sum(r["status"] == "missing" for r in review), 1)

    def test_poor_match_left_missing(self):
        aligned, _, unmatched = propose_page({"blocks": [gold("a", "完全不同的内容")]}, [block("xyz")], "h", 0)
        self.assertEqual(aligned, [])
        self.assertEqual(unmatched[0]["text"], "xyz")

    def test_empty_gold_cell_needs_empty_engine_cell_at_same_position(self):
        truth = {"blocks": [gold("e", "", CELL)]}
        self.assertEqual(propose_page(truth, [block("", {**CELL, "col": 2})], "h", 0)[0], [])
        aligned, _, _ = propose_page(truth, [block("", CELL)], "h", 0)
        self.assertEqual((aligned[0]["text"], aligned[0]["cell"]), ("", CELL))

    def test_rotated_page_without_observed_box_never_proposes_location(self):
        aligned, _, _ = propose_page({"blocks": [gold("a", "文本")]}, [block("文本")], "h", 4)
        self.assertFalse(aligned[0]["locationProposed"])

    def test_location_proposed_only_when_gold_box_inside_engine_box(self):
        aligned, _, _ = propose_page({"blocks": [gold("a", "文本")]}, [block("文本")], "h", 0)
        self.assertTrue(aligned[0]["locationProposed"])
        far = block("文本", bbox=(0.7, 0.7, 0.9, 0.9))
        self.assertFalse(propose_page({"blocks": [gold("a", "文本")]}, [far], "h", 0)[0][0]["locationProposed"])


class AdapterTests(unittest.TestCase):
    def test_html_table_spans(self):
        parser = _TableHTML()
        parser.feed("<table><tr><td colspan=2>合计</td><td rowspan=2>备注</td></tr>"
                    "<tr><td>1</td><td>&lt;2&gt;</td></tr></table>")
        self.assertEqual([(c["row"], c["col"], c["rowSpan"], c["colSpan"], c["text"]) for c in parser.cells],
                         [(0, 0, 1, 2, "合计"), (0, 2, 2, 1, "备注"), (1, 0, 1, 1, "1"), (1, 1, 1, 1, "<2>")])

    def test_docling_grid_keeps_empty_cells_and_reading_order(self):
        def cell(text, r, c, bbox=True):
            box = {"l": 0, "t": 0, "r": 10, "b": 10, "coord_origin": "TOPLEFT"} if bbox else None
            return {"text": text, "start_row_offset_idx": r, "start_col_offset_idx": c,
                    "row_span": 1, "col_span": 1, "bbox": box}
        raw = {"pages": {"1": {"size": {"width": 100, "height": 200}}},
               "body": {"children": [{"$ref": "#/texts/0"}, {"$ref": "#/tables/0"}]},
               "texts": [{"self_ref": "#/texts/0", "text": "标题", "children": [],
                          "prov": [{"bbox": {"l": 10, "t": 190, "r": 50, "b": 180, "coord_origin": "BOTTOMLEFT"}}]}],
               "tables": [{"self_ref": "#/tables/0", "children": [], "data": {
                   "table_cells": [cell("a", 0, 0)], "grid": [[cell("a", 0, 0), cell("", 0, 1, False)]]}}]}
        blocks = docling_blocks(raw)
        self.assertEqual([b["text"] for b in blocks], ["标题", "a", ""])
        self.assertEqual(blocks[0]["bbox"], [0.1, 0.05, 0.5, 0.1])
        self.assertIsNone(blocks[2]["bbox"])

    def test_paddle_keeps_list_order_and_pairs_cell_boxes(self):
        raw = {"res": {"width": 100, "height": 200, "parsing_res_list": [
            {"block_label": "text", "block_content": "先", "block_bbox": [0, 0, 50, 20], "block_order": 1},
            {"block_label": "table", "block_content": "<table><tr><td>a</td><td></td></tr></table>",
             "block_bbox": [0, 100, 100, 140], "block_order": None},
            {"block_label": "vision_footnote", "block_content": "注", "block_bbox": [0, 150, 50, 160],
             "block_order": None}],
            "table_res_list": [{"cell_box_list": [[0, 100, 50, 140], [50, 100, 100, 140]]}]}}
        blocks = paddle_blocks(raw)
        self.assertEqual([b["text"] for b in blocks], ["先", "a", "", "注"])
        self.assertEqual(blocks[2]["bbox"], [0.5, 0.5, 1.0, 0.7])
        self.assertEqual(blocks[2]["cell"], {"row": 0, "col": 1, "rowSpan": 1, "colSpan": 1})

    def test_paddle_mismatched_cell_boxes_fall_back_to_table_box(self):
        raw = {"res": {"width": 100, "height": 200, "parsing_res_list": [
            {"block_label": "table", "block_content": "<table><tr><td>a</td><td>b</td></tr></table>",
             "block_bbox": [0, 100, 100, 140]}], "table_res_list": [{"cell_box_list": [[0, 100, 50, 140]]}]}}
        self.assertEqual({tuple(b["bbox"]) for b in paddle_blocks(raw)}, {(0.0, 0.5, 1.0, 0.7)})


if __name__ == "__main__":
    unittest.main()
