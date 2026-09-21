# 04 · 调研、对比与选型决策

> 调研日期：2026-09-21。方法：官方文档、官方仓库、模型卡与本地源码交叉核对。没有运行候选基准。本文的“首选”是工程路线选择，只有通过专题验收后才能成为发行默认。

## 1. 比较原则

先过硬门槛：Windows 可部署、用户数据本地处理、离线模型可预置、许可可按实际分发方式履行、结果有出处、组件可停止和卸载。未过硬门槛，不用综合分掩盖。

通过者再按本地实测评分：任务质量 40%、CPU/Windows 体验 20%、离线部署 20%、接口维护成本 10%、资源占用 10%。各项 0–5 分，原始证据随评分保存；本次不凭文档填写虚构分数。

代码许可证、模型许可证、运行时和辅助二进制许可证分别记录。以下许可信息只用于工程筛选，发行时复核锁定版本实际附带的文件。

## 2. 文档解析

| 候选 | 官方资料确认的能力 | 需要验证的成本 | 本次决定 |
| --- | --- | --- | --- |
| Docling | 多格式解析、版面/阅读顺序/表格、OCR、结构化结果、本地执行；代码 MIT | 中文扫描件效果、Windows 依赖体积、每种 OCR 后端的模型预取 | D1 首选；固定一个 OCR 后端，先做 CPU 档 |
| PaddleOCR PP-StructureV3 | 文档结构解析；项目代码 Apache-2.0 | 指定 Paddle 版本与 CPU/GPU wheel、依赖冲突、输出映射 | 必测备选；中文质量明显更好且安装过关时替换 |
| MinerU | 当前主线已含解析、资料库、页/块定位和服务工具 | 与如意任务管理的重叠、具体后端体积、当前附加许可条款 | 独立增强候选，先核验后决定是否进入完整评测 |
| 现有 pypdf + Windows OCR | 主仓已具备部分基础能力 | 没有统一版面/表格结构链 | 作为现有基线，不当作等价完整替代 |

选择 Docling 的依据是结构化输出与本地使用方式适合统一材料契约，而不是声称其中文精度最好。官方已有 MCP 能力：预试验先检查能否直接配置满足需要；若其任务、路径和返回大小契约不足，再写薄适配层，避免重写解析器。[S1–S4]

MinerU 的调查改变了最初印象：其当前主线已不只是 PDF 转 Markdown。当前许可证是 Apache-2.0 加附加条款，不能沿用旧文章的许可判断，也不能标成纯 Apache-2.0。它可能减少重复开发，但需评估引入整套资料库的维护代价。[S5–S6]

## 3. 本地资料检索

| 层次 | 候选对比 | 决定 |
| --- | --- | --- |
| 持久化 | SQLite 单文件/事务；Qdrant client 有无需独立服务的 local mode，也有服务端路线 | 首版 SQLite 存元数据与全文索引，向量以固定格式保存；不先部署独立数据库 |
| 中文 embedding | BGE-small-zh-v1.5；Qwen3-Embedding-0.6B 的多语言、指令与可变维度能力 | BGE 为轻量首选；Qwen 为质量增强比较项，不引用上游榜单推算本机收益 |
| 向量执行 | 标准模型运行时；ONNX 导出/量化 | 先用官方支持路径建立正确性基线；ONNX 仅在导出和检索回归通过后发行 |
| 检索组合 | 纯关键词、纯向量、两者 RRF、再加 reranker | 关键词 + 向量 + RRF 为目标；reranker 经增益/延迟评测后选装 |

SQLite FTS5 提供全文索引和 BM25，但默认 tokenizer 不能直接视作优质中文分词。计划采用显式中文二元切分、ASCII 词及短词补充索引，查询侧用同规则；原文另存，不用分词串显示引用。启动体检验证实际 Python SQLite 构建含 FTS5。[S7]

首版上限拟设 5 万块，先用 NumPy 精确向量扫描做基线；若热查询 p95 达不到专题目标，再引入 ANN 或 Qdrant local。更换存储必须解决事务一致性和迁移，不按文件数量武断决定。[S8–S11]

## 4. 长音频

| 路线 | 收益 | 代价 | 决定 |
| --- | --- | --- | --- |
| 现有 ASR + 分段作业 | 最大程度复用，CPU/GPU 已有选择 | 当前整段接口无有效 segments；精确对齐需新增 | A1 默认，先提供诚实的片段时间范围 |
| faster-whisper | 本地识别、分段及词时间信息等已有能力 | 多一套模型/运行时，需要比较中文效果 | 完整时间轴的基准备选 |
| WhisperX | 强制对齐、说话人流程 | 额外对齐模型和依赖；说话人模型的获取条件 | A2 比较项，不整套替换已装 ASR |
| Qwen3 ForcedAligner | 同系列对齐路线可调研复用 | 当前 shim 并未暴露，需验证语言、显存和版本 | A2 与 WhisperX 对齐对比 |

