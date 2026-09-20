"""ruyi-asr-shim：把 Qwen3-ASR 包成 OpenAI 兼容的本地转写服务。

只绑 127.0.0.1；音频不出本机、不落盘；模型懒加载＋空闲卸载。
接进如意工作台的方式见 asr-shim/README.md 与 docs/00-component-registry.md。
"""

__version__ = "0.1.0"

COMPONENT_ID = "asr-shim"
COMPONENT_NAME_TAG = "ruyi-asr-shim"  # /health 回体与登记文件 service.component 的值

__all__ = ["__version__", "COMPONENT_ID", "COMPONENT_NAME_TAG"]
