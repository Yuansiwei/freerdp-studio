"""Tests for rdp_chat.py with a scripted screen and a recording sender (nothing reaches the remote)."""
import collections
import itertools
import os
import sys
import time
import unittest

import cv2
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

    def read(self, f=None, region=None):
        lines = self.reads[min(self.r, len(self.reads) - 1)]
        self.r += 1
        return (f or frame(200)), lines

    def snapshot(self, f, region):  # called by the capture thread right after capture()
        return self.read(f, region)[1]

    def read_snapshot(self, snap):
        return snap


class FakeSender:
    def __init__(self):
        self.log = []

    def __call__(self, command, value):
        self.log.append((command, value))


def short_sleep(seconds):
    time.sleep(min(seconds, 0.01))  # fake screens react instantly; keep the settle loop from spinning hot


def run(screen, send, **kw):
    kw.setdefault("settle", 0.3)
    kw.setdefault("poll", 0.05)
    kw.setdefault("timeout", 5)
    kw.setdefault("sleep", short_sleep)
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


WORK_HEADER = {"text": "cudamicrobench 分析：rcpBw 32 参数", "box": [1216, 120, 584, 50]}
TEST_HEADER = {"text": "端到端連線測試 E2E-OK-4821", "box": [1216, 120, 520, 50]}
TEST_ITEM = {"text": "端到端連線測試 E2E-OK-4821", "box": [260, 580, 440, 40]}


class SessionSwitchTests(unittest.TestCase):
    def test_switches_and_verifies_header_before_typing(self):
        screen = FakeScreen([[WORK_HEADER, TEST_ITEM, PLACEHOLDER], [TEST_HEADER, TEST_ITEM, PLACEHOLDER],
                             TYPED, FINAL], shades=(200, 90, 90))
        send = FakeSender()
        r = run(screen, send, session="E2E-OK-4821")
        self.assertEqual(send.log[0], ("--click", "480,600"))            # the sidebar entry
        self.assertEqual([c for c, _ in send.log][1:], ["--click", "--text", "--key"])
        self.assertIn("E2E-OK-4821", r["active_session"])

    def test_already_active_session_is_not_clicked(self):
        screen = FakeScreen([[TEST_HEADER, TEST_ITEM, PLACEHOLDER], TYPED, FINAL], shades=(200, 90, 90))
        send = FakeSender()
        run(screen, send, session="E2E-OK-4821")
        self.assertEqual([c for c, _ in send.log], ["--click", "--text", "--key"])
        self.assertEqual(send.log[0][1], "1460,1865")                      # straight to the input box

    def test_refuses_when_header_still_shows_another_session(self):  # negative control
        screen = FakeScreen([[WORK_HEADER, TEST_ITEM, PLACEHOLDER], [WORK_HEADER, TEST_ITEM, PLACEHOLDER]])
        send = FakeSender()
        with self.assertRaisesRegex(rdp_chat.ChatError, "refusing to type"):
            run(screen, send, session="E2E-OK-4821")
        self.assertEqual([c for c, _ in send.log], ["--click"])           # only the sidebar click

    def test_unknown_session_sends_nothing(self):  # negative control
        send = FakeSender()
        with self.assertRaisesRegex(rdp_chat.ChatError, "not found in the sidebar"):
            run(FakeScreen([[WORK_HEADER, TEST_ITEM, PLACEHOLDER]]), send, session="no-such-session")
        self.assertEqual(send.log, [])

    def test_session_and_new_session_conflict(self):  # negative control
        with self.assertRaises(rdp_chat.ChatError):
            run(FakeScreen([[PLACEHOLDER]]), FakeSender(), session="x", new_session=True)


