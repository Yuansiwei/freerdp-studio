"""PP-OCRv6 engine for rdp-reader: the official ONNX models, run through RapidOCR's pre/post-processing.

Why PP-OCRv6 small (measured 2026-11-18 on real remote frames and 27 synthetic pages):
  lowest error of every engine tried (synthetic CER 1.07%, real frames 0-0.9%), gives a box per line,
  7.7M parameters. PaddleOCR-VL looped on 4/27 pages; v6 medium was no more accurate and 3x slower.

GPU path. onnxruntime's CUDA EP pays ~750 ms whenever a session's input shape differs from its previous
run (measured on this Hyper-V GPU-P VM; the same shape repeated costs ~30 ms), and RapidOCR batches 6
text lines at the width of the longest one, so the shape changes almost every batch. Hence:
  * det runs on CUDA as-is: frames have a constant size, so its shape never changes;
  * rec runs through one session per width bucket, every batch padded to (6, 3, 48, bucket), and the
    output time steps that only saw padding are sliced off again.
Measured per 1920x1080 frame: CPU 1.33 s -> GPU 0.61 s, same accuracy on real frames.
CPU path: plain RapidOCR; 2-4 threads are as fast as 12 here, so the default leaves the CPU free.
"""
from __future__ import annotations

import contextlib
import ctypes
import glob
import importlib.util
import msvcrt
import os
import sys
import tempfile
import threading
import time

import numpy as np
import onnxruntime as ort
import yaml
from rapidocr_onnxruntime import RapidOCR

DEFAULT_MODEL_DIR = r"C:\Studio\vlm\ppocrv6"
REC_BATCH = 6          # RapidOCR's rec_batch_num
REC_HEIGHT = 48
BUCKETS = (320, 480, 640, 960, 1280, 1920, 2560, 3840)
CUDA_OPTIONS = {"cudnn_conv_algo_search": "HEURISTIC"}  # RapidOCR hardcodes EXHAUSTIVE (slow per new shape)


def pick_bucket(width: int):
    return next((b for b in BUCKETS if b >= width), None)


_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.GetStdHandle.restype = ctypes.c_void_p
_k32.SetStdHandle.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
_quiet_lock = threading.RLock()
STD_ERROR_HANDLE = 0xFFFFFFF4  # (DWORD)-12


@contextlib.contextmanager
def quiet_stderr():
    """Silence native stderr (C runtime fd 2 and the Win32 std handle) for the duration."""
    with _quiet_lock:
        try:
            sys.stderr.flush()
        except Exception:
            pass
        try:
            saved_fd = os.dup(2)
        except OSError:
            saved_fd = None
        null_fd = os.open(os.devnull, os.O_WRONLY)
        saved_handle = _k32.GetStdHandle(STD_ERROR_HANDLE)
        try:
            if saved_fd is not None:
                os.dup2(null_fd, 2)
            _k32.SetStdHandle(STD_ERROR_HANDLE, msvcrt.get_osfhandle(null_fd))
            yield
        finally:
            _k32.SetStdHandle(STD_ERROR_HANDLE, saved_handle)
            if saved_fd is not None:
                os.dup2(saved_fd, 2)
                os.close(saved_fd)
            os.close(null_fd)


def cuda_dll_dir():
    """Where CUDA 12 / cuDNN 9 DLLs live: $RDP_READER_CUDA_DLLS, else torch\\lib (without importing torch)."""
    env = os.environ.get("RDP_READER_CUDA_DLLS")
    if env:
        return env
    spec = importlib.util.find_spec("torch")
    if spec and spec.origin:
        lib = os.path.join(os.path.dirname(spec.origin), "lib")
        if glob.glob(os.path.join(lib, "cublasLt64_12.dll")):
            return lib
    return None


_cuda_lock = threading.Lock()
_cuda_reason = None  # None = not probed yet, "" = usable, text = why not


