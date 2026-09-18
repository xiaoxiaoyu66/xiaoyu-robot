"""本地 TTS 模型定位的测试。

这组测试针对的是一个**真实踩过的坑**：

    sherpa-onnx 官方把所有模型都堆在 models/tts/ 下，
    声码器 vocos-22khz-univ.onnx 就跟 piper 的目录**并排**。
    如果照着“找得到声码器就当 matcha”去判断，piper 会被当成 matcha
    去加载，报一个完全看不懂的错：
    'use_eos_bos' does not exist in the metadata。
    一个上午就消耗在这一句报错上。

    => 结论：只能看模型自己的文件名。
       test_vocoder_next_door_is_not_matcha 就是这条的回归测试。

不需要真模型、不需要联网，用空文件就能跑：
    python -m unittest discover tests -v
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

try:
    from xiaoyu.tts.engine import _is_vocoder, _looks_like_matcha, resolve_model_files
except ImportError:                                   # pragma: no cover
    resolve_model_files = None
    _looks_like_matcha = None
    _is_vocoder = None

_SKIP = "未安装 loguru，跳过"


def _touch(path: Path, size: int = 1024) -> Path:
    """造一个指定大小的假文件。

    大小是真的要紧：代码靠“声学模型永远是最大的那个”来挑。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)
    return path


@unittest.skipIf(resolve_model_files is None, _SKIP)
class ResolveModelFilesTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    # ---------------------------------------------------------- piper / vits

    def test_piper_dir_is_vits_and_needs_no_vocoder(self) -> None:
        d = self.root / "vits-piper-zh_CN-huayan-medium"
        _touch(d / "tokens.txt", 100)
        _touch(d / "zh_CN-huayan-medium.onnx", 60_000_000)
        (d / "espeak-ng-data").mkdir()

        files = resolve_model_files(d)

        self.assertEqual(files.kind, "vits")
        self.assertEqual(files.model.name, "zh_CN-huayan-medium.onnx")
        self.assertIsNone(files.vocoder)
        self.assertEqual(files.data_dir, d / "espeak-ng-data")

    def test_vocoder_next_door_is_not_matcha(self) -> None:
        """回归测试：piper 旁边放着声码器，不能把它当成 matcha。

        真实目录就长这样：
            models/tts/vocos-22khz-univ.onnx          <- 声码器，与模型目录并排
            models/tts/vits-piper-zh_CN-huayan-medium/
        """
        _touch(self.root / "vocos-22khz-univ.onnx", 51_000_000)
        d = self.root / "vits-piper-zh_CN-huayan-medium"
        _touch(d / "tokens.txt", 100)
        _touch(d / "zh_CN-huayan-medium.onnx", 60_000_000)

        files = resolve_model_files(d)

        self.assertEqual(files.kind, "vits")
        self.assertIsNone(files.vocoder, "把壠码器当成 matcha 会报 'use_eos_bos' 那个看不懂的错")

    def test_espeak_data_dir_is_optional(self) -> None:
        d = self.root / "some-vits"
        _touch(d / "tokens.txt", 100)
        _touch(d / "model.onnx", 10_000_000)

        self.assertIsNone(resolve_model_files(d).data_dir)

    def test_lexicon_and_dict_are_picked_up_when_present(self) -> None:
        d = self.root / "with-lexicon"
        _touch(d / "tokens.txt", 100)
        _touch(d / "model.onnx", 10_000_000)
        _touch(d / "lexicon.txt", 100)
        (d / "dict").mkdir()

        files = resolve_model_files(d)

        self.assertEqual(files.lexicons, (d / "lexicon.txt",))
        self.assertEqual(files.dict_dir, d / "dict")

    # ---------------------------------------------------------- matcha

    def test_matcha_finds_vocoder_in_sibling_dir(self) -> None:
        vocoder = _touch(self.root / "vocos-22khz-univ.onnx", 51_000_000)
        d = self.root / "matcha-icefall-zh-baker"
        _touch(d / "tokens.txt", 100)
        _touch(d / "model-steps-3.onnx", 72_000_000)

        files = resolve_model_files(d)

        self.assertEqual(files.kind, "matcha")
        self.assertEqual(files.vocoder, vocoder)

    def test_matcha_prefers_vocoder_inside_its_own_dir(self) -> None:
        d = self.root / "matcha-x"
        _touch(d / "tokens.txt", 100)
        _touch(d / "model-steps-100.onnx", 72_000_000)
        own = _touch(d / "vocos-22khz.onnx", 51_000_000)
        _touch(self.root / "vocos-22khz-univ.onnx", 51_000_000)

        self.assertEqual(resolve_model_files(d).vocoder, own)

    def test_matcha_without_vocoder_says_what_to_do(self) -> None:
        d = self.root / "matcha-x"
        _touch(d / "tokens.txt", 100)
        _touch(d / "model-steps-3.onnx", 72_000_000)

        with self.assertRaises(FileNotFoundError) as ctx:
            resolve_model_files(d)

        message = str(ctx.exception)
        self.assertIn("声码器", message)
        self.assertIn("download_models", message)
        # 报错里不该冒出一个裸的 None，那看起来像代码 bug
        self.assertNotIn("None", message)

    def test_explicit_vocoder_argument_wins(self) -> None:
        d = self.root / "matcha-x"
        _touch(d / "tokens.txt", 100)
        _touch(d / "model-steps-3.onnx", 72_000_000)
        chosen = _touch(self.root / "my-vocoder.onnx", 1_000)

        self.assertEqual(resolve_model_files(d, chosen).vocoder, chosen)

    # ---------------------------------------------------------- 出错路径

    def test_missing_dir(self) -> None:
        with self.assertRaises(FileNotFoundError):
            resolve_model_files(self.root / "不存在")

    def test_missing_tokens(self) -> None:
        d = self.root / "no-tokens"
        _touch(d / "model.onnx", 10_000_000)

        with self.assertRaises(FileNotFoundError) as ctx:
            resolve_model_files(d)

        self.assertIn("tokens.txt", str(ctx.exception))

    def test_missing_onnx_points_at_download_script(self) -> None:
        d = self.root / "no-onnx"
        _touch(d / "tokens.txt", 100)

        with self.assertRaises(FileNotFoundError) as ctx:
            resolve_model_files(d)

        self.assertIn("download_models", str(ctx.exception))

    # ---------------------------------------------------------- rule_fsts

    def test_all_fst_files_are_joined(self) -> None:
        """中文模型靠 .fst 把数字念对（2026 -> 二零二六），一个都不能漏。"""
        d = self.root / "with-fst"
        _touch(d / "tokens.txt", 100)
        _touch(d / "model.onnx", 10_000_000)
        for name in ("number.fst", "date.fst", "phone.fst"):
            _touch(d / name, 100)

        rule_fsts = resolve_model_files(d).rule_fsts

        self.assertEqual(len(rule_fsts.split(",")), 3)
        self.assertIn("date.fst", rule_fsts)

    def test_no_fst_is_empty_string(self) -> None:
        d = self.root / "no-fst"
        _touch(d / "tokens.txt", 100)
        _touch(d / "model.onnx", 10_000_000)

        self.assertEqual(resolve_model_files(d).rule_fsts, "")


