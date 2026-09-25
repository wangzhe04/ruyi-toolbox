"""Render corpus pages with gold boxes and reading order drawn on top, for annotation review."""
import argparse
from pathlib import Path

from PIL import ImageDraw
import pypdfium2

from benchmark import local_file, read_json, validate


def render(manifest_path, output, ids=None, selection="pilot", dpi=110):
    manifest = validate(manifest_path)
    output.mkdir(parents=True, exist_ok=False)
    for page in manifest["pages"]:
        if ids and page["id"] not in ids:
            continue
        if not ids and not (page["pilot"] if selection == "pilot" else page["split"] == selection):
            continue
        truth = read_json(local_file(manifest_path.parent, page["annotation"]))
        document = pypdfium2.PdfDocument(local_file(manifest_path.parent, page["file"]))
        image = document[0].render(scale=dpi / 72).to_pil().convert("RGB")
        document.close()
        draw = ImageDraw.Draw(image)
        w, h = image.size
        centers = []
        for order, block in enumerate(truth["blocks"]):
            # On rotated pages, draw where the text actually is on the page as shown.
            l, t, r, b = block.get("observedBbox", block["bbox"])
            color = (220, 40, 40) if block.get("keyNumber") else ((40, 90, 220) if block.get("cell") else (20, 150, 60))
            draw.rectangle([l * w, t * h, r * w, b * h], outline=color, width=1)
            draw.text((l * w, max(0, t * h - 9)), str(order), fill=color)
            centers.append(((l + r) / 2 * w, (t + b) / 2 * h))
        draw.line(centers, fill=(255, 150, 0), width=1)
        image.save(output / f"{page['id']}.png")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("output", type=Path, help="new directory for PNGs")
    parser.add_argument("--id", action="append", help="page id; repeatable (default: selection)")
    parser.add_argument("--selection", choices=("pilot", "dev", "holdout"), default="pilot")
    args = parser.parse_args()
    render(args.manifest, args.output, args.id, args.selection)
