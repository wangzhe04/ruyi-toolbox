"""Pilot-only pypdf text-layer baseline; does not provide OCR or table structure."""
import argparse
from importlib.metadata import version
from pathlib import Path
import platform
import time

from pypdf import PdfReader

from benchmark import digest, local_file, validate, write_json


def run(manifest_path, output):
    manifest = validate(manifest_path)
    # No annotations are sent to an engine; validation reads only to check integrity.
    rows = []
    output.mkdir(parents=True, exist_ok=False)
    for page in manifest["pages"]:
        if not page["pilot"]:
            continue
        started = time.perf_counter()
        row = {"id": page["id"], "sourceSha256": page["sha256"], "page": 1}
        try:
            reader = PdfReader(local_file(manifest_path.parent, page["file"]))
            text = reader.pages[0].extract_text()
            row.update(status="succeeded" if text.strip() else "unsupported",
                       reason=None if text.strip() else "no_text_layer", textChars=len(text))
            write_json(output / f"{page['id']}.raw.json", {"text": text})
        except Exception as error:
            row.update(status="failed", errorType=type(error).__name__)
        row["elapsedSec"] = time.perf_counter() - started
        rows.append(row)
    report = {"schemaVersion": 1, "engine": "pypdf-text-only", "version": version("pypdf"),
              "python": platform.python_version(), "manifestSha256": digest(manifest_path),
              "qualityScored": False, "releaseQualified": False, "pages": rows}
    write_json(output / "run.json", report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    report = run(args.manifest, args.output)
    for status in ("succeeded", "unsupported", "failed"):
        print(f"{status}: {sum(row['status'] == status for row in report['pages'])}")
