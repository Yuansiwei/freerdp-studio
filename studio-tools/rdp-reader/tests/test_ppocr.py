"""Tests for the PP-OCRv6 engine (ppocr.py). GPU tests skip when CUDA is not usable."""
import os
import sys
import tempfile
import time
import unittest
import unicodedata

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ppocr  # noqa: E402
import rdp_capture  # noqa: E402

MODELS = all(os.path.exists(os.path.join(ppocr.DEFAULT_MODEL_DIR, f"small_{p}", f))
             for p in ("det", "rec") for f in ("inference.onnx", "inference.yml"))
CJK_FONT = r"C:\Windows\Fonts\msjh.ttc"
MONO_FONT = r"C:\Windows\Fonts\consola.ttf"


def page(lines, size=(2400, 1000), px=56):
    """Native-resolution page (like a 4K session at 200%); returns BGR. lines: [(x, y, text, font)]."""
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    for x, y, text, font in lines:
        d.text((x, y), text, fill=(20, 20, 20), font=ImageFont.truetype(font, px))
    return cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2BGR)


def half(bgr):
    return cv2.resize(bgr, (bgr.shape[1] // 2, bgr.shape[0] // 2), interpolation=cv2.INTER_AREA)


def norm(s):
    return "".join(unicodedata.normalize("NFKC", s).split())


SAMPLE = [(80, 80, "確認顯示：遠端桌面已連線", CJK_FONT),
          (80, 240, "cmTopLevel.cpp:79 skipped=620000 9.95%", MONO_FONT),
          (80, 400, "OK 12", CJK_FONT),
          (80, 560, "for (int i = 0; i < n; ++i) sum += a[i] * b[i]; // 1,472,889 B", MONO_FONT)]


class BucketAndConfigTests(unittest.TestCase):
    def test_pick_bucket(self):
        self.assertEqual([ppocr.pick_bucket(w) for w in (1, 320, 321, 1000, 3840)], [320, 320, 480, 1280, 3840])
        self.assertIsNone(ppocr.pick_bucket(3841))  # negative control: too wide -> no bucket

    def test_bad_device_value_rejected(self):  # negative control
        with self.assertRaises(ValueError):
            ppocr.PPOCR(device="tpu")

    def test_missing_models_raise_clearly(self):  # negative control
        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaisesRegex(FileNotFoundError, "model file missing"):
                ppocr.PPOCR(model_dir=empty, device="cpu")

    def test_quiet_stderr_hides_only_inside(self):
        with tempfile.TemporaryFile() as log:
            saved = os.dup(2)
            os.dup2(log.fileno(), 2)
            try:
                with ppocr.quiet_stderr():
                    os.write(2, b"HIDDEN\n")
                os.write(2, b"SHOWN\n")
            finally:
                os.dup2(saved, 2)
                os.close(saved)
            log.seek(0)
            data = log.read()
        self.assertIn(b"SHOWN", data)
        self.assertNotIn(b"HIDDEN", data)

    @unittest.skipUnless(MODELS, "PP-OCRv6 small models not installed")
    def test_auto_falls_back_to_cpu_when_cuda_unusable(self):  # negative control
        real = ppocr.cuda_unavailable_reason
        ppocr.cuda_unavailable_reason = lambda probe: "no CUDA (simulated)"
        try:
            eng = ppocr.PPOCR(device="auto")
            self.assertEqual(eng.device, "cpu")
            self.assertEqual(eng.fallback_reason, "no CUDA (simulated)")
            with self.assertRaisesRegex(RuntimeError, "GPU requested but unusable"):
                ppocr.PPOCR(device="gpu")
        finally:
            ppocr.cuda_unavailable_reason = real


@unittest.skipUnless(MODELS, "PP-OCRv6 small models not installed")
class CpuEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eng = ppocr.PPOCR(device="cpu")

    def test_reads_traditional_chinese_and_code(self):
        r = self.eng.recognize(half(page(SAMPLE)), scale=0.5)
        text = norm(r["text"])
        for expected in ("確認顯示：遠端桌面已連線", "cmTopLevel.cpp:79", "skipped=620000", "9.95%", "1,472,889B"):
            self.assertIn(norm(expected), text)
        self.assertEqual(r["device"], "cpu")

    def test_boxes_are_in_native_pixels(self):
        r = self.eng.recognize(half(page(SAMPLE)), scale=0.5)
        first = next(l for l in r["lines"] if "遠端" in l["text"])
        x, y, w, h = first["box"]
        self.assertTrue(60 <= x <= 110 and 60 <= y <= 120, first["box"])  # not halved
        self.assertGreater(w, 500)

    def test_blank_frame_yields_nothing(self):  # negative control
        r = self.eng.recognize(np.full((540, 960, 3), 255, np.uint8))
        self.assertEqual((r["text"], r["lines"]), ("", []))


GPU = MODELS and ppocr.cuda_unavailable_reason(
    os.path.join(ppocr.DEFAULT_MODEL_DIR, "small_det", "inference.onnx")) == ""


@unittest.skipUnless(GPU, "CUDA not usable for onnxruntime")
class GpuEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gpu = ppocr.PPOCR(device="gpu")
        cls.cpu = ppocr.PPOCR(device="cpu")

    def test_gpu_reads_the_same_text_as_cpu(self):
        img = half(page(SAMPLE))
        g, c = self.gpu.recognize(img, 0.5), self.cpu.recognize(img, 0.5)
        self.assertEqual(g["device"], "gpu")
        self.assertEqual(len(g["lines"]), len(c["lines"]))
        for gl, cl in zip(g["lines"], c["lines"]):
            self.assertEqual(norm(gl["text"]), norm(cl["text"]))

    def test_repeated_frames_reuse_fixed_shape_sessions(self):
        # What keeps the GPU fast is that no session ever sees a new input shape (~750 ms each on
        # this VM). Check that structurally - wall time depends on whatever else loads the machine
        # (a 1.5 s bound failed while another job was compiling) - and keep only a loose time cap.
        img = half(page(SAMPLE))
        self.gpu.recognize(img, 0.5)
        rec = self.gpu.engine.text_rec.session
        before = {b: id(s) for b, s in rec.sessions.items()}
        t = time.perf_counter()
        for _ in range(3):
            self.gpu.recognize(img, 0.5)
        self.assertEqual({b: id(s) for b, s in rec.sessions.items()}, before)   # no new sessions
        self.assertLess((time.perf_counter() - t) / 3, 5.0)

    def test_oversized_batches_fall_back_to_cpu(self):  # negative control
        rec = self.gpu.engine.text_rec.session
        before = dict(rec.sessions)
        for shape in ((1, 3, 48, 4000), (7, 3, 48, 320)):
            out = rec(np.zeros(shape, np.float32))[0]
            self.assertEqual(out.shape[0], shape[0])
        self.assertEqual(rec.sessions, before)  # no new GPU session for unusable shapes


if __name__ == "__main__":
    unittest.main()
