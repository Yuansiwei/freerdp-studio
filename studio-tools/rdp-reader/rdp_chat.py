"""End-to-end chat with the remote OpenCode through the studio FreeRDP client.

    send:  OCR finds the empty input box -> click it -> SendRdpInput --text -> OCR checks the text
           really landed in the box -> --key ENTER
    read:  capture every second (rdp_capture, downsampled, no disk) until the screen has changed and
           then stayed still for --settle seconds; OCR (PP-OCRv6) the last frame and cut out the lines
           between our prompt and the input box.

Safety: refuses to type when the input box is not empty/visible (it may hold the user's draft), and
never presses Enter unless the typed text is seen inside the input box. Nothing is ever deleted.

    python rdp_chat.py --text "..." [--new-session] [--settle 8] [--timeout 180] [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
import unicodedata

import cv2

import rdp_capture
import rdp_reader

PLACEHOLDER = "随便问点什么"   # OpenCode's empty input box
NEW_SESSION = "新建会话"
SIDEBAR_FRACTION = 0.22        # chat panel starts right of ~22% of the session width
CREATE_NO_WINDOW = 0x08000000


class ChatError(RuntimeError):
    pass


def norm(s: str) -> str:
    return "".join(unicodedata.normalize("NFKC", s).split())


def center(box):
    x, y, w, h = box
    return x + w // 2, y + h // 2


def find_line(lines, needle: str, min_x: int = 0):
    n = norm(needle)
    return next((l for l in lines if l["box"][0] >= min_x and n in norm(l["text"])), None)


def extract_reply(lines, prompt: str, panel_min_x: int):
    """Panel lines strictly between the last line holding (the start of) our prompt and the input box."""
    panel = sorted((l for l in lines if l["box"][0] >= panel_min_x), key=lambda l: (l["box"][1], l["box"][0]))
    box = find_line(panel, PLACEHOLDER)
    bottom = box["box"][1] if box else None
    if bottom is None:  # input box not visible (e.g. still holding text): stop at the bottom toolbar
        bar = find_line(panel, "DeepSeek") or find_line(panel, "Build")
        bottom = bar["box"][1] - 60 if bar else 10 ** 9
    anchor = norm(prompt)[:12]
    start, prompt_visible = 0, False
    for i, l in enumerate(panel):
        if l["box"][1] < bottom and anchor and anchor in norm(l["text"]):
            start, prompt_visible = i + 1, True
    reply = [l for l in panel[start:] if l["box"][1] < bottom]
    return {"text": "\n".join(l["text"] for l in reply), "lines": reply, "prompt_visible": prompt_visible}


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

    def __init__(self, client, ocr, scale: float = 0.5):
        self.client, self.ocr, self.scale = client, ocr, scale

    def capture(self):
        return self.client.capture(self.scale)

    def read(self, frame=None):
        frame = frame or self.capture()
        return frame, self.ocr.recognize(frame.bgr, frame.scale)["lines"]


def chat(screen, send, text: str, new_session: bool = False, settle: float = 8.0, timeout: float = 180.0,
         poll: float = 1.0, dry_run: bool = False, out_dir: str | None = None, sleep=time.sleep) -> dict:
    if not text or len(text) > 4096 or any(c in text for c in "\r\n\t\0"):
        raise ChatError("text must be 1-4096 characters without newlines/tabs")
    frame, lines = screen.read()
    width = frame.native_size[0]
    panel_x = int(width * SIDEBAR_FRACTION)
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

    send("--click", "%d,%d" % click)
    sleep(0.4)
    send("--text", text)
    sleep(0.8)
    _, lines = screen.read()
    in_box = [l for l in lines if l["box"][0] >= panel_x and box["box"][1] - 120 <= l["box"][1] <= box["box"][1] + 260]
    typed = norm("".join(l["text"] for l in in_box))
    if norm(text)[:12] not in typed or find_line(in_box, PLACEHOLDER):
        raise ChatError("typed text not seen in the input box; NOT pressing Enter (check the remote screen)")
    send("--key", "ENTER")
    t_send = time.monotonic()
    sent_at = time.time()

    det = rdp_reader.ChangeDetector()
    det.accept(det.thumb(screen.capture().bgr))
    first_change = last_change = None
    frames = 0
    while True:
        sleep(poll)
        frame = screen.capture()
        frames += 1
        thumb = det.thumb(frame.bgr)
        changed, _, _ = det.compare(thumb, frame.native_size)
        now = time.monotonic()
        if changed:
            det.accept(thumb)
            first_change = first_change or now
            last_change = now
        if first_change and now - last_change >= settle:
            timed_out = False
            break
        if now - t_send >= timeout:
            timed_out = True
            break
    _, lines = screen.read(frame)
    reply = extract_reply(lines, text, panel_x)
    result = {"sent_at": rdp_reader.iso(sent_at), "prompt": text, "reply": reply["text"],
              "prompt_visible": reply["prompt_visible"], "timed_out": timed_out, "frames": frames,
              "first_change_s": round(first_change - t_send, 1) if first_change else None,
              "done_s": round((last_change or now) - t_send, 1), "lines": reply["lines"]}
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
    p.add_argument("--settle", type=float, default=8.0, help="seconds without change that mean 'reply done'")
    p.add_argument("--timeout", type=float, default=180.0)
    p.add_argument("--dry-run", action="store_true", help="locate everything, send nothing")
    p.add_argument("--ppocr-device", choices=["auto", "gpu", "cpu"], default="auto")
    p.add_argument("--out", help=r"save chat.json + final.jpg here (default C:\Studio\rdpread\chat-<time>)")
    a = p.parse_args(argv)
    import ppocr
    screen = Screen(rdp_capture.RdpClient(a.target), ppocr.PPOCR(device=a.ppocr_device))
    out = a.out or os.path.join(r"C:\Studio\rdpread", "chat-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
    try:
        result = chat(screen, Sender(a.target), a.text, a.new_session, a.settle, a.timeout,
                      dry_run=a.dry_run, out_dir=None if a.dry_run else out)
    except ChatError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({k: v for k, v in result.items() if k != "lines"}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
