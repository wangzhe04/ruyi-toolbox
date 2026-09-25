"""Generate local synthetic D0 fixtures (generator v2), never a substitute for real blind data.

v1 used one template per page and was too easy to separate engines; v2 lays out every
page from its own seed. Gold blocks are single visual lines (or table cells), so an
engine that merges lines into paragraphs is still aligned by substring, while one that
reads across columns is caught by reading order.
"""
import argparse
from importlib.metadata import version
from io import BytesIO
import math
from pathlib import Path
import random
import re

import numpy
from PIL import Image, ImageFilter
import pypdfium2
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from benchmark import CATEGORIES, digest, write_json

WIDTH, HEIGHT = 595, 842
MARGIN = 48
GENERATOR_VERSION = 2

# Original wording for this project (Apache-2.0); no real customers, people or figures.
SUBJECTS = ["本季度资料室", "项目组", "档案扫描小组", "财务核对岗", "运营支持组", "第二批次", "质检环节", "外包录入方"]
ACTIONS = ["完成了纸质凭证的逐页扫描", "复核了合同附件中的金额", "整理了跨年度的往来明细", "补录了缺失的签收记录",
           "比对了系统导出与原件", "标注了需要人工确认的页面", "归档了已经核对的批次", "更新了设备借用台账"]
DETAILS = ["其中部分页面存在折痕和装订孔", "个别数字需要回到原页核对", "表格跨页时表头重复出现", "空白单元格保持为空不补零",
           "负数统一使用括号或负号表示", "单位与数值之间可能有空格", "手写批注不纳入本次识别范围", "扫描分辨率因设备不同而有差异"]
ENDINGS = ["下周继续推进。", "结果已同步给负责人。", "待抽样复核后关闭。", "异常清单另附。", "不影响整体进度。"]
ITEMS = ["打印耗材", "档案盒", "扫描外包", "差旅补贴", "设备维护", "软件订阅", "场地租赁", "培训费用", "快递费", "办公家具"]
GROUPS = ["华东", "华北", "西南", "华南"]
ENGLISH = ["Batch", "Scanner", "Invoice", "Version", "Model", "Ref", "Ticket", "Region"]
FULLWIDTH = str.maketrans("0123456789.,%-", "０１２３４５６７８９．，％－")
NO_LINE_START = "，。、；：？！）》」』”’％"
NUMBER = re.compile(r"[-+(]?\d[\d,]*(?:\.\d+)?%?\)?")


def numbers_in(text):
    """Every numeric token of a line, as printed (normalization happens at scoring)."""
    ascii_text = "".join(chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else c for c in text)
    return [m.group() for m in NUMBER.finditer(ascii_text.replace(" ", "")) if any(ch.isdigit() for ch in m.group())]


def sentence(rng, numbers=False):
    parts = [rng.choice(SUBJECTS), rng.choice(ACTIONS)]
    if numbers:
        parts.append(f"共 {rng.randint(12, 980)} 页，涉及金额 {rng.randint(1, 99)},{rng.randint(100, 999)}.{rng.randint(10, 99)} 元")
    parts.append(rng.choice(DETAILS))
    return "，".join(parts) + "，" + rng.choice(ENDINGS)


def money(rng, negative=False):
    value = f"{rng.randint(1, 98)},{rng.randint(100, 999)}.{rng.randint(0, 99):02d}" if rng.random() < 0.5 \
        else f"{rng.randint(10, 999)}.{rng.randint(0, 99):02d}"
    return f"({value})" if negative and rng.random() < 0.5 else (f"-{value}" if negative else value)


