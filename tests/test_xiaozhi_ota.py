"""OTA 配置应答层（`xiaoyu/xiaozhi/ota.py`）的测试。

跑法：py -3.11 -m unittest discover tests

全程**离线**：不开端口、不 import 网络库、真服务器一行不碰。喂进去的请求头
按上游 `main/ota.cc` 的 `SetupHttp()` 照抄，应答按 `CheckVersion()` 的读法核对。

这里最要紧的两条不是 happy path，是**两条会把板子搞死的回归**：

1. 应答里**必须有 `websocket` 段** —— 少了它，`application.cc:543` 会让设备
   退回 MQTT，从此永远不来连我们，而且症状只是"板子没反应"。
2. 应答里**绝不能有 `activation` 段** —— 有了它 `application.cc:502-527`
   会让设备进激活循环，卡在激活界面等用户输入。这条是 2026-09-20 读上游源码
   才钉住的，之前几份文档都没写，所以专门用几个用例守着。
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from xiaoyu.xiaozhi import ota

# 上游 main/ota.cc 的 SetupHttp()：
#   SetHeader("Activation-Version", "2"); SetHeader("Device-Id", MAC);
#   SetHeader("Client-Id", UUID); SetHeader("User-Agent", ...);
#   SetHeader("Accept-Language", ...); SetHeader("Content-Type", "application/json")
# 下面按原样抄，只是值换成假数据。
DEVICE_HEADERS = {
    "Activation-Version": "2",
    "Device-Id": "0c:8b:fd:11:22:33",
    "Client-Id": "b7f0e2d4-5a6c-4f1e-9d3a-2b8c7e0f1a55",
    "User-Agent": "xiaozhi-esp32/2.5.0",
    "Accept-Language": "zh-CN",
    "Content-Type": "application/json",
    "Host": "10.35.76.134:8766",
}

# 设备自述（board.GetSystemInfoJson()）的形状：上游示例里就是版本 + 板卡 + MAC
DEVICE_INFO = {
    "version": 2,
    "board": {"type": "bread-compact-wifi-lcd", "name": "面包板"},
    "mac_address": "0c:8b:fd:11:22:33",
}


class HeaderParsing(unittest.TestCase):
    """请求头归一化：认得出、坏行不炸。"""

    def test_from_mapping_lowercases_names(self):
        got = ota.normalize_headers(DEVICE_HEADERS)
        self.assertEqual(got["device-id"], "0c:8b:fd:11:22:33")
        self.assertEqual(got["host"], "10.35.76.134:8766")
        self.assertNotIn("Device-Id", got)

    def test_from_lines(self):
        lines = ["Host: 10.0.0.5:8766", "Device-Id: aa:bb"]
        got = ota.normalize_headers(lines)
        self.assertEqual(got, {"host": "10.0.0.5:8766", "device-id": "aa:bb"})

    def test_single_line_string(self):
        self.assertEqual(ota.normalize_headers("Host: 1.2.3.4:80"), {"host": "1.2.3.4:80"})

    def test_duplicate_names_last_wins(self):
        """HTTP 的规矩：同名取最后一个。"""
        got = ota.normalize_headers(["Host: first", "Host: second"])
        self.assertEqual(got["host"], "second")

    def test_malformed_lines_are_skipped(self):
        got = ota.normalize_headers(["这是垃圾", ": 没有名字", "Host: ok"])
        self.assertEqual(got, {"host": "ok"})

    def test_none_and_junk_return_empty(self):
        for junk in (None, 42, object()):
            with self.subTest(junk=type(junk).__name__):
                self.assertEqual(ota.normalize_headers(junk), {})

    def test_bytes_values_decode(self):
        got = ota.normalize_headers({b"Host": b"1.2.3.4:9"})
        self.assertEqual(got["host"], "1.2.3.4:9")


class DeviceIdTests(unittest.TestCase):
    """Device-Id > Client-Id > Serial-Number；认不出来给空串，不给 None。"""

    def test_device_id_wins(self):
        headers = ota.normalize_headers(DEVICE_HEADERS)
        self.assertEqual(ota.device_id(headers), "0c:8b:fd:11:22:33")

    def test_falls_back_to_client_id(self):
        headers = ota.normalize_headers({"Client-Id": "uuid-1"})
        self.assertEqual(ota.device_id(headers), "uuid-1")

    def test_falls_back_to_serial_number(self):
        self.assertEqual(ota.device_id({"serial-number": "SN-9"}), "SN-9")

    def test_empty_when_nothing_known(self):
        self.assertEqual(ota.device_id({}), "")

    def test_blank_values_are_skipped(self):
        self.assertEqual(ota.device_id({"device-id": "   ", "client-id": "uuid-2"}), "uuid-2")


class DeviceInfoTests(unittest.TestCase):
    """设备自述：解析不出来一律 {}，绝不抛。"""

    def test_good_json(self):
        self.assertEqual(ota.parse_device_info(json.dumps(DEVICE_INFO))["board"]["type"],
                         "bread-compact-wifi-lcd")

    def test_accepts_bytes_and_bad_utf8(self):
        self.assertEqual(ota.parse_device_info(json.dumps(DEVICE_INFO).encode("utf-8")),
                         DEVICE_INFO)
        self.assertEqual(ota.parse_device_info(b"\xff\xfe\x00bad"), {})

    def test_junk_returns_empty_dict(self):
        for junk in (None, "", "   ", "{不是 JSON", "[1, 2]", 42, DEVICE_INFO):
            with self.subTest(junk=str(junk)[:24]):
                self.assertEqual(ota.parse_device_info(junk), {})


class IsOtaPathTests(unittest.TestCase):
    """路径判定：结尾斜杠和查询串都放过。"""

    def test_exact(self):
        self.assertTrue(ota.is_ota_path("/xiaozhi/ota/"))

    def test_without_trailing_slash(self):
        self.assertTrue(ota.is_ota_path("/xiaozhi/ota"))

    def test_with_query_string(self):
        self.assertTrue(ota.is_ota_path("/xiaozhi/ota/?device=abc"))

    def test_full_url(self):
        self.assertTrue(ota.is_ota_path("http://10.0.0.5:8766/xiaozhi/ota/"))

    def test_other_paths_rejected(self):
        for path in ("/", "/xiaozhi/v1/", "/xiaozhi/ota/extra", "/face/", "", None, 42):
            with self.subTest(path=path):
                self.assertFalse(ota.is_ota_path(path))


class WebsocketUrlTests(unittest.TestCase):
    """Host 头 -> ws:// 地址。合法才给，不合法给空串。"""

    def test_normal(self):
        self.assertEqual(ota.websocket_url("10.35.76.134:8766"),
                         "ws://10.35.76.134:8766/xiaozhi/v1/")

    def test_host_without_port(self):
        self.assertEqual(ota.websocket_url("192.168.1.9"), "ws://192.168.1.9/xiaozhi/v1/")

    def test_custom_path_gets_leading_slash(self):
        self.assertEqual(ota.websocket_url("h:1", "other/"), "ws://h:1/other/")

    def test_strips_surrounding_whitespace(self):
        self.assertEqual(ota.websocket_url("  10.0.0.1:80  "), "ws://10.0.0.1:80/xiaozhi/v1/")

    def test_rejects_empty_and_blank(self):
        for host in ("", "   ", None):
            with self.subTest(host=host):
                self.assertEqual(ota.websocket_url(host), "")

    def test_rejects_host_injection(self):
        """Host 是外部输入、要拼进 URL —— 不挡的话能把它指到别处去。"""
        for host in ("evil/x", "evil?x", "evil#x", "has space", "a\tb"):
            with self.subTest(host=host):
                self.assertEqual(ota.websocket_url(host), "")


