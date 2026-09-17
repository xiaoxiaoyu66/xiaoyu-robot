"""音频设备选择的测试。

这一组针对的是两个**实测踩出来的坑**：

    坑 1 · 默认设备不一定是喇叭
        系统默认输出实测是显示器的 HDMI 音频，声音全进了显示器，
        笔记本扬声器一点动静都没有，而日志看起来一切正常。

    坑 2 · 设备编号会漂移
        同一台机器、同一次开机，两次运行之间 [3] 和 [4] 互换了
        （扬声器 <-> 显示器的 HDMI 音频）。
        所以".env 里写死 XIAOYU_SPK_DEVICE=4"是**会时好时坏**的。

    => 结论：按名字找设备，编号只当备用。
       下面 test_order_change_still_finds_same_device 就是这条的回归测试。

零依赖可跑（devices.py 不 import sounddevice）：
    python -m unittest discover tests -v
"""

from __future__ import annotations

import unittest

try:
    from xiaoyu.audio.devices import match_devices, normalize_device_name
except ImportError:                                   # pragma: no cover
    match_devices = None
    normalize_device_name = None

_SKIP = "未安装 loguru，跳过"


def _dev(name: str, *, in_ch: int = 0, out_ch: int = 0, api: int = 0) -> dict:
    return {
        "name": name,
        "max_input_channels": in_ch,
        "max_output_channels": out_ch,
        "hostapi": api,
    }


# 照着用户实机的样子造一份：MME(0) 一组、DirectSound(1) 一组、WDM-KS(3) 一组
_APIS = [
    {"name": "MME"},
    {"name": "Windows DirectSound"},
    {"name": "Windows WASAPI"},
    {"name": "Windows WDM-KS"},
]

_DEVICES = [
    _dev("Microsoft 声音映射器 - Input", in_ch=2, api=0),                # 0
    _dev("麦克风 (Realtek(R) Audio)", in_ch=2, api=0),                   # 1
    _dev("Microsoft 声音映射器 - Output", out_ch=2, api=0),              # 2
    _dev("H25T7-3 (NVIDIA High Definition", out_ch=2, api=0),            # 3 显示器 HDMI
    _dev("扬声器 (Realtek(R) Audio)", out_ch=2, api=0),                  # 4 真实喇叭(MME)
    _dev("扬声器 (Realtek(R) Audio)", out_ch=2, api=1),                  # 5 同一喇叭(DirectSound)
    _dev("麦克风 (Realtek(R) Audio)", in_ch=2, api=1),                   # 6
    _dev("Speakers 1 (Realtek HD Audio output with HAP)", out_ch=2, api=3),  # 7
]

# 把 [3] 和 [4] 对调 —— 这正是实机上真实发生过的漂移
_DEVICES_SWAPPED = list(_DEVICES)
_DEVICES_SWAPPED[3], _DEVICES_SWAPPED[4] = _DEVICES_SWAPPED[4], _DEVICES_SWAPPED[3]


@unittest.skipIf(match_devices is None, _SKIP)
class TestNormalizeDeviceName(unittest.TestCase):
    def test_case_and_spaces_ignored(self):
        self.assertEqual(
            normalize_device_name("  Speakers   1  "),
            normalize_device_name("speakers 1"),
        )

    def test_fullwidth_space_ignored(self):
        """Windows 上同一个设备名里混全角空格很常见。"""
        self.assertEqual(
            normalize_device_name("扬声器\u3000(Realtek)"),
            normalize_device_name("扬声器 (Realtek)"),
        )


@unittest.skipIf(match_devices is None, _SKIP)
class TestMatchDevices(unittest.TestCase):
    def test_finds_speaker_by_partial_name(self):
        self.assertEqual(
            match_devices(_DEVICES, _APIS, "扬声器", output=True),
            [4, 5],
            "应该同时命中 MME 和 DirectSound 上的同一个喇叭",
        )

    def test_never_matches_hdmi_monitor(self):
        """这是最初的病根：千万别把显示器的 HDMI 当成喇叭。"""
        self.assertNotIn(3, match_devices(_DEVICES, _APIS, "扬声器", output=True))

    def test_never_matches_sound_mapper_alias(self):
        """'声音映射器' 是系统的设备别名，不是真设备。"""
        self.assertEqual(match_devices(_DEVICES, _APIS, "声音映射器", output=True), [])

    def test_mme_is_preferred(self):
        """同一个喇叭在多个驱动下各出现一次，优先挑 MME。"""
        self.assertEqual(match_devices(_DEVICES, _APIS, "realtek", output=True)[0], 4)

    def test_input_and_output_are_separated(self):
        """麦克风没有输出通道，反过来也一样，不能串。"""
        self.assertEqual(match_devices(_DEVICES, _APIS, "麦克风", output=True), [])
        self.assertEqual(match_devices(_DEVICES, _APIS, "麦克风", output=False), [1, 6])
        self.assertEqual(match_devices(_DEVICES, _APIS, "扬声器", output=False), [])

    def test_exact_match_beats_partial(self):
        devices = [
            _dev("Speakers 1 (Realtek HD Audio)", out_ch=2, api=0),
            _dev("扬声器", out_ch=2, api=0),
        ]
        self.assertEqual(match_devices(devices, _APIS, "扬声器", output=True), [1])

    def test_empty_name_matches_nothing(self):
        self.assertEqual(match_devices(_DEVICES, _APIS, "", output=True), [])

    def test_unknown_name_matches_nothing(self):
        self.assertEqual(match_devices(_DEVICES, _APIS, "不存在的设备名", output=True), [])

    def test_result_is_stable(self):
        """同样的输入必须给同样的输出，否则日志和排查都会变成玄学。"""
        first = match_devices(_DEVICES, _APIS, "realtek", output=True)
        for _ in range(5):
            self.assertEqual(match_devices(_DEVICES, _APIS, "realtek", output=True), first)

    def test_order_change_still_finds_same_device(self):
        """回归测试：设备编号漂移之后，按名字找必须还是同一个物理设备。

        这正是实机上发生的事 —— 写死编号就会时好时坏。
        """
        before = match_devices(_DEVICES, _APIS, "扬声器", output=True)
        after = match_devices(_DEVICES_SWAPPED, _APIS, "扬声器", output=True)

        self.assertNotEqual(before, after, "编号确实变了，这是前提")
        self.assertEqual(_DEVICES[before[0]]["name"], _DEVICES_SWAPPED[after[0]]["name"])
        self.assertEqual(_DEVICES[before[0]]["name"], "扬声器 (Realtek(R) Audio)")


if __name__ == "__main__":
    unittest.main()