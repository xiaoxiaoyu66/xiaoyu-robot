"""音频设备选择。

这个模块回答一个问题：**到底该往哪个设备录音、往哪个设备放音？**

为什么要有它（两个实测踩出来的坑）：

    坑 1 · 默认设备不一定是喇叭。
        系统默认输出实测是显示器的 HDMI 音频，声音全进了显示器，
        笔记本扬声器一点动静都没有，而日志看起来一切正常。

    坑 2 · 设备编号会漂移。
        同一台机器、同一次开机，两次运行之间 [3] 和 [4] 互换了
        （扬声器 <-> 显示器的 HDMI 音频）。
        原因是 PortAudio 的 MME 枚举顺序不是固定的，
        所以".env 里写死 XIAOYU_SPK_DEVICE=4"是**会时好时坏**的。
        设备名字基本不变，所以默认按名字找，编号只当备用手段。

这个模块刻意不 import sounddevice：
    match_devices() 是纯函数，不装声卡驱动也能跑测试。
    真正碰硬件的那一小段在 resolve_device() 里，且是延迟导入。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..logger import get_logger

logger = get_logger(__name__)

# Windows 上 MME 这一组兼容性最好、也最省事。
# 同一台机器同一个设备会在 MME / DirectSound / WASAPI / WDM-KS 里各出现一次，
# 按名字找时会命中好几个，于是优先挑 MME 那个。
_PREFERRED_API = "mme"

# 这几个不是真实设备，而是 Windows 的"设备别名"。
# "Microsoft 声音映射器 / Sound Mapper" 的含义就是"用系统默认设备"。
# 如果按名字把它选中，等于绕一圈又回到"用默认设备"——
# 显示器的 HDMI 那个坑会原样回来，所以直接排除。
_ALIAS_MARKERS = ("声音映射器", "sound mapper")


def _channel_key(output: bool) -> str:
    return "max_output_channels" if output else "max_input_channels"


def normalize_device_name(text: str) -> str:
    """设备名归一化，比之前先过一遍。

    大小写、全角空格、连续空格都不该影响匹配 ——
    Windows 上同一个设备在不同驱动下的名字差一个空格太常见了。
    """
    return " ".join(text.replace("\u3000", " ").split()).casefold()


def match_devices(
    devices: Sequence[Mapping[str, Any]],
    host_apis: Sequence[Mapping[str, Any]] = (),
    name: str = "",
    *,
    output: bool,
) -> list[int]:
    """按名字找设备，返回候选编号，越靠前越优先。

    排序规则：
        1. 名字**完全一样**的，排在"名字里包含它"的前面；
        2. 同一档里 MME 驱动优先；
        3. 还分不出就按编号从小到大，保证结果稳定可预期。

    顺带排除 Windows 的"设备别名"（声音映射器 / Sound Mapper）——
    它代表的是"用系统默认设备"，被名字选中就等于没选，坑会原样回来。

    >>> devices = [
    ...     {"name": "H25T7-3 (NVIDIA HDMI)", "max_output_channels": 2, "hostapi": 0},
    ...     {"name": "扬声器 (Realtek(R) Audio)", "max_output_channels": 2, "hostapi": 0},
    ... ]
    >>> match_devices(devices, [{"name": "MME"}], "扬声器", output=True)
    [1]
    """
    if not name:
        return []

    wanted = normalize_device_name(name)
    channels = _channel_key(output)

    def api_name(device: Mapping[str, Any]) -> str:
        api_index = device.get("hostapi")
        if not isinstance(api_index, int) or not 0 <= api_index < len(host_apis):
            return ""
        try:
            return normalize_device_name(str(host_apis[api_index]["name"]))
        except Exception:
            return ""

    scored: list[tuple[int, int, int]] = []
    for index, device in enumerate(devices):
        if device.get(channels, 0) <= 0:
            continue          # 没有对应方向的通道，直接排除

        got = normalize_device_name(str(device.get("name", "")))
        if any(marker in got for marker in _ALIAS_MARKERS):
            continue          # 设备别名，等于"用系统默认"，不能当成明确选择

        if got == wanted:
            rank = 0
        elif wanted in got:
            rank = 1
        else:
            continue
        scored.append((rank, 0 if api_name(device) == _PREFERRED_API else 1, index))

    scored.sort()
    return [index for _, _, index in scored]


def resolve_device(
    configured: int | None = None,
    name: str | None = None,
    *,
    output: bool,
) -> tuple[int | None, str]:
    """算出实际要用的设备编号，并给出人能看懂的名字。

    优先级：**名字 > 编号 > 系统默认**。

    名字找不到时不硬闯，退回用编号 / 系统默认，并留一条 warning ——
    宁可出一个明显的警告，也不要静悄悄地用错设备。

    返回 (编号, 描述)。编号为 None 表示交给 sounddevice 自己挑默认设备。
    """
    import sounddevice as sd          # 延迟导入：上面的纯函数不需要它

    try:
        devices = sd.query_devices()
        host_apis = sd.query_hostapis()
        default_in, default_out = sd.default.device
    except Exception as exc:
        logger.warning("查询音频设备失败：{}", exc)
        return configured, "未知（查询设备列表失败）"

    what = "输出" if output else "输入"
    channels = _channel_key(output)
    default_index = default_out if output else default_in

    if name:
        candidates = match_devices(devices, host_apis, name, output=output)
        if candidates:
            index = candidates[0]
            if len(candidates) > 1:
                logger.debug(
                    "名字含「{}」的{}设备有 {} 个，用了第一个 [{}] {}",
                    name, what, len(candidates), index, devices[index]["name"],
                )
            return index, f"[{index}] {devices[index]['name']}"
        logger.warning(
            "设备列表里找不到名字含「{}」的{}设备，退回用编号 / 系统默认。"
            "跑 python scripts\\check_audio.py 看看现在有哪些设备",
            name, what,
        )

    if configured is not None:
        try:
            device = devices[configured]
            if device[channels] > 0:
                return configured, f"[{configured}] {device['name']}"
            logger.error(
                "编号 {} 是「{}」，但它没有{}通道 —— 编号会漂移，"
                "建议改用 XIAOYU_{}_DEVICE_NAME 按名字选；本次改用系统默认设备",
                configured, device["name"], what, "SPK" if output else "MIC",
            )
        except Exception:
            logger.error(
                "设备编号 {} 无效（插拔过耳机 / 显示器就会变），本次改用系统默认设备",
                configured,
            )

    if default_index is None or default_index < 0:
        return None, "系统默认（编号未知）"
    try:
        return default_index, f"[{default_index}] {devices[default_index]['name']}  ← 系统默认"
    except Exception:
        return default_index, "系统默认（编号无效）"