class FuzzyMatchTests(unittest.TestCase):
    def test_tolerates_an_ocr_slip(self):
        self.assertTrue(rdp_chat.fuzzy_in("第二輪測試：請把4821乘以", "第二轮測試：請把4821乘以2，只回覆"))

    def test_unrelated_text_does_not_match(self):  # negative control
        self.assertFalse(rdp_chat.fuzzy_in("第二輪測試：請把4821乘以", "cudamicrobench 分析：rcpBw 32 参数"))
        self.assertFalse(rdp_chat.fuzzy_in("", "anything"))

    def test_enter_pressed_when_typed_text_has_one_misread_char(self):
        typo = [SIDEBAR, {"text": PROMPT.replace("測", "测"), "box": [1366, 1844, 900, 40]}, TOOLBAR]
        screen = FakeScreen([[SIDEBAR, PLACEHOLDER, TOOLBAR], typo, FINAL], shades=(200, 90, 90))
        send = FakeSender()
        run(screen, send)
        self.assertIn(("--key", "ENTER"), send.log)

    def test_no_enter_when_other_text_is_in_the_box(self):  # negative control
        other = [SIDEBAR, {"text": "完全不同的另一段文字內容在這裡", "box": [1366, 1844, 900, 40]}, TOOLBAR]
        send = FakeSender()
        with self.assertRaisesRegex(rdp_chat.ChatError, "NOT pressing Enter"):
            run(FakeScreen([[SIDEBAR, PLACEHOLDER, TOOLBAR], other]), send)
        self.assertNotIn("--key", [c for c, _ in send.log])


class FakeScrollScreen:
    """A transcript taller than the viewport. --wheel scrolls it (positive = up, 2 lines per notch).
    After --text the next read shows the typed prompt in the input box."""

    def __init__(self, doc, view=12, line_h=60, top=300):
        self.doc, self.view, self.line_h, self.top = doc, view, line_h, top
        self.offset = max(0, len(doc) - view)          # opened at the bottom
        self.bottom_offset = self.offset
        self.typed_pending = False
        self.captures = 0

    def scroll(self, notches):
        self.offset = max(0, min(len(self.doc) - self.view, self.offset - notches * 2))

    def capture(self):
        self.captures += 1
        return frame(200 if self.captures <= 2 else 90)

    def read(self, f=None, region=None):
        if self.typed_pending:
            self.typed_pending = False
            return frame(200), [{"text": PROMPT, "box": [1366, 1844, 900, 40]}, TOOLBAR]
        vis = self.doc[self.offset:self.offset + self.view]
        lines = [{"text": t, "box": [x, self.top + i * self.line_h, 400, 40]} for i, (t, x) in enumerate(vis)]
        return frame(200), lines + [PLACEHOLDER, TOOLBAR]

    def snapshot(self, f, region):  # the screen as it was when `f` was captured
        return self.read(f, region)[1]

    def read_snapshot(self, snap):
        return snap


class ScrollSender(FakeSender):
    def __init__(self, screen):
        super().__init__()
        self.screen = screen

    def __call__(self, command, value):
        super().__call__(command, value)
        if command == "--wheel":
            self.screen.scroll(int(value))
        elif command == "--text":
            self.screen.typed_pending = True


ITEMS = [(f"{i}. 項目 {i}", 1362) for i in range(1, 41)]
DOC = [(f"更早的訊息 {i}", 1362) for i in range(5)] + [(PROMPT, 2800)] + ITEMS + [("Build · DeepSeek-V4-Flash · 9s", 1362)]


def no_sleep(_):
    pass


