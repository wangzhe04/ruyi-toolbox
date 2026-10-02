"""热词切词口径：modeling_unit / bpe_vocab 的探测、英文转大写、词内空格保留、真后端把参数交给 sherpa。

修前（如意 voice-lexicon 那一波查出来的）：SherpaBackend 不传 modeling_unit，sherpa 缺省 cjkchar，
中英双语模型的英文热词整词查不到 token、被静默丢掉；_clean_hotwords 还把「PULL REQUEST」删成「PULLREQUEST」。
真模型不进测试：sherpa_onnx 用一个假模块顶上，只看构造参数与 create_stream 收到的字符串。
"""

from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest

from ruyi_asr_stream.engine import SherpaBackend, _clean_hotwords, detect_modeling_unit, hotwords_for_model


def _model_dir(tokens: list[str], *, vocab: bool) -> str:
    d = tempfile.mkdtemp(prefix="ruyi-asr-stream-hot-")
    with open(os.path.join(d, "tokens.txt"), "w", encoding="utf-8") as fh:
        for i, sym in enumerate(tokens):
            fh.write("%s %d\n" % (sym, i))
    for name in ("encoder-epoch-99-avg-1.int8.onnx", "decoder-epoch-99-avg-1.onnx", "joiner-epoch-99-avg-1.int8.onnx"):
        open(os.path.join(d, name), "wb").close()
    if vocab:
        with open(os.path.join(d, "bpe.vocab"), "w", encoding="utf-8") as fh:
            fh.write("▁DE\t-1.0\nBUG\t-2.0\n")
    return d


BILINGUAL = ["<blk>", "<sos/eos>", "<unk>", "如", "意", "▁DE", "BUG", "▁PULL", "▁RE", "QUEST"]


class _FakeRecognizer:
    def __init__(self, kwargs):
        self.kwargs = kwargs
        self.streams: list = []

    def create_stream(self, hotwords=None):
        self.streams.append(hotwords)
        return object()


def _fake_sherpa(reject_unit: bool = False):
    calls: list[dict] = []

    class OnlineRecognizer:
        @staticmethod
        def from_transducer(**kwargs):
            if reject_unit and "modeling_unit" in kwargs:
                raise TypeError("unexpected keyword argument 'modeling_unit'")
            calls.append(kwargs)
            return _FakeRecognizer(kwargs)

    return types.SimpleNamespace(OnlineRecognizer=OnlineRecognizer), calls


class DetectTest(unittest.TestCase):
    def test_bilingual_with_vocab(self):
        d = _model_dir(BILINGUAL, vocab=True)
        unit, vocab, upper = detect_modeling_unit(d, os.path.join(d, "tokens.txt"))
        self.assertEqual(unit, "cjkchar+bpe")
        self.assertEqual(vocab, os.path.join(d, "bpe.vocab"))
        self.assertTrue(upper)

    def test_bilingual_without_vocab_falls_back_to_cjkchar(self):
        d = _model_dir(BILINGUAL, vocab=False)
        self.assertEqual(detect_modeling_unit(d, os.path.join(d, "tokens.txt")), ("cjkchar", "", True))

    def test_pure_chinese(self):
        d = _model_dir(["<blk>", "如", "意", "工"], vocab=False)
        self.assertEqual(detect_modeling_unit(d, os.path.join(d, "tokens.txt")), ("cjkchar", "", False))

    def test_lowercase_english_bpe(self):
        d = _model_dir(["<blk>", "▁he", "llo"], vocab=True)
        unit, _, upper = detect_modeling_unit(d, os.path.join(d, "tokens.txt"))
        self.assertEqual((unit, upper), ("bpe", False))

    def test_missing_tokens_file_is_cjkchar(self):
        self.assertEqual(detect_modeling_unit("", "/nonexistent/tokens.txt"), ("cjkchar", "", False))


class ShapeTest(unittest.TestCase):
    def test_upper_only_ascii_letters(self):
        self.assertEqual(hotwords_for_model("debug\nRedis 集群\npull request", True), "DEBUG\nREDIS 集群\nPULL REQUEST")
        self.assertEqual(hotwords_for_model("debug", False), "debug")

    def test_clean_keeps_single_inner_space(self):
        self.assertEqual(_clean_hotwords(["pull   request", "\tgit  ", "如 意", "a\u0000b"]), ["pull request", "git", "如 意", "ab"])


class SherpaBackendTest(unittest.TestCase):
    def setUp(self):
        self._saved = sys.modules.get("sherpa_onnx")

    def tearDown(self):
        if self._saved is None:
            sys.modules.pop("sherpa_onnx", None)
        else:
            sys.modules["sherpa_onnx"] = self._saved

    def test_beam_search_passes_unit_and_vocab_and_uppercases(self):
        fake, calls = _fake_sherpa()
        sys.modules["sherpa_onnx"] = fake
        d = _model_dir(BILINGUAL, vocab=True)
        b = SherpaBackend(d)
        self.assertEqual(calls[-1]["decoding_method"], "modified_beam_search")
        self.assertEqual(calls[-1]["modeling_unit"], "cjkchar+bpe")
        self.assertEqual(calls[-1]["bpe_vocab"], os.path.join(d, "bpe.vocab"))
        b.create_stream("如意\npull request\ndebug")
        self.assertEqual(b.recognizer.streams[-1], "如意\nPULL REQUEST\nDEBUG")
        b.create_stream("")
        self.assertIsNone(b.recognizer.streams[-1])   # 没热词 = 不带参数的 create_stream

    def test_greedy_does_not_pass_unit_and_ignores_hotwords(self):
        fake, calls = _fake_sherpa()
        sys.modules["sherpa_onnx"] = fake
        b = SherpaBackend(_model_dir(BILINGUAL, vocab=True), decoding="greedy_search")
        self.assertNotIn("modeling_unit", calls[-1])
        b.create_stream("debug")
        self.assertIsNone(b.recognizer.streams[-1])

    def test_old_sherpa_without_unit_kwargs_still_starts(self):
        fake, calls = _fake_sherpa(reject_unit=True)
        sys.modules["sherpa_onnx"] = fake
        b = SherpaBackend(_model_dir(BILINGUAL, vocab=True))
        self.assertNotIn("modeling_unit", calls[-1])
        self.assertNotIn("bpe_vocab", calls[-1])
        self.assertEqual((b.modeling_unit, b.bpe_vocab), ("cjkchar", ""))


if __name__ == "__main__":
    unittest.main()
