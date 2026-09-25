# 文档解析 D0 评测工具（开发用，非 MCP 组件）

落实 [05 号规划](../../docs/05-document-processing-plan.md) 的首个增量：可重建合成样本、标注、校验和评分；不注册到如意，不代表 D0/D1 已完成。

## 样本与标注

`generate_corpus.py`（生成器 v2）生成 60 个单页 PDF：文字层、扫描、倾斜低清、多栏、合并表格、中英数字混排各 10 页。除文字层类外，其余 PDF 只含 JPEG 栅格图像，不能靠隐藏文字层通过 OCR。每页用自己的种子独立排版（字体、字号、段落、表格尺寸、有线/无线表格均不同），生成结果逐字节可复现。

- **标注粒度是“一行”或“一个单元格”**。引擎把多行合成段落时，对齐器切子串照样能对上；读串栏会在阅读顺序上暴露。
- **多栏**：两栏或三栏，段落多行、长短不一、行距不同，左右不对齐——只有“先读完一栏”才对。v1 的双栏每段一行且逐行对齐，横读也说得通，已作废。
- **合并表格**：两级表头（跨列）、跨行分组、跨列合计、空白格；四分之一无竖线。
- **数字**：表格数值格用 `keyNumber`（整格精确）；行内数字写进 `numbers` 列表（整词出现才算对，`925` 不能匹配 `9250`）。混排页的编号、日期、版本号（`AB-924`、`2026-03-05`、`v2.11.9`）显式列为整词，连字符不当负号。
- **扫描/倾斜**：150–200 dpi 或 105–130 dpi，高斯噪声、模糊、JPEG 压缩；倾斜 ±1–4°。gold `bbox` 仍是未旋转页坐标，另存按旋转精确换算的 `observedBbox`，供位置核对。

固定 40 页 dev、20 页 holdout；每类前两页共 12 页是 pilot，全部来自 dev。**这些是同一生成器产生的合成回归样本，holdout 不是独立盲测**，不能证明真实中文办公材料准确率。正式 D0a 仍需引入有明确授权的真实材料、独立复核标注及隔离的盲测；不得用合成材料顶替规划质量门槛。

正文由本项目原创，按仓库 Apache-2.0 使用。字体由调用者提供（可重复 `--font`，每页确定性地选一个），manifest 记录每个字体哈希；本机 Windows 字体仅用于本地生成验证，不随 git 分发字体或 PDF。

生成依赖：ReportLab、Pillow、pypdfium2、numpy，装在本目录独立的 `.venv-fixtures`，不改系统包。`generate_corpus_v1.py` 保留旧生成器（需 Poppler `pdftoppm`），仅用于复现 2026-09-22/24 的 v1 证据。

从仓库根目录执行（输出目录必须不存在，避免覆盖旧证据）：

```powershell
uv venv --python 3.12 evaluation/document-parser/.venv-fixtures
uv pip install --python evaluation/document-parser/.venv-fixtures/Scripts/python.exe reportlab==4.4.9 pillow==12.3.0 pypdfium2==5.13.0 numpy
$fx = "evaluation/document-parser/.venv-fixtures/Scripts/python.exe"
& $fx evaluation/document-parser/generate_corpus.py evaluation/document-parser/work/corpus-v2 --font C:/Windows/Fonts/simhei.ttf --font C:/Windows/Fonts/simsun.ttc --font C:/Windows/Fonts/msyh.ttc --font C:/Windows/Fonts/simkai.ttf --font C:/Windows/Fonts/Deng.ttf
& $fx evaluation/document-parser/render_qa.py evaluation/document-parser/work/corpus-v2/manifest.json evaluation/document-parser/work/qa-v2
python evaluation/document-parser/benchmark.py evaluation/document-parser/work/corpus-v2/manifest.json
python -m unittest discover -s evaluation/document-parser/tests -v
```

`render_qa.py` 把 gold 框（绿＝文字行、蓝＝单元格、红＝关键数值格）和阅读顺序折线画在页面上，用于人工核对标注。

manifest 记录分类、分组、pilot、输入及标注哈希、字体哈希和生成工具版本；`inventory.csv` 是 v1 语料的逐页哈希清单。`work/`、虚拟环境和日志不入 git。