class ScrollbackTests(unittest.TestCase):
    def test_reply_taller_than_screen_is_read_in_full_and_view_restored(self):
        screen = FakeScrollScreen(DOC)
        r = rdp_chat.read_full(screen, ScrollSender(screen), PROMPT, int(W * 0.22), W, H, sleep=no_sleep)
        self.assertEqual(r["text"].splitlines(), [t for t, _ in ITEMS])  # all 40, in order, no repeats
        self.assertEqual(r["footer"], "Build · DeepSeek-V4-Flash · 9s")
        self.assertTrue(r["prompt_visible"])
        self.assertFalse(r["gaps"])
        self.assertTrue(r["restored"])
        self.assertEqual(screen.offset, screen.bottom_offset)            # left where the user had it

    def test_missing_prompt_stops_at_the_top_and_says_so(self):  # negative control
        screen = FakeScrollScreen([(f"別的對話 {i}", 1362) for i in range(30)])
        r = rdp_chat.read_full(screen, ScrollSender(screen), PROMPT, int(W * 0.22), W, H, sleep=no_sleep)
        self.assertFalse(r["prompt_visible"])
        self.assertTrue(r["top_reached"])
        self.assertTrue(r["restored"])

    def test_pages_that_do_not_overlap_are_flagged(self):  # negative control
        screen = FakeScrollScreen(DOC)
        r = rdp_chat.read_full(screen, ScrollSender(screen), PROMPT, int(W * 0.22), W, H, step=10, sleep=no_sleep)
        self.assertTrue(r["gaps"])

    def test_floating_button_and_cut_edge_lines_do_not_duplicate(self):
        # real remote: a "↓" button floats at a fixed spot while scrolled up, and the bottom line of a
        # page can be cut (OCR'd shorter) - the contiguous-run stitcher duplicated 74 of 80 items.
        class Messy(FakeScrollScreen):
            def read(self, f=None, region=None):
                fr, lines = super().read(f)
                if self.offset != self.bottom_offset:
                    body = [l for l in lines if l not in (PLACEHOLDER, TOOLBAR)]
                    body[-1] = dict(body[-1], text=body[-1]["text"][:3])        # cut by the edge
                    lines = body + [{"text": "↓", "box": [2200, 700, 30, 30]}, PLACEHOLDER, TOOLBAR]
                return fr, lines

        screen = Messy(DOC)
        r = rdp_chat.read_full(screen, ScrollSender(screen), PROMPT, int(W * 0.22), W, H, sleep=no_sleep)
        self.assertEqual(r["text"].splitlines(), [t for t, _ in ITEMS])
        self.assertFalse(r["gaps"])

    def test_slow_repaint_is_not_mistaken_for_the_top_or_for_restored(self):
        # real remote: one read right after a wheel can still show the old view
        class OneStaleRead(FakeScrollScreen):
            def __init__(self, doc):
                super().__init__(doc)
                self.pending_stale = None

            def scroll(self, notches):
                self.pending_stale = self.offset   # the next read still shows this
                super().scroll(notches)

            def read(self, f=None, region=None):
                if self.pending_stale is not None:
                    shown, self.pending_stale = self.pending_stale, None
                    real = self.offset
                    self.offset = shown
                    try:
                        return super().read(f)
                    finally:
                        self.offset = real
                return super().read(f)

        screen = OneStaleRead(DOC)
        r = rdp_chat.read_full(screen, ScrollSender(screen), PROMPT, int(W * 0.22), W, H, sleep=no_sleep)
        self.assertTrue(r["prompt_visible"])
        self.assertFalse(r["top_reached"])
        self.assertTrue(r["restored"])
        self.assertEqual(screen.offset, screen.bottom_offset)
        self.assertEqual(r["text"].splitlines(), [t for t, _ in ITEMS])

    def test_restores_even_when_bursts_lose_notches(self):
        # real remote: a 24-notch wheel-down burst moved only a third as far as 8 x 3-notch steps up
        class Lossy(FakeScrollScreen):
            def scroll(self, notches):
                if notches < -3:
                    notches = -3                     # only the first 3 notches of a burst count
                super().scroll(notches)

        screen = Lossy(DOC)
        r = rdp_chat.read_full(screen, ScrollSender(screen), PROMPT, int(W * 0.22), W, H, sleep=no_sleep)
        self.assertTrue(r["restored"])
        self.assertEqual(screen.offset, screen.bottom_offset)

    def test_restore_is_not_claimed_when_the_view_did_not_come_back(self):  # negative control
        class Stuck(FakeScrollScreen):
            def scroll(self, notches):
                if notches > 0:                      # can scroll up but never back down
                    super().scroll(notches)

        screen = Stuck(DOC)
        r = rdp_chat.read_full(screen, ScrollSender(screen), PROMPT, int(W * 0.22), W, H, sleep=no_sleep)
        self.assertFalse(r["restored"])

    def test_chat_scrolls_back_when_the_prompt_left_the_screen(self):
        screen = FakeScrollScreen(DOC)
        send = ScrollSender(screen)
        r = run(screen, send)
        self.assertEqual(r["reply"].splitlines(), [t for t, _ in ITEMS])
        self.assertEqual(r["scrollback"]["gaps"], False)
        self.assertIn("--wheel", [c for c, _ in send.log])