class Page:
    def __init__(self, font):
        self.stream = BytesIO()
        self.pdf = canvas.Canvas(self.stream, pagesize=(WIDTH, HEIGHT), invariant=1)
        self.font = font
        self.blocks = []

    def width(self, value, size):
        return pdfmetrics.stringWidth(value, self.font, size)

    def line(self, value, x, y, size, cell=None, key=False, align="left", numbers=False):
        """Draw one line at baseline y (PDF points, bottom-left origin) and annotate it.

        numbers: False, True (extract plain numeric tokens) or an explicit token list."""
        self.pdf.setFont(self.font, size)
        w = self.width(value, size)
        if align == "right":
            x -= w
            self.pdf.drawRightString(x + w, y, value)
        else:
            self.pdf.drawString(x, y, value)
        self.blocks.append({"id": f"b{len(self.blocks) + 1}", "text": value,
                            "bbox": [x / WIDTH, (HEIGHT - y - size) / HEIGHT,
                                     (x + max(6, w)) / WIDTH, (HEIGHT - y + 0.3 * size) / HEIGHT],
                            "cell": cell, "keyNumber": key,
                            "numbers": list(numbers) if isinstance(numbers, list) else (numbers_in(value) if numbers else [])})

    def wrap(self, value, width, size):
        # CJK breaks anywhere; an ASCII run (word, number) is never split.
        tokens, run = [], ""
        for char in value:
            if char.isascii() and not char.isspace():
                run += char
                continue
            if run:
                tokens.append(run)
                run = ""
            tokens.append(char)
        if run:
            tokens.append(run)
        lines, current = [], ""
        for token in tokens:
            # Closing punctuation never starts a line; it may overhang the measure instead.
            if current and token not in NO_LINE_START and self.width(current + token, size) > width:
                lines.append(current.rstrip())
                current = token.lstrip()
            else:
                current += token
        if current.strip():
            lines.append(current.rstrip())
        return lines

    def paragraph(self, value, x, y, width, size, leading=1.55, numbers=False):
        for text in self.wrap(value, width, size):
            self.line(text, x, y, size, numbers=numbers)
            y -= size * leading
        return y

    def table(self, x, top, widths, rows, height, spans=None, wired=True, keys=(), size=10.5, header_rows=1):
        """rows[r][c] is text or None for a position covered by a span; spans maps origin -> (rs, cs)."""
        spans = spans or {}
        lefts = [x + sum(widths[:c]) for c in range(len(widths) + 1)]
        self.pdf.setLineWidth(0.7)
        for r, values in enumerate(rows):
            for c, value in enumerate(values):
                if value is None:
                    continue
                rs, cs = spans.get((r, c), (1, 1))
                x0, x1 = lefts[c], lefts[c + cs]
                y1 = top - r * height
                y0 = y1 - rs * height
                if wired:
                    self.pdf.rect(x0, y0, x1 - x0, y1 - y0)
                numeric = r >= header_rows and any(ch.isdigit() for ch in value) and c > 0
                baseline = (y0 + y1) / 2 - size * 0.35
                cell = {"row": r, "col": c, "rowSpan": rs, "colSpan": cs}
                key = (r, c) in keys
                if numeric:
                    self.line(value, x1 - 6, baseline, size, cell, key, align="right")
                else:
                    self.line(value, x0 + 6, baseline, size, cell, key)
        if not wired:
            bottom = top - len(rows) * height
            for y in (top, top - header_rows * height, bottom):
                self.pdf.line(lefts[0], y, lefts[-1], y)
        return top - len(rows) * height

    def finish(self, path):
        self.pdf.showPage()
        self.pdf.save()
        path.write_bytes(self.stream.getvalue())
        return {"schemaVersion": 1, "pageSize": [WIDTH, HEIGHT], "rotation": 0,
                "coordinateSpace": "upright-top-left-normalized", "blocks": self.blocks}


def header(page, rng, index):
    page.line(f"如意文档评测 · 合成材料 {index:02d}", MARGIN, 790, rng.choice((17, 18, 19)))
    page.line("仅供解析回归测试，不含真实客户、人员或财务数据。", MARGIN, 762, 10.5)
    return 730