坐标采用左上角原点、0–1 范围；倾斜样本的 `bbox` 属于未倾斜页面，`observedBbox` 是它在倾斜页面上的精确外包框（v1 样本没有，只能人工核对位置）。阅读顺序按 blocks 数组排列；多栏先完整左栏，再完整右栏。表格 row/col 从 0 开始，合并跨度与空白格均显式保留。

## 评分输入

引擎原始输出另存。评分接受**人工复核后的块对应关系**，不能把 gold 文本复制成引擎结果。每个结果块通过 `goldId` 与标注对应，`text`、`cell`、`order` 必须来自引擎；位置核对后才设置 `locationVerified=true`。无法对齐的块视作缺失。当前工具用于诊断，不自动完成块匹配，不计算引擎额外幻觉块的精确率；字符错误率是对齐 gold 块的编辑距离。

```json
{
  "manifestSha256": "本轮 manifest 文件的 SHA-256",
  "alignmentReviewed": true,
  "pages": [{
    "id": "text-01",
    "status": "succeeded",
    "blocks": [{
      "goldId": "b1", "text": "引擎实际输出", "order": 0,
      "sourceSha256": "原 PDF 的 SHA-256", "page": 1,
      "locationVerified": true, "cell": null
    }]
  }]
}
```

失败页明确记录 `status=failed`，无需 blocks；完全漏掉的页按 missing 记账，两者仍计入所有质量指标分母。重复页、跨 split 结果、哈希变化和未复核结果直接拒绝。

```powershell
python evaluation/document-parser/benchmark.py evaluation/document-parser/work/corpus/manifest.json --results evaluation/document-parser/work/aligned.json --selection pilot --output evaluation/document-parser/work/score.json
```

分别报告六类的单元格准确率、关键数值精确匹配率、相邻块顺序、出处、字符错误率（均为 05 号规划门槛指标），另附诊断指标：`cellTextAccuracy`（格内文字对、不管结构）、`cellNeighbourAccuracy`（相邻格在引擎里仍左右/上下相邻，容忍整体平移，TEDS 的简化版）、`allPairsOrderAccuracy`（全部块对的先后）。诊断指标用来区分“认错字”和“结构错”，不替代门槛。只去空白、转换全角 ASCII，不删除负号、小数点、百分号、单位或括号；缺失空白格不能算正确。没有该类标注时指标为 null，不报 100%。`releaseQualified` 始终为 false，本工具无权替代全部发布验收。

## 候选安装探针

轻量文字层基线（需安装 pypdf；只处理 12 页 pilot，不做 OCR/表格识别）：

```powershell
python evaluation/document-parser/run_text_baseline.py evaluation/document-parser/work/corpus/manifest.json evaluation/document-parser/work/text-baseline
```

无文字层页记为 `unsupported/no_text_layer`，不能将空文本当成功。原始正文保存在被忽略的 work 目录，报告仅含元数据；该结果未经块对齐，不能直接传给评分器或当成候选质量分数。

`docling.in` / `paddle.in` 固定本轮顶层候选版本，**不是发行锁文件**。两个引擎各自使用 Python 3.12 venv。解析依赖和实际安装是两个不同的门槛，必须分别记录。

```powershell
$env:UV_CACHE_DIR = Join-Path $PWD '.tmp/uv-document-probe'
uv venv --python 3.12 evaluation/document-parser/.venv-docling
uv pip install --python evaluation/document-parser/.venv-docling/Scripts/python.exe -r evaluation/document-parser/docling.in
evaluation/document-parser/.venv-docling/Scripts/python.exe evaluation/document-parser/probe_install.py docling --output evaluation/document-parser/work/docling-probe.json

uv venv --python 3.12 evaluation/document-parser/.venv-paddle
uv pip install --python evaluation/document-parser/.venv-paddle/Scripts/python.exe -r evaluation/document-parser/paddle.in
evaluation/document-parser/.venv-paddle/Scripts/python.exe evaluation/document-parser/probe_install.py paddle --output evaluation/document-parser/work/paddle-probe.json
```

探针仅导入候选 API、运行 CPU 张量运算，记录包版本、线程数、耗时、RSS 和 Windows 峰值工作集。缓存重定向本目录，设置 HF 离线变量，不实例化需加载模型的转换器。环境变量不等于网络沙箱；**不因此宣称已通过断网推理**。进程异常退出时调用者还需保存退出码和 stderr。

