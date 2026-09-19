"""小智设备协议层（`xiaoyu/xiaozhi/protocol.py`）的测试。

跑法：py -3.11 -m unittest discover tests

全程**离线**：不连板子、不联网、不装任何依赖。
输入全部来自 `tests/fixtures/xiaozhi/` —— 那是从上游 `docs/websocket_zh.md`
逐字抄下来的真样本（溯源见同目录 `README.md`），不是自己编的 JSON。

这个文件里最要紧的一条：**解析不许抛异常**。
所以坏输入是一张大表，每个都必须"拿到返回值 + 拿到错误码"，而不是"抛了算过"。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from xiaoyu.xiaozhi import protocol
from xiaoyu.xiaozhi.protocol import (
    ERR_BAD_FIELD,
    ERR_BAD_INPUT,
    ERR_BAD_TRANSPORT,
    ERR_MISSING_FIELD,
    ERR_MISSING_TYPE,
    ERR_NOT_JSON,
    ERR_NOT_OBJECT,
    ERR_UNEXPECTED_TYPE,
    ERR_UNSUPPORTED_VERSION,
    AudioParams,
    FrameKind,
    build_server_hello,
    classify_frame,
    decode_message,
    encode_message,
    parse_audio_params,
    parse_device_hello,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "xiaozhi"

DEVICE_HELLO_FULL = "device/device_hello_full.json"
DEVICE_HELLO = "device/device_hello.json"
SERVER_HELLO_24000 = "server/server_hello_24000.json"
SERVER_HELLO_16000 = "server/server_hello_16000.json"

# 设备端发的、但不是 hello 的样本 —— 拿它们验"type 不对要挡掉"
DEVICE_NON_HELLO = (
    "device/device_listen_start_manual.json",
    "device/device_listen_start_auto.json",
    "device/device_listen_detect.json",
    "device/device_abort.json",
    "device/device_mcp_result.json",
)

# 已知错误码全集：用它断言"返回的错误码是我们认识的那个"，
# 而不是某个随手拼出来的字符串
ALL_CODES = {
    ERR_BAD_INPUT,
    ERR_NOT_JSON,
    ERR_NOT_OBJECT,
    ERR_MISSING_TYPE,
    ERR_UNEXPECTED_TYPE,
    ERR_MISSING_FIELD,
    ERR_BAD_FIELD,
    ERR_BAD_TRANSPORT,
    ERR_UNSUPPORTED_VERSION,
}


def _text(rel: str) -> str:
    return (FIXTURES / rel).read_text(encoding="utf-8")


def _json(rel: str) -> dict:
    return json.loads(_text(rel))


def _all_fixtures() -> list[tuple[str, dict]]:
    """所有 fixture：(相对路径, 解析出来的对象)。"""
    items = []
    for path in sorted(FIXTURES.rglob("*.json")):
        rel = str(path.relative_to(FIXTURES)).replace("\\", "/")
        items.append((rel, json.loads(path.read_text(encoding="utf-8"))))
    return items


# 恶意 / 畸形输入大表：解析函数对它们**一个都不许抛**
HOSTILE_INPUTS = (
    None, 0, 1, 3.14, True, False, [], {}, (), set(), object(),
    b"", b"\xff\xfe", bytearray(b"\xff"), memoryview(b"\xff"),
    "", "   ", "\n", "{", "}", "[", "]", "null", "true", "false",
    "123", "3.14", '"只是一段字符串"', "[1,2,3]", "{}", "[]",
    '{"type": null}', '{"type": 123}', '{"type": ""}', '{"type": {}}',
    '{"state": "start"}',
    # 走到 hello 各道关卡的半成品：缺字段 / 类型不对 / 取值越界
    '{"type": "hello"}',
    '{"type": "hello", "transport": null}',
    '{"type": "hello", "transport": "websocket"}',
    '{"type": "hello", "transport": "websocket", "version": null}',
    '{"type": "hello", "transport": "websocket", "version": 1}',
    '{"type": "hello", "transport": "websocket", "version": 1, "audio_params": null}',
    '{"type": "hello", "transport": "websocket", "version": 1, "audio_params": {}}',
    '{"type": "hello", "transport": "websocket", "version": 1,'
    ' "audio_params": {"format": "opus", "sample_rate": "16000"}}',
    '{"type": "hello", "transport": "websocket", "version": 1,'
    ' "audio_params": {"format": "opus", "sample_rate": -1}}',
    '{"type": "hello", "transport": "websocket", "version": 1,'
    ' "audio_params": {"format": "opus", "sample_rate": 16000}, "features": 3}',
    '{"type": "hello", "transport": "websocket", "version": 1,'
    ' "audio_params": {"format": "opus", "sample_rate": 16000}, "text_font": "x"}',
    # 合法但方向不对的：设备端 listen / 我们自己的服务端 hello
    _text("device/device_listen_start_manual.json"),
    _text(SERVER_HELLO_24000),
)


class FixtureInventoryTest(unittest.TestCase):
    """fixture 本身先自检 —— 样本没了或坏了，下面所有断言都失去意义。"""

    def test_fixtures_exist_in_both_directions(self):
        items = _all_fixtures()
        self.assertGreaterEqual(len(items), 20, "fixture 少了，检查 tests/fixtures/xiaozhi/")
        directions = {rel.split("/")[0] for rel, _ in items}
        self.assertEqual(directions, {"device", "server"})

    def test_every_fixture_has_a_type(self):
        """文档 §8.6：没有 type 的消息没有任何意义。fixture 里不该有这种。"""
        for rel, obj in _all_fixtures():
            with self.subTest(fixture=rel):
                self.assertIsInstance(obj.get("type"), str)
                self.assertTrue(obj["type"])


class ClassifyFrameTest(unittest.TestCase):
    """§1.5：文本帧 = JSON，二进制帧 = Opus。判定看帧类型，不猜内容。"""

    def test_text_frame_is_str(self):
        self.assertIs(classify_frame('{"type": "hello"}'), FrameKind.TEXT)

    def test_binary_frame_is_bytes(self):
        self.assertIs(classify_frame(b"\x00\x01\x02"), FrameKind.BINARY)

    def test_bytearray_and_memoryview_count_as_binary(self):
        self.assertIs(classify_frame(bytearray(b"abc")), FrameKind.BINARY)
        self.assertIs(classify_frame(memoryview(b"abc")), FrameKind.BINARY)

    def test_empty_bytes_is_still_a_binary_frame(self):
        """空帧也是二进制帧，只是里面没东西。内容合不合法归 audio_codec 管。"""
        self.assertIs(classify_frame(b""), FrameKind.BINARY)

    def test_non_frame_values_are_unknown(self):
        for value in (None, 123, 3.14, [], {}, object()):
            with self.subTest(value=repr(value)):
                self.assertIs(classify_frame(value), FrameKind.UNKNOWN)

    def test_json_text_that_looks_like_audio_is_not_guessed(self):
        """关键：不能靠"试着 json.loads 一下"来判类型。

        bytes 里就算**恰好**是合法 JSON，也还是二进制帧（那是音频，不是消息）。
        反过来，str 里就算不像 JSON，也是文本帧。判定只认帧类型本身。
        """
        self.assertIs(classify_frame(b'{"type": "hello"}'), FrameKind.BINARY)
        self.assertIs(classify_frame("这不是 JSON"), FrameKind.TEXT)

    def test_real_fixture_text_is_a_text_frame(self):
        self.assertIs(classify_frame(_text(DEVICE_HELLO_FULL)), FrameKind.TEXT)


class DecodeMessageTest(unittest.TestCase):
    def test_all_doc_samples_decode(self):
        """22 个真样本一个不落地能解出来，且 type 和文件里写的一致。"""
        for rel, obj in _all_fixtures():
            with self.subTest(fixture=rel):
                result = decode_message(_text(rel))
                self.assertTrue(result.ok, msg=getattr(result.error, "detail", ""))
                self.assertEqual(result.value["type"], obj["type"])

    def test_compact_and_no_session_variants_decode(self):
        """刻意留的两种写法：紧凑单行、没有 session_id（文档 §6 长这样）。"""
        compact = (
            "server/server_stt_compact.json",
            "server/server_tts_start_compact.json",
            "server/server_tts_stop_compact.json",
        )
        no_session = (
            "server/server_tts_start_no_session.json",
            "server/server_tts_stop_no_session.json",
        )
        for rel in compact + no_session:
            with self.subTest(fixture=rel):
                self.assertTrue(decode_message(_text(rel)).ok)
        for rel in compact:
            with self.subTest(fixture=rel):
                # 紧凑 != 没有 session_id，这两件事是分开的，别混一块断言
                self.assertIn("session_id", _json(rel))
        for rel in no_session:
            with self.subTest(fixture=rel):
                self.assertNotIn("session_id", _json(rel))

    def test_bytes_input_is_accepted(self):
        """网络那头给的可能是 bytes —— 同样的内容要能解。"""
        raw = _text(DEVICE_HELLO)
        result = decode_message(raw.encode("utf-8"))
        self.assertTrue(result.ok)
        self.assertEqual(result.value["type"], "hello")

    def test_invalid_utf8_bytes_give_not_json(self):
        result = decode_message(b"\xff\xfe\x00")
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ERR_NOT_JSON)

    def test_not_json(self):
        for raw in ("", "   ", "{", '{"type": }', "hello"):
            with self.subTest(raw=raw):
                result = decode_message(raw)
                self.assertFalse(result.ok)
                self.assertEqual(result.error.code, ERR_NOT_JSON)

    def test_json_that_is_not_an_object(self):
        for raw in ("[1,2,3]", "42", "3.14", '"字符串"', "null", "true"):
            with self.subTest(raw=raw):
                result = decode_message(raw)
                self.assertFalse(result.ok)
                self.assertEqual(result.error.code, ERR_NOT_OBJECT)

    def test_missing_type_is_rejected(self):
        """文档 §8.6：缺 type，设备端只记错误日志、不执行业务。我们同样挡掉。"""
        for raw in ("{}", '{"state": "start"}', '{"type": null}', '{"type": ""}',
                    '{"type": 123}', '{"type": []}'):
            with self.subTest(raw=raw):
                result = decode_message(raw)
                self.assertFalse(result.ok)
                self.assertEqual(result.error.code, ERR_MISSING_TYPE)

    def test_unknown_type_is_not_this_layer_s_business(self):
        """这一层只判"格式合不合法"，不判"认不认识这个 type"。

        认不认识是各调用方的事（parse_device_hello 只认 hello；session.py 认
        listen/abort/mcp）。放行未知 type，以后上游加新消息类型不会把我们卡死。
        """
        result = decode_message('{"type": "将来才有的消息", "x": 1}')
        self.assertTrue(result.ok)
        self.assertEqual(result.value["type"], "将来才有的消息")

    def test_non_text_non_bytes_input(self):
        for raw in (None, 123, [], {}, object()):
            with self.subTest(raw=repr(raw)):
                result = decode_message(raw)
                self.assertFalse(result.ok)
                self.assertEqual(result.error.code, ERR_BAD_INPUT)


class EncodeMessageTest(unittest.TestCase):
    def test_round_trip_every_fixture(self):
        """编码再解码要还原成同一个对象 —— 22 个样本全过一遍。"""
        for rel, obj in _all_fixtures():
            with self.subTest(fixture=rel):
                decoded = decode_message(encode_message(obj))
                self.assertTrue(decoded.ok)
                self.assertEqual(decoded.value, obj)

    def test_chinese_is_not_escaped(self):
        """和文档样本一致：中文原样，不转 \\uXXXX（日志和抓包都看得懂）。"""
        text = encode_message({"type": "custom", "payload": {"message": "自定义内容"}})
        self.assertIn("自定义内容", text)
        self.assertNotIn("\\u", text)

    def test_output_is_str(self):
        self.assertIsInstance(encode_message({"type": "listen", "state": "start"}), str)

    def test_non_mapping_raises(self):
        """构造是我们自己的代码在调，参数错了当场炸（跟解析相反）。"""
        for value in ([], "字符串", 1, None):
            with self.subTest(value=repr(value)):
                with self.assertRaises(TypeError):
                    encode_message(value)

    def test_missing_type_raises(self):
        for message in ({}, {"state": "start"}, {"type": ""}, {"type": None}, {"type": 1}):
            with self.subTest(message=message):
                with self.assertRaises(ValueError):
                    encode_message(message)


class AudioParamsTest(unittest.TestCase):
    def test_to_dict_omits_absent_frame_duration(self):
        """§9.2 的服务端 hello 就俩键 —— 没值就别写这个键（不是写 null）。"""
        params = AudioParams(format="opus", sample_rate=16000)
        self.assertEqual(
            params.to_dict(), {"format": "opus", "sample_rate": 16000, "channels": 1}
        )

    def test_to_dict_keeps_frame_duration_when_present(self):
        params = AudioParams(format="opus", sample_rate=24000, channels=1, frame_duration=60)
        self.assertEqual(
            params.to_dict(),
            {"format": "opus", "sample_rate": 24000, "channels": 1, "frame_duration": 60},
        )

    def test_parse_device_direction_sample(self):
        raw = _json(DEVICE_HELLO_FULL)["audio_params"]
        result = parse_audio_params(raw)
        self.assertTrue(result.ok)
        self.assertEqual(result.value.format, "opus")
        self.assertEqual(result.value.sample_rate, 16000)
        self.assertEqual(result.value.channels, 1)
        self.assertEqual(result.value.frame_duration, 60)

    def test_parse_sample_without_optional_keys(self):
        """§9.2 那个样本只有 format + sample_rate，不能因为少了俩键就报错。"""
        raw = _json(SERVER_HELLO_16000)["audio_params"]
        self.assertEqual(set(raw), {"format", "sample_rate"})
        result = parse_audio_params(raw)
        self.assertTrue(result.ok)
        self.assertEqual(result.value.channels, 1)
        self.assertIsNone(result.value.frame_duration)

    def test_missing_required_keys(self):
        cases = (
            ({}, "format"),
            ({"format": "opus"}, "sample_rate"),
            ({"sample_rate": 16000}, "format"),
        )
        for raw, missing in cases:
            with self.subTest(raw=raw):
                result = parse_audio_params(raw)
                self.assertFalse(result.ok)
                self.assertEqual(result.error.code, ERR_MISSING_FIELD)
                self.assertIn(missing, result.error.detail)

    def test_bad_types(self):
        cases = (
            "不是对象", [], None,
            {"format": "", "sample_rate": 16000},
            {"format": 1, "sample_rate": 16000},
            {"format": "opus", "sample_rate": "16000"},
            {"format": "opus", "sample_rate": 0},
            {"format": "opus", "sample_rate": -16000},
            {"format": "opus", "sample_rate": 16000, "channels": 0},
            {"format": "opus", "sample_rate": 16000, "frame_duration": "60"},
        )
        for raw in cases:
            with self.subTest(raw=repr(raw)):
                result = parse_audio_params(raw)
                self.assertFalse(result.ok)
                self.assertIn(result.error.code, {ERR_BAD_FIELD, ERR_MISSING_FIELD})

    def test_bool_is_not_an_integer(self):
        """bool 是 int 的子类。不特意挡掉的话，True 会被当成 1 混进来。"""
        result = parse_audio_params({"format": "opus", "sample_rate": True})
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ERR_BAD_FIELD)

    def test_sample_rate_is_not_whitelisted(self):
        """设备说多少就是多少，不写死校验 —— 不然换个固件就白白连不上。"""
        result = parse_audio_params({"format": "opus", "sample_rate": 48000})
        self.assertTrue(result.ok)
        self.assertEqual(result.value.sample_rate, 48000)


class ParseDeviceHelloTest(unittest.TestCase):
    def test_full_sample_fields(self):
        result = parse_device_hello(_text(DEVICE_HELLO_FULL))
        self.assertTrue(result.ok, msg=getattr(result.error, "detail", ""))
        hello = result.value
        self.assertEqual(hello.version, 1)
        self.assertEqual(hello.transport, "websocket")
        self.assertEqual(hello.audio_params.format, "opus")
        self.assertEqual(hello.audio_params.sample_rate, 16000)
        self.assertEqual(hello.audio_params.channels, 1)
        self.assertEqual(hello.audio_params.frame_duration, 60)
        self.assertEqual(hello.features, {"mcp": True, "glyph_push": True})
        self.assertEqual(
            hello.text_font,
            {"bundle": "noto-v1", "charset": "common", "size": 20, "bpp": 4},
        )

    def test_minimal_sample_fields(self):
        result = parse_device_hello(_text(DEVICE_HELLO))
        self.assertTrue(result.ok)
        self.assertEqual(result.value.features, {"mcp": True})
        self.assertIsNone(result.value.text_font)

    def test_bytes_input(self):
        self.assertTrue(parse_device_hello(_text(DEVICE_HELLO).encode("utf-8")).ok)

    def test_optional_fields_may_be_absent(self):
        """features / text_font 是可选扩展（§1.3），缺席要给默认值而不是报错。"""
        raw = _json(DEVICE_HELLO)
        raw.pop("features")
        result = parse_device_hello(json.dumps(raw))
        self.assertTrue(result.ok)
        self.assertEqual(result.value.features, {})
        self.assertIsNone(result.value.text_font)

    def test_features_null_counts_as_absent(self):
        raw = _json(DEVICE_HELLO)
        raw["features"] = None
        result = parse_device_hello(json.dumps(raw))
        self.assertTrue(result.ok)
        self.assertEqual(result.value.features, {})

    def test_hello_from_the_other_direction_is_rejected(self):
        """服务端 hello 也是 type=hello，但它没有 version 字段（§1.4）。

        两个方向字段不同，别指望一个函数两边都能用 —— 这里必须挡住。
        """
        result = parse_device_hello(_text(SERVER_HELLO_24000))
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ERR_MISSING_FIELD)
        self.assertIn("version", result.error.detail)

    def test_non_hello_device_messages_are_rejected(self):
        for rel in DEVICE_NON_HELLO:
            with self.subTest(fixture=rel):
                result = parse_device_hello(_text(rel))
                self.assertFalse(result.ok)
                self.assertEqual(result.error.code, ERR_UNEXPECTED_TYPE)

    def test_bad_json_bubbles_up_as_its_own_error(self):
        result = parse_device_hello("{不是 JSON")
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ERR_NOT_JSON)

    def test_transport_must_be_websocket(self):
        """第一版只做 WebSocket（docs/xiaozhi拆解.md §3），MQTT 要走别的路。"""
        raw = _json(DEVICE_HELLO)
        raw["transport"] = "mqtt"
        result = parse_device_hello(json.dumps(raw))
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ERR_BAD_TRANSPORT)

    def test_transport_missing(self):
        raw = _json(DEVICE_HELLO)
        raw.pop("transport")
        result = parse_device_hello(json.dumps(raw))
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ERR_MISSING_FIELD)

    def test_version_must_be_supported(self):
        """文档 §3 的二进制协议版本 2/3 我们还没做，明确报错而不是硬着头皮上。

        这个判断依赖"hello.version 和 §3 的二进制协议版本是同一个数"这个理解，
        文档没写清 —— 板子到了抓真机 hello 对一遍（见模块 docstring）。
        """
        for version in (2, 3, 99):
            with self.subTest(version=version):
                raw = _json(DEVICE_HELLO)
                raw["version"] = version
                result = parse_device_hello(json.dumps(raw))
                self.assertFalse(result.ok)
                self.assertEqual(result.error.code, ERR_UNSUPPORTED_VERSION)

    def test_version_must_be_a_positive_integer(self):
        for version in ("1", 1.0, None, 0, -1, True):
            with self.subTest(version=repr(version)):
                raw = _json(DEVICE_HELLO)
                raw["version"] = version
                result = parse_device_hello(json.dumps(raw))
                self.assertFalse(result.ok)
                self.assertIn(result.error.code, {ERR_BAD_FIELD, ERR_MISSING_FIELD})

    def test_audio_params_missing_or_wrong_type(self):
        raw = _json(DEVICE_HELLO)
        raw.pop("audio_params")
        result = parse_device_hello(json.dumps(raw))
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ERR_MISSING_FIELD)

        raw["audio_params"] = "opus/16000"
        result = parse_device_hello(json.dumps(raw))
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ERR_BAD_FIELD)

    def test_features_and_text_font_must_be_objects(self):
        for key in ("features", "text_font"):
            with self.subTest(key=key):
                raw = _json(DEVICE_HELLO)
                raw[key] = "不是对象"
                result = parse_device_hello(json.dumps(raw))
                self.assertFalse(result.ok)
                self.assertEqual(result.error.code, ERR_BAD_FIELD)
                self.assertIn(key, result.error.detail)


class BuildServerHelloTest(unittest.TestCase):
    def test_matches_the_document_sample_exactly(self):
        """构造出来的要**逐字段等于**协议文档 §1.4 那个样本（session_id 用它的占位符）。

        这条是"我们没自己发明协议"的直接证据：样本长什么样，我们就生成什么样。
        """
        expected = _json(SERVER_HELLO_24000)
        self.assertEqual(build_server_hello("xxx"), expected)

    def test_default_sample_rate_is_24000(self):
        """§8.3：下行用 24000 音乐效果更好；设备上行是 16000，两边不一样是文档写明的。"""
        message = build_server_hello("s-1")
        self.assertEqual(message["audio_params"]["sample_rate"], 24000)
        self.assertEqual(protocol.DEFAULT_SERVER_SAMPLE_RATE, 24000)

    def test_round_trips_as_a_text_frame(self):
        message = build_server_hello("s-2")
        decoded = decode_message(encode_message(message))
        self.assertTrue(decoded.ok)
        self.assertEqual(decoded.value, message)
        self.assertEqual(decoded.value["type"], "hello")
        self.assertEqual(decoded.value["transport"], "websocket")
        self.assertEqual(decoded.value["session_id"], "s-2")

    def test_audio_params_can_be_overridden(self):
        message = build_server_hello(
            "s-3", AudioParams(format="opus", sample_rate=16000)
        )
        self.assertEqual(
            message["audio_params"], {"format": "opus", "sample_rate": 16000, "channels": 1}
        )

    def test_blank_session_id_raises(self):
        """session_id 是我们自己生成的，空了就是 bug —— 对内参数错误要当场炸。"""
        for value in ("", "   ", None, 123):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):
                    build_server_hello(value)


class NeverRaisesTest(unittest.TestCase):
    """§2.2.1 的验收：坏 JSON / 缺字段 / 版本不符 —— **不许抛到调用方炸掉**。

    设备那头只会说"无法连接到服务"，原因全在我们这侧。解析一旦抛异常，
    主循环就得靠 try/except 兜，兜漏一次就是整个服务端挂掉。
    """

    def test_hostile_inputs_never_raise(self):
        for raw in HOSTILE_INPUTS:
            with self.subTest(raw=repr(raw)[:80]):
                for func in (decode_message, parse_device_hello, parse_audio_params):
                    result = func(raw)
                    self.assertIsInstance(result, protocol.ParseResult)
                    self.assertIsInstance(result.ok, bool)

    def test_every_failure_carries_a_known_code_and_a_message(self):
        for raw in HOSTILE_INPUTS:
            with self.subTest(raw=repr(raw)[:80]):
                result = parse_device_hello(raw)
                if result.ok:
                    continue
                self.assertIn(result.error.code, ALL_CODES)
                self.assertTrue(result.error.detail)
                # str() 要能直接进日志，别是默认的 <object at 0x...>
                self.assertIn(result.error.code, str(result.error))

    def test_success_result_carries_no_error(self):
        result = parse_device_hello(_text(DEVICE_HELLO_FULL))
        self.assertTrue(result.ok)
        self.assertIsNone(result.error)
        self.assertTrue(bool(result))

    def test_failure_result_carries_no_value(self):
        result = parse_device_hello("{}")
        self.assertFalse(result.ok)
        self.assertIsNone(result.value)
        self.assertFalse(bool(result))


class ImportSurfaceTest(unittest.TestCase):
    def test_package_exports_resolve(self):
        """`xiaoyu.xiaozhi` 的 __all__ 里每个名字都得真能取到（防拼错导出）。"""
        import xiaoyu.xiaozhi as pkg

        for name in pkg.__all__:
            with self.subTest(name=name):
                self.assertTrue(hasattr(pkg, name))


if __name__ == "__main__":
    unittest.main()
