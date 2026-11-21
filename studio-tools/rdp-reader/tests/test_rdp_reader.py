"""Tests for rdp_capture / rdp_reader.

A FakeClient window in this process answers the same WM_COPYDATA kinds as wfreerdp
(studio_input.h), so the pipe protocol, failure paths and the reading loop are tested
without touching the live RDP session. Live/OCR-server tests skip when unavailable.
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import secrets
import struct
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import rdp_capture  # noqa: E402
import rdp_reader  # noqa: E402

PY_EXE = (os.path.basename(sys.executable),)
FONT = next((p for p in (r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\segoeui.ttf") if os.path.exists(p)), None)


# --------------------------------------------------------------------------- helpers
def make_bmp(bgra: np.ndarray) -> bytes:
    h, w = bgra.shape[:2]
    n = w * h * 4
    return (struct.pack("<HIHHI", 0x4D42, 54 + n, 0, 0, 54) +
            struct.pack("<IiiHHIIiiII", 40, w, -h, 1, 32, 0, n, 0, 0, 0, 0) + bgra.tobytes())


def text_frame(text: str, size=(1280, 720), at=(80, 120), font_px=56) -> np.ndarray:
    img = Image.new("RGB", size, "white")
    font = ImageFont.truetype(FONT, font_px) if FONT else ImageFont.load_default()
    ImageDraw.Draw(img).text(at, text, fill="black", font=font)
    rgb = np.asarray(img)
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGRA)


_u = ctypes.WinDLL("user32", use_last_error=True)
_k = ctypes.WinDLL("kernel32", use_last_error=True)
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", ctypes.c_void_p),
                ("hCursor", ctypes.c_void_p), ("hbrBackground", ctypes.c_void_p),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]


_u.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
_u.DefWindowProcW.restype = LRESULT
_u.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
_u.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                               ctypes.c_int, ctypes.c_int, wt.HWND, ctypes.c_void_p, wt.HINSTANCE, ctypes.c_void_p]
_u.CreateWindowExW.restype = wt.HWND
_u.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
_u.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
_u.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
_u.DestroyWindow.argtypes = [wt.HWND]
_k.GetModuleHandleW.restype = wt.HINSTANCE
WM_CLOSE, WM_DESTROY = 0x0010, 0x0002


class FakeClient:
    """Hidden window answering studio WM_COPYDATA kinds the way wfreerdp does.

    mode: ok | reject (returns 0) | lie (returns 1, never opens the pipe)
          | truncate (stream cut short) | garbage (not a BMP)
    """

    def __init__(self, frames, mode="ok"):
        self.frames = frames          # callable(capture_index) -> BGRA array
        self.mode = mode
        self.captures = 0
        self.title = f"fake-rdp-{secrets.token_hex(4)}"
        self.target = self.title + ":3389"
        self.hwnd = None
        self._ready = threading.Event()
        self._proc = WNDPROC(self._wndproc)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(5)

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == rdp_capture.WM_COPYDATA:
            cds = ctypes.cast(lparam, ctypes.POINTER(rdp_capture.COPYDATASTRUCT)).contents
            if cds.dwData == rdp_capture.KIND_STATUS:
                return 1003
            if cds.dwData == rdp_capture.KIND_CAPTURE:
                path = ctypes.string_at(cds.lpData, cds.cbData).decode("utf-8").rstrip("\0")
                return self._capture(path)
            return 0
        if msg == WM_CLOSE:
            _u.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            _u.PostQuitMessage(0)
            return 0
        return _u.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _capture(self, path):
        self.captures += 1
        if self.mode == "reject":
            return 0
        if self.mode == "lie":
            return 1
        data = make_bmp(self.frames(self.captures))
        if self.mode == "truncate":
            data = data[: len(data) // 2]
        elif self.mode == "garbage":
            data = b"NOT A BMP" * 100
        with open(path, "wb") as f:   # same as fopen(path, "wb") in the client
            f.write(data)
        return 1

    def _run(self):
        hinst = _k.GetModuleHandleW(None)
        wc = WNDCLASSW(lpfnWndProc=self._proc, hInstance=hinst, lpszClassName=self.title + "-cls")
        _u.RegisterClassW(ctypes.byref(wc))
        self.hwnd = _u.CreateWindowExW(0, wc.lpszClassName, self.title, 0, 0, 0, 0, 0, None, None, hinst, None)
        self._ready.set()
        msg = wt.MSG()
        while _u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            _u.TranslateMessage(ctypes.byref(msg))
            _u.DispatchMessageW(ctypes.byref(msg))

    def client(self, **kw):
        return rdp_capture.RdpClient(self.target, exe_names=PY_EXE, timeout_ms=3000, io_timeout_ms=3000, **kw)

    def close(self):
        _u.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
        self._thread.join(5)


def vl_server_up(url="http://127.0.0.1:8080"):
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url + "/health", timeout=2) as r:
            return json.load(r).get("status") == "ok"
    except Exception:
        return False


# --------------------------------------------------------------------------- capture
class TargetAndHeaderTests(unittest.TestCase):
    def test_parse_target_matches_sendrdpinput(self):
        self.assertEqual(rdp_capture.parse_target("172.30.251.1:3390"), "172.30.251.1")
        self.assertEqual(rdp_capture.parse_target("host"), "host")
        self.assertEqual(rdp_capture.parse_target("[fe80::1]:3389"), "fe80::1")

    def test_header_accepts_studio_bmp(self):
        bmp = make_bmp(np.zeros((4, 6, 4), np.uint8))
        self.assertEqual(rdp_capture.parse_bmp_header(bmp[:54]), (6, 4, 96))

    def test_header_rejects_bottom_up(self):  # negative control
        hdr = bytearray(make_bmp(np.zeros((4, 6, 4), np.uint8))[:54])
        struct.pack_into("<i", hdr, 22, 4)
        with self.assertRaisesRegex(rdp_capture.CaptureError, "top-down"):
            rdp_capture.parse_bmp_header(bytes(hdr))

    def test_header_rejects_24bit_and_size_mismatch(self):  # negative controls
        hdr = bytearray(make_bmp(np.zeros((4, 6, 4), np.uint8))[:54])
        bad_bpp = bytearray(hdr)
        struct.pack_into("<H", bad_bpp, 28, 24)
        with self.assertRaises(rdp_capture.CaptureError):
            rdp_capture.parse_bmp_header(bytes(bad_bpp))
        struct.pack_into("<I", hdr, 2, 54 + 95)
        with self.assertRaisesRegex(rdp_capture.CaptureError, "size field"):
            rdp_capture.parse_bmp_header(bytes(hdr))


class PipeCaptureTests(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(1)
        self.src = rng.integers(0, 256, (360, 640, 4), dtype=np.uint8)
        self.fakes = []

    def tearDown(self):
        for f in self.fakes:
            f.close()

    def fake(self, mode="ok"):
        f = FakeClient(lambda i: self.src, mode)
        self.fakes.append(f)
        return f

    def test_full_scale_roundtrip_is_exact(self):
        frame = self.fake().client().capture(1.0)
        self.assertEqual(frame.native_size, (640, 360))
        np.testing.assert_array_equal(frame.bgr, self.src[:, :, :3])

    def test_downsample_halves_and_averages(self):
        frame = self.fake().client().capture(0.5)
        self.assertEqual(frame.size, (320, 180))
        self.assertEqual(frame.native_size, (640, 360))
        expected = cv2.cvtColor(cv2.resize(self.src, (320, 180), interpolation=cv2.INTER_AREA), cv2.COLOR_BGRA2BGR)
        np.testing.assert_array_equal(frame.bgr, expected)

    def test_auto_scale_targets_width_and_never_upscales(self):
        c = self.fake().client()
        f = c.capture("auto", target_width=320)
        self.assertEqual((f.size, f.scale), ((320, 180), 0.5))
        f = c.capture("auto")  # 640 wide is already below 1920: keep native
        self.assertEqual((f.size, f.scale), ((640, 360), 1.0))
        with self.assertRaises(ValueError):  # negative control
            c.capture("big")

    def test_sequence_numbers_increase(self):
        c = self.fake().client()
        self.assertEqual([c.capture(0.5).seq for _ in range(3)], [1, 2, 3])

    def test_status(self):
        self.assertEqual(self.fake().client().status(),
                         {"connected": True, "unicode": True, "input_suspended": False})

    def test_rejected_capture_fails_fast(self):  # negative control
        t = time.perf_counter()
        with self.assertRaisesRegex(rdp_capture.CaptureError, "rejected"):
            self.fake("reject").client().capture()
        self.assertLess(time.perf_counter() - t, 1.5)

    def test_success_without_pipe_fails_fast(self):  # negative control: no hang
        t = time.perf_counter()
        with self.assertRaisesRegex(rdp_capture.CaptureError, "never opened"):
            self.fake("lie").client().capture()
        self.assertLess(time.perf_counter() - t, 1.5)

    def test_truncated_stream_detected(self):  # negative control
        with self.assertRaisesRegex(rdp_capture.CaptureError, "closed the pipe"):
            self.fake("truncate").client().capture()

    def test_garbage_stream_detected(self):  # negative control
        with self.assertRaisesRegex(rdp_capture.CaptureError, "not a studio capture BMP"):
            self.fake("garbage").client().capture()

    def test_unknown_title_finds_nothing(self):  # negative control
        with self.assertRaisesRegex(rdp_capture.CaptureError, "found 0"):
            rdp_capture.RdpClient("no-such-host-" + secrets.token_hex(3) + ":1", exe_names=PY_EXE).capture()

    def test_other_executables_are_never_targeted(self):  # negative control
        f = self.fake()
        with self.assertRaisesRegex(rdp_capture.CaptureError, "found 0"):
            rdp_capture.RdpClient(f.target).capture()  # default exe filter is wfreerdp.exe
        self.assertEqual(f.captures, 0)


# --------------------------------------------------------------------------- change detection
class ChangeDetectorTests(unittest.TestCase):
    def setUp(self):
        self.det = rdp_reader.ChangeDetector()
        self.base = np.full((1080, 1920, 3), 255, np.uint8)
        self.det.accept(self.det.thumb(self.base))

    def check(self, img):
        return self.det.compare(self.det.thumb(img), (3840, 2160))

    def test_identical_frame_is_unchanged(self):  # negative control
        self.assertFalse(self.check(self.base.copy())[0])

    def test_caret_sized_change_is_ignored(self):  # negative control
        img = self.base.copy()
        img[500:530, 900:902] = 0
        self.assertFalse(self.check(img)[0])

    def test_text_block_change_reports_native_bbox(self):
        img = self.base.copy()
        img[400:440, 600:1000] = 0
        changed, ratio, (x, y, w, h) = self.check(img)
        self.assertTrue(changed)
        self.assertGreater(ratio, 0.001)
        self.assertTrue(x <= 1200 and y <= 800 and x + w >= 2000 and y + h >= 880, (x, y, w, h))

    def test_gradual_changes_accumulate(self):
        img = self.base.copy()
        triggered = False
        for i in range(12):  # small step each time; reference stays at the last processed frame
            img[100 + i * 6:104 + i * 6, 100:400] = 0
            triggered = triggered or self.check(img)[0]
        self.assertTrue(triggered)


class TidyTests(unittest.TestCase):
    def test_removes_gaps_between_cjk_only(self):
        self.assertEqual(rdp_reader.tidy_cjk("Q 搜 索 opencode"), "Q 搜索 opencode")
        self.assertEqual(rdp_reader.tidy_cjk("Read rest of runner"), "Read rest of runner")
        self.assertEqual(rdp_reader.tidy_cjk("新 建 会 话 ： ok"), "新建会话： ok")  # CJK-Latin gap kept


# --------------------------------------------------------------------------- OCR engines
class WinOcrTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ocr = rdp_reader.WinOcr("en-US")

    @classmethod
    def tearDownClass(cls):
        cls.ocr.close()

    def job(self, bgra, scale=0.5):
        bgr = rdp_capture.downsample(bgra, scale)
        frame = rdp_capture.Frame(1, time.time(), (bgra.shape[1], bgra.shape[0]), scale, bgr, 0, 0)
        return rdp_reader.Job(frame, None, 1.0)

    def test_reads_text_and_maps_boxes_to_native_pixels(self):
        result = self.ocr(self.job(text_frame("Hello OCR 12345", at=(200, 300))))
        self.assertIn("12345", result["text"])
        self.assertIn("Hello", result["text"])
        x, y, w, h = result["lines"][0]["box"]
        self.assertTrue(180 <= x <= 230 and 290 <= y <= 340, (x, y))  # native coords, not halved

    def test_blank_frame_yields_no_lines(self):  # negative control
        result = self.ocr(self.job(np.full((720, 1280, 4), 255, np.uint8)))
        self.assertEqual(result["lines"], [])

    def test_consecutive_frames_are_never_stale(self):
        # Regression: re-reading one overwritten PNG through StorageFile failed or went stale.
        seen = [self.ocr(self.job(text_frame(t)))["text"] for t in ("FIRST 1357", "SECOND 8642", "FIRST 1357")]
        self.assertIn("1357", seen[0])
        self.assertIn("8642", seen[1])
        self.assertNotIn("1357", seen[1])
        self.assertIn("1357", seen[2])

    def test_bad_payload_is_reported_not_hung(self):  # negative control
        with self.assertRaisesRegex(RuntimeError, "Gray8 bytes"):
            self.ocr._request({"w": 10, "h": 10, "scale": 1}, "AAAA")
        self.assertIn("1357", self.ocr(self.job(text_frame("AFTER 1357")))["text"])  # worker still alive


@unittest.skipUnless(vl_server_up(), "PaddleOCR-VL server not running on 127.0.0.1:8080")
class VlOcrTests(unittest.TestCase):
    def test_reads_rendered_text(self):
        bgr = rdp_capture.downsample(text_frame("PaddleOCR 2468 check"), 0.5)
        result = rdp_reader.VlOcr(max_tokens=64).recognize_image(bgr)
        self.assertIn("2468", result["text"])
        self.assertFalse(result["truncated"])


# --------------------------------------------------------------------------- reading loop
class ReaderLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def events(self):
        with open(os.path.join(self.tmp.name, "events.jsonl"), encoding="utf-8") as f:
            return [json.loads(l) for l in f]

    def test_only_changed_frames_reach_ocr(self):
        alpha, bravo = text_frame("ALPHA 1111"), text_frame("BRAVO 2222")
        fake = FakeClient(lambda i: alpha if i <= 2 else bravo)  # A, A, B, B
        try:
            args = rdp_reader.parse_args(["--target", fake.target, "--out", self.tmp.name, "--engine", "win",
                                          "--win-lang", "en-US", "--interval", "0.15", "--max-frames", "4",
                                          "--quiet"])
            summary = rdp_reader.run(args, client=fake.client())
        finally:
            fake.close()
        ev = self.events()
        frames = [e["seq"] for e in ev if e["type"] == "frame"]
        ocr = {e["seq"]: e["text"] for e in ev if e["type"] == "ocr"}
        self.assertEqual(frames, [1, 3])                  # seq 2 and 4 unchanged -> no OCR
        self.assertEqual(summary["changed"], 2)
        self.assertTrue(set(ocr) <= {1, 3} and 3 in ocr)  # latest-wins may drop 1, never 3
        self.assertIn("2222", ocr[3])
        with open(os.path.join(self.tmp.name, "latest_win.txt"), encoding="utf-8") as f:
            self.assertIn("2222", f.read())
        self.assertEqual(len(os.listdir(os.path.join(self.tmp.name, "frames"))), 2)

    def test_capture_errors_are_recorded_once_and_loop_continues(self):  # negative control
        fake = FakeClient(lambda i: None, mode="reject")
        try:
            args = rdp_reader.parse_args(["--target", fake.target, "--out", self.tmp.name, "--engine", "none",
                                          "--interval", "0.05", "--max-frames", "5", "--quiet"])
            summary = rdp_reader.run(args, client=fake.client())
        finally:
            fake.close()
        self.assertEqual(summary["capture_errors"], 5)
        self.assertEqual(sum(e["type"] == "capture_error" for e in self.events()), 1)

    @unittest.skipUnless(os.path.exists(r"C:\Studio\vlm\ppocrv6\small_rec\inference.onnx"), "PP-OCRv6 not installed")
    def test_default_engine_is_ppocr_and_reads_changed_frames(self):
        alpha, bravo = text_frame("ALPHA 1111"), text_frame("BRAVO 2222")
        fake = FakeClient(lambda i: alpha if i <= 2 else bravo)
        try:
            args = rdp_reader.parse_args(["--target", fake.target, "--out", self.tmp.name, "--ppocr-device", "cpu",
                                          "--interval", "0.15", "--max-frames", "4", "--quiet"])
            self.assertEqual(args.engines, ["ppocr"])
            rdp_reader.run(args, client=fake.client())
        finally:
            fake.close()
        ev = self.events()
        self.assertEqual(ev[0]["engines"], {"ppocr": {"device": "cpu"}})
        ocr = {e["seq"]: e for e in ev if e["type"] == "ocr"}
        self.assertIn(3, ocr)
        self.assertIn("2222", ocr[3]["text"])
        self.assertTrue(all(len(l["box"]) == 4 for l in ocr[3]["lines"]))
        self.assertNotIn(2, ocr)  # unchanged frame never reaches OCR (negative control)


class ScaleArgTests(unittest.TestCase):
    def test_scale_defaults_to_auto_and_accepts_factors(self):
        self.assertEqual(rdp_reader.parse_args([]).scale, "auto")
        self.assertEqual(rdp_reader.parse_args(["--scale", "0.75"]).scale, 0.75)

    def test_bad_scale_rejected(self):  # negative control
        for bad in ("x", "0", "1.5"):
            with self.assertRaises(SystemExit):
                rdp_reader.parse_args(["--scale", bad])


class EngineListTests(unittest.TestCase):
    def test_parsing(self):
        self.assertEqual(rdp_reader.engine_list("ppocr"), ["ppocr"])
        self.assertEqual(rdp_reader.engine_list("ppocr, vl,ppocr"), ["ppocr", "vl"])
        self.assertEqual(rdp_reader.engine_list("both"), ["win", "vl"])  # old command lines still work
        self.assertEqual(rdp_reader.engine_list("none"), [])

    def test_unknown_engine_rejected(self):  # negative control
        with self.assertRaisesRegex(ValueError, "unknown engine"):
            rdp_reader.engine_list("ppocr,tesseract")


class CutRepetitionTests(unittest.TestCase):
    def test_single_char_loop_is_cut(self):  # the failure seen on 4/27 synthetic pages
        text, cut = rdp_reader.cut_repetition("本地磁盘 (C:)\n隨便同點" + "點" * 600)
        self.assertTrue(cut)
        self.assertEqual(text, "本地磁盘 (C:)\n隨便同點")

    def test_multi_char_loop_stopping_mid_unit_is_cut(self):
        text, cut = rdp_reader.cut_repetition("header\n" + "ab12 " * 80 + "ab")
        self.assertTrue(cut)
        self.assertEqual(text, "header\nab12 ")

    def test_normal_text_is_untouched(self):  # negative control
        normal = "gating ON 58.50 58.13 59.36\n========\n0000 1111"
        self.assertEqual(rdp_reader.cut_repetition(normal), (normal, False))


# --------------------------------------------------------------------------- live session
LIVE_TARGET = os.environ.get("RDP_READER_LIVE_TARGET", "172.30.251.1:3390")


@unittest.skipUnless(len(rdp_capture.find_client_windows(rdp_capture.parse_target(LIVE_TARGET))) == 1,
                     "no live wfreerdp window for " + LIVE_TARGET)
class LiveTests(unittest.TestCase):
    def test_live_capture_is_downsampled_and_fast(self):
        c = rdp_capture.RdpClient(LIVE_TARGET)
        self.assertTrue(c.status()["connected"])
        frame = c.capture(0.5)
        w, h = frame.native_size
        self.assertEqual(frame.size, (round(w * 0.5), round(h * 0.5)))
        self.assertLess(frame.capture_ms, 1000)
        self.assertGreater(float(frame.bgr.std()), 1.0)  # not a blank buffer


if __name__ == "__main__":
    unittest.main()