class FakeStreamScreen(FakeScrollScreen):
    """After ENTER the prompt appears, then the reply streams in `per_frame` lines per capture while
    the view stays pinned to the bottom (like OpenCode). Every capture looks different until done."""

    def __init__(self, before, reply, per_frame=3, view=12, subtle_end=False, ocr_cost_frames=0):
        super().__init__(list(before), view=view)
        self.pending = [(PROMPT, 2800)] + list(reply)
        self.per_frame = per_frame
        self.streaming = False
        self.subtle_end = subtle_end      # the last addition does not change the frame visibly
        self.ocr_cost_frames = ocr_cost_frames   # the stream moves on this many captures per OCR
        self.ocr_calls = 0
        self.answering = False

    def read_snapshot(self, snap):
        # A slow OCR: wait (bounded) until the capture thread has taken `ocr_cost_frames` more frames,
        # i.e. far more than a screenful has streamed past while this one frame was being read.
        self.ocr_calls += 1
        start, deadline = self.captures, time.monotonic() + 1.0
        while (self.ocr_cost_frames and self.pending and self.captures < start + self.ocr_cost_frames
               and time.monotonic() < deadline):
            time.sleep(0.001)
        return snap

    def capture(self):
        shade_before = 50 + (len(self.doc) * 10) % 150
        if self.streaming and not self.answering:
            self.answering = True         # the model needs a moment: the capture right after ENTER
            return frame(shade_before)    # (the baseline) still shows the old screen
        if self.streaming and self.pending:
            last_step = len(self.pending) <= self.per_frame
            self.doc += self.pending[:self.per_frame]
            del self.pending[:self.per_frame]
            self.offset = self.bottom_offset = max(0, len(self.doc) - self.view)
            if last_step and self.subtle_end:
                return frame(shade_before)
        return frame(50 + (len(self.doc) * 10) % 150)


class StreamSender(ScrollSender):
    def __call__(self, command, value):
        super().__call__(command, value)
        if command == "--key" and value == "ENTER":
            self.screen.streaming = True


STREAM_REPLY = [(f"{i}. 數值 {100 + i}", 1362) for i in range(1, 61)] + [("Build · DeepSeek-V4-Flash · 4s", 1362)]
BEFORE = [(f"更早的訊息 {i}", 1362) for i in range(8)]


class FollowStreamTests(unittest.TestCase):
    def test_long_reply_is_collected_while_streaming_without_scrolling(self):
        screen = FakeStreamScreen(BEFORE, STREAM_REPLY, per_frame=3)
        send = StreamSender(screen)
        r = run(screen, send)
        self.assertEqual(r["mode"], "follow")
        self.assertEqual(r["reply"].splitlines(), [t for t, _ in STREAM_REPLY[:-1]])  # all 60, in order
        self.assertEqual(r["footer"], "Build · DeepSeek-V4-Flash · 4s")
        self.assertNotIn("--wheel", [c for c, _ in send.log])                         # no scrolling at all
        self.assertFalse(r["follow"]["gaps"])

    def test_stream_faster_than_ocr_falls_back_to_scrolling(self):  # negative control
        screen = FakeStreamScreen(BEFORE, STREAM_REPLY, per_frame=30)  # 30 new lines > 12-line view
        send = StreamSender(screen)
        r = run(screen, send)
        self.assertTrue(r["follow"]["gaps"])
        self.assertEqual(r["mode"], "scrollback")
        self.assertIn("--wheel", [c for c, _ in send.log])
        self.assertEqual(r["reply"].splitlines(), [t for t, _ in STREAM_REPLY[:-1]])

    def test_gaps_are_reported_when_scrolling_is_not_allowed(self):  # negative control
        screen = FakeStreamScreen(BEFORE, STREAM_REPLY, per_frame=30)
        send = StreamSender(screen)
        r = run(screen, send, scrollback=False)
        self.assertTrue(r["follow"]["gaps"])
        self.assertFalse(r["prompt_visible"])                 # honest: not the full reply
        self.assertNotIn("--wheel", [c for c, _ in send.log])

    def test_end_of_reply_with_a_tiny_visual_change_is_not_lost(self):
        # the change detector may not fire for the last few lines/footer; the final frame is re-read
        screen = FakeStreamScreen(BEFORE, STREAM_REPLY, per_frame=3, subtle_end=True)
        r = run(screen, StreamSender(screen))
        self.assertEqual(r["reply"].splitlines()[-1], "60. 數值 160")
        self.assertEqual(r["footer"], "Build · DeepSeek-V4-Flash · 4s")

    def test_short_reply_is_unaffected(self):
        screen = FakeStreamScreen(BEFORE, [("OK 4821", 1362)], per_frame=3)
        r = run(screen, StreamSender(screen))
        self.assertEqual((r["mode"], r["reply"]), ("follow", "OK 4821"))

    def test_stream_faster_than_ocr_is_caught_up_from_the_queue(self):
        # 4 new lines per capture and 6 captures per OCR: 24 lines stream past while one frame is
        # read, twice the 12-line view. Reading only the newest frame would lose lines (the old
        # capture->OCR loop did, on the real remote); the queue still holds every step.
        screen = FakeStreamScreen(BEFORE, STREAM_REPLY, per_frame=4, ocr_cost_frames=6)
        send = StreamSender(screen)
        r = run(screen, send)
        self.assertEqual(r["mode"], "follow")
        self.assertEqual(r["reply"].splitlines(), [t for t, _ in STREAM_REPLY[:-1]])
        self.assertFalse(r["follow"]["gaps"])
        self.assertNotIn("--wheel", [c for c, _ in send.log])
        self.assertEqual(r["follow"]["left_in_queue"], 0)                  # every snapshot released
        self.assertLess(r["follow"]["ocr_frames"], r["follow"]["snapshots"])  # and some skipped unread

    def test_capture_failure_while_waiting_is_raised(self):  # negative control
        class Broken(FakeStreamScreen):
            def capture(self):
                if self.streaming:
                    raise RuntimeError("client window closed")
                return super().capture()

        screen = Broken(BEFORE, STREAM_REPLY)
        with self.assertRaisesRegex(RuntimeError, "client window closed"):
            run(screen, StreamSender(screen))


