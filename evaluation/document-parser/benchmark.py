"""D0 corpus validation and conservative scoring; no engine dependencies."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

CATEGORIES = ("text", "scan", "skew", "columns", "merged_table", "mixed")


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    # Serialize before creating files; atomically publish without replacing evidence.
    content = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=path.parent, prefix=".json-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def normalize(text):
    """Only whitespace and full-width ASCII; preserve units and punctuation."""
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    text = "".join(chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else c for c in text)
    return re.sub(r"\s+", "", text)


def local_file(root, relative):
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("corpus path must be relative and contained")
    resolved = (root / path).resolve(strict=True)
    if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
        raise ValueError("corpus path escapes root or is not a file")
    return resolved


def validate(manifest_path):
    manifest_path = Path(manifest_path)
    manifest = read_json(manifest_path)
    if manifest.get("schemaVersion") != 1:
        raise ValueError("unsupported corpus schema")
    pages = manifest["pages"]
    if len(pages) != 60 or Counter(p["category"] for p in pages) != Counter(dict.fromkeys(CATEGORIES, 10)):
        raise ValueError("expected 60 pages, 10 per category")
    if Counter(p["split"] for p in pages) != Counter({"dev": 40, "holdout": 20}):
        raise ValueError("expected 40 dev / 20 holdout")
    if len({p["id"] for p in pages}) != 60:
        raise ValueError("duplicate page IDs")
    pilot = [p for p in pages if p["pilot"]]
    if Counter(p["category"] for p in pilot) != Counter(dict.fromkeys(CATEGORIES, 2)) or any(p["split"] != "dev" for p in pilot):
        raise ValueError("pilot must contain two dev pages per category")
    for page in pages:
        if page["page"] != 1 or not page.get("rights"):
            raise ValueError("each sample is a single page with rights metadata")
        for key, hash_key in (("file", "sha256"), ("annotation", "annotationSha256")):
            if digest(local_file(manifest_path.parent, page[key])) != page[hash_key]:
                raise ValueError(f"hash mismatch: {page['id']} {key}")
        truth = read_json(local_file(manifest_path.parent, page["annotation"]))
        blocks = truth["blocks"]
        ids = [b["id"] for b in blocks]
        if not ids or len(set(ids)) != len(ids):
            raise ValueError("empty or duplicate annotation blocks")
        for block in blocks:
            normalize(block["text"])
            box = block["bbox"]
            if len(box) != 4 or not 0 <= box[0] < box[2] <= 1 or not 0 <= box[1] < box[3] <= 1:
                raise ValueError("invalid normalized annotation bbox")
    return manifest


def distance(a, b):
    previous = list(range(len(b) + 1))
    for i, char in enumerate(a, 1):
        current = [i]
        for j, other in enumerate(b, 1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (char != other)))
        previous = current
    return previous[-1]


def cell_neighbours(blocks, actual):
    """Structure diagnostic, tolerant of a global shift: for every right/down neighbour
    pair of gold cells, does the engine keep them adjacent in the same direction?"""
    cells = {(b["cell"]["row"], b["cell"]["col"]): b for b in blocks if b.get("cell")}
    total = correct = 0
    for (row, col), gold in cells.items():
        span = gold["cell"]
        for other_key, axis in (((row, col + span["colSpan"]), "right"), ((row + span["rowSpan"], col), "down")):
            other = cells.get(other_key)
            if other is None:
                continue
            total += 1
            a, b = actual.get(gold["id"], {}).get("cell"), actual.get(other["id"], {}).get("cell")
            if not a or not b:
                continue
            if axis == "right":
                correct += a["row"] == b["row"] and b["col"] == a["col"] + a["colSpan"]
            else:
                correct += a["col"] == b["col"] and b["row"] == a["row"] + a["rowSpan"]
    return total, correct


def score_page(truth, result, sha256, provisional=False):
    """Alignment is a reviewed artifact, not guessed from recognized text.

    Missing/failed pages retain every denominator. A coordinate/key belongs to
    the gold annotation; only a separate reviewed alignment maps engine blocks.
    """
    blocks = result.get("blocks", []) if result.get("status") == "succeeded" else []
    ids = [b["goldId"] for b in blocks]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate aligned block")
    gold_ids = {b["id"] for b in truth["blocks"]}
    if set(ids) - gold_ids:
        raise ValueError("unknown aligned block")
    actual = {b["goldId"]: b for b in blocks}
    counts = Counter()
    for gold in truth["blocks"]:
        found = actual.get(gold["id"])
        expected = normalize(gold["text"])
        text = normalize(found["text"]) if found else ""
        counts["characters"] += len(expected)
        counts["edits"] += distance(expected, text)
        counts["blocks"] += 1
        # Coordinates need human verification in alignment; presence alone is insufficient.
        located = found and (found.get("locationVerified") is True
                             or (provisional and found.get("locationProposed") is True))
        if located and found.get("sourceSha256") == sha256 and found.get("page") == 1:
            counts["correctSources"] += 1
        if gold.get("keyNumber"):
            counts["numbers"] += 1
            counts["correctNumbers"] += bool(found and text == expected)
        for number in gold.get("numbers", []):
            # A number inside a line must appear whole: not part of a longer number.
            token = re.escape(normalize(number))
            counts["numbers"] += 1
            counts["correctNumbers"] += bool(found and re.search(rf"(?<![\d.,]){token}(?![\d.,%])", text))
        if gold.get("cell") is not None:
            counts["cells"] += 1
            counts["correctCells"] += bool(found and text == expected and found.get("cell") == gold["cell"])
            # Diagnostic split: recognition right, wherever the structure put it.
            counts["correctCellTexts"] += bool(found and text == expected)
    neighbours = cell_neighbours(truth["blocks"], actual)
    counts["cellNeighbours"] += neighbours[0]
    counts["correctCellNeighbours"] += neighbours[1]
    for left, right in zip(truth["blocks"], truth["blocks"][1:]):
        counts["orderPairs"] += 1
        a, b = actual.get(left["id"]), actual.get(right["id"])
        counts["correctOrderPairs"] += bool(a and b and a["order"] < b["order"])
    # Diagnostic beyond the plan's adjacent-pair gate: interleaved columns keep most
    # adjacent pairs in order, but break many non-adjacent ones.
    for i, left in enumerate(truth["blocks"]):
        for right in truth["blocks"][i + 1:]:
            counts["allOrderPairs"] += 1
            a, b = actual.get(left["id"]), actual.get(right["id"])
            counts["correctAllOrderPairs"] += bool(a and b and a["order"] < b["order"])
    return counts


def metrics(counts):
    ratios = {"cellAccuracy": ("correctCells", "cells"), "cellTextAccuracy": ("correctCellTexts", "cells"),
              "cellNeighbourAccuracy": ("correctCellNeighbours", "cellNeighbours"), "numberAccuracy": ("correctNumbers", "numbers"),
              "sourceAccuracy": ("correctSources", "blocks"), "orderAccuracy": ("correctOrderPairs", "orderPairs"),
              "allPairsOrderAccuracy": ("correctAllOrderPairs", "allOrderPairs"),
              "characterErrorRate": ("edits", "characters")}
    return {**dict(counts), **{key: counts[a] / counts[b] if counts[b] else None for key, (a, b) in ratios.items()}}


def score(manifest_path, results_path, selection="pilot", provisional=False):
    manifest = validate(manifest_path)
    run = read_json(results_path)
    if run.get("manifestSha256") != digest(manifest_path):
        raise ValueError("run belongs to a different corpus")
    if run.get("alignmentReviewed") is not True and not provisional:
        raise ValueError("alignment must be independently reviewed before scoring")
    rows = run["pages"]
    if len({r["id"] for r in rows}) != len(rows):
        raise ValueError("duplicate result page")
    pages = [p for p in manifest["pages"] if (p["pilot"] if selection == "pilot" else p["split"] == selection)]
    if {r["id"] for r in rows} - {p["id"] for p in pages}:
        raise ValueError("unexpected result pages (split leakage)")
    by_id = {r["id"]: r for r in rows}
    counts = defaultdict(Counter)
    failures = []
    for page in pages:
        result = by_id.get(page["id"], {"status": "missing"})
        if result["status"] not in ("succeeded", "failed", "missing"):
            raise ValueError("invalid page status")
        if result["status"] != "succeeded":
            failures.append({"id": page["id"], "status": result["status"]})
        truth = read_json(local_file(Path(manifest_path).parent, page["annotation"]))
        counts[page["category"]].update(score_page(truth, result, page["sha256"], provisional))
    reviewed = run.get("alignmentReviewed") is True
    note = "D0 diagnostic only; synthetic holdout is not independent blind validation"
    if not reviewed:
        note += ". PROVISIONAL: machine-proposed alignment and locations, not reviewed; not decision evidence"
    return {"schemaVersion": 1, "selection": selection, "expectedPages": len(pages),
            "accountedPages": len(rows), "failures": failures, "alignmentReviewed": reviewed,
            "provisional": not reviewed, "releaseQualified": False, "note": note,
            "categories": {k: metrics(v) for k, v in sorted(counts.items())}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--results", type=Path)
    parser.add_argument("--selection", choices=("pilot", "dev", "holdout"), default="pilot")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--provisional", action="store_true",
                        help="score an unreviewed machine proposal; the report is labelled provisional")
    args = parser.parse_args()
    report = score(args.manifest, args.results, args.selection, args.provisional) if args.results else {"valid": True, "pages": len(validate(args.manifest)["pages"])}
    if args.output:
        write_json(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
