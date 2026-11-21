"""Per-second reading workflow for the studio FreeRDP client.

    capture (1/s, named pipe, downsampled)  ->  change detection  ->  OCR  ->  events.jsonl / latest.*

* Capture: rdp_capture.RdpClient (no disk, no client rebuild).
* Change detection: grey thumbnail diff against the last *processed* frame, so a blinking
  caret or a spinner does not trigger OCR, while slow accumulated change still does.
* OCR, latest-wins (stale frames are dropped, never queued); --engine takes a comma list:
    ppocr  PP-OCRv6 small (default). GPU with fixed-shape buckets, CPU fallback. ~0.6 s per
           1920x1080 frame on GPU, ~1.1-1.3 s on CPU. Most accurate engine measured; lines + boxes.
    win    Windows.Media.Ocr in a hidden PowerShell worker. ~0.2 s, but 15-30% character errors.
    vl     PaddleOCR-VL on the local llama-server. ~2-4 s; can loop on a token until max_tokens,
           such tails are cut and flagged "repetition_cut".
  Boxes are reported in remote-session pixels, usable with SendRdpInput --click X,Y.

Run `python rdp_reader.py --help` for options.
"""
from __future__ import annotations

import argparse
import base64
import collections
import datetime as dt
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.request

import cv2
import numpy as np

import rdp_capture

HERE = os.path.dirname(os.path.abspath(__file__))
CREATE_NO_WINDOW = 0x08000000
CJK = r"[\u2e80-\u9fff\uf900-\ufaff\u3000-\u303f\uff00-\uffef]"
CJK_GAP = re.compile(rf"(?<={CJK}) +(?={CJK})")


def iso(t: float) -> str:
    return dt.datetime.fromtimestamp(t).isoformat(timespec="milliseconds")


def tidy_cjk(text: str) -> str:
    """Windows OCR puts a space between every CJK character; drop those."""
    return CJK_GAP.sub("", text)


# --------------------------------------------------------------------------- change detection
class ChangeDetector:
    def __init__(self, thumb_width: int = 480, pixel_delta: int = 8, min_ratio: float = 0.001):
        self.thumb_width = thumb_width
        self.pixel_delta = pixel_delta
        self.min_ratio = min_ratio
        self.reference = None

    def thumb(self, bgr: np.ndarray) -> np.ndarray:
        h, w = bgr.shape[:2]
        tw = min(self.thumb_width, w)
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        return cv2.resize(gray, (tw, max(1, round(h * tw / w))), interpolation=cv2.INTER_AREA)

    def compare(self, thumb: np.ndarray, full_size):
        """Return (changed, ratio, bbox) with bbox [x, y, w, h] in `full_size` pixels."""
        fw, fh = full_size
        if self.reference is None or self.reference.shape != thumb.shape:
            return True, 1.0, [0, 0, fw, fh]
        mask = cv2.absdiff(thumb, self.reference) > self.pixel_delta
        ratio = float(mask.mean())
        if ratio < self.min_ratio:
            return False, ratio, None
        ys, xs = np.nonzero(mask)
        sx, sy = fw / thumb.shape[1], fh / thumb.shape[0]
        x0, y0 = max(0, int((xs.min() - 1) * sx)), max(0, int((ys.min() - 1) * sy))
        x1, y1 = min(fw, int((xs.max() + 2) * sx)), min(fh, int((ys.max() + 2) * sy))
        return True, ratio, [x0, y0, x1 - x0, y1 - y0]

    def accept(self, thumb: np.ndarray):
        self.reference = thumb


