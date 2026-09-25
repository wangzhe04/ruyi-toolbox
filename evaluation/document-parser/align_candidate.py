"""Propose (never approve) gold alignments for one candidate run; writes a review sheet.

Text, cell and order always come from the engine. Gold is used only to *choose*
which engine span a gold block corresponds to. An engine block that merges
several gold blocks (e.g. two columns read as one paragraph) is split into
engine substrings; their order follows the engine's own sequence.
"""
from __future__ import annotations

import argparse
import heapq
import html
from html.parser import HTMLParser
import json
from pathlib import Path

from benchmark import digest, local_file, normalize, read_json, validate, write_json

ACCEPT_COST = 0.5  # edits / gold characters; above this the gold block stays missing
CONTAINED = 0.5  # share of the gold box inside the engine box to *propose* a location
GRANULARITY = 12  # a partial span is located only if its engine box is at most this many gold boxes


# ---------- engine adapters: raw output -> ordered blocks ----------

def _box(left, top, right, bottom, width, height):
    return [max(0.0, left / width), max(0.0, top / height), min(1.0, right / width), min(1.0, bottom / height)]


def docling_blocks(raw):
    size = raw["pages"]["1"]["size"]
    width, height = size["width"], size["height"]
    items = {}
    for kind in ("texts", "groups", "tables", "pictures", "key_value_items", "form_items"):
        for item in raw.get(kind, []):
            items[item["self_ref"]] = item

    def bbox(b):
        if b.get("coord_origin") == "BOTTOMLEFT":
            return _box(b["l"], height - b["t"], b["r"], height - b["b"], width, height)
        return _box(b["l"], b["t"], b["r"], b["b"], width, height)

    blocks = []

    def visit(ref):
        item = items.get(ref)
        if item is None:
            return
        if ref.startswith("#/tables/"):
            # The grid keeps cells Docling recognised as empty; table_cells drops them.
            # A spanning cell repeats at each position it covers, so keep its origin only.
            seen, cells = set(), []
            for c in (c for row in item["data"].get("grid") or [] for c in row):
                key = (c["start_row_offset_idx"], c["start_col_offset_idx"])
                if key not in seen:
                    seen.add(key)
                    cells.append(c)
            if not cells:
                cells = item["data"]["table_cells"]
            cells.sort(key=lambda c: (c["start_row_offset_idx"], c["start_col_offset_idx"]))
            for c in cells:
                blocks.append({"text": c["text"], "bbox": bbox(c["bbox"]) if c.get("bbox") else None,
                               "cell": {"row": c["start_row_offset_idx"], "col": c["start_col_offset_idx"],
                                        "rowSpan": c["row_span"], "colSpan": c["col_span"]},
                               "engineRef": ref})
        elif "text" in item:
            prov = item.get("prov") or []
            blocks.append({"text": item["text"], "bbox": bbox(prov[0]["bbox"]) if prov else None,
                           "cell": None, "engineRef": ref})
        for child in item.get("children", []):
            visit(child["$ref"])

    for child in raw["body"]["children"]:
        visit(child["$ref"])
    return blocks


class _TableHTML(HTMLParser):
    """Flatten <table> HTML into positioned cells, honouring row/col spans."""

    def __init__(self):
        super().__init__()
        self.cells, self.row, self.taken, self.current = [], -1, set(), None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "tr":
            self.row += 1
            self.col = 0
        elif tag in ("td", "th"):
            if self.row < 0:
                self.row, self.col = 0, 0
            while (self.row, self.col) in self.taken:
                self.col += 1
            rs, cs = int(attrs.get("rowspan") or 1), int(attrs.get("colspan") or 1)
            for r in range(self.row, self.row + rs):
                for c in range(self.col, self.col + cs):
                    self.taken.add((r, c))
            self.current = {"row": self.row, "col": self.col, "rowSpan": rs, "colSpan": cs, "text": ""}
            self.col += cs

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.current is not None:
            self.cells.append(self.current)
            self.current = None

    def handle_data(self, data):
        if self.current is not None:
            self.current["text"] += data