class BuildConfigTests(unittest.TestCase):
    """应答 JSON 的形状 —— 两条会把板子搞死的回归都在这个类里。"""

    def _payload(self, **kwargs):
        config = kwargs.pop("config", None)
        return ota.build_config("10.35.76.134:8766", config, **kwargs)

    def test_must_have_websocket_section(self):
        """少了它设备会退回 MQTT，永远不来连我们（application.cc:543）。"""
        self.assertIn("websocket", self._payload())

    def test_never_has_activation_section(self):
        """有了它设备会卡在激活界面（application.cc:502-527）。我们不做激活。"""
        self.assertNotIn("activation", self._payload())
        self.assertNotIn("activation", self._payload(config=ota.OtaConfig(token="t")))

    def test_only_known_top_level_keys(self):
        """多出来的键要么被设备忽略、要么被写进闪存 —— 不接受第三种可能。"""
        payload = self._payload(config=ota.OtaConfig(include_server_time=True))
        self.assertLessEqual(set(payload), {"websocket", "server_time"})

    def test_websocket_holds_exactly_three_keys(self):
        """这三个键会被设备逐个写进闪存，夹带的东西也会被写进去。"""
        self.assertEqual(set(self._payload()["websocket"]), {"url", "token", "version"})

    def test_values_from_config(self):
        config = ota.OtaConfig(token="xiaoyu", version=1, websocket_path="/xiaozhi/v1/")
        section = self._payload(config=config)["websocket"]
        self.assertEqual(section["token"], "xiaoyu")
        self.assertEqual(section["version"], 1)
        self.assertEqual(section["url"], "ws://10.35.76.134:8766/xiaozhi/v1/")

    def test_version_matches_protocol_layer(self):
        """OTA 给的 version 和握手认的 version 是同一个数，别各自写一份。"""
        from xiaoyu.xiaozhi import protocol

        self.assertIn(ota.CONFIG_VERSION, protocol.SUPPORTED_PROTOCOL_VERSIONS)

    def test_empty_token_is_allowed(self):
        """token 空也得能回：设备那边没空格就自己补 Bearer，不会因为我们而连不上。"""
        self.assertEqual(self._payload(config=ota.OtaConfig(token=""))["websocket"]["token"], "")

    def test_no_server_time_by_default(self):
        self.assertNotIn("server_time", self._payload())

    def test_bad_host_raises(self):
        """构造是我们自己的代码在调 —— 参数错了就抛，别把空 url 发给设备。"""
        for host in ("", "   ", "has space", "evil/x"):
            with self.subTest(host=host):
                with self.assertRaises(ValueError):
                    ota.build_config(host)

    def test_result_is_json_serializable(self):
        self.assertIsInstance(json.dumps(self._payload()), str)