@unittest.skipIf(_looks_like_matcha is None, _SKIP)
class LooksLikeMatchaTest(unittest.TestCase):
    def test_matcha_names(self) -> None:
        for name in ("model-steps-3.onnx", "model-steps-100.onnx", "MODEL-STEPS-1.ONNX"):
            self.assertTrue(_looks_like_matcha(Path(name)), name)

    def test_non_matcha_names(self) -> None:
        for name in ("zh_CN-huayan-medium.onnx", "model.onnx", "vocos-22khz-univ.onnx"):
            self.assertFalse(_looks_like_matcha(Path(name)), name)

    def test_vocoder_detection(self) -> None:
        self.assertTrue(_is_vocoder(Path("vocos-22khz-univ.onnx")))
        self.assertTrue(_is_vocoder(Path("hifigan_v2.onnx")))
        self.assertFalse(_is_vocoder(Path("model-steps-3.onnx")))
        self.assertFalse(_is_vocoder(Path("zh_CN-huayan-medium.onnx")))


@unittest.skipIf(resolve_model_files is None, _SKIP)
class KokoroTest(unittest.TestCase):
    """Kokoro 的识别。

    判据是目录里有没有 voices.bin —— 只有它需要一张额外的音色表。
    这一步必须排在 matcha 判断**之前**，因为 Kokoro 的主模型也叫 model.onnx，
    跟普通 vits 长得一模一样，光看文件名分不出来。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="xiaoyu_kokoro_")
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _make_kokoro(self, name: str = "kokoro-int8-multi-lang-v1_1") -> Path:
        d = self.root / name
        _touch(d / "tokens.txt", 100)
        # 尺寸无所谓 —— 这里只验证"认不认得出来"，不用真造 100MB 的假文件
        _touch(d / "model.int8.onnx", 10_000_000)
        _touch(d / "voices.bin", 5_000_000)
        (d / "espeak-ng-data").mkdir(exist_ok=True)
        (d / "dict").mkdir(exist_ok=True)
        _touch(d / "lexicon-zh.txt", 100)
        _touch(d / "lexicon-us-en.txt", 100)
        return d

    def test_voices_bin_means_kokoro(self) -> None:
        self.assertEqual(resolve_model_files(self._make_kokoro()).kind, "kokoro")

    def test_voices_bin_is_exposed(self) -> None:
        d = self._make_kokoro()
        self.assertEqual(resolve_model_files(d).voices, d / "voices.bin")

    def test_kokoro_is_not_mistaken_for_vits(self) -> None:
        self.assertNotEqual(resolve_model_files(self._make_kokoro()).kind, "vits")

    def test_kokoro_does_not_need_an_external_vocoder(self) -> None:
        self.assertIsNone(resolve_model_files(self._make_kokoro()).vocoder)

    def test_all_lexicons_are_collected(self) -> None:
        files = resolve_model_files(self._make_kokoro())
        joined = ",".join(str(p) for p in files.lexicons)
        self.assertIn("lexicon-zh.txt", joined)
        self.assertIn("lexicon-us-en.txt", joined)

    def test_a_plain_vits_dir_is_not_kokoro(self) -> None:
        """反向也要测：普通 vits 目录不能被误判成 Kokoro。"""
        d = self.root / "vits-piper-zh_CN-huayan-medium"
        _touch(d / "tokens.txt", 100)
        _touch(d / "zh_CN-huayan-medium.onnx", 60_000_000)

        files = resolve_model_files(d)

        self.assertEqual(files.kind, "vits")
        self.assertIsNone(files.voices)


try:
    from xiaoyu.llm.client import DeepSeekClient, _WARMUP_REUSE_SECONDS
except ImportError:                                   # pragma: no cover
    DeepSeekClient = None
    _WARMUP_REUSE_SECONDS = 0.0


@unittest.skipIf(DeepSeekClient is None, _SKIP)
class WarmupTest(unittest.TestCase):
    """连接预热的控制逻辑。

    为什么要测这个：预热本身是一次**真的 API 调用**，
    重复发就是白花钱。而它又是在后台线程里发的，
    没测过的话很容易“每唤醒一次就多打一次”而没人发现。

    用 object.__new__ 绕过 __init__：真构造要读密钥、连网，
    而这里只想验证“什么时候该发、什么时候不该发”。
    """

    def _client(self):
        client = object.__new__(DeepSeekClient)
        client._warmup_lock = threading.Lock()
        client._warmup_running = False
        client._warmed_at = 0.0
        client.calls = 0

        def fake_warmup() -> float:
            client.calls += 1
            time.sleep(0.02)          # 模拟真实网络请求
            client._warmed_at = time.perf_counter()
            return 0.02

        client.warmup = fake_warmup
        return client

    def _wait_idle(self, client, timeout: float = 2.0) -> None:
        """等后台预热线程真正收工。

        只等 client.calls 变成 1 是不够的：那时候线程还没跑到 finally，
        _warmup_running 还是 True。这里等的是“整个函数都退出了”。
        """
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            if not client._warmup_running:
                return
            time.sleep(0.005)
        self.fail("预热线程超时没收工")

    def test_skips_when_recently_warmed(self) -> None:
        client = self._client()
        client._warmed_at = time.perf_counter()

        self.assertTrue(client._warmup_fresh())
        self.assertFalse(client.warmup_async(), "刚预热过就不该再打一次 API")
        self.assertEqual(client.calls, 0)

    def test_fires_when_stale(self) -> None:
        client = self._client()
        client._warmed_at = time.perf_counter() - _WARMUP_REUSE_SECONDS - 1

        self.assertTrue(client.warmup_async())
        self._wait_idle(client)
        self.assertEqual(client.calls, 1)

    def test_concurrent_calls_only_fire_once(self) -> None:
        """唤醒后又碰上快速连说两句，不能因此打两次预热。"""
        client = self._client()
        results = [client.warmup_async() for _ in range(5)]

        self.assertEqual(sum(results), 1, "只能有一次真正发出去")
        self._wait_idle(client)
        self.assertEqual(client.calls, 1)
        self.assertFalse(client._warmup_running, "跑完了要把标志放掉，否则以后永远不预热了")

    def test_warmup_failure_does_not_raise(self) -> None:
        """预热只是优化，网络抖了不能抦着启动。"""
        client = object.__new__(DeepSeekClient)
        client._warmup_lock = threading.Lock()
        client._warmup_running = False
        client._warmed_at = 0.0
        client._client = _ExplodingClient()
        client._cfg = _FakeLlmConfig()

        client.warmup()               # 不应抛异常
        self.assertEqual(client._warmed_at, 0.0, "失败了不能假装已经预热好了")


class _FakeLlmConfig:
    model = "fake"
    temperature = 0.0


class _ExplodingClient:
    class chat:                                   # noqa: N801 - 模拟 SDK 的属性链
        class completions:
            @staticmethod
            def create(**_kwargs):
                raise ConnectionError("模拟的断网")


if __name__ == "__main__":
    unittest.main()