class QueueStub:
    """Stands in for Capturer: a fixed queue of snapshots (each one the visible lines of a frame)."""

    def __init__(self, snaps):
        self.queue = collections.deque(snaps)

    def peek(self, n):
        return list(itertools.islice(self.queue, n))

    def drop(self, n):
        for _ in range(n):
            self.queue.popleft()


class SnapReader:
    def __init__(self):
        self.calls = 0

    def read_snapshot(self, snap):
        self.calls += 1
        return snap


def stream_frames(per_frame, n=60, view=12):
    """Snapshots of a reply streaming `per_frame` lines per frame into a `view`-line pinned view."""
    doc = BEFORE + [(PROMPT, 2800)] + [(f"{i}. 數值 {100 + i}", 1362) for i in range(1, n + 1)]
    out = []
    for end in list(range(len(BEFORE) + 1, len(doc), per_frame)) + [len(doc)]:
        vis = doc[max(0, end - view):end]
        out.append([{"text": t, "box": [x, 300 + i * 60, 400, 40]} for i, (t, x) in enumerate(vis)])
    return out, [t for t, _ in doc[len(BEFORE) + 1:]]


class FollowerTests(unittest.TestCase):
    """The adaptive skip, deterministically (no threads, no timing)."""

    def follow(self, frames):
        reader = SnapReader()
        fol = rdp_chat.Follower(reader, 1000, H, time.monotonic())
        q = QueueStub(frames)
        while fol.consume(q):
            pass
        got, visible = rdp_chat.reply_from_anchor(
            [{k: v for k, v in l.items() if k != "gy"} for l in fol.transcript], PROMPT)
        return fol, reader, [l["text"] for l in got], visible

    def test_slow_stream_reads_few_frames_and_misses_nothing(self):
        frames, expected = stream_frames(per_frame=1)          # lots of overlap between frames
        fol, reader, got, visible = self.follow(frames)
        self.assertTrue(visible)
        self.assertEqual(got, expected)
        self.assertFalse(fol.gaps)
        self.assertLess(reader.calls, len(frames) / 2)          # skipped most frames unread

    def test_step_backs_off_when_a_jump_is_too_far(self):
        frames, expected = stream_frames(per_frame=5)          # 3 frames ahead = 15 lines > 12 view
        fol, reader, got, _ = self.follow(frames)
        self.assertEqual(got, expected)
        self.assertFalse(fol.gaps)
        self.assertTrue(any(p.get("ok") is False and p["ahead"] > 1 for p in fol.pages))  # tried, backed off

    def test_gap_only_when_even_the_next_frame_does_not_overlap(self):  # negative control
        frames, _ = stream_frames(per_frame=13)                # every frame jumps more than the view
        fol, _, _, _ = self.follow(frames)
        self.assertTrue(fol.gaps)