def simple_table(page, rng, top):
    rows = [["项目", "数量", "单价（元）", "金额（元）"]]
    keys = []
    for r in range(rng.randint(3, 5)):
        qty, price = rng.randint(1, 60), rng.randint(3, 900) + rng.randint(0, 99) / 100
        amount = "" if r == 1 else f"{qty * price:,.2f}"
        rows.append([rng.choice(ITEMS), str(qty), f"{price:.2f}", amount])
        keys += [(r + 1, 2)] + ([(r + 1, 3)] if amount else [])
    rows.append(["差额 adjustment", "", "", money(rng, negative=True)])
    keys.append((len(rows) - 1, 3))
    return page.table(MARGIN, top, [170, 80, 110, 139], rows, 24, wired=rng.random() < 0.7, keys=keys)


def body_page(page, rng, index):
    y = header(page, rng, index)
    width = WIDTH - 2 * MARGIN
    size = rng.choice((10.5, 11, 12))
    for _ in range(rng.randint(2, 3)):
        y = page.paragraph(sentence(rng, rng.random() < 0.5) + sentence(rng), MARGIN, y, width, size,
                           numbers=True) - size * 0.8
    y = simple_table(page, rng, y - 6) - 22
    page.line(f"[1] 金额单位为元；括号表示负数，按原文保留。编号 RY-{rng.randint(2000, 2099)}-{index:02d}", MARGIN, y, 9)


def columns_page(page, rng, index):
    y = header(page, rng, index)
    width = WIDTH - 2 * MARGIN
    y = page.paragraph("本页为分栏排版：" + sentence(rng), MARGIN, y, width, 11) - 12
    count = 3 if index % 3 == 0 else 2
    gap = 22
    col_width = (width - gap * (count - 1)) / count
    size = rng.choice((10, 10.5, 11))
    bottoms = []
    for col in range(count):
        x = MARGIN + col * (col_width + gap)
        cy = y - rng.uniform(0, 8)
        for para in range(rng.randint(2, 4)):
            label = f"第{'一二三'[col]}栏第{para + 1}段："
            text = label + "".join(sentence(rng) for _ in range(rng.randint(1, 2)))
            cy = page.paragraph(text, x, cy, col_width, size, leading=rng.choice((1.45, 1.6, 1.75))) - rng.uniform(6, 18)
        bottoms.append(cy)
    page.line("栏后说明：以上分栏内容应先读完一栏，再读下一栏。", MARGIN, min(bottoms) - 16, 10)


def merged_table_page(page, rng, index):
    y = header(page, rng, index)
    y = page.paragraph("下表含两级表头、跨行分组与跨列小计：" + sentence(rng), MARGIN, y, WIDTH - 2 * MARGIN, 11) - 14
    rows = [["地区", "项目", "数量", None, "金额（元）", None],
            [None, None, "本期", "上期", "本期", "上期"]]
    spans = {(0, 0): (2, 1), (0, 1): (2, 1), (0, 2): (1, 2), (0, 4): (1, 2)}
    keys = []
    for group in rng.sample(GROUPS, 2):
        n = rng.randint(2, 3)
        spans[(len(rows), 0)] = (n, 1)
        for i in range(n):
            empty = rng.random() < 0.2
            row = [group if i == 0 else None, rng.choice(ITEMS), str(rng.randint(1, 90)),
                   "" if empty else str(rng.randint(1, 90)), money(rng), "" if empty else money(rng, rng.random() < 0.2)]
            keys += [(len(rows), 4)] + ([] if empty else [(len(rows), 5)])
            rows.append(row)
    rows.append(["合计", None, None, None, money(rng), money(rng)])
    spans[(len(rows) - 1, 0)] = (1, 4)
    keys += [(len(rows) - 1, 4), (len(rows) - 1, 5)]
    y = page.table(MARGIN, y, [58, 120, 60, 60, 101, 100], rows, 25, spans,
                   wired=index % 4 != 0, keys=keys, header_rows=2) - 22
    page.line("注：“上期”为空表示当期新增项目，不代表零。", MARGIN, y, 9.5)