# --------------------------------------------------------------------------- OCR engines
class WinOcr:
    """Windows.Media.Ocr through one long-lived hidden PowerShell process.

    Frames go over stdin as Gray8 pixels (no temp files). The worker is started with
    CREATE_NO_WINDOW, so no console window ever flashes on screen.
    """

    name = "win"

    def __init__(self, lang: str = "zh-Hans-CN", timeout_s: float = 30.0):
        self.lang = lang
        self.timeout_s = timeout_s
        self.proc = None
        self.lines = None
        self.lock = threading.Lock()
        self.next_id = 0
        self._start()

    def _start(self):
        self.proc = subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", os.path.join(HERE, "winocr_worker.ps1"), "-Lang", self.lang],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW, text=True, encoding="ascii", errors="replace", bufsize=1)
        # Fresh queue per process, so a dead worker's pump can never feed the new one.
        self.lines = collections.deque()
        self.line_ready = threading.Condition()
        threading.Thread(target=self._pump, args=(self.proc, self.lines, self.line_ready), daemon=True).start()
        ready = self._read_line(self.timeout_s)
        if not ready.get("ready"):
            raise RuntimeError(f"Windows OCR worker failed to start: {ready.get('error')}")

    @staticmethod
    def _pump(proc, lines, line_ready):
        for line in proc.stdout:
            with line_ready:
                lines.append(line)
                line_ready.notify_all()
        with line_ready:
            lines.append(None)
            line_ready.notify_all()

    def _read_line(self, timeout_s: float) -> dict:
        deadline = time.monotonic() + timeout_s
        with self.line_ready:
            while not self.lines:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError("Windows OCR worker did not answer")
                self.line_ready.wait(left)
            line = self.lines.popleft()
        if line is None:
            raise RuntimeError("Windows OCR worker exited")
        return json.loads(line)

    def _request(self, header: dict, payload: str | None = None) -> dict:
        with self.lock:
            if self.proc.poll() is not None:
                self._start()
            self.next_id += 1
            header = dict(header, id=self.next_id)
            self.proc.stdin.write(json.dumps(header) + "\n" + (payload + "\n" if payload is not None else ""))
            self.proc.stdin.flush()
            while True:
                reply = self._read_line(self.timeout_s)
                if reply.get("id") == header["id"]:
                    break
        if not reply.get("ok"):
            raise RuntimeError(f"Windows OCR failed: {reply.get('error')} ({reply.get('hresult')})")
        lines = [{"text": tidy_cjk(l["text"]), "box": l["box"]} for l in reply.get("lines") or []]
        return {"text": "\n".join(l["text"] for l in lines), "lines": lines, "engine_ms": reply.get("ms")}

    def recognize_gray(self, gray: np.ndarray, scale: float = 1.0) -> dict:
        """`gray` is H x W uint8; boxes come back divided by `scale`."""
        gray = np.ascontiguousarray(gray, dtype=np.uint8)
        h, w = gray.shape
        return self._request({"w": w, "h": h, "scale": scale}, base64.b64encode(gray).decode("ascii"))

    def recognize_file(self, path: str, scale: float = 1.0) -> dict:
        return self._request({"path": os.path.abspath(path), "scale": scale})

    def __call__(self, job: "Job") -> dict:
        return self.recognize_gray(cv2.cvtColor(job.frame.bgr, cv2.COLOR_BGR2GRAY), job.frame.scale)

    def close(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.close()
                self.proc.wait(5)
            except Exception:
                self.proc.kill()
        if self.proc:
            self.proc.stdout.close()


def cut_repetition(text: str, min_repeats: int = 8, max_unit: int = 16, min_span: int = 16):
    """If `text` ends in a unit repeated >= min_repeats times (a decoding loop), keep one copy.

    Returns (text, cut). Measured: PaddleOCR-VL looped on 4 of 27 synthetic pages, e.g. 「點點點…」.
    """
    best = None  # (covered chars, cut position)
    for unit_len in range(1, max_unit + 1):
        for tail in range(unit_len):  # the loop may stop mid-unit at max_tokens
            end = len(text) - tail
            if end < unit_len * min_repeats:
                continue
            unit = text[end - unit_len:end]
            start = end - unit_len
            while start - unit_len >= 0 and text[start - unit_len:start] == unit:
                start -= unit_len
            repeats = (end - start) // unit_len
            span = end - start + tail
            if repeats >= min_repeats and span >= min_span and (best is None or span > best[0]):
                best = (span, start + unit_len)
    if best is None:
        return text, False
    return text[:best[1]], True


class VlOcr:
    """PaddleOCR-VL served by llama-server (OpenAI-compatible, prompt "OCR:")."""

    name = "vl"

    def __init__(self, url: str = "http://127.0.0.1:8080", max_width: int = 1600, max_tokens: int = 2048,
                 region: str = "full", timeout_s: float = 120.0):
        self.url = url.rstrip("/") + "/v1/chat/completions"
        self.max_width = max_width
        self.max_tokens = max_tokens
        self.region = region
        self.timeout_s = timeout_s
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def recognize_image(self, bgr: np.ndarray) -> dict:
        h, w = bgr.shape[:2]
        if w > self.max_width:
            s = self.max_width / w
            bgr = cv2.resize(bgr, (self.max_width, max(1, round(h * s))), interpolation=cv2.INTER_AREA)
        ok, jpg = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 92])
        if not ok:
            raise RuntimeError("JPEG encode failed")
        body = {"model": "ppocr", "max_tokens": self.max_tokens, "temperature": 0,
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": "OCR:"},
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpg).decode()}}]}]}
        req = urllib.request.Request(self.url, data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with self.opener.open(req, timeout=self.timeout_s) as resp:
            reply = json.load(resp)
        choice = reply["choices"][0]
        usage = reply.get("usage", {})
        text = choice["message"]["content"].strip()
        truncated = choice.get("finish_reason") == "length"
        cut = False
        if truncated:  # a decoding loop runs until max_tokens; legit repeats ("=====") never get here alone
            text, cut = cut_repetition(text)
        return {"text": text, "input_size": [bgr.shape[1], bgr.shape[0]],
                "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"),
                "truncated": truncated, "repetition_cut": cut}

    def __call__(self, job: "Job") -> dict:
        bgr = job.frame.bgr
        if self.region == "changed" and job.bbox:
            s = job.frame.scale
            x, y, w, h = (round(v * s) for v in job.bbox)
            bgr = bgr[y:y + h, x:x + w]
        result = self.recognize_image(np.ascontiguousarray(bgr))
        result["region"] = self.region if self.region == "full" or not job.bbox else job.bbox
        return result

    def close(self):
        pass


