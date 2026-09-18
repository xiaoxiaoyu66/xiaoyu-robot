"""情绪系统纯逻辑测试：标记剥离 + 解析 + 协议消息。

跑法：python -m unittest discover tests
不依赖大模型 —— 测的全是情绪模块和协议里的纯函数。
"""

from __future__ import annotations

import unittest

from xiaoyu.face import protocol
from xiaoyu.llm.emotion import parse_emotion, split_visible


class SplitVisibleTest(unittest.TestCase):
    def test_complete_mark_removed_from_text(self):
        visible, hold = split_visible("今天好开心呀！[emotion]happy,0.8[/emotion]")
        self.assertEqual(visible, "今天好开心呀！")
        self.assertEqual(hold, "")

    def test_partial_mark_held_back(self):
        # 流式中途：标记只来了一半，按住别显示
        visible, hold = split_visible("好呀！[emotion]hap")
        self.assertEqual(visible, "好呀！")
        self.assertEqual(hold, "[emotion]hap")

    def test_partial_mark_completes_later(self):
        visible, hold = split_visible("[emotion]hap")
        self.assertEqual(hold, "[emotion]hap")
        visible2, hold2 = split_visible(hold + "py,0.8[/emotion]")
        self.assertEqual(visible2, "")
        self.assertEqual(hold2, "")

    def test_no_mark_passthrough(self):
        visible, hold = split_visible("就是一句普通的话。")
        self.assertEqual(visible, "就是一句普通的话。")
        self.assertEqual(hold, "")


class ParseEmotionTest(unittest.TestCase):
    def test_parse_happy(self):
        self.assertEqual(parse_emotion("……[emotion]happy,0.8[/emotion]"), ("happy", 0.8))

    def test_parse_unknown_mood_falls_back_to_neutral(self):
        self.assertEqual(parse_emotion("[emotion]furious,0.9[/emotion]"), ("neutral", 0.5))

    def test_parse_no_mark_is_neutral(self):
        self.assertEqual(parse_emotion("没有标记"), ("neutral", 0.5))

    def test_parse_intensity_clamped(self):
        self.assertEqual(parse_emotion("[emotion]sad,1.7[/emotion]"), ("sad", 1.0))
        self.assertEqual(parse_emotion("[emotion]angry,-0.2[/emotion]"), ("angry", 0.0))

    def test_parse_garbage_intensity_defaults_half(self):
        self.assertEqual(parse_emotion("[emotion]surprised,abc[/emotion]"), ("surprised", 0.5))


class StreamingHoldbackTest(unittest.TestCase):
    """流式压测：token 边界切在标记中间时也不能漏。

    回归的是真出过的 bug：split_visible 一开始只认**完整**的 "[emotion]"，
    于是 "[", "emotion", "]" 这种切法会把 "[" 当正文发出去，后面的
    "emotion]happy,0.9[/emotion]" 跟着漏 —— 标记会被念出来、上字幕、写进历史。
    逐字喂必现，真 DeepSeek 冒烟也复现过。
    """

    MARK = "[emotion]happy,0.9[/emotion]"

    def _stream_visible(self, chunks):
        """照抄 stream_reply 的循环：hold 累积 -> 拆 -> 攒进 buffer。"""
        hold = ""
        out = ""
        for piece in chunks:
            hold += piece
            visible, hold = split_visible(hold)
            out += visible
        return out

    def test_lone_open_bracket_is_held(self):
        visible, hold = split_visible("好呀！[")
        self.assertEqual(visible, "好呀！")
        self.assertEqual(hold, "[")

    def test_every_prefix_of_open_tag_is_held(self):
        for frag in ("[", "[e", "[em", "[emo", "[emot", "[emoti", "[emotio", "[emotion"):
            with self.subTest(frag=frag):
                visible, hold = split_visible("好呀！" + frag)
                self.assertEqual(visible, "好呀！")
                self.assertEqual(hold, frag)

    def test_char_by_char_never_leaks(self):
        # 逐字喂是最狠的切法：一个字符一个 chunk。修之前这条必红。
        out = self._stream_visible(list("当然高兴呀！" + self.MARK))
        self.assertNotIn("[emotion", out)
        self.assertNotIn("[/emotion]", out)
        self.assertEqual(out, "当然高兴呀！")

    def test_split_inside_open_tag_never_leaks(self):
        out = self._stream_visible(["稳了。", "[", "emotion]hap", "py,0.9[/emotion]"])
        self.assertNotIn("emotion", out)
        self.assertEqual(out, "稳了。")


class EmotionProtocolTest(unittest.TestCase):
    def test_emotion_message_validates(self):
        msg = protocol.emotion_message("angry", 0.7)
        self.assertEqual(msg, {"type": "emotion", "mood": "angry", "intensity": 0.7})
        # 不认识的 mood 降为 neutral，坏强度给 0.5
        self.assertEqual(protocol.emotion_message("furious", 9)["mood"], "neutral")
        self.assertEqual(protocol.emotion_message("happy", "bad")["intensity"], 0.5)