def mixed_page(page, rng, index):
    y = header(page, rng, index)
    width = WIDTH - 2 * MARGIN
    # Tokens are listed explicitly: codes, dates and versions must be read whole, and a
    # hyphen inside "AB-924" or "2026-03-05" is not a minus sign.
    code = f"{rng.choice('ABCDEFG')}{rng.randint(10, 99)}"
    date = f"2026年{rng.randint(1, 12)}月{rng.randint(1, 28)}日"
    temperature = f"-{rng.randint(1, 30)}.{rng.randint(0, 9)}"
    total, change, area = money(rng), f"{rng.choice('+-')}{rng.randint(0, 40)}.{rng.randint(0, 99):02d}%", str(rng.randint(20, 900))
    wide_money, wide_weight = money(rng).translate(FULLWIDTH), f"{rng.randint(1, 99)}.{rng.randint(10, 99)}".translate(FULLWIDTH)
    release, ticket = f"v{rng.randint(1, 5)}.{rng.randint(0, 20)}.{rng.randint(0, 9)}", f"AB-{rng.randint(100, 999)}"
    iso = f"2026-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
    lines = [(f"{rng.choice(ENGLISH)} {code} / 日期 {date} / 温度 {temperature}°C", [code, date, temperature]),
             (f"合计 {total} 元，同比 {change}，面积 {area} ㎡", [total, change, area]),
             (f"全角写法：{wide_money} 元 / {wide_weight} kg", [wide_money, wide_weight]),
             (f"版本 {release}；编号 {ticket}；ISO 8601：{iso}", [release, ticket, iso])]
    for text, tokens in lines:
        page.line(text, MARGIN, y, 11, numbers=tokens)
        y -= 22
    y = page.paragraph(sentence(rng, numbers=True), MARGIN, y - 4, width, 11, numbers=True) - 12
    rows = [["指标 Metric", "单位", "Q1", "Q2", "变化"]]
    keys = []
    for name, unit in rng.sample([("收入 Revenue", "万元"), ("成本 Cost", "万元"), ("毛利率 Margin", "%"),
                                  ("工单 Tickets", "件"), ("时长 Duration", "h")], 4):
        a, b = rng.randint(10, 999) + rng.randint(0, 9) / 10, rng.randint(10, 999) + rng.randint(0, 9) / 10
        rows.append([name, unit, f"{a:.1f}", f"{b:.1f}", f"{b - a:+.1f}"])
        keys += [(len(rows) - 1, 2), (len(rows) - 1, 3), (len(rows) - 1, 4)]
    page.table(MARGIN, y, [150, 60, 95, 95, 99], rows, 23, wired=False, keys=keys)


def rotate_box(box, degrees):
    """Exact axis-aligned box of an upright normalized box after PIL rotate(degrees, expand=False)."""
    theta = math.radians(degrees)
    cx, cy = WIDTH / 2, HEIGHT / 2
    xs, ys = [], []
    for x, y in ((box[0], box[1]), (box[2], box[1]), (box[0], box[3]), (box[2], box[3])):
        dx, dy = x * WIDTH - cx, y * HEIGHT - cy
        xs.append(cx + dx * math.cos(theta) + dy * math.sin(theta))
        ys.append(cy - dx * math.sin(theta) + dy * math.cos(theta))
    return [max(0.0, min(xs) / WIDTH), max(0.0, min(ys) / HEIGHT), min(1.0, max(xs) / WIDTH), min(1.0, max(ys) / HEIGHT)]