class StitchGuardTests(unittest.TestCase):
    def test_repeated_lines_alone_do_not_register(self):  # negative control
        def page(texts, y0=300):
            return [{"text": t, "box": [1362, y0 + i * 60, 400, 40]} for i, t in enumerate(texts)]
        # the pages do not overlap at all; only the two "}" lines vote (for a 60 px shift)
        upper = [dict(l, gy=l["box"][1]) for l in page(["}", "}", "int a = 1;", "a += 2;", "return a;"])]
        lower = page(["void other() {", "}", "}", "x = 2;", "y = 3;", "z = 4;"])
        _, ok = rdp_chat.stitch(upper, lower, fresh=True)
        self.assertFalse(ok)        # the rest of the overlap disagrees with that shift

    def test_true_overlap_still_registers(self):
        def page(texts, y0=300):
            return [{"text": t, "box": [1362, y0 + i * 60, 400, 40]} for i, t in enumerate(texts)]
        upper = [dict(l, gy=l["box"][1]) for l in page(["a1", "a2", "a3", "a4"])]
        merged, ok = rdp_chat.stitch(upper, page(["a3", "a4", "a5", "a6"]), fresh=True)
        self.assertTrue(ok)
        self.assertEqual([l["text"] for l in merged], ["a1", "a2", "a3", "a4", "a5", "a6"])


class ScreenSnapshotTests(unittest.TestCase):
    def test_snapshot_is_lossless_compact_and_boxes_return_in_session_pixels(self):
        class StubOcr:
            def recognize(self, img, scale):
                self.img, self.scale = img, scale
                return {"lines": [{"text": "x", "box": [10, 20, 30, 40]}]}

        bgr = np.full((H // 2, W // 2, 3), 245, np.uint8)
        for i in range(12):
            cv2.putText(bgr, f"line {i} value {i * 37}", (520, 240 + i * 50), cv2.FONT_HERSHEY_SIMPLEX, 1.0,
                        (30, 30, 30), 2)
        f = rdp_capture.Frame(1, time.time(), (W, H), 0.5, bgr, 0, 0)
        screen = rdp_chat.Screen(None, StubOcr())
        snap = screen.snapshot(f, (1000, 400, 3000, 1800))
        lines = screen.read_snapshot(snap)
        crop = bgr[200:900, 500:1500]
        np.testing.assert_array_equal(screen.ocr.img, crop)       # PNG round trip is exact
        self.assertEqual(screen.ocr.scale, 0.5)
        self.assertEqual(lines[0]["box"], [1010, 420, 30, 40])     # crop origin added back
        self.assertLess(len(snap[0]) * 10, crop.nbytes)            # small enough to queue many


class ExtractReplyTests(unittest.TestCase):
    def test_sidebar_and_older_messages_are_excluded(self):
        r = rdp_chat.extract_reply(FINAL, PROMPT, panel_min_x=int(W * 0.22))
        self.assertEqual([l["text"] for l in r["lines"]], ["E2E-OK-4821"])

    def test_wrapped_prompt_tail_is_not_part_of_the_reply(self):
        # measured on the real remote (2560x1440): the bubble's 2nd line sat at x=1424, reply at 816
        lines = [{"text": "第三輪測試：請列出 1 到 60 的整數，每行一個，格式為「第N行」，不要執行任何工具", "box": [1424, 720, 1100, 35]},
                 {"text": "或指令。", "box": [1424, 763, 84, 35]},
                 {"text": "第1行第2行第3行", "box": [816, 931, 1427, 27]},
                 {"text": "随便问点什么...", "box": [818, 1190, 158, 34]}]
        r = rdp_chat.extract_reply(lines, "第三輪測試：請列出 1 到 60 的整數，每行一個，格式為「第N行」，不要執行任何工具或指令。", 563)
        self.assertEqual(r["text"], "第1行第2行第3行")

    def test_reply_equal_to_part_of_the_prompt_is_kept(self):  # negative control
        # round 1: the reply "E2E-OK-4821" also occurs inside the prompt; it is left-aligned, keep it
        r = rdp_chat.extract_reply(FINAL, PROMPT, panel_min_x=int(W * 0.22))
        self.assertEqual(r["text"], "E2E-OK-4821")

    def test_prompt_scrolled_away_returns_whole_panel(self):
        lines = [SIDEBAR, {"text": "很長的回覆第 40 行", "box": [1362, 300, 500, 34]}, PLACEHOLDER]
        r = rdp_chat.extract_reply(lines, PROMPT, panel_min_x=int(W * 0.22))
        self.assertFalse(r["prompt_visible"])
        self.assertEqual(r["text"], "很長的回覆第 40 行")


if __name__ == "__main__":
    unittest.main()
