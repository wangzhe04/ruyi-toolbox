"""Explicit model preparation and CPU-only candidate adapters for D0."""
import os
from pathlib import Path
import sys

from benchmark import digest, read_json, write_json

# The table sub-pipeline builds its own OCR from YAML (server det/rec, textline orientation on),
# ignoring top-level model names, so even the light profile needs the server OCR pair.
PADDLE_LAZY_MODELS = ("PP-LCNet_x1_0_doc_ori", "PP-LCNet_x1_0_textline_ori",
                      "PP-OCRv5_server_det", "PP-OCRv5_server_rec")
# "default" is PP-StructureV3 as shipped (server OCR, MKL-DNN off for determinism);
# "light" swaps in the mobile OCR pair and MKL-DNN to see what speed/memory buys.
PADDLE_PROFILES = {
    "default": {"enable_mkldnn": False},
    "light": {"enable_mkldnn": True, "text_detection_model_name": "PP-OCRv5_mobile_det",
              "text_recognition_model_name": "PP-OCRv5_mobile_rec"},
}


def configure(root, threads, offline):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    for name, folder in (("HF_HOME", "hf"), ("PADDLE_PDX_CACHE_HOME", "paddlex"),
                         ("MODELSCOPE_CACHE", "modelscope"), ("TORCH_HOME", "torch"),
                         ("XDG_CACHE_HOME", "xdg")):
        os.environ[name] = str(root / folder)
    os.environ.update(OMP_NUM_THREADS=str(threads), MKL_NUM_THREADS=str(threads),
                      CUDA_VISIBLE_DEVICES="", PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK="True")
    for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"):
        os.environ[name] = "1" if offline else "0"
    return root


def deny_python_network(event, args):
    # A diagnostic guard, not an OS network sandbox; native library traffic is not proven blocked.
    if event in ("socket.connect", "socket.getaddrinfo", "socket.sendto"):
        raise RuntimeError("D0 inference disallows Python network access; prepare models explicitly")


def inventory(root):
    root = Path(root).resolve()
    rows = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if not path.is_file() or any(part.startswith(".") for part in relative.parts):
            continue
        if path.suffix in (".lock", ".log", ".pyc"):
            continue
        if not path.resolve().is_relative_to(root):
            raise ValueError("model file escapes cache root")
        rows.append({"path": relative.as_posix(), "bytes": path.stat().st_size, "sha256": digest(path)})
    return rows


def lock_models(root, destination, engine):
    files = inventory(root)
    if not any(Path(f["path"]).suffix in (".onnx", ".safetensors", ".pdiparams", ".pth", ".bin") for f in files):
        raise ValueError("no model weights found")
    write_json(destination, {"schemaVersion": 1, "engine": engine, "files": files,
                             "upstreamRevisionVerified": False})


def verify_models(root, lock, engine):
    document = read_json(lock)
    if document.get("schemaVersion") != 1 or document.get("engine") != engine or not document.get("files"):
        raise ValueError("invalid model inventory")
    root = Path(root).resolve()
    names = set()
    for entry in document["files"]:
        relative = Path(entry["path"])
        if relative.is_absolute() or ".." in relative.parts or entry["path"] in names:
            raise ValueError("invalid model path")
        names.add(entry["path"])
        path = (root / relative).resolve(strict=True)
        if not path.is_relative_to(root) or path.stat().st_size != entry["bytes"] or digest(path) != entry["sha256"]:
            raise ValueError("model inventory mismatch")
    # Extra files could silently select a different model/config, even with old files intact.
    current_names = {p.relative_to(root).as_posix() for p in root.rglob("*")
                     if p.is_file() and not any(part.startswith(".") for part in p.relative_to(root).parts)
                     and p.suffix not in (".lock", ".log", ".pyc")}
    if current_names != names:
        raise ValueError("unlocked model files")


def build_engine(engine, root, threads, prepare=False, profile="default"):
    if engine != "paddle" and profile != "default":
        raise ValueError("only paddle has alternative profiles")
    root = configure(root, threads, offline=not prepare)
    if not prepare:
        sys.addaudithook(deny_python_network)
    if engine == "docling":
        import torch
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions
        from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption
        torch.set_num_threads(threads)
        artifacts = root / "artifacts"
        if prepare:
            from docling.utils.model_downloader import download_models
            download_models(output_dir=artifacts, with_code_formula=False,
                            with_picture_classifier=False, with_rapidocr=True,
                            rapidocr_models=["onnxruntime:ch"])
        options = PdfPipelineOptions(artifacts_path=artifacts, enable_remote_services=False,
                    accelerator_options=AcceleratorOptions(num_threads=threads, device=AcceleratorDevice.CPU),
                    ocr_options=RapidOcrOptions(backend="onnxruntime", lang=["ch"]))
        converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})
        converter.initialize_pipeline(InputFormat.PDF)
        return converter
    if engine == "paddle":
        import paddle
        from paddleocr import PPStructureV3
        paddle.set_device("cpu")
        if prepare:
            # Lazily constructed in predict(), so absent from the eager pipeline build:
            # table orientation (even with document orientation off) and the table
            # sub-pipeline's own OCR, whose YAML keeps textline orientation on.
            from paddlex.inference.utils.official_models import official_models
            for name in PADDLE_LAZY_MODELS:
                official_models[name]
        return PPStructureV3(device="cpu", cpu_threads=threads, **PADDLE_PROFILES[profile],
                    use_doc_orientation_classify=False, use_doc_unwarping=False,
                    use_textline_orientation=False, use_formula_recognition=False,
                    use_seal_recognition=False, use_chart_recognition=False,
                    use_region_detection=False, use_table_recognition=True,
                    lang="ch", ocr_version="PP-OCRv5")
    raise ValueError("unknown candidate")


def convert(engine, backend, path):
    if engine == "docling":
        result = backend.convert(path, max_num_pages=1, max_file_size=200 * 1024 * 1024, raises_on_error=True)
        if result.status.value != "success":
            raise RuntimeError(f"conversion status: {result.status.value}")
        return result.document.export_to_dict()
    results = list(backend.predict(input=str(path)))
    if len(results) != 1:
        raise RuntimeError("expected exactly one output page")
    return results[0].json
