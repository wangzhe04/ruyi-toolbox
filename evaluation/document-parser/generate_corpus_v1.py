"""Generator v1 (kept to reproduce the 2026-09-22/24 pilot evidence); superseded by generate_corpus.py v2."""
import argparse
from importlib.metadata import version
from io import BytesIO
from pathlib import Path
import subprocess

from PIL import Image, ImageFilter
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.utils import ImageReader

from benchmark import CATEGORIES, digest, write_json

WIDTH, HEIGHT = 595, 842


def make_page(path, category, index):
    stream = BytesIO()
    pdf = canvas.Canvas(stream, pagesize=(WIDTH, HEIGHT), invariant=1)
    blocks = []

    def text(value, x, y, size=13, cell=None, key=False):
        pdf.setFont("CorpusChinese", size)
        pdf.drawString(x, y, value)
        blocks.append({"id": f"b{len(blocks) + 1}", "text": value,
                       "bbox": [x / WIDTH, (HEIGHT - y - size) / HEIGHT,
                                (x + max(8, pdfmetrics.stringWidth(value, "CorpusChinese", size))) / WIDTH,
                                (HEIGHT - y + 3) / HEIGHT], "cell": cell, "keyNumber": key})

    text(f"如意文档评测 / 合成材料 {index:02d}", 42, 795, 19)
    text("仅供解析回归测试，不含真实客户或财务数据。", 42, 765)
    if category == "columns":
        for x, label in ((42, "左栏"), (310, "右栏")):
            for line in range(8):
                text(f"{label}第{line + 1}段：核对项目进度。", x, 710 - line * 32, 12)
    else:
        text("项目：本地资料数字化与逐页核对", 42, 710)
        text("编号 RY-2026 / CPU 4 threads / 批次 " + str(index), 42, 680)
        text("金额与单位需原样保留；空单元格不补零。", 42, 650)
        # Each numeric fact gets its own annotation, so substring matches cannot pass.
        rows = [["项目", "数量", "金额（元）"],
                [f"材料 A-{index}", str(index * 7), f"{index * 137}.25"],
                ["差额 adjustment", "", f"(-{index * 11}.50)"],
                ["税率 / rate", "13.00%", "备注[1]"]]
        xs, top, step = [42, 237, 357, 552], 580, 44
        if category == "merged_table":
            pdf.rect(xs[0], top, xs[-1] - xs[0], step)
            text("季度汇总（跨三列合并）", 52, top + 16, 13,
                 {"row": 0, "col": 0, "rowSpan": 1, "colSpan": 3})
        offset = int(category == "merged_table")
        for row, values in enumerate(rows):
            for col, value in enumerate(values):
                y = top - (row + 1) * step
                pdf.rect(xs[col], y, xs[col + 1] - xs[col], step)
                text(value, xs[col] + 9, y + 16, 12,
                     {"row": row + offset, "col": col, "rowSpan": 1, "colSpan": 1},
                     key=(row in (1, 2) and col == 2) or (row == 3 and col == 1))
        text("[1] 括号和负号均属于原文，不进行数字纠正。", 42, 370, 11)
        if category == "mixed":
            text("Model Qwen3 / 编号 AB-09 / 温度 -12.5°C", 42, 352)
            text("１，２３４．５０元 / 8.25 kg / 版本 v2.1", 42, 324)
    pdf.showPage()
    pdf.save()
    path.write_bytes(stream.getvalue())
    return {"schemaVersion": 1, "pageSize": [WIDTH, HEIGHT], "rotation": 0,
            "coordinateSpace": "upright-top-left-normalized", "blocks": blocks}


def generate(root, font, poppler):
    root.mkdir(parents=True, exist_ok=False)
    pdfmetrics.registerFont(TTFont("CorpusChinese", str(font)))
    pages = []
    for cat_index, category in enumerate(CATEGORIES):
        # Four holdout pages in first two classes, three in others: 20 total.
        dev_count = 6 if cat_index < 2 else 7
        for index in range(1, 11):
            sample_id = f"{category}-{index:02d}"
            path = root / f"{sample_id}.pdf"
            truth = make_page(path, category, index)
            if category != "text":
                prefix = root / sample_id
                subprocess.run([poppler, "-singlefile", "-r", "110" if category == "skew" else "170",
                                "-png", str(path), str(prefix)], check=True, capture_output=True)
                image_path = prefix.with_suffix(".png")
                with Image.open(image_path) as original:
                    image = original.convert("RGB")
                if category == "skew":
                    image = image.filter(ImageFilter.GaussianBlur(0.6)).rotate(2, expand=False, fillcolor="white")
                    # Gold boxes describe the upright source; no invented mapping to rotated pixels.
                    truth["rotation"] = 2
                    truth["requiresManualLocationReview"] = True
                pdf = canvas.Canvas(str(path), pagesize=(WIDTH, HEIGHT), invariant=1)
                pdf.drawImage(ImageReader(image), 0, 0, width=WIDTH, height=HEIGHT)
                pdf.showPage()
                pdf.save()
                image_path.unlink()
            annotation = root / f"{sample_id}.gold.json"
            write_json(annotation, truth)
            pages.append({"id": sample_id, "category": category, "split": "dev" if index <= dev_count else "holdout",
                          "pilot": index <= 2, "page": 1, "file": path.name, "sha256": digest(path),
                          "annotation": annotation.name, "annotationSha256": digest(annotation),
                          "rights": "Original synthetic text, Apache-2.0; local font is not redistributed in git"})
    write_json(root / "manifest.json", {"schemaVersion": 1, "synthetic": True, "blind": False,
               "generatorVersion": 1, "fontSha256": digest(font),
               "dependencies": {name: version(name) for name in ("reportlab", "pillow")},
               "popplerVersion": subprocess.run([poppler, "-v"], capture_output=True, text=True, check=True).stderr.strip(),
               "pages": pages})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--font", type=Path, required=True)
    parser.add_argument("--pdftoppm", default="pdftoppm")
    args = parser.parse_args()
    generate(args.output, args.font, args.pdftoppm)