def paddle_blocks(raw):
    """parsing_res_list is already in PP-StructureV3's reading order (its Markdown export
    follows it); block_order numbers only text-like blocks and is None for tables/notes."""
    res = raw.get("res", raw)
    width, height = res["width"], res["height"]
    tables = iter(res.get("table_res_list") or [])
    blocks = []
    for i, block in enumerate(res["parsing_res_list"]):
        box = _box(*block["block_bbox"], width, height)
        content = block.get("block_content") or ""
        if block.get("block_label") == "table" and "<t" in content:
            parser = _TableHTML()
            parser.feed(content)
            boxes = (next(tables, None) or {}).get("cell_box_list") or []
            # Row-major cell boxes; trust them only when they pair one-to-one with the cells.
            if len(boxes) != len(parser.cells):
                boxes = [None] * len(parser.cells)
            for c, cell_box in zip(parser.cells, boxes):
                text = html.unescape(c.pop("text")).strip()
                blocks.append({"text": text, "bbox": _box(*cell_box, width, height) if cell_box else box,
                               "cell": c, "engineRef": f"parsing/{i}"})
        else:
            blocks.append({"text": content, "bbox": box, "cell": None, "engineRef": f"parsing/{i}"})
    return blocks


# ---------- proposal ----------

def best_span(gold, text):
    """Semi-global edit distance: cheapest engine substring for the gold text."""
    n = len(text)
    previous = [0] * (n + 1)  # free start anywhere in the engine text
    starts = list(range(n + 1))
    for i, char in enumerate(gold, 1):
        current, current_starts = [i], [0]
        for j in range(1, n + 1):
            options = ((previous[j - 1] + (char != text[j - 1]), starts[j - 1]),
                       (previous[j] + 1, starts[j]),
                       (current[j - 1] + 1, current_starts[j - 1]))
            cost, start = min(options)
            current.append(cost)
            current_starts.append(start)
        previous, starts = current, current_starts
    cost, end = min((c, j) for j, c in enumerate(previous))
    return cost, starts[end], end


def contained(gold_box, engine_box):
    if not engine_box:
        return 0.0
    l, t = max(gold_box[0], engine_box[0]), max(gold_box[1], engine_box[1])
    r, b = min(gold_box[2], engine_box[2]), min(gold_box[3], engine_box[3])
    area = (gold_box[2] - gold_box[0]) * (gold_box[3] - gold_box[1])
    return max(0.0, r - l) * max(0.0, b - t) / area if area else 0.0


def area(box):
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def gold_box(gold, rotation):
    # Rotated pages use observedBbox (exact box on the page as shown); v1 has none.
    return gold.get("observedBbox") or (None if rotation else gold["bbox"])


def union(boxes):
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


def location_ok(box, block, content):
    """Propose a location when the gold line lies inside the engine block's box and that
    box is tight around what the block actually holds (content = union of the gold boxes
    matched to it). A line inside a paragraph block counts; a page-sized box does not.
    An engine cell box is cell-sized by definition (gold boxes only cover the text in the
    cell), so the tightness test applies to text blocks only."""
    if not box or not block["bbox"] or contained(box, block["bbox"]) < CONTAINED:
        return False
    return block["cell"] is not None or area(block["bbox"]) <= GRANULARITY * area(content)


