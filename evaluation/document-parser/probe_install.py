"""Run with each candidate's Python. Import/CPU probe, not an OCR benchmark."""
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import time

from benchmark import write_json


def probe(engine, root, threads):
    root.mkdir(parents=True, exist_ok=True)
    for name, folder in (("HF_HOME", "hf"), ("PADDLE_PDX_CACHE_HOME", "paddlex"),
                         ("MODELSCOPE_CACHE", "modelscope"), ("TORCH_HOME", "torch")):
        os.environ[name] = str(root / folder)
    os.environ.update(OMP_NUM_THREADS=str(threads), MKL_NUM_THREADS=str(threads),
                      CUDA_VISIBLE_DEVICES="", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                      PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK="True")
    start = time.perf_counter()
    report = {"schemaVersion": 1, "engine": engine, "python": platform.python_version(),
              "platform": platform.platform(), "machine": platform.machine(), "threads": threads,
              "modelInferenceTested": False, "offlineInferenceVerified": False,
              "packages": {d.metadata["Name"]: d.version for d in importlib.metadata.distributions()}}
    try:
        if engine == "docling":
            import torch
            from docling.document_converter import DocumentConverter
            from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions
            torch.set_num_threads(threads)
            value = (torch.ones(2) + 1).tolist()
            options = PdfPipelineOptions(ocr_options=RapidOcrOptions(backend="onnxruntime"))
            report.update(cpuTensor=value, ocrBackend=options.ocr_options.backend,
                          entryPoint=DocumentConverter.__name__)
        else:
            import paddle
            from paddleocr import PPStructureV3
            paddle.set_device("cpu")
            paddle.set_flags({"FLAGS_paddle_num_threads": threads})
            report.update(cpuTensor=(paddle.ones([2]) + 1).tolist(), entryPoint=PPStructureV3.__name__)
        report["status"] = "passed"
    except Exception as error:
        report.update(status="failed", errorType=type(error).__name__, message=str(error))
    report["elapsedSec"] = time.perf_counter() - start
    try:
        import psutil
        info = psutil.Process().memory_info()
        report["peakWorkingSetBytes"] = getattr(info, "peak_wset", None)
        report["rssBytes"] = info.rss
    except ImportError:
        report["rssBytes"] = None
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("engine", choices=("docling", "paddle"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--threads", type=int, default=4)
    args = parser.parse_args()
    if args.threads < 1:
        parser.error("threads must be positive")
    report = probe(args.engine, Path(__file__).resolve().parent / "work" / "cache" / args.engine, args.threads)
    write_json(args.output, report)
    print(json.dumps({k: v for k, v in report.items() if k != "packages"}, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["status"] == "passed" else 1)