后续 D0b：固定模型 revision/哈希与 OCR 后端，显式预取必要模型，再以系统禁网验证 12 页逐页输出；记录 3 次冷启动、10 次热运行与失败页。没有这些证据，不冻结 D1 默认引擎。

## 12 页试跑、对齐提议与临时评分

`run_candidate.py` 默认只跑 12 页 pilot，`--selection dev` 跑全部 40 页 dev（holdout 不可选）；Paddle 另有 `--profile light`（PP-OCRv5 mobile 检测/识别 + MKL-DNN，需单独的 `--cache`）。运行器和适配器的哈希在启动时记录。分两步：`--prepare` 联网下载并把模型目录逐文件哈希成清单；推理时先校验清单（多、少、改一个文件都拒绝），再在子进程里逐页运行，Python 层拦截联网（**不是**系统级断网）。输出目录必须不存在；每页原始输出与耗时、峰值工作集写入 `pages.jsonl`，超时/崩溃也按页记账。

```powershell
$py = "evaluation/document-parser/.venv-paddle/Scripts/python.exe"
& $py evaluation/document-parser/run_candidate.py paddle --prepare --cache evaluation/document-parser/work/models-paddle --output evaluation/document-parser/work/prepare-paddle-v3
& $py evaluation/document-parser/run_candidate.py paddle --cache evaluation/document-parser/work/models-paddle --manifest evaluation/document-parser/work/corpus/manifest.json --model-lock evaluation/document-parser/work/prepare-paddle-v3/models.json --output evaluation/document-parser/work/pilot-paddle-v3 --timeout 1800
```

PP-StructureV3 的表格子流水线在 `predict()` 时才懒加载表格方向模型和它自己的 OCR（按 YAML 用 server 检测/识别并开文本行方向，不受顶层参数影响）。首轮 prepare 漏掉导致推理全页失败；现在 prepare 显式预取 `PADDLE_LAZY_MODELS`，light 配置的表格内 OCR 因此仍是 server 模型。

`align_candidate.py`（仅标准库）把原始输出转成有序块，并**提议**每个 gold 块对应哪段引擎文本，生成 JSON 与 HTML 复核表：

```powershell
python evaluation/document-parser/align_candidate.py evaluation/document-parser/work/corpus/manifest.json evaluation/document-parser/work/pilot-docling --output evaluation/document-parser/work/align-docling.json --sheet evaluation/document-parser/work/align-docling.html
python evaluation/document-parser/benchmark.py evaluation/document-parser/work/corpus/manifest.json --results evaluation/document-parser/work/align-docling.json --provisional
```

- 文本、单元格坐标、顺序全部取自引擎；gold 只用来挑选对应哪段。一个引擎块吞掉多个 gold 块（如双栏被读成一段）时，切成引擎原文子串，顺序按引擎自己的先后。
- 近似子串匹配代价（编辑数/标注字数）> 0.5 视为缺失；空白格只能配到同位置的引擎空单元格。Docling 读 `grid`（保留空单元格），Paddle 读 `parsing_res_list` 列表顺序（`block_order` 对表格和脚注为空）。
- 单元格 gold 只能配到引擎单元格，或一个整块独立的文本块，不能从正文里切出来（否则“%”这类短格会抢走正文）。4 字以下的短 gold 最后分配；被占的片段会遮住后重算下一处；代价相同时优先位置落在 gold 框的那个引擎块（同一句话出现两次时靠位置区分）。
- gold 框（倾斜页用 `observedBbox`）至少一半落在引擎框内，且引擎框不超过配给它的全部 gold 行外包框的 12 倍时，给 `locationProposed=true`：段落里的一行算定位正确，整页大框不算。`locationVerified` 和 `alignmentReviewed` 永远是 false，必须人工复核后改。
- `--provisional` 允许对未复核提议打分，出处按 `locationProposed` 计；报告带 `provisional: true` 和 PROVISIONAL 说明，**不能作为选型或验收证据**。
- 额外报告诊断指标 `allPairsOrderAccuracy`（全部块对的先后）。规划门槛仍是相邻块顺序；双栏被左右交错读时相邻对多数仍“正确”，此指标用来把问题显出来，不替代门槛。