def propose_page(truth, blocks, sha256, rotation):
    # Match on normalized text but hand back the engine's raw characters.
    engine = []
    for index, block in enumerate(blocks):
        raw = block["text"]
        keep = [k for k, c in enumerate(raw) if normalize(c)]
        norm = normalize(raw)
        engine.append({**block, "index": index, "norm": norm, "map": keep, "chars": set(norm)})
    golds = {gold["id"]: gold for gold in truth["blocks"]}
    used = {}  # engine block index -> claimed [start, end) spans

    def candidate(gold, block):
        """Best still-free span of this engine block for this gold, or None."""
        expected = normalize(gold["text"])
        if not expected:
            # An empty gold cell only matches an engine cell that is empty at the same position.
            if gold.get("cell") and block["cell"] == gold["cell"] and not block["norm"] and not used.get(block["index"]):
                return 0.0, 0, 0
            return None
        if not block["norm"]:
            return None
        # A cell may sit inside an engine cell, or be a whole standalone block, but a short
        # cell like "%" must not claim a substring of running text.
        whole_only = gold.get("cell") is not None and block["cell"] is None
        if whole_only and abs(len(block["norm"]) - len(expected)) > ACCEPT_COST * len(expected):
            return None
        # Cheap bound: characters absent from the block each cost at least one edit.
        if sum(c not in block["chars"] for c in expected) / len(expected) > ACCEPT_COST:
            return None
        text = block["norm"]
        for s, e in used.get(block["index"], []):
            text = text[:s] + "\0" * (e - s) + text[e:]  # claimed characters never match
        cost, start, end = best_span(expected, text)
        if whole_only and (start, end) != (0, len(text)):
            return None
        if cost / len(expected) > ACCEPT_COST or any(start < e and s < end for s, e in used.get(block["index"], [])):
            return None
        return cost / len(expected), start, end

    def priority(gold_id, cost, index, start):
        # Long lines first: a 2-character header can match almost anywhere at low cost.
        # Equal cost (identical phrases): prefer the engine block that is where the gold is.
        box, engine_box = gold_box(golds[gold_id], rotation), engine[index]["bbox"]
        inside = bool(box and engine_box and contained(box, engine_box) >= CONTAINED)
        return (len(normalize(golds[gold_id]["text"])) < 4, cost, not inside, index, start)

    heap = []
    for gold in truth["blocks"]:
        for block in engine:
            found = candidate(gold, block)
            if found:
                cost, start, end = found
                heap.append((priority(gold["id"], cost, block["index"], start), gold["id"], block["index"], start, end, cost))
    heapq.heapify(heap)
    chosen = {}
    while heap:
        _, gold_id, index, start, end, cost = heapq.heappop(heap)
        if gold_id in chosen:
            continue
        spans = used.setdefault(index, [])
        if any(start < e and s < end for s, e in spans) or (start == end and spans):
            # Taken meanwhile: retry the same block with claimed text masked out.
            found = candidate(golds[gold_id], engine[index])
            if found:
                cost, start, end = found
                heapq.heappush(heap, (priority(gold_id, cost, index, start), gold_id, index, start, end, cost))
            continue
        spans.append((start, end))
        chosen[gold_id] = (cost, index, start, end)
    contents = {}
    for gold_id, (_, index, _, _) in chosen.items():
        box = gold_box(golds[gold_id], rotation)
        if box:
            contents.setdefault(index, []).append(box)
    aligned, review = [], []
    for gold in truth["blocks"]:
        pick = chosen.get(gold["id"])
        row = {"goldId": gold["id"], "gold": gold["text"], "cellGold": gold.get("cell")}
        if not pick:
            review.append({**row, "status": "missing"})
            continue
        cost, index, start, end = pick
        block = engine[index]
        whole = start == 0 and end == len(block["norm"])
        if block["map"] and end > start:
            text = block["text"][block["map"][start]:block["map"][end - 1] + 1]
        else:
            text = "" if not whole else block["text"]
        box = gold_box(gold, rotation)
        location = bool(box) and location_ok(box, block, union(contents[index]))
        aligned.append({"goldId": gold["id"], "text": text, "order": index * 100000 + start,
                        "sourceSha256": sha256, "page": 1, "cell": block["cell"],
                        "locationVerified": False, "locationProposed": location,
                        "engineRef": block["engineRef"], "span": [start, end], "wholeBlock": whole,
                        "cost": round(cost, 4)})
        review.append({**row, "status": "proposed", "engine": text, "cellEngine": block["cell"],
                       "cost": round(cost, 4), "wholeBlock": whole, "locationProposed": location,
                       "engineRef": block["engineRef"]})
    unmatched = [{"engineRef": b["engineRef"], "text": b["text"]} for b in engine
                 if b["index"] not in used and b["norm"]]
    return aligned, review, unmatched


def propose(manifest_path, run_dir):
    manifest = validate(manifest_path)
    run = read_json(run_dir / "run.json")
    if run.get("manifestSha256") != digest(manifest_path):
        raise ValueError("run belongs to a different corpus")
    by_id = {p["id"]: p for p in manifest["pages"]}
    pages, sheet = [], []
    for row in run["pages"]:
        page = by_id[row["id"]]
        if row["sourceSha256"] != page["sha256"]:
            raise ValueError("run source hash differs from manifest")
        truth = read_json(local_file(Path(manifest_path).parent, page["annotation"]))
        if row["status"] != "succeeded":
            pages.append({"id": row["id"], "status": "failed"})
            sheet.append({"id": row["id"], "category": page["category"], "status": "failed", "rows": [], "unmatched": []})
            continue
        raw_path = local_file(run_dir, row["rawFile"])
        if digest(raw_path) != row["rawSha256"]:
            raise ValueError(f"raw output changed: {row['id']}")
        raw = read_json(raw_path)
        if run["engine"] == "docling":
            blocks = docling_blocks(raw)
        else:
            blocks = paddle_blocks(raw)
        aligned, review, unmatched = propose_page(truth, blocks, page["sha256"], truth.get("rotation", 0))
        pages.append({"id": row["id"], "status": "succeeded", "blocks": aligned})
        sheet.append({"id": row["id"], "category": page["category"], "status": "succeeded",
                      "rows": review, "unmatched": unmatched})
    proposal = {"schemaVersion": 1, "engine": run["engine"], "manifestSha256": run["manifestSha256"],
                "runSha256": digest(run_dir / "run.json"), "alignmentReviewed": False,
                "proposer": {"sha256": digest(Path(__file__)), "acceptCost": ACCEPT_COST, "contained": CONTAINED,
                             "granularity": GRANULARITY},
                "note": "Machine proposal. A reviewer must correct goldId/span/locationVerified, then set alignmentReviewed.",
                "pages": pages}
    return proposal, sheet