class ServerTimeTests(unittest.TestCase):
    """时间段的单位是「UTC 毫秒 + 时区偏移（分钟）」（main/ota.cc:194-216）。"""

    def test_beijing_offset_is_plus_480_minutes(self):
        moment = datetime(2026, 9, 20, 12, 0, 0, tzinfo=timezone(timedelta(hours=8)))
        got = ota.server_time(moment)
        self.assertEqual(got["timezone_offset"], 480)
        self.assertEqual(got["timestamp"], int(moment.timestamp() * 1000))

    def test_utc_offset_is_zero(self):
        moment = datetime(2026, 9, 20, 4, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(ota.server_time(moment)["timezone_offset"], 0)

    def test_timestamp_is_milliseconds_not_seconds(self):
        """搞错单位设备的时间会飞到 1970 或者 5 万年以后 —— 差 1000 倍很明显。"""
        moment = datetime(2026, 9, 20, 4, 0, 0, tzinfo=timezone.utc)
        self.assertGreater(ota.server_time(moment)["timestamp"], 1_700_000_000_000)

    def test_enabled_by_config(self):
        payload = ota.build_config("h:1", ota.OtaConfig(include_server_time=True))
        self.assertIn("timestamp", payload["server_time"])
        self.assertIn("timezone_offset", payload["server_time"])


class EncodeConfigTests(unittest.TestCase):
    def test_roundtrip(self):
        payload = ota.build_config("10.0.0.2:8766", ota.OtaConfig(token="小柚子"))
        self.assertEqual(json.loads(ota.encode_config(payload).decode("utf-8")), payload)

    def test_chinese_is_not_escaped(self):
        """设备那边是 cJSON，认得 UTF-8；转义成 \\uXXXX 只是让人自己看不懂日志。"""
        raw = ota.encode_config({"token": "小柚子"})
        self.assertIn("小柚子".encode("utf-8"), raw)


class HandleTests(unittest.TestCase):
    """一条请求 -> 一条应答；坏输入翻成状态码，不抛异常。"""

    def _handle(self, path="/xiaozhi/ota/", headers=None, **kwargs):
        return ota.handle_request(path, DEVICE_HEADERS if headers is None else headers, **kwargs)

    def test_happy_path(self):
        got = self._handle()
        self.assertEqual(got.status, ota.STATUS_OK)
        self.assertTrue(got.ok)
        self.assertEqual(got.content_type, ota.CONTENT_TYPE_JSON)

    def test_body_is_parsable_and_points_at_us(self):
        """把设备那一侧的读法走一遍：解 JSON -> 取 websocket.url -> 它是个 ws 地址。"""
        payload = json.loads(self._handle().body.decode("utf-8"))
        parsed = urlparse(payload["websocket"]["url"])
        self.assertEqual(parsed.scheme, "ws")
        self.assertEqual(parsed.hostname, "10.35.76.134")
        self.assertEqual(parsed.port, 8766)
        self.assertEqual(parsed.path, "/xiaozhi/v1/")

    def test_accepts_raw_header_lines(self):
        lines = [f"{name}: {value}" for name, value in DEVICE_HEADERS.items()]
        self.assertEqual(ota.handle_request("/xiaozhi/ota/", lines).status, ota.STATUS_OK)

    def test_non_ota_path_is_404(self):
        """server.py 靠这个把请求让给别人（静态页 / 以后别的端点）。"""
        for path in ("/", "/face/", "/xiaozhi/v1/"):
            with self.subTest(path=path):
                got = ota.handle_request(path, DEVICE_HEADERS)
                self.assertEqual(got.status, ota.STATUS_NOT_FOUND)
                self.assertIn("不是 OTA 端点", got.reason)

    def test_missing_host_is_400(self):
        """设备一定会带 Host；真没有说明有别的东西坏了，回 400 让它重试并留下痕迹。"""
        got = ota.handle_request("/xiaozhi/ota/", {"Device-Id": "aa:bb"})
        self.assertEqual(got.status, ota.STATUS_BAD_REQUEST)
        self.assertEqual(got.content_type, ota.CONTENT_TYPE_TEXT)

    def test_bad_host_is_400_not_a_crash(self):
        got = ota.handle_request("/xiaozhi/ota/", {"host": "evil/x"})
        self.assertEqual(got.status, ota.STATUS_BAD_REQUEST)
        self.assertFalse(got.ok)

    def test_junk_headers_never_raise(self):
        for junk in (None, 42, [], ["垃圾行"], {b"\xff": object()}):
            with self.subTest(junk=str(junk)[:24]):
                got = ota.handle_request("/xiaozhi/ota/", junk)
                self.assertIn(got.status, (ota.STATUS_BAD_REQUEST, ota.STATUS_OK))

    def test_junk_path_never_raises(self):
        for path in (None, 42, b"/xiaozhi/ota/"):
            with self.subTest(path=path):
                self.assertIsInstance(ota.handle_request(path, DEVICE_HEADERS), ota.OtaResponse)

    def test_device_body_is_ignored_by_handler(self):
        """body 在这儿不看 —— 要看得走 parse_device_info()，那是给日志用的。"""
        got = ota.handle_request("/xiaozhi/ota/", DEVICE_HEADERS, json.dumps(DEVICE_INFO))
        self.assertEqual(got.status, ota.STATUS_OK)

    def test_every_response_has_a_reason(self):
        for got in (self._handle(), ota.handle_request("/", DEVICE_HEADERS),
                    ota.handle_request("/xiaozhi/ota/", {"host": "evil/x"})):
            with self.subTest(status=got.status):
                self.assertTrue(got.reason)


class IntegrationWithProtocolTests(unittest.TestCase):
    """OTA 给的 url/token，和握手层要的是同一套语义 —— 别各自写一份。"""

    def test_token_without_space_is_what_the_device_expects(self):
        """上游 websocket_protocol.cc:97-103：token 没空格就自动补 "Bearer "。

        所以这里给个短串就行 —— 我们自己别去拼 Bearer，不然会变成 "Bearer Bearer x"。
        """
        section = ota.build_config("h:1", ota.OtaConfig(token="xiaoyu"))["websocket"]
        self.assertNotIn(" ", section["token"])
        self.assertEqual(section["token"], "xiaoyu")


if __name__ == "__main__":
    unittest.main()