# --------------------------------------------------------------------------- plumbing
class Job:
    __slots__ = ("frame", "bbox", "change_ratio")

    def __init__(self, frame, bbox, change_ratio):
        self.frame, self.bbox, self.change_ratio = frame, bbox, change_ratio


class LatestWins:
    """Runs `engine` on the newest submitted job; a newer submit replaces an unstarted one."""

    def __init__(self, engine, workers: int, sink: "Sink"):
        self.engine, self.sink = engine, sink
        self.cv = threading.Condition()
        self.pending = None
        self.stopping = False
        self.done = self.failed = self.dropped = 0
        self.ms = []
        self.threads = [threading.Thread(target=self._loop, daemon=True, name=f"ocr-{engine.name}-{i}")
                        for i in range(workers)]
        for t in self.threads:
            t.start()

    def submit(self, job: Job):
        with self.cv:
            if self.pending is not None:
                self.dropped += 1
            self.pending = job
            self.cv.notify()

    def _loop(self):
        while True:
            with self.cv:
                while self.pending is None and not self.stopping:
                    self.cv.wait()
                if self.pending is None:
                    return
                job, self.pending = self.pending, None
            t0 = time.perf_counter()
            try:
                result = self.engine(job)
            except Exception as exc:
                with self.cv:
                    self.failed += 1
                self.sink.event({"type": "ocr_error", "engine": self.engine.name, "seq": job.frame.seq,
                                 "error": f"{type(exc).__name__}: {exc}"})
                continue
            ocr_ms = (time.perf_counter() - t0) * 1000
            with self.cv:
                self.done += 1
                self.ms.append(ocr_ms)
            record = {"type": "ocr", "engine": self.engine.name, "seq": job.frame.seq,
                      "frame_ts": iso(job.frame.wall_time), "ocr_ms": round(ocr_ms, 1),
                      "latency_ms": round((time.time() - job.frame.wall_time) * 1000, 1)}
            record.update(result)
            self.sink.event(record)

    def close(self):
        """Finish the job already pending (if any), then stop."""
        with self.cv:
            self.stopping = True
            self.cv.notify_all()
        for t in self.threads:
            t.join()
        self.engine.close()

    def stats(self) -> dict:
        ms = sorted(self.ms)
        return {"done": self.done, "failed": self.failed, "dropped": self.dropped,
                "median_ms": round(ms[len(ms) // 2], 1) if ms else None}


class Sink:
    """events.jsonl (append-only), latest.json / latest_<engine>.txt (atomic), frames/ (bounded)."""

    def __init__(self, out_dir: str, keep_frames: int = 300, save_frames: bool = True, echo: bool = True):
        self.out_dir = out_dir
        self.keep_frames = keep_frames
        self.save_frames = save_frames
        self.echo = echo
        os.makedirs(os.path.join(out_dir, "frames"), exist_ok=True)
        self.lock = threading.Lock()
        self.events = open(os.path.join(out_dir, "events.jsonl"), "a", encoding="utf-8")
        self.latest = {}
        self.saved = collections.deque()

    def _atomic(self, name: str, text: str):
        path = os.path.join(self.out_dir, name)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)

    def event(self, record: dict):
        record.setdefault("ts", iso(time.time()))
        with self.lock:
            self.events.write(json.dumps(record, ensure_ascii=False) + "\n")
            self.events.flush()
            if record["type"] == "ocr":
                engine = record["engine"]
                if record["seq"] >= self.latest.get(engine, {}).get("seq", -1):
                    self.latest[engine] = record
                    self._atomic("latest.json", json.dumps(self.latest, ensure_ascii=False, indent=1))
                    self._atomic(f"latest_{engine}.txt", record["text"] + "\n")
            if self.echo:
                print(self.summary_line(record), flush=True)

    @staticmethod
    def summary_line(r: dict) -> str:
        t = r.get("ts", "")[11:19]
        kind = r["type"]
        if kind == "frame":
            return (f"[{t}] #{r['seq']} changed {r['change_ratio'] * 100:.2f}% bbox={r['bbox']} "
                    f"capture={r['capture_ms']}ms")
        if kind == "ocr":
            first = next((l for l in r["text"].splitlines() if l.strip()), "")[:60]
            return (f"[{t}] #{r['seq']} {r['engine']:>3} {r['ocr_ms']:.0f}ms "
                    f"{len(r['text'].splitlines())} lines | {first}")
        return f"[{t}] {kind}: " + json.dumps({k: v for k, v in r.items() if k not in ('type', 'ts')},
                                               ensure_ascii=False)

    def save_frame(self, frame) -> str | None:
        if not self.save_frames:
            return None
        rel = os.path.join("frames", f"{frame.seq:06d}.jpg")
        cv2.imwrite(os.path.join(self.out_dir, rel), frame.bgr, [cv2.IMWRITE_JPEG_QUALITY, 90])
        self.saved.append(rel)
        while len(self.saved) > self.keep_frames:
            try:
                os.remove(os.path.join(self.out_dir, self.saved.popleft()))
            except OSError:
                pass
        return rel.replace("\\", "/")

    def close(self):
        with self.lock:
            self.events.close()


# --------------------------------------------------------------------------- main loop
ENGINES = ("ppocr", "win", "vl")
ENGINE_ALIASES = {"both": ["win", "vl"], "none": []}  # "both" kept for older command lines


def engine_list(spec: str) -> list:
    names = []
    for part in (p.strip().lower() for p in spec.split(",")):
        for name in ENGINE_ALIASES.get(part, [part] if part else []):
            if name not in ENGINES:
                raise ValueError(f"unknown engine {name!r} (choose from {', '.join(ENGINES)}, none)")
            if name not in names:
                names.append(name)
    return names


def build_engines(args):
    engines = []
    for name in args.engines:
        if name == "ppocr":
            import ppocr  # onnxruntime/RapidOCR are only loaded when this engine is used
            engines.append((ppocr.PPOCR(args.ppocr_models, args.ppocr_size, args.ppocr_device, args.ppocr_threads), 1))
        elif name == "win":
            engines.append((WinOcr(args.win_lang), 1))
        elif name == "vl":
            engines.append((VlOcr(args.vl_url, args.vl_width, args.vl_max_tokens, args.vl_region), args.vl_inflight))
    return engines


def describe_engines(engines) -> dict:
    out = {}
    for engine, _ in engines:
        info = {"device": getattr(engine, "device", None)}
        if getattr(engine, "fallback_reason", None):
            info["gpu_fallback_reason"] = engine.fallback_reason
        out[engine.name] = info
    return out


def run(args, client=None, stop_event: threading.Event | None = None) -> dict:
    client = client or rdp_capture.RdpClient(args.target)
    stop_event = stop_event or threading.Event()
    out_dir = args.out or os.path.join(r"C:\Studio\rdpread", dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
    sink = Sink(out_dir, args.keep, not args.no_frames, echo=not args.quiet)
    detector = ChangeDetector(min_ratio=args.min_change)
    engines = build_engines(args)
    dispatchers = [LatestWins(engine, n, sink) for engine, n in engines]
    sink.event({"type": "start", "target": args.target, "interval_s": args.interval, "scale": args.scale,
                "engines": describe_engines(engines), "out": os.path.abspath(out_dir)})

    ticks = changed = errors = idle_streak = 0
    capture_ms = []
    last_error = None
    started = time.monotonic()
    next_tick = started
    try:
        while not stop_event.is_set():
            if args.duration and time.monotonic() - started >= args.duration:
                break
            if args.max_frames and ticks >= args.max_frames:
                break
            ticks += 1
            try:
                frame = client.capture(args.scale)
            except rdp_capture.CaptureError as exc:
                errors += 1
                if str(exc) != last_error:
                    sink.event({"type": "capture_error", "error": str(exc)})
                    last_error = str(exc)
            else:
                last_error = None
                capture_ms.append(frame.capture_ms)
                thumb = detector.thumb(frame.bgr)
                is_changed, ratio, bbox = detector.compare(thumb, frame.native_size)
                if is_changed:
                    changed += 1
                    idle_streak = 0
                    detector.accept(thumb)
                    record = {"type": "frame", "seq": frame.seq, "frame_ts": iso(frame.wall_time),
                              "native_size": list(frame.native_size), "size": list(frame.size),
                              "scale": frame.scale, "capture_ms": round(frame.capture_ms, 1),
                              "change_ratio": round(ratio, 5), "bbox": bbox, "frame": sink.save_frame(frame)}
                    sink.event(record)
                    job = Job(frame, bbox, ratio)
                    for d in dispatchers:
                        d.submit(job)
                else:
                    idle_streak += 1
                    if args.heartbeat and idle_streak % args.heartbeat == 0:
                        sink.event({"type": "idle", "seq": frame.seq, "unchanged_ticks": idle_streak,
                                    "change_ratio": round(ratio, 5)})
            next_tick += args.interval
            now = time.monotonic()
            if next_tick < now:  # overran: skip missed ticks instead of bursting
                next_tick = now
            stop_event.wait(next_tick - now)
    finally:
        for d in dispatchers:
            d.close()
        cm = sorted(capture_ms)
        summary = {"type": "stop", "ticks": ticks, "changed": changed, "capture_errors": errors,
                   "capture_median_ms": round(cm[len(cm) // 2], 1) if cm else None,
                   "elapsed_s": round(time.monotonic() - started, 1),
                   "ocr": {d.engine.name: d.stats() for d in dispatchers}}
        sink.event(summary)
        sink.close()
    summary["out"] = os.path.abspath(out_dir)
    return summary


def read_once(args, client=None) -> dict:
    """One capture, every selected engine run synchronously; returns a JSON-able dict."""
    client = client or rdp_capture.RdpClient(args.target)
    frame = client.capture(args.scale)
    out = {"seq": frame.seq, "frame_ts": iso(frame.wall_time), "native_size": list(frame.native_size),
           "size": list(frame.size), "capture_ms": round(frame.capture_ms, 1), "ocr": {}}
    job = Job(frame, None, 1.0)
    for engine, _ in build_engines(args):
        t0 = time.perf_counter()
        try:
            result = engine(job)
            result["ocr_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        except Exception as exc:
            result = {"error": f"{type(exc).__name__}: {exc}"}
        finally:
            engine.close()
        out["ocr"][engine.name] = result
    return out


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--target", default="172.30.251.1:3390", help="HOST:PORT of the running wfreerdp session")
    p.add_argument("--interval", type=float, default=1.0, help="seconds between captures (default 1)")
    p.add_argument("--scale", default="auto",
                   help="downsample right after capture: 'auto' (default) shrinks to ~1920 wide "
                        "(3840 -> 0.5, 2560 -> 0.75), or a factor in 0.05..1.0")
    p.add_argument("--engine", default="ppocr",
                   help="comma list of ppocr, win, vl (or none). Default ppocr. Example: ppocr,vl")
    p.add_argument("--ppocr-device", choices=["auto", "gpu", "cpu"], default="auto",
                   help="auto = GPU when usable, else CPU (reason recorded in the start event)")
    p.add_argument("--ppocr-size", choices=["small", "medium"], default="small")
    p.add_argument("--ppocr-models", default=r"C:\Studio\vlm\ppocrv6", help="dir with <size>_det / <size>_rec")
    p.add_argument("--ppocr-threads", type=int, default=4, help="CPU threads (2-4 are as fast as 12 here)")
    p.add_argument("--out", help=r"output dir (default C:\Studio\rdpread\<timestamp>)")
    p.add_argument("--duration", type=float, default=0, help="stop after N seconds (0 = until Ctrl+C)")
    p.add_argument("--max-frames", type=int, default=0, help="stop after N captures (0 = unlimited)")
    p.add_argument("--min-change", type=float, default=0.001,
                   help="fraction of thumbnail pixels that must change to trigger OCR (default 0.001)")
    p.add_argument("--heartbeat", type=int, default=30, help="write an idle record every N unchanged ticks")
    p.add_argument("--keep", type=int, default=300, help="keep the newest N changed frames as JPEG")
    p.add_argument("--no-frames", action="store_true", help="do not save frame JPEGs")
    p.add_argument("--win-lang", default="zh-Hans-CN", help="Windows OCR language (en-US, zh-Hans-CN, zh-Hant-TW)")
    p.add_argument("--vl-url", default="http://127.0.0.1:8080")
    p.add_argument("--vl-width", type=int, default=1600, help="max image width sent to PaddleOCR-VL")
    p.add_argument("--vl-max-tokens", type=int, default=2048)
    p.add_argument("--vl-inflight", type=int, default=2, help="concurrent PaddleOCR-VL requests")
    p.add_argument("--vl-region", choices=["full", "changed"], default="full",
                   help="send the full frame or only the changed area to PaddleOCR-VL")
    p.add_argument("--once", action="store_true", help="capture one frame, OCR it, print JSON, exit")
    p.add_argument("--quiet", action="store_true", help="no per-event console lines")
    args = p.parse_args(argv)
    if args.interval <= 0:
        p.error("--interval must be > 0")
    if args.scale != "auto":
        try:
            args.scale = float(args.scale)
        except ValueError:
            p.error("--scale must be 'auto' or a number")
        if not 0.05 <= args.scale <= 1.0:
            p.error("--scale must be within 0.05..1.0")
    try:
        args.engines = engine_list(args.engine)
    except ValueError as exc:
        p.error(str(exc))
    return args


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args(argv)
    if args.once:
        print(json.dumps(read_once(args), ensure_ascii=False, indent=1))
        return 0
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    summary = run(args, stop_event=stop)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