def render_sheet(engine, sheet):
    def cell(c):
        return "" if not c else f"r{c['row']}c{c['col']}" + (f" {c['rowSpan']}×{c['colSpan']}" if c["rowSpan"] * c["colSpan"] > 1 else "")

    parts = [f"""<!doctype html><meta charset="utf-8"><title>D0 alignment review · {html.escape(engine)}</title>
<style>body{{font:14px system-ui,sans-serif;margin:16px;color:#1b1b1b;background:#fff}}
table{{border-collapse:collapse;width:100%;margin:8px 0 24px}}td,th{{border:1px solid #ccc;padding:3px 6px;vertical-align:top}}
th{{background:#f2f2f2;text-align:left}}.missing{{background:#fde8e8}}.diff{{background:#fff6d6}}.ok{{background:#eaf7ea}}
code{{font-size:12px;color:#555}}</style>
<h1>对齐复核表 · {html.escape(engine)}</h1>
<p>机器提议，未复核。绿＝文本一致；黄＝文本有差异或非整块；红＝缺失。复核时核对原页位置后再在 JSON 中设置 <code>locationVerified</code> 和 <code>alignmentReviewed</code>。</p>"""]
    for page in sheet:
        parts.append(f"<h2>{html.escape(page['id'])} <small>{page['category']} · {page['status']}</small></h2>")
        if page["status"] != "succeeded":
            continue
        parts.append("<table><tr><th>gold</th><th>标注文本</th><th>引擎文本</th><th>单元格 标注/引擎</th><th>代价</th><th>位置提议</th><th>来源</th></tr>")
        for r in page["rows"]:
            if r["status"] == "missing":
                klass, engine_text, meta = "missing", "（缺失）", ("", "", "", "")
            else:
                same = normalize(r["gold"]) == normalize(r["engine"]) and r["cellGold"] == r["cellEngine"]
                klass = "ok" if same and r["wholeBlock"] else "diff"
                engine_text = r["engine"]
                meta = (cell(r["cellEngine"]), r["cost"], "是" if r["locationProposed"] else "否",
                        r["engineRef"] + ("" if r["wholeBlock"] else " (片段)"))
            parts.append(f"<tr class={klass}><td>{r['goldId']}</td><td>{html.escape(r['gold'])}</td>"
                         f"<td>{html.escape(engine_text)}</td><td>{cell(r['cellGold'])} / {meta[0]}</td>"
                         f"<td>{meta[1]}</td><td>{meta[2]}</td><td><code>{html.escape(str(meta[3]))}</code></td></tr>")
        parts.append("</table>")
        if page["unmatched"]:
            parts.append("<p>未对上任何标注的引擎块：</p><ul>" + "".join(
                f"<li><code>{html.escape(u['engineRef'])}</code> {html.escape(u['text'])}</li>" for u in page["unmatched"]) + "</ul>")
    return "\n".join(parts) + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("run", type=Path, help="candidate output directory containing run.json")
    parser.add_argument("--output", type=Path, required=True, help="proposal JSON (must not exist)")
    parser.add_argument("--sheet", type=Path, help="HTML review sheet (must not exist)")
    args = parser.parse_args()
    proposal, sheet = propose(args.manifest, args.run)
    write_json(args.output, proposal)
    if args.sheet:
        with args.sheet.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(render_sheet(proposal["engine"], sheet))
    counts = {"pages": len(proposal["pages"]),
              "alignedBlocks": sum(len(p.get("blocks", [])) for p in proposal["pages"])}
    print(json.dumps(counts))