pyannote Community-1 提供本地使用方式，但获取模型涉及模型页条件；不能把“代码可安装”写成“免账号开箱即用”。说话人功能做选装，不能因此阻塞基础转写。只输出 A/B 标签，不识别真实姓名。[S12–S15]

## 5. 数据分析

| 候选 | 适配点 | 决定 |
| --- | --- | --- |
| DuckDB | 结构化多文件查询与聚合，SQL 便于复算 | T1 计算内核首选；首版只暴露结构化操作，不给任意 SQL |
| Polars | DataFrame 表达式与查询引擎 | 复杂转换的比较备选，不与 DuckDB 同时成为硬依赖 |
| 现有 Excel 工具 / 临时脚本 | 展示、格式和小任务足够 | 继续复用；新增组件解决重复批量计算 |

DuckDB Excel 扩展支持 XLSX，但首次使用可能自动获取扩展，且不支持旧 XLS。T1 首版用明确打包的 Excel 读取依赖导入数据，避免隐式下载；若后续启用该扩展，必须预置匹配版本和平台的二进制。SQL 即使以 SELECT 开头也可能读文件或访问外部资源，不能作为权限判断。[S16–S19]

## 6. TTS

| 候选 | 本次决定 |
| --- | --- |
| sherpa-onnx TTS | 首选运行时路线，已有该生态部署经验；具体中文声音需另比对模型效果与许可 |
| Piper 当前维护仓 | 可比较的本地方案；当前仓名/许可为 GPL 路线，发行须明确其边界，不能按旧仓资料处理 |
| edge-tts | 使用在线服务，不进入离线默认方案 |

TTS 引擎仅解决合成，不解决如意里的播放、停止、打断和切换释放。V1 先冻结主仓接口再实施组件。[S20–S22]

## 7. 发行前必须补齐的证据

为每个入选组合建立一条记录：上游 tag/commit、包版本与 wheel 哈希、模型 revision 与逐文件哈希、配置、硬件/驱动、许可证路径、首次下载清单、离线测试记录、冷/热资源、失败样本、最终取舍。

本次链接指向调研时的官方页面，部分是可变主线；它们不是可复现构建锁文件。后续试验必须固定 revision，不能直接按 latest 发布。未能验证的项保留“待实测”，不能改写成“支持”。

## 8. 官方资料索引

- S1 [Docling 能力](https://docling-project.github.io/docling/)、[代码许可](https://github.com/docling-project/docling/blob/main/LICENSE)
- S2 [Docling 模型预取与离线设置](https://github.com/docling-project/docling/blob/main/docs/usage/advanced_options.md)
- S3 [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR)、[许可](https://github.com/PaddlePaddle/PaddleOCR/blob/main/LICENSE)
- S4 [PP-Structure 版本迁移说明](https://github.com/PaddlePaddle/PaddleOCR/blob/main/ppstructure/README.md)
- S5 [MinerU 当前能力与部署](https://github.com/opendatalab/MinerU)
- S6 [MinerU 当前许可原文](https://github.com/opendatalab/MinerU/blob/master/LICENSE.md)
- S7 [SQLite FTS5](https://www.sqlite.org/fts5.html)
- S8 [Qdrant Python client 与 local mode](https://github.com/qdrant/qdrant-client)
- S9 [BGE-small-zh-v1.5 模型卡](https://huggingface.co/BAAI/bge-small-zh-v1.5)
- S10 [Qwen3-Embedding-0.6B 模型卡](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B)
- S11 [ONNX Runtime](https://github.com/microsoft/onnxruntime)
- S12 [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
- S13 [WhisperX](https://github.com/m-bain/whisperX)
- S14 [Qwen3-ASR 与 ForcedAligner](https://github.com/QwenLM/Qwen3-ASR)
- S15 [pyannote Community-1 模型卡](https://huggingface.co/pyannote/speaker-diarization-community-1)
- S16 [DuckDB Excel 扩展](https://duckdb.org/docs/lts/core_extensions/excel)
- S17 [DuckDB 扩展安装及缓存](https://www.duckdb.org/docs/current/extensions/installing_extensions)
- S18 [DuckDB 安全边界](https://duckdb.org/docs/current/operations_manual/securing_duckdb/overview)
- S19 [Polars](https://github.com/pola-rs/polars)
- S20 [sherpa-onnx TTS](https://k2-fsa.github.io/sherpa/onnx/tts/index.html)
- S21 [Piper 当前维护仓](https://github.com/OHF-Voice/piper1-gpl)
- S22 [edge-tts 的在线服务说明](https://github.com/rany2/edge-tts)
- S23 [Python Windows 嵌入式发行说明](https://docs.python.org/3.12/using/windows.html#the-embeddable-package)
