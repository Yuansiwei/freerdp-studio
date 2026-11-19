"""Tests for rdp_chat.py with a scripted screen and a recording sender (nothing reaches the remote)."""
import os
import sys
import time
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import rdp_capture  # noqa: E402
import rdp_chat  # noqa: E402

W, H = 3840, 2160
PLACEHOLDER = {"text": "随便问点什么...", "box": [1366, 1844, 188, 42]}
SIDEBAR = {"text": "cudamicrobench 分析：rcp...", "box": [210, 332, 356, 36]}
TOOLBAR = {"text": "DeepSeek-V4-Flash", "box": [1496, 1990, 318, 34]}
PROMPT = "端到端測試，只回覆一行：E2E-OK-4821"


def frame(shade, seq=1):
    bgr = np.full((H // 2, W // 2, 3), shade, np.uint8)
    return rdp_capture.Frame(seq, time.time(), (W, H), 0.5, bgr, 0, 0)


class FakeScreen:
    """reads: list of line lists returned by successive read() calls (last one repeats).
    shades: grey levels returned by successive capture() calls (last one repeats)."""

    def __init__(self, reads, shades=(200,)):
        self.reads, self.shades, self.r, self.c = reads, list(shades), 0, 0

    def capture(self):
        shade = self.shades[min(self.c, len(self.shades) - 1)]
        self.c += 1
        return frame(shade, self.c)

    def read(self, f=None):
        lines = self.reads[min(self.r, len(self.reads) - 1)]
        self.r += 1
        return (f or frame(200)), lines


class FakeSender:
    def __init__(self):
        self.log = []

    def __call__(self, command, value):
        self.log.append((command, value))


def run(screen, send, **kw):
    kw.setdefault("settle", 0.3)
    kw.setdefault("poll", 0.05)
    kw.setdefault("timeout", 5)
    return rdp_chat.chat(screen, send, PROMPT, **kw)


TYPED = [SIDEBAR, {"text": PROMPT, "box": [1366, 1844, 900, 40]}, TOOLBAR]
FINAL = [SIDEBAR, {"text": "上一輪的回覆", "box": [1362, 900, 500, 34]},
         {"text": PROMPT, "box": [2800, 1500, 900, 36]},
         {"text": "E2E-OK-4821", "box": [1362, 1620, 300, 34]}, PLACEHOLDER, TOOLBAR]


class ChatFlowTests(unittest.TestCase):
    def test_happy_path_clicks_types_verifies_then_enters(self):
        screen = FakeScreen([[SIDEBAR, PLACEHOLDER, TOOLBAR], TYPED, FINAL], shades=(200, 200, 120, 60, 60))
        send = FakeSender()
        r = run(screen, send)
        self.assertEqual([c for c, _ in send.log], ["--click", "--text", "--key"])
        self.assertEqual(send.log[0][1], "1460,1865")      # centre of the input box
        self.assertEqual(send.log[2][1], "ENTER")
        self.assertEqual(r["reply"], "E2E-OK-4821")         # only lines after our prompt, above the box
        self.assertTrue(r["prompt_visible"])
        self.assertFalse(r["timed_out"])

    def test_refuses_to_type_over_a_draft(self):  # negative control
        screen = FakeScreen([[SIDEBAR, {"text": "我還沒打完的草稿", "box": [1366, 1844, 400, 42]}, TOOLBAR]])
        send = FakeSender()
        with self.assertRaisesRegex(rdp_chat.ChatError, "refusing to type"):
            run(screen, send)
        self.assertEqual(send.log, [])

    def test_no_enter_when_text_did_not_land(self):  # negative control
        screen = FakeScreen([[SIDEBAR, PLACEHOLDER, TOOLBAR], [SIDEBAR, PLACEHOLDER, TOOLBAR]])
        send = FakeSender()
        with self.assertRaisesRegex(rdp_chat.ChatError, "NOT pressing Enter"):
            run(screen, send)
        self.assertNotIn("--key", [c for c, _ in send.log])

    def test_new_session_button_is_clicked_first(self):
        btn = {"text": "+ 新建会话", "box": [294, 226, 142, 38]}
        screen = FakeScreen([[btn, PLACEHOLDER], [btn, PLACEHOLDER], TYPED, FINAL], shades=(200, 90, 90))
        send = FakeSender()
        run(screen, send, new_session=True)
        self.assertEqual(send.log[0], ("--click", "365,245"))
        self.assertEqual([c for c, _ in send.log][1:], ["--click", "--text", "--key"])

    def test_dry_run_sends_nothing(self):
        send = FakeSender()
        r = run(FakeScreen([[PLACEHOLDER]]), send, dry_run=True)
        self.assertEqual(r["would_click_input"], (1460, 1865))
        self.assertEqual(send.log, [])

    def test_multiline_text_rejected_before_anything_is_sent(self):  # negative control
        send = FakeSender()
        with self.assertRaises(rdp_chat.ChatError):
            rdp_chat.chat(FakeScreen([[PLACEHOLDER]]), send, "line1\nline2")
        self.assertEqual(send.log, [])

    def test_times_out_when_nothing_changes(self):  # negative control
        screen = FakeScreen([[PLACEHOLDER], TYPED, FINAL], shades=(200,))
        r = run(screen, FakeSender(), timeout=0.4)
        self.assertTrue(r["timed_out"])
        self.assertIsNone(r["first_change_s"])


class ExtractReplyTests(unittest.TestCase):
    def test_sidebar_and_older_messages_are_excluded(self):
        r = rdp_chat.extract_reply(FINAL, PROMPT, panel_min_x=int(W * 0.22))
        self.assertEqual([l["text"] for l in r["lines"]], ["E2E-OK-4821"])

    def test_prompt_scrolled_away_returns_whole_panel(self):
        lines = [SIDEBAR, {"text": "很長的回覆第 40 行", "box": [1362, 300, 500, 34]}, PLACEHOLDER]
        r = rdp_chat.extract_reply(lines, PROMPT, panel_min_x=int(W * 0.22))
        self.assertFalse(r["prompt_visible"])
        self.assertEqual(r["text"], "很長的回覆第 40 行")


if __name__ == "__main__":
    unittest.main()