def degrade(pdf_bytes, rng, category):
    """Rasterize and replace the page: scanned categories never keep a hidden text layer."""
    dpi = {"skew": rng.randint(105, 130), "scan": rng.randint(150, 200)}.get(category, 170)
    document = pypdfium2.PdfDocument(pdf_bytes)
    image = document[0].render(scale=dpi / 72).to_pil().convert("L")
    document.close()
    settings = {"dpi": dpi}
    if category in ("scan", "skew"):
        blur = rng.uniform(0.3, 0.9) if category == "skew" else rng.uniform(0.0, 0.4)
        image = image.filter(ImageFilter.GaussianBlur(blur))
        sigma = rng.uniform(4, 10)
        noisy = numpy.asarray(image, dtype=numpy.float32) + numpy.random.default_rng(rng.randint(0, 2**32 - 1)).normal(0, sigma, (image.height, image.width))
        image = Image.fromarray(numpy.clip(noisy, 0, 255).astype(numpy.uint8))
        settings.update(blur=round(blur, 3), noiseSigma=round(sigma, 2))
    if category == "skew":
        angle = rng.choice((-1, 1)) * rng.uniform(1.0, 4.0)
        image = image.rotate(angle, resample=Image.BICUBIC, expand=False, fillcolor=255)
        settings["rotation"] = round(angle, 3)
    quality = rng.randint(55, 70) if category == "skew" else rng.randint(70, 88)
    jpeg = BytesIO()
    image.save(jpeg, "JPEG", quality=quality)
    settings["jpegQuality"] = quality
    stream = BytesIO()
    pdf = canvas.Canvas(stream, pagesize=(WIDTH, HEIGHT), invariant=1)
    pdf.drawImage(ImageReader(BytesIO(jpeg.getvalue())), 0, 0, width=WIDTH, height=HEIGHT)
    pdf.showPage()
    pdf.save()
    return stream.getvalue(), settings


LAYOUTS = {"text": body_page, "scan": body_page, "skew": body_page, "columns": columns_page,
           "merged_table": merged_table_page, "mixed": mixed_page}


def generate(root, fonts):
    root.mkdir(parents=True, exist_ok=False)
    names = []
    for i, font in enumerate(fonts):
        names.append(f"CorpusFont{i}")
        options = {"subfontIndex": 0} if font.suffix.lower() == ".ttc" else {}
        pdfmetrics.registerFont(TTFont(names[-1], str(font), **options))
    pages = []
    for cat_index, category in enumerate(CATEGORIES):
        dev_count = 6 if cat_index < 2 else 7  # 20 holdout pages in total
        for index in range(1, 11):
            sample_id = f"{category}-{index:02d}"
            rng = random.Random(f"ruyi-d0-v{GENERATOR_VERSION}/{sample_id}")
            font = rng.randrange(len(fonts))
            page = Page(names[font])
            LAYOUTS[category](page, rng, index)
            path = root / f"{sample_id}.pdf"
            truth = page.finish(path)
            truth["font"] = fonts[font].name
            if category != "text":
                data, settings = degrade(path.read_bytes(), rng, category)
                path.write_bytes(data)
                truth["degradation"] = settings
                if "rotation" in settings:
                    # Gold stays upright; observedBbox is the exact box on the rotated page as shown.
                    truth["rotation"] = settings["rotation"]
                    for block in truth["blocks"]:
                        block["observedBbox"] = rotate_box(block["bbox"], settings["rotation"])
            annotation = root / f"{sample_id}.gold.json"
            write_json(annotation, truth)
            pages.append({"id": sample_id, "category": category, "split": "dev" if index <= dev_count else "holdout",
                          "pilot": index <= 2, "page": 1, "file": path.name, "sha256": digest(path),
                          "annotation": annotation.name, "annotationSha256": digest(annotation),
                          "rights": "Original synthetic text, Apache-2.0; local fonts are not redistributed in git"})
    write_json(root / "manifest.json", {"schemaVersion": 1, "synthetic": True, "blind": False,
               "generatorVersion": GENERATOR_VERSION, "generatorSha256": digest(Path(__file__)),
               "fonts": [{"name": f.name, "sha256": digest(f)} for f in fonts],
               "dependencies": {name: version(name) for name in ("reportlab", "pillow", "pypdfium2", "numpy")},
               "pages": pages})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--font", type=Path, action="append", required=True,
                        help="repeatable; each page picks one deterministically")
    args = parser.parse_args()
    generate(args.output, args.font)