def cuda_unavailable_reason(probe_model: str) -> str:
    """'' when a CUDA session can really be created, otherwise a human-readable reason (cached)."""
    global _cuda_reason
    with _cuda_lock:
        if _cuda_reason is not None:
            return _cuda_reason
        if "CUDAExecutionProvider" not in ort.get_available_providers():
            _cuda_reason = "onnxruntime has no CUDAExecutionProvider (install onnxruntime-gpu)"
            return _cuda_reason
        dll_dir = cuda_dll_dir()
        try:
            if dll_dir and hasattr(ort, "preload_dlls"):
                # NVRTC is deliberately NOT loaded: with it, cuDNN JIT-compiles engines and startup
                # grows from ~5 s to ~13 s for no per-frame gain (measured). Without it cuDNN prints
                # "Could not locate nvrtc64_120_0.dll" on stderr for each new shape -> quiet_stderr().
                ort.preload_dlls(directory=dll_dir)
            with quiet_stderr():
                s = _session(probe_model, cuda=True)
            _cuda_reason = "" if s.get_providers()[0] == "CUDAExecutionProvider" else \
                f"CUDA session fell back to CPU (CUDA 12/cuDNN 9 DLLs not found; looked in {dll_dir or 'PATH'})"
        except Exception as exc:
            _cuda_reason = f"CUDA init failed: {type(exc).__name__}: {exc}"
        return _cuda_reason


def _session(path: str, cuda: bool, threads: int | None = None):
    so = ort.SessionOptions()
    so.log_severity_level = 3
    if threads:
        so.intra_op_num_threads = threads
    providers = [("CUDAExecutionProvider", CUDA_OPTIONS), "CPUExecutionProvider"] if cuda else ["CPUExecutionProvider"]
    return ort.InferenceSession(path, so, providers=providers)


class _CudaSession:
    """Stand-in for RapidOCR's OrtInferSession (called as session(x) -> list of outputs)."""

    def __init__(self, path: str):
        self.session = _session(path, cuda=True)
        self.input = self.session.get_inputs()[0].name

    def __call__(self, x):
        return self.session.run(None, {self.input: x})


class _BucketedRec:
    """Rec on CUDA with one session per width bucket, so no session ever sees a new shape."""

    def __init__(self, path: str, cpu_session):
        self.path = path
        self.cpu = cpu_session      # RapidOCR's own session: fallback + dictionary accessors
        self.sessions = {}
        self.input = None
        self.lock = threading.Lock()

    def session(self, bucket: int):
        with self.lock:
            if bucket not in self.sessions:
                with quiet_stderr():
                    s = _CudaSession(self.path)
                    s(np.zeros((REC_BATCH, 3, REC_HEIGHT, bucket), np.float32))  # pay the first-shape cost now
                self.sessions[bucket] = s
            return self.sessions[bucket]

    def warm(self):
        for b in BUCKETS:
            self.session(b)

    def __call__(self, x):
        n, c, h, w = x.shape
        bucket = pick_bucket(w)
        if bucket is None or n > REC_BATCH or h != REC_HEIGHT:
            return self.cpu(x)  # unusual shape: correctness first
        padded = np.zeros((REC_BATCH, c, h, bucket), np.float32)
        padded[:n, :, :, :w] = x
        out = self.session(bucket)(padded)[0][:n]
        steps = int(np.ceil(out.shape[1] * w / bucket))
        return [out[:, :steps]]

    def __getattr__(self, name):
        if name == "cpu":
            raise AttributeError(name)
        return getattr(self.cpu, name)


def _dictionary(rec_dir: str) -> str:
    """RapidOCR wants a keys file; PP-OCRv6 ships the dictionary inside inference.yml."""
    for path in (os.path.join(rec_dir, "dict.txt"),
                 os.path.join(tempfile.gettempdir(), "rdp-reader", os.path.basename(rec_dir) + "-dict.txt")):
        if os.path.exists(path):
            return path
        try:
            with open(os.path.join(rec_dir, "inference.yml"), encoding="utf-8") as f:
                chars = yaml.safe_load(f)["PostProcess"]["character_dict"]
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write("\n".join(str(ch) for ch in chars) + "\n")
            return path
        except OSError:
            continue
    raise OSError(f"cannot write the recognition dictionary for {rec_dir}")


