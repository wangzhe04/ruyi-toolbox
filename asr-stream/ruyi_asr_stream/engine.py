"""引擎层：流式识别会话（方案 02 §3）。

形状：一个【后端】（真的是 sherpa-onnx；测试里是假的）+ 一个【会话管理器】。
后端接口刻意小到能用假的完整替换（测试不载真模型、不联网、不碰 onnxruntime）：
    create_stream(hotwords) -> stream
    accept(stream, samples: np.float32[16 kHz]) -> None
    decode(stream) -> (text: str, endpoint: bool)     # 解到当前为止；endpoint=这一句该收口了
    reset(stream) -> None                              # 收口之后清掉状态，下一句从零开始
    finish(stream) -> str                              # 冲尾巴：最后一句的文本（可空）

会话规则（与主仓 13b 代理路由对齐）：
  · 同时最多 max_sessions 个；超了 SessionLimit；
  · idle_sec 没收到音频就自动关闭（浏览器页没关会话就没了 —— 不等它）；
  · 解码全局串行（一把锁）：CPU 小模型远快于实时，并行只会互相抢核；
  · finals 只记元数据到日志，文本本身不进日志。
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np

LOG = logging.getLogger("ruyi_asr_stream.engine")

SAMPLE_RATE = 16000


class Backend(Protocol):
    name: str

    def create_stream(self, hotwords: str): ...
    def accept(self, stream, samples: np.ndarray) -> None: ...
    def decode(self, stream) -> tuple[str, bool]: ...
    def reset(self, stream) -> None: ...
    def finish(self, stream) -> str: ...


class SessionLimit(Exception):
    pass


class UnknownSession(KeyError):
    pass


@dataclass
class Final:
    text: str
    start_ms: int
    end_ms: int

    def as_dict(self) -> dict:
        return {"text": self.text, "startMs": self.start_ms, "endMs": self.end_ms}


@dataclass
class Session:
    id: str
    stream: object
    created_at: float
    last_audio_at: float
    samples_total: int = 0        # 收到的样本总数（会话时间轴）
    segment_start: int = 0        # 当前这一句从哪个样本开始
    chunks: int = 0
    finals_count: int = 0
    closed: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


class SessionManager:
    def __init__(self, backend: Backend, *, max_sessions: int = 4, idle_sec: float = 30.0, clock=time.monotonic):
        self.backend = backend
        self.max_sessions = max(1, int(max_sessions))
        self.idle_sec = max(1.0, float(idle_sec))
        self._clock = clock
        self._sessions: dict[str, Session] = {}
        self._table_lock = threading.Lock()
        self._decode_lock = threading.Lock()

    # ── 生命周期 ────────────────────────────────────────────────────────────

    def open(self, hotwords: list[str] | None = None) -> Session:
        words = _clean_hotwords(hotwords)
        with self._table_lock:
            self.reap_idle()
            if len(self._sessions) >= self.max_sessions:
                raise SessionLimit("同时最多 %d 个流式会话" % self.max_sessions)
            stream = self.backend.create_stream("\n".join(words))
            now = self._clock()
            sess = Session(id=secrets.token_hex(16), stream=stream, created_at=now, last_audio_at=now)
            self._sessions[sess.id] = sess
        LOG.info("session open id=%s hotwords=%d active=%d", sess.id[:8], len(words), self.count())
        return sess

    def get(self, sid: str) -> Session:
        with self._table_lock:
            sess = self._sessions.get(sid)
        if sess is None or sess.closed:
            raise UnknownSession(sid)
        return sess

    def close(self, sid: str, *, reason: str = "delete") -> bool:
        with self._table_lock:
            sess = self._sessions.pop(sid, None)
        if sess is None:
            return False
        sess.closed = True
        LOG.info("session close id=%s reason=%s chunks=%d finals=%d audio_s=%.1f",
                 sess.id[:8], reason, sess.chunks, sess.finals_count, sess.samples_total / SAMPLE_RATE)
        return True

    def count(self) -> int:
        with self._table_lock:
            return len(self._sessions)

    def reap_idle(self) -> int:
        """调用方须持有 _table_lock，或从定时线程调 reap()。"""
        now = self._clock()
        dead = [s.id for s in self._sessions.values() if now - s.last_audio_at > self.idle_sec]
        for sid in dead:
            sess = self._sessions.pop(sid, None)
            if sess is not None:
                sess.closed = True
                LOG.info("session close id=%s reason=idle chunks=%d finals=%d", sid[:8], sess.chunks, sess.finals_count)
        return len(dead)

    def reap(self) -> int:
        with self._table_lock:
            return self.reap_idle()

    def shutdown(self) -> None:
        with self._table_lock:
            for sess in self._sessions.values():
                sess.closed = True
            self._sessions.clear()

    # ── 音频 ────────────────────────────────────────────────────────────────

    def feed(self, sid: str, pcm16: bytes) -> dict:
        """喂一块 16 kHz 单声道 PCM16LE，回 {partial, finals}。finals 是这一块里收口的句子（可空）。"""
        sess = self.get(sid)
        samples = pcm16_to_float32(pcm16)
        t0 = time.perf_counter()
        finals: list[Final] = []
        with sess.lock:
            if sess.closed:
                raise UnknownSession(sid)
            sess.last_audio_at = self._clock()
            sess.chunks += 1
            with self._decode_lock:
                if samples.size:
                    self.backend.accept(sess.stream, samples)
                    sess.samples_total += int(samples.size)
                text, endpoint = self.backend.decode(sess.stream)
                if endpoint:
                    if text.strip():
                        finals.append(Final(text.strip(), _ms(sess.segment_start), _ms(sess.samples_total)))
                    self.backend.reset(sess.stream)
                    sess.segment_start = sess.samples_total
                    text = ""
            sess.finals_count += len(finals)
        LOG.debug("feed id=%s bytes=%d dur_ms=%.0f partial_len=%d finals=%d",
                  sess.id[:8], len(pcm16), (time.perf_counter() - t0) * 1000, len(text), len(finals))
        return {"partial": text, "finals": [f.as_dict() for f in finals]}

    def finish(self, sid: str) -> dict:
        """冲掉尾巴：把还没收口的当一句；然后关闭会话。"""
        sess = self.get(sid)
        finals: list[Final] = []
        with sess.lock:
            if sess.closed:
                raise UnknownSession(sid)
            with self._decode_lock:
                text = self.backend.finish(sess.stream)
            if text.strip():
                finals.append(Final(text.strip(), _ms(sess.segment_start), _ms(sess.samples_total)))
            sess.finals_count += len(finals)
        self.close(sid, reason="finish")
        return {"finals": [f.as_dict() for f in finals]}


def _ms(samples: int) -> int:
    return int(round(samples * 1000 / SAMPLE_RATE))


def _clean_hotwords(words) -> list[str]:
    """会话热词清洗：≤ 200 条、每条 ≤ 40 字、去不可打印字符、去重。
    词内的空白收成一个空格而不是删光 —— sherpa 按空格切词，「PULL REQUEST」删成「PULLREQUEST」
    就成了另一串 BPE token，偏置的根本不是模型会出的那串。"""
    out: list[str] = []
    seen: set[str] = set()
    for w in (words or [])[:200]:
        if not isinstance(w, str):
            continue
        s = " ".join("".join(ch for ch in part if ch.isprintable()) for part in w.split())[:40].strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def detect_modeling_unit(model_dir: str, tokens_path: str) -> tuple[str, str, bool]:
    """热词要先按模型自己的建模单元切成 token，sherpa 才认得。返回 (modeling_unit, bpe_vocab, upper_latin)。

    sherpa-onnx 的 modeling_unit 缺省是 cjkchar：中文逐字切，英文整词当一个 token 去查 tokens.txt ——
    中英双语 zipformer 的英文是 BPE（「▁DE」「BUG」这种），整词查不到，英文热词就被静默丢掉。
    所以：模型目录里有 bpe.vocab 就用它（有中文 token → cjkchar+bpe，纯英文 → bpe）；没有就只能 cjkchar（中文照常）。
    upper_latin：tokens 里的英文字母只有大写（缺省双语模型就是）→ 热词里的英文要先转大写，否则切出来的 token 对不上。
    """
    has_cjk = False
    has_upper = False
    has_lower = False
    try:
        with open(tokens_path, encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()   # 一行「符号 编号」；符号本身不含空白
                sym = parts[0] if parts else ""
                if sym.startswith("<") and sym.endswith(">"):
                    continue           # <blk> <unk> <sos/eos> 这类特殊符号不算词表里的英文
                for ch in sym:
                    if "一" <= ch <= "鿿":
                        has_cjk = True
                    elif "A" <= ch <= "Z":
                        has_upper = True
                    elif "a" <= ch <= "z":
                        has_lower = True
    except OSError:
        pass
    vocab = os.path.join(model_dir, "bpe.vocab") if model_dir else ""
    if not (vocab and os.path.isfile(vocab)):
        vocab = ""
    if vocab:
        unit = "cjkchar+bpe" if has_cjk else "bpe"
    else:
        unit = "cjkchar"
    return unit, vocab, has_upper and not has_lower


def hotwords_for_model(hotwords: str, upper_latin: bool) -> str:
    """会话热词（换行分隔）按模型口味整形：词表只有大写英文时把 ASCII 小写字母转大写，其余原样。"""
    if not upper_latin:
        return hotwords
    return "".join(ch.upper() if "a" <= ch <= "z" else ch for ch in hotwords)


def pcm16_to_float32(pcm16: bytes) -> np.ndarray:
    if len(pcm16) % 2:
        raise ValueError("PCM16 字节数必须是偶数")
    if not pcm16:
        return np.zeros(0, dtype=np.float32)
    return (np.frombuffer(pcm16, dtype="<i2").astype(np.float32) / 32768.0)


# ── 真后端：sherpa-onnx 流式 transducer ───────────────────────────────────────

ENCODER_PATTERNS = ("encoder", )
DEFAULT_DECODING = "modified_beam_search"
DECODINGS = ("greedy_search", "modified_beam_search")


def normalize_decoding(name: str) -> str:
    s = str(name or "").strip().lower().replace("-", "_")
    if s in ("", "auto", "beam", "beam_search", "modified_beam_search"):
        return "modified_beam_search"
    if s in ("greedy", "greedy_search"):
        return "greedy_search"
    LOG.warning("看不懂的 decoding=%r，按 %s 处理（可选：%s）", name, DEFAULT_DECODING, "/".join(DECODINGS))
    return DEFAULT_DECODING


def resolve_model_files(model_dir: str) -> dict:
    """在模型目录里找 tokens / encoder / decoder / joiner。encoder 与 joiner 优先 int8（快、小、准确度几乎不变）。"""
    if not model_dir or not os.path.isdir(model_dir):
        raise FileNotFoundError("模型目录不存在：%r" % model_dir)
    names = sorted(os.listdir(model_dir))

    def pick(prefix: str, prefer_int8: bool) -> str:
        cands = [n for n in names if n.startswith(prefix) and n.endswith(".onnx")]
        if not cands:
            raise FileNotFoundError("模型目录里没有 %s*.onnx：%s" % (prefix, model_dir))
        int8 = [n for n in cands if ".int8." in n]
        fp = [n for n in cands if ".int8." not in n]
        chosen = (int8 or fp) if prefer_int8 else (fp or int8)
        return os.path.join(model_dir, chosen[0])

    tokens = os.path.join(model_dir, "tokens.txt")
    if not os.path.isfile(tokens):
        raise FileNotFoundError("模型目录里没有 tokens.txt：%s" % model_dir)
    return {
        "tokens": tokens,
        "encoder": pick("encoder", True),
        "decoder": pick("decoder", False),   # decoder 一般没有 int8 版；有也不用（量化对它不划算）
        "joiner": pick("joiner", True),
    }


class SherpaBackend:
    """sherpa-onnx OnlineRecognizer 的薄壳。导入放在构造里：单元测试用假后端时不碰 onnxruntime。"""

    name = "sherpa-onnx"

    def __init__(self, model_dir: str, *, num_threads: int = 2, rule1_sec: float = 2.0, rule2_sec: float = 0.8,
                 rule3_sec: float = 20.0, hotwords_file: str = "", hotwords_score: float = 1.5, provider: str = "cpu",
                 decoding: str = "", max_active_paths: int = 4):
        import sherpa_onnx  # noqa: PLC0415

        files = resolve_model_files(model_dir)
        self.files = files
        self.hotwords_enabled = bool(hotwords_file)
        # 131a（52 号文 §2）：缺省 modified_beam_search(4)。评测里它比 greedy 在嘈杂条件下少 13% 的错字
        # （hard 6.78→5.92），每块解码耗时不变（18.8 vs 19.8 ms）；而且只有 beam search 才能用热词，
        # 所以会话级热词从此不用再看「有没有热词文件」。想回 greedy：RUYI_ASR_STREAM_DECODING=greedy_search
        # （给了热词文件时仍强制 beam search，否则热词根本不起作用）。
        decoding = normalize_decoding(decoding)
        if hotwords_file and decoding != "modified_beam_search":
            decoding = "modified_beam_search"
        kwargs = dict(
            tokens=files["tokens"], encoder=files["encoder"], decoder=files["decoder"], joiner=files["joiner"],
            num_threads=max(1, int(num_threads)), sample_rate=SAMPLE_RATE, feature_dim=80,
            enable_endpoint_detection=True,
            rule1_min_trailing_silence=float(rule1_sec), rule2_min_trailing_silence=float(rule2_sec),
            rule3_min_utterance_length=float(rule3_sec),
            decoding_method=decoding, max_active_paths=max(1, int(max_active_paths)), provider=provider,
        )
        if hotwords_file:
            kwargs["hotwords_file"] = hotwords_file
            kwargs["hotwords_score"] = float(hotwords_score)
        # 热词的切词口径（detect_modeling_unit 头注）：修前不传 → sherpa 缺省 cjkchar → 双语模型的英文热词全被丢掉。
        unit, vocab, upper = detect_modeling_unit(model_dir, files["tokens"])
        self.modeling_unit, self.bpe_vocab, self.upper_latin = unit, vocab, upper
        if decoding == "modified_beam_search":
            kwargs["modeling_unit"] = unit
            if vocab:
                kwargs["bpe_vocab"] = vocab
        try:
            self.recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(**kwargs)
        except TypeError:
            if "modeling_unit" not in kwargs:
                raise
            # 太老的 sherpa-onnx 不认这两个参数：照旧起（中文热词照常），只是英文热词仍不生效。
            LOG.warning("这个 sherpa-onnx 不认 modeling_unit/bpe_vocab，英文热词不会生效（升级 sherpa-onnx 即可）")
            kwargs.pop("modeling_unit", None)
            kwargs.pop("bpe_vocab", None)
            self.modeling_unit, self.bpe_vocab = "cjkchar", ""
            self.recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(**kwargs)
        self.decoding = decoding
        if decoding == "modified_beam_search" and not self.bpe_vocab and unit == "cjkchar" and upper:
            LOG.warning("模型目录里没有 bpe.vocab：中文热词照常，英文热词切不出 token、不会生效（重跑 download-model.ps1 补上）")
        LOG.info("热词切词：modeling_unit=%s bpe_vocab=%s 英文转大写=%s", self.modeling_unit, "有" if self.bpe_vocab else "无", upper)

    def create_stream(self, hotwords: str):
        if hotwords and self.decoding == "modified_beam_search":
            try:
                return self.recognizer.create_stream(hotwords_for_model(hotwords, self.upper_latin))
            except TypeError:
                pass
        return self.recognizer.create_stream()

    def accept(self, stream, samples: np.ndarray) -> None:
        stream.accept_waveform(SAMPLE_RATE, samples)

    def decode(self, stream) -> tuple[str, bool]:
        while self.recognizer.is_ready(stream):
            self.recognizer.decode_stream(stream)
        endpoint = bool(self.recognizer.is_endpoint(stream))
        return _result_text(self.recognizer.get_result(stream)), endpoint

    def reset(self, stream) -> None:
        self.recognizer.reset(stream)

    def finish(self, stream) -> str:
        # 补 0.5 s 静音再宣告输入结束：让编码器把最后几帧看完整（流式模型有右上下文延迟）。
        stream.accept_waveform(SAMPLE_RATE, np.zeros(SAMPLE_RATE // 2, dtype=np.float32))
        stream.input_finished()
        while self.recognizer.is_ready(stream):
            self.recognizer.decode_stream(stream)
        return _result_text(self.recognizer.get_result(stream))


def _result_text(result) -> str:
    text = getattr(result, "text", result)
    return str(text or "").strip()


# ── 131c（52 号文 §4）：离线整句识别（SenseVoice）—— 不要显卡的「第二遍」 ─────────────────────
#
# 流式小模型管「立刻出字」，句尾改错需要一个更准的整句模型。SenseVoice-small int8（sherpa-onnx，230 MB，CPU）
# 评测里一句 4.5 s 音频 0.25 s、错字率接近 Qwen3-ASR-0.6B —— 于是同一个进程再载一个离线识别器、多开一条
# OpenAI 形的 /v1/audio/transcriptions，登记时同时 provides `asr`。没有显卡的机器也能有「句尾自动改错」。
# 与流式解码各用各的锁：一句 250 ms 的整句解码不该把正在出字的那条流卡住。

class OfflineBackend(Protocol):
    name: str
    model_name: str

    def transcribe(self, samples: np.ndarray) -> tuple[str, str]: ...   # (text, language)


def resolve_offline_model_files(model_dir: str) -> dict:
    """SenseVoice 目录：tokens.txt + model(.int8).onnx，优先 int8。"""
    if not model_dir or not os.path.isdir(model_dir):
        raise FileNotFoundError("离线模型目录不存在：%r" % model_dir)
    names = sorted(os.listdir(model_dir))
    tokens = os.path.join(model_dir, "tokens.txt")
    if not os.path.isfile(tokens):
        raise FileNotFoundError("离线模型目录里没有 tokens.txt：%s" % model_dir)
    cands = [n for n in names if n.startswith("model") and n.endswith(".onnx")]
    if not cands:
        raise FileNotFoundError("离线模型目录里没有 model*.onnx：%s" % model_dir)
    int8 = [n for n in cands if ".int8." in n]
    return {"tokens": tokens, "model": os.path.join(model_dir, (int8 or cands)[0])}


class SenseVoiceBackend:
    name = "sense-voice"

    def __init__(self, model_dir: str, *, num_threads: int = 2, model_name: str = "sensevoice-small", provider: str = "cpu"):
        import sherpa_onnx  # noqa: PLC0415

        files = resolve_offline_model_files(model_dir)
        self.files = files
        self.model_name = model_name
        # use_itn=True：数字、日期规整成阿拉伯数字（「三点」→「3点」），与 Qwen3-ASR 的输出习惯一致。
        self.recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=files["model"], tokens=files["tokens"], num_threads=max(1, int(num_threads)),
            use_itn=True, language="auto", provider=provider,
        )

    def transcribe(self, samples: np.ndarray) -> tuple[str, str]:
        s = self.recognizer.create_stream()
        s.accept_waveform(SAMPLE_RATE, np.ascontiguousarray(samples, dtype=np.float32))
        self.recognizer.decode_stream(s)
        r = s.result
        text = str(getattr(r, "text", r) or "").strip()
        lang = str(getattr(r, "lang", "") or "").strip().strip("<|>")
        return text, lang


class OfflineTranscriber:
    """离线识别器的薄壳：一把自己的锁（串行）、只记元数据。"""

    def __init__(self, backend: OfflineBackend):
        self.backend = backend
        self._lock = threading.Lock()
        self.count = 0

    @property
    def model_name(self) -> str:
        return str(getattr(self.backend, "model_name", "") or "")

    def transcribe(self, samples: np.ndarray) -> dict:
        t0 = time.perf_counter()
        with self._lock:
            text, lang = self.backend.transcribe(samples)
            self.count += 1
        LOG.info("offline ok audio_s=%.2f dur_ms=%.0f text_len=%d lang=%s",
                 samples.size / SAMPLE_RATE, (time.perf_counter() - t0) * 1000, len(text), lang or "-")
        return {"text": text, "language": lang}
