"""End-to-end chat with the remote OpenCode through the studio FreeRDP client.

    send:  OCR finds the empty input box -> click it -> SendRdpInput --text -> OCR checks the text
           really landed in the box -> --key ENTER
    read:  a capture thread grabs frames back to back (rdp_capture, downsampled, no disk) and queues
           the chat body of every changed frame; the main thread OCRs the queue (PP-OCRv6, skipping
           frames adaptively) and stitches the reply while it streams, so a reply taller than the
           screen needs no scrolling. Done when the screen stayed still for --settle seconds. If the
           stream could not be followed without a gap, it scrolls back up and reads page by page.

Safety: refuses to type when the input box is not empty/visible (it may hold the user's draft), and
never presses Enter unless the typed text is seen inside the input box. Nothing is ever deleted.

    python rdp_chat.py --text "..." [--new-session] [--settle 8] [--timeout 180] [--dry-run]
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import difflib
import itertools
import json
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata

import cv2
import numpy as np

import rdp_capture
import rdp_reader

PLACEHOLDER = "随便问点什么"   # OpenCode's empty input box
NEW_SESSION = "新建会话"
SIDEBAR_FRACTION = 0.22        # chat panel starts right of ~22% of the session width
HEADER_FRACTION = 0.08         # top strip of the chat panel (session title) is above 8% of the height
CREATE_NO_WINDOW = 0x08000000


class ChatError(RuntimeError):
    pass


def norm(s: str) -> str:
    return "".join(unicodedata.normalize("NFKC", s).split())


def median(xs):
    return round(sorted(xs)[len(xs) // 2]) if xs else None


def center(box):
    x, y, w, h = box
    return x + w // 2, y + h // 2


def fuzzy_in(needle: str, hay: str, max_ratio: float = 0.2) -> bool:
    """`needle` occurs in `hay` with at most max_ratio*len edits (OCR may misread a character or two;
    an unrelated text never gets this close)."""
    n, h = norm(needle), norm(hay)
    if not n:
        return False
    if n in h:
        return True
    budget = max(1, int(len(n) * max_ratio))
    prev = [0] * (len(h) + 1)          # free start anywhere in hay
    for i, ch in enumerate(n, 1):
        cur = [i] + [0] * len(h)
        for j in range(1, len(h) + 1):
            cur[j] = min(prev[j - 1] + (ch != h[j - 1]), prev[j] + 1, cur[j - 1] + 1)
        prev = cur
    return min(prev) <= budget


def find_line(lines, needle: str, min_x: int = 0):
    n = norm(needle)
    return next((l for l in lines if l["box"][0] >= min_x and n in norm(l["text"])), None)


# OpenCode's per-message footer, e.g. "Build · DeepSeek-V4-Flash · 17s" (metadata, not reply text)
FOOTER = re.compile(r"^Build[·・.•].+[·・.•]\d+(\.\d+)?(ms|s|m|min)$")


def body_lines(lines, panel_x: int, height: int | None = None):
    """Chat panel lines between the header strip (session title, toolbar) and the input box."""
    top = height * HEADER_FRACTION if height else -1
    panel = sorted((l for l in lines if l["box"][0] >= panel_x and l["box"][1] >= top),
                   key=lambda l: (l["box"][1], l["box"][0]))
    box = find_line(panel, PLACEHOLDER)
    if box:
        bottom = box["box"][1]
    else:  # input box not visible (e.g. still holding text): stop above the bottom toolbar
        bar = next((l for l in reversed(panel) if "DeepSeek" in l["text"] and not FOOTER.match(norm(l["text"]))), None)
        bottom = bar["box"][1] - (0.03 * height if height else 60) if bar else 10 ** 9
    return [l for l in panel if l["box"][1] < bottom]


def reply_from_anchor(body, prompt: str):
    """Lines after the last line holding (the start of) our prompt; (lines, prompt_visible)."""
    anchor = norm(prompt)[:12]
    start, anchor_x = 0, None
    for i, l in enumerate(body):
        if anchor and fuzzy_in(anchor, l["text"]):
            start, anchor_x = i + 1, l["box"][0]
    reply = list(body[start:])
    if anchor_x is not None and reply:
        # Our prompt is a right-aligned bubble; a long prompt wraps and its tail ("或指令。") sits
        # right after the anchor, far right of the reply's left margin. Drop those leading lines.
        margin = min(l["box"][0] for l in reply)
        if anchor_x - margin > 60:
            cut = margin + (anchor_x - margin) / 2
            while reply and reply[0]["box"][0] >= cut:
                reply.pop(0)
    return reply, anchor_x is not None


def extract_reply(lines, prompt: str, panel_min_x: int, height: int | None = None):
    """Reply lines between our prompt and the input box on one screen."""
    body = body_lines(lines, panel_min_x, height)
    reply, visible = reply_from_anchor(body, prompt)
    return split_footer(reply, visible)


def split_footer(reply, prompt_visible: bool, **extra):
    footer = None
    if reply and FOOTER.match(norm(reply[-1]["text"])):
        footer = reply[-1]["text"]
        reply = reply[:-1]
    out = {"text": "\n".join(l["text"] for l in reply), "lines": reply,
           "prompt_visible": prompt_visible, "footer": footer}
    out.update(extra)
    return out


def similar_line(na: str, nb: str, threshold: float = 0.9) -> bool:
    """Whole-line similarity of normalized texts (not containment: "1.項目1" must not equal "11.項目11")."""
    if not na or not nb or abs(len(na) - len(nb)) > max(1, 0.1 * max(len(na), len(nb))):
        return False
    sm = difflib.SequenceMatcher(None, na, nb, autojunk=False)
    return sm.real_quick_ratio() >= threshold and sm.quick_ratio() >= threshold and sm.ratio() >= threshold


def stitch(upper, lower, tol: float = 15, fresh: bool = False):
    """Append the page `lower` (screen coordinates) below `upper` (lines carrying a document y "gy").

    Registration by vote: every pair of matching texts proposes an offset (gy - screen y); the most
    common one wins, so an overlay that stays put on screen (the floating "↓" button), a misread
    line or repeated text cannot derail it (measured: a contiguous-run match failed on the real
    remote because of that button). The line cut by the bottom edge of `upper` is replaced by its
    complete copy from `lower`. Returns (lines, ok); ok is False when nothing registered."""
    recent = [(norm(a["text"]), a["gy"]) for a in upper[-80:]]
    lower_n = [(norm(b["text"]), b["box"][1]) for b in lower]
    votes = collections.Counter()
    for nb, yb in lower_n:                       # 1st pass: identical text (the usual case)
        for na, gy in recent:
            if na and na == nb:
                votes[round((gy - yb) / 4)] += 1
    if sum(votes.values()) < 2:                  # 2nd pass: tolerate OCR differences
        for nb, yb in lower_n:
            for na, gy in recent:
                if na != nb and similar_line(na, nb):
                    votes[round((gy - yb) / 4)] += 1
    if not votes:
        return upper + [dict(b, gy=b["box"][1] + (upper[-1]["gy"] if upper else 0) + 1e6) for b in lower], False
    bucket, count = votes.most_common(1)[0]
    support = sum(votes[k] for k in (bucket - 1, bucket, bucket + 1))
    offset = sum(k * votes[k] for k in (bucket - 1, bucket, bucket + 1)) * 4 / support
    placed = sorted((dict(b, gy=b["box"][1] + offset) for b in lower), key=lambda p: p["gy"])
    last = max(a["gy"] for a in upper)
    # Two votes can come from repeated lines alone ("}", "---") when the pages do not overlap at
    # all. Accept the offset only if most of what `lower` puts inside `upper`'s span agrees with it.
    tail = [(norm(a["text"]), a["gy"]) for a in upper[-80:]]
    first = min(gy for _, gy in tail)
    overlap = [(norm(p["text"]), p["gy"]) for p in placed if first - tol <= p["gy"] <= last + tol]
    agree = sum(1 for nb, yb in overlap
                if any(abs(gy - yb) <= tol and (na == nb or similar_line(na, nb)) for na, gy in tail))
    ok = support >= 2 and agree * 2 >= len(overlap)
    if fresh and len(placed) >= 2:
        # `lower` is a newer look at the same place (a reply still streaming): its lines win over
        # the overlap, except its first line, which the top edge may have cut.
        cut = placed[1]["gy"] - tol
        return [a for a in upper if a["gy"] < cut] + placed[1:], ok
    start = last + tol
    if any(abs(p["gy"] - last) <= tol for p in placed):        # prefer lower's copy of the edge line
        upper = [a for a in upper if a["gy"] < last - tol]
        start = last - tol
    return upper + [p for p in placed if p["gy"] >= start], ok


def is_overlay(line) -> bool:
    """UI glyphs drawn over the transcript, e.g. the floating scroll-to-bottom "↓" button."""
    t = norm(line["text"])
    return len(t) == 1 and not t.isalnum()


def read_full(screen, send, prompt: str, panel_x: int, width: int, height: int, step: int = 3,
              max_pages: int = 40, sleep=time.sleep) -> dict:
    """The reply is taller than the screen: scroll up page by page until our prompt shows, stitch the
    pages top-down on their overlap, then scroll back to the bottom where the user left it."""
    anchor = norm(prompt)[:12]

    def texts(page):
        return [norm(l["text"]) for l in page]

    def read_body():
        _, lines = screen.read()
        return body_lines(lines, panel_x, height)

    send("--move", "%d,%d" % ((panel_x + width) // 2, int(height * 0.45)))  # wheel goes to the chat
    sleep(0.2)
    pages = [read_body()]
    found = any(fuzzy_in(anchor, l["text"]) for l in pages[0])
    top_reached, up_notches = False, 0
    while not found and len(pages) <= max_pages:
        send("--wheel", str(step))
        up_notches += step
        sleep(0.8)
        page = read_body()
        if texts(page) == texts(pages[-1]):
            sleep(1.0)                        # the remote may simply not have repainted yet
            page = read_body()
            if texts(page) == texts(pages[-1]):
                top_reached = True
                break
        pages.append(page)
        found = any(fuzzy_in(anchor, l["text"]) for l in page)
    pages_clean = [[l for l in p if not is_overlay(l)] for p in pages]
    top, _ = reply_from_anchor(pages_clean[-1], prompt) if found else (list(pages_clean[-1]), False)
    combined = [dict(l, gy=l["box"][1]) for l in top]
    gaps = False
    for page in reversed(pages_clean[:-1]):
        if not combined:  # the reply started on a lower page
            combined = [dict(l, gy=l["box"][1]) for l in page]
            continue
        combined, ok = stitch(combined, page)
        gaps = gaps or not ok
    combined = [{k: v for k, v in l.items() if k != "gy"} for l in combined]
    # Put the view back where it was. Go down the way we came up, `step` notches per call: one
    # 24-notch burst only moved a third as far on the real remote (OpenCode drops part of a burst).
    # Then keep stepping until the original first page is on screen again; stop when the view no
    # longer moves (bottom). "The screen stopped moving" alone is not proof of being back.
    restored = up_notches == 0
    if up_notches:
        for _ in range(up_notches // step):
            send("--wheel", str(-step))
            sleep(0.15)
        sleep(0.8)
        last = None
        for _ in range(3 * (up_notches // step) + 3):
            now = texts(read_body())
            if now == texts(pages[0]):
                restored = True
                break
            if now == last:                  # bottom reached without matching
                break
            last = now
            send("--wheel", str(-step))
            sleep(0.8)
    return split_footer(combined, found, pages=len(pages), gaps=gaps, top_reached=top_reached,
                        restored=restored)


class Sender:
    """SendRdpInput.exe next to the running wfreerdp.exe (the tool insists on that location)."""

    def __init__(self, target: str, exe: str | None = None):
        self.target = target
        if exe is None:
            found = rdp_capture.find_client_windows(rdp_capture.parse_target(target))
            if len(found) != 1:
                raise ChatError(f"expected one wfreerdp window for {target}, found {len(found)}")
            exe = os.path.join(os.path.dirname(found[0][2]), "SendRdpInput.exe")
        if not os.path.exists(exe):
            raise ChatError(f"SendRdpInput.exe not found: {exe}")
        self.exe = exe
        self.log = []

    def __call__(self, command: str, value: str):
        r = subprocess.run([self.exe, "--target", self.target, command, value], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW, timeout=30)
        self.log.append((command, value if command != "--text" else f"<{len(value)} chars>", r.returncode))
        if r.returncode != 0:
            raise ChatError(f"SendRdpInput {command} failed ({r.returncode}): {(r.stderr or r.stdout).strip()}")


class Screen:
    """Downsampled captures + PP-OCRv6 lines in session pixels."""

    def __init__(self, client, ocr, scale="auto"):
        self.client, self.ocr, self.scale = client, ocr, scale

    def capture(self):
        return self.client.capture(self.scale)

    def read(self, frame=None, region=None):
        """OCR the frame, or only `region` = (x0, y0, x1, y1) in session pixels (much faster: the
        chat body alone took 187 ms vs 422 ms for the whole 1920x1080 frame). Boxes are always
        returned in session pixels."""
        frame = frame or self.capture()
        if region is None:
            return frame, self.ocr.recognize(frame.bgr, frame.scale)["lines"]
        crop, s, origin = _crop(frame, region)
        return frame, self._ocr_crop(crop, s, origin)

    @staticmethod
    def snapshot(frame, region):
        """`region` of a frame, PNG-compressed, to OCR later: the capture thread keeps one per changed
        frame while the reply streams. Lossless and small (a 1498x778 chat body: 46 KB, 21 ms to
        encode, 7 ms to decode, vs 3.5 MB raw), so a long backlog costs little memory."""
        crop, s, origin = _crop(frame, region)
        ok, png = cv2.imencode(".png", crop, [cv2.IMWRITE_PNG_COMPRESSION, 1])
        if not ok:
            raise ChatError("PNG encoding of a snapshot failed")
        return png, s, origin

    def read_snapshot(self, snap):
        png, s, origin = snap
        return self._ocr_crop(cv2.imdecode(png, cv2.IMREAD_COLOR), s, origin)

    def _ocr_crop(self, crop, s, origin):
        lines = self.ocr.recognize(crop, s)["lines"]
        ox, oy = origin
        for l in lines:
            l["box"] = [round(l["box"][0] + ox), round(l["box"][1] + oy), l["box"][2], l["box"][3]]
        return lines


def _crop(frame, region):
    """(x0, y0, x1, y1) in session pixels -> (contiguous crop of the downsampled frame, scale, origin)."""
    s = frame.scale
    x0, y0, x1, y1 = (max(0, int(round(v * s))) for v in region)
    return np.ascontiguousarray(frame.bgr[y0:y1, x0:x1]), s, (x0 / s, y0 / s)


class Capturer(threading.Thread):
    """Captures back to back (`poll` apart) while a reply streams and keeps a snapshot of the chat
    body of every frame that changed. OCR runs elsewhere, so however slow it is, every step of the
    stream stays in the queue (the old capture->OCR->capture loop saw one frame per ~0.3 s and lost
    lines whenever more than a screenful streamed in between)."""

    def __init__(self, screen, region, poll: float, sleep):
        super().__init__(name="rdp-chat-capture", daemon=True)
        self.screen, self.region, self.poll, self.sleep = screen, region, poll, sleep
        self.det = rdp_reader.ChangeDetector()
        self.lock = threading.Lock()
        self.queue = collections.deque()          # snapshots the OCR side has not consumed yet
        self.halt = threading.Event()
        self.frame = None
        self.frame_kept = False
        self.frames = self.snapshots = self.max_queue = 0
        self.first_change = self.last_change = None
        self.capture_ms = []
        self.error = None

    def _keep(self, frame):
        self.frame_kept = True
        if self.region is None:
            return
        snap = self.screen.snapshot(frame, self.region)
        with self.lock:
            self.queue.append(snap)
            self.snapshots += 1
            self.max_queue = max(self.max_queue, len(self.queue))

    def baseline(self):
        self.frame = self.screen.capture()
        self.det.accept(self.det.thumb(self.frame.bgr))
        self._keep(self.frame)

    def run(self):
        try:
            while not self.halt.is_set():
                self.sleep(self.poll)
                t = time.perf_counter()
                frame = self.screen.capture()
                self.capture_ms.append((time.perf_counter() - t) * 1000)
                self.frames += 1
                thumb = self.det.thumb(frame.bgr)
                changed, _, _ = self.det.compare(thumb, frame.native_size)
                self.frame, self.frame_kept = frame, False
                if changed:
                    self.det.accept(thumb)
                    now = time.monotonic()
                    self.first_change = self.first_change or now
                    self.last_change = now
                    self._keep(frame)
        except Exception as e:  # surfaced by the caller after join()
            self.error = e

    def keep_final(self):
        """After stopping: the last bit of a reply (a short line, the footer) can be too small for the
        change detector, so the newest frame is always queued once."""
        if not self.frame_kept:
            self._keep(self.frame)

    def peek(self, n: int):
        with self.lock:
            return list(itertools.islice(self.queue, n))

    def drop(self, n: int):
        with self.lock:
            for _ in range(n):
                self.queue.popleft()


class Follower:
    """OCR side: works through the capture queue in order and stitches a running transcript.

    Skips adaptively: the next frame tried is `step` frames ahead; when it registers, the frames in
    between are dropped unread and the step doubles; when it does not, the step halves and a closer
    frame is tried. Only when even the very next frame fails is a gap recorded. So OCR effort follows
    how fast the text moves, and nothing is skipped that could not be stitched over."""

    MAX_STEP = 16

    def __init__(self, screen, panel_x: int, height: int, t0: float):
        self.screen, self.panel_x, self.height, self.t0 = screen, panel_x, height, t0
        self.transcript, self.gaps, self.pages, self.ocr_ms = [], False, [], []
        self.step = 1

    def consume(self, capt: Capturer) -> bool:
        """One OCR on a queued frame; False when the queue is empty."""
        pending = capt.peek(self.step)
        if not pending:
            return False
        i = len(pending) - 1
        t = time.perf_counter()
        got = self.screen.read_snapshot(pending[i])
        self.ocr_ms.append((time.perf_counter() - t) * 1000)
        body = [l for l in body_lines(got, self.panel_x, self.height) if not is_overlay(l)]
        page = {"t_ms": round((time.monotonic() - self.t0) * 1000), "ahead": i + 1, "n": len(body),
                "first": body[0]["text"][:24] if body else "", "last": body[-1]["text"][:24] if body else ""}
        self.pages.append(page)
        if not self.transcript:
            self.transcript = [dict(l, gy=l["box"][1]) for l in body]
            capt.drop(i + 1)
            return True
        merged, ok = stitch(self.transcript, body, fresh=True) if body else (self.transcript, False)
        page["ok"] = ok
        if ok or i == 0:
            if body:                       # a blank frame (repaint) adds nothing and is not a gap
                self.transcript = merged
                self.gaps = self.gaps or not ok
            if ok and i + 1 == self.step:
                self.step = min(self.step * 2, self.MAX_STEP)
            capt.drop(i + 1)
        else:
            self.step = max(1, (i + 1) // 2)
        return True


def header_text(lines, panel_x: int, height: int) -> str:
    """Text of the chat panel's top strip, which holds the active session's title."""
    top = [l for l in lines if l["box"][0] >= panel_x and l["box"][1] < height * HEADER_FRACTION]
    return " ".join(l["text"] for l in sorted(top, key=lambda l: (l["box"][1], l["box"][0])))


def chat(screen, send, text: str, new_session: bool = False, settle: float = 8.0, timeout: float = 180.0,
         poll: float = 0.02, dry_run: bool = False, out_dir: str | None = None, sleep=time.sleep,
         session: str | None = None, scrollback: bool = True, follow_stream: bool = True) -> dict:
    if not text or len(text) > 4096 or any(c in text for c in "\r\n\t\0"):
        raise ChatError("text must be 1-4096 characters without newlines/tabs")
    if session and new_session:
        raise ChatError("use either session or new_session, not both")
    frame, lines = screen.read()
    width, height = frame.native_size
    panel_x = int(width * SIDEBAR_FRACTION)
    if session:
        # Never type into whatever happens to be open: switch, then prove the header shows it.
        wanted = norm(session)
        target = next((l for l in lines if l["box"][0] < panel_x and wanted in norm(l["text"])), None)
        if not target:
            raise ChatError(f"session {session!r} not found in the sidebar")
        if dry_run:
            return {"dry_run": True, "would_click_session": center(target["box"]),
                    "active_session": header_text(lines, panel_x, height)}
        if wanted not in norm(header_text(lines, panel_x, height)):
            send("--click", "%d,%d" % center(target["box"]))
            sleep(1.5)
            frame, lines = screen.read()
        if wanted not in norm(header_text(lines, panel_x, height)):
            raise ChatError(f"active session is not {session!r} after switching; refusing to type")
    if new_session:
        btn = find_line(lines, NEW_SESSION)
        if not btn:
            raise ChatError("'新建会话' button not visible")
        if dry_run:
            return {"dry_run": True, "would_click_new_session": center(btn["box"])}
        send("--click", "%d,%d" % center(btn["box"]))
        sleep(1.5)
        frame, lines = screen.read()
    box = find_line(lines, PLACEHOLDER, panel_x)
    if not box:
        raise ChatError("empty input box not found (it may hold a draft); refusing to type")
    click = center(box["box"])
    if dry_run:
        return {"dry_run": True, "would_click_input": click, "input_box": box["box"]}

    active = header_text(lines, panel_x, height)
    send("--click", "%d,%d" % click)
    sleep(0.4)
    send("--text", text)
    sleep(0.8)
    _, lines = screen.read()
    # the input box grows downwards as text wraps; window scaled to the session height
    in_box = [l for l in lines if l["box"][0] >= panel_x
              and box["box"][1] - 0.056 * height <= l["box"][1] <= box["box"][1] + 0.12 * height]
    typed = "".join(l["text"] for l in in_box)
    if not fuzzy_in(norm(text)[:16], typed) or find_line(in_box, PLACEHOLDER):
        raise ChatError("typed text not seen in the input box; NOT pressing Enter (check the remote screen)")
    send("--key", "ENTER")
    t_send = time.monotonic()
    sent_at = time.time()

    # Follow the reply while it streams: OpenCode keeps the view pinned to the bottom, so every part
    # of the reply passes across the screen. A capture thread grabs frames back to back and queues
    # the chat body of each changed one; this thread OCRs the queue (about 0.2 s a frame) and
    # stitches a running transcript. At the end the whole reply is there and nothing has to be
    # scrolled. Scrolling back stays as the fallback.
    region = (panel_x, int(height * HEADER_FRACTION), width, box["box"][1])
    capt = Capturer(screen, region if follow_stream else None, poll, sleep)
    fol = Follower(screen, panel_x, height, t_send)
    capt.baseline()
    capt.start()
    try:
        while capt.error is None:
            now = time.monotonic()
            if capt.first_change and now - capt.last_change >= settle:
                timed_out = False
                break
            if now - t_send >= timeout:
                timed_out = True
                break
            if not (follow_stream and fol.consume(capt)):
                sleep(poll)
    finally:
        capt.halt.set()
        capt.join(10)
    if capt.error is not None:
        raise capt.error
    frame, now = capt.frame, time.monotonic()
    if follow_stream:
        capt.keep_final()
        while fol.consume(capt):          # catch up on whatever is still queued
            pass

    reply, mode = None, "screen"
    if follow_stream and fol.transcript and not fol.gaps:
        got, visible = reply_from_anchor([{k: v for k, v in l.items() if k != "gy"} for l in fol.transcript], text)
        if visible:
            reply, mode = split_footer(got, True), "follow"
    if reply is None:
        _, lines = screen.read(frame)
        reply = extract_reply(lines, text, panel_x, height)
        if scrollback and not reply["prompt_visible"]:
            reply, mode = read_full(screen, send, text, panel_x, width, height, sleep=sleep), "scrollback"
    first_change, last_change = capt.first_change, capt.last_change
    result = {"sent_at": rdp_reader.iso(sent_at), "active_session": active, "prompt": text, "reply": reply["text"],
              "footer": reply["footer"], "prompt_visible": reply["prompt_visible"], "mode": mode,
              "timed_out": timed_out, "frames": capt.frames,
              "capture_ms_median": median(capt.capture_ms),
              "first_change_s": round(first_change - t_send, 1) if first_change else None,
              "done_s": round((last_change or now) - t_send, 1),
              "collected_s": round(time.monotonic() - t_send, 1), "lines": reply["lines"]}
    if follow_stream:
        result["follow"] = {"snapshots": capt.snapshots, "ocr_frames": len(fol.ocr_ms), "gaps": fol.gaps,
                            "max_queue": capt.max_queue, "left_in_queue": len(capt.queue),
                            "ocr_ms_median": median(fol.ocr_ms), "pages": fol.pages}
    if "pages" in reply:
        result["scrollback"] = {k: reply[k] for k in ("pages", "gaps", "top_reached", "restored")}
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        cv2.imwrite(os.path.join(out_dir, "final.jpg"), frame.bgr, [cv2.IMWRITE_JPEG_QUALITY, 90])
        with open(os.path.join(out_dir, "chat.json"), "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=1)
    return result


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--target", default="172.30.251.1:3390")
    p.add_argument("--text", required=True, help="prompt to send (one line, <= 4096 chars)")
    p.add_argument("--new-session", action="store_true", help="click 新建会话 first")
    p.add_argument("--session", help="switch to the sidebar session containing this text and verify the "
                                     "header shows it before typing (refuses otherwise)")
    p.add_argument("--settle", type=float, default=8.0, help="seconds without change that mean 'reply done'")
    p.add_argument("--timeout", type=float, default=180.0)
    p.add_argument("--poll", type=float, default=0.02,
                   help="pause between captures while waiting (one capture itself takes ~25-80 ms)")
    p.add_argument("--dry-run", action="store_true", help="locate everything, send nothing")
    p.add_argument("--no-follow", action="store_true",
                   help="do not OCR the reply while it streams (default: follow it, so long replies need no scrolling)")
    p.add_argument("--no-scrollback", action="store_true",
                   help="never scroll up; by default it is the fallback when following missed part of a reply")
    p.add_argument("--ppocr-device", choices=["auto", "gpu", "cpu"], default="auto")
    p.add_argument("--out", help=r"save chat.json + final.jpg here (default C:\Studio\rdpread\chat-<time>)")
    a = p.parse_args(argv)
    import ppocr
    screen = Screen(rdp_capture.RdpClient(a.target), ppocr.PPOCR(device=a.ppocr_device))
    out = a.out or os.path.join(r"C:\Studio\rdpread", "chat-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
    try:
        result = chat(screen, Sender(a.target), a.text, a.new_session, a.settle, a.timeout,
                      poll=a.poll, dry_run=a.dry_run, out_dir=None if a.dry_run else out, session=a.session,
                      scrollback=not a.no_scrollback, follow_stream=not a.no_follow)
    except ChatError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({k: v for k, v in result.items() if k != "lines"}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
