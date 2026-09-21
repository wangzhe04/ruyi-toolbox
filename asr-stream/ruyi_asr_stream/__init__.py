"""ruyi-asr-stream：把 sherpa-onnx 的流式 Zipformer 包成「有会话的 HTTP」，给如意工作台的麦克风边说边出字。

与 asr-shim 并存、互不依赖：asr-shim 是整段识别（第二遍改错、附件、工具），本组件是第一遍（立刻出字）。
"""

__version__ = "0.1.0"

COMPONENT_ID = "asr-stream"              # 登记文件名与 id（登记约定 §2）
COMPONENT_NAME_TAG = "ruyi-asr-stream"   # /health 里的 component 字段（如意靠它认出「端口上活着的就是我」）
DEFAULT_MODEL_NAME = "zipformer-bilingual-zh-en"