class PPOCR:
    """PP-OCRv6 engine. device: auto (GPU if usable, else CPU) | gpu (fail if unusable) | cpu."""

    name = "ppocr"

    def __init__(self, model_dir: str = DEFAULT_MODEL_DIR, size: str = "small", device: str = "auto",
                 threads: int = 4, warm: bool = True):
        if device not in ("auto", "gpu", "cpu"):
            raise ValueError("device must be auto, gpu or cpu")
        det_dir, rec_dir = (os.path.join(model_dir, f"{size}_{part}") for part in ("det", "rec"))
        det_path, rec_path = (os.path.join(d, "inference.onnx") for d in (det_dir, rec_dir))
        for p in (det_path, rec_path, os.path.join(det_dir, "inference.yml"), os.path.join(rec_dir, "inference.yml")):
            if not os.path.exists(p):
                raise FileNotFoundError(f"PP-OCRv6 {size} model file missing: {p}")
        with open(os.path.join(det_dir, "inference.yml"), encoding="utf-8") as f:
            post = yaml.safe_load(f)["PostProcess"]
        self.engine = RapidOCR(
            use_cls=False,
            det_model_path=det_path, det_mean=[0.485, 0.456, 0.406], det_std=[0.229, 0.224, 0.225],
            det_thresh=post["thresh"], det_box_thresh=post["box_thresh"], det_unclip_ratio=post["unclip_ratio"],
            det_intra_op_num_threads=threads, rec_intra_op_num_threads=threads,
            rec_model_path=rec_path, rec_keys_path=_dictionary(rec_dir))
        self.size = size
        self.device = "cpu"
        self.fallback_reason = None
        if device != "cpu":
            reason = cuda_unavailable_reason(det_path)
            if reason:
                if device == "gpu":
                    raise RuntimeError(f"GPU requested but unusable: {reason}")
                self.fallback_reason = reason
            else:
                with quiet_stderr():
                    self.engine.text_det.infer = _CudaSession(det_path)
                rec = _BucketedRec(rec_path, self.engine.text_rec.session)
                if warm:
                    rec.warm()
                self.engine.text_rec.session = rec
                self.device = "gpu"
        self.lock = threading.Lock()
        self._seen_sizes = set()

    def recognize(self, bgr: np.ndarray, scale: float = 1.0) -> dict:
        """`bgr` is H x W x 3 uint8; boxes come back divided by `scale` (remote-session pixels)."""
        t0 = time.perf_counter()
        with self.lock:
            image = np.ascontiguousarray(bgr)
            # A new frame size is a new det shape: on GPU that is the one-off slow, noisy call.
            new_size = self.device == "gpu" and image.shape not in self._seen_sizes
            with quiet_stderr() if new_size else contextlib.nullcontext():
                result, _ = self.engine(image)
            self._seen_sizes.add(image.shape)
        lines = []
        for quad, text, score in result or []:
            text = text.strip()  # bucket padding can add a trailing space on GPU
            if not text:
                continue
            xs = [p[0] for p in quad]
            ys = [p[1] for p in quad]
            x0, y0 = min(xs), min(ys)
            lines.append({"text": text, "score": round(float(score), 3),
                          "box": [round(x0 / scale), round(y0 / scale),
                                  round((max(xs) - x0) / scale), round((max(ys) - y0) / scale)]})
        return {"text": "\n".join(l["text"] for l in lines), "lines": lines, "device": self.device,
                "engine_ms": round((time.perf_counter() - t0) * 1000, 1)}

    def __call__(self, job) -> dict:
        return self.recognize(job.frame.bgr, job.frame.scale)

    def close(self):
        pass
