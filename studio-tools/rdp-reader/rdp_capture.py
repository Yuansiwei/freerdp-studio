"""Grab frames from the studio FreeRDP client (wfreerdp) without touching the disk.

The client already answers WM_COPYDATA kind 0x46524435 (``SendRdpInput --capture``):
it writes its primary frame buffer as a 32-bit top-down BMP to the path in the payload
(``fopen(path, "wb")`` in ``client/Windows/studio_input.h``). Instead of a file path we
hand it a one-shot named pipe, so the frame (31.6 MB at 3840x2160) goes straight into
this process. It is downsampled right away; nothing at native resolution is kept.
No client rebuild is needed and the live session is never interrupted.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import secrets
import struct
import threading
import time
from dataclasses import dataclass

import cv2
import numpy as np

KIND_STATUS = 0x46524430
KIND_CAPTURE = 0x46524435
WM_COPYDATA = 0x004A
SMTO_FLAGS = 0x0002 | 0x0020  # SMTO_ABORTIFHUNG | SMTO_ERRORONEXIT, as SendRdpInput uses

PIPE_ACCESS_INBOUND = 0x00000001
FILE_FLAG_OVERLAPPED = 0x40000000
FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
PIPE_TYPE_BYTE = 0x0
PIPE_REJECT_REMOTE_CLIENTS = 0x8
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x102
ERROR_IO_PENDING = 997
ERROR_PIPE_CONNECTED = 535
ERROR_BROKEN_PIPE = 109
ERROR_OPERATION_ABORTED = 995
PIPE_BUFFER = 4 << 20
BMP_HEADER = 54
INVALID_HANDLE = ctypes.c_void_p(-1).value

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class COPYDATASTRUCT(ctypes.Structure):
    _fields_ = [("dwData", ctypes.c_size_t), ("cbData", wt.DWORD), ("lpData", ctypes.c_void_p)]


class OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                ("Offset", wt.DWORD), ("OffsetHigh", wt.DWORD), ("hEvent", ctypes.c_void_p)]


WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
user32.EnumWindows.argtypes = [WNDENUMPROC, wt.LPARAM]
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.IsWindow.argtypes = [wt.HWND]
user32.SendMessageTimeoutW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, ctypes.c_void_p, wt.UINT,
                                       wt.UINT, ctypes.POINTER(ctypes.c_size_t)]
user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenProcess.restype = ctypes.c_void_p
kernel32.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
kernel32.CreateNamedPipeW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD,
                                      wt.DWORD, ctypes.c_void_p]
kernel32.CreateNamedPipeW.restype = ctypes.c_void_p
kernel32.ConnectNamedPipe.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
kernel32.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p, ctypes.c_void_p]
kernel32.GetOverlappedResult.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(wt.DWORD), wt.BOOL]
kernel32.CancelIoEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.BOOL, wt.LPCWSTR]
kernel32.CreateEventW.restype = ctypes.c_void_p
kernel32.SetEvent.argtypes = [ctypes.c_void_p]
kernel32.WaitForMultipleObjects.argtypes = [wt.DWORD, ctypes.POINTER(ctypes.c_void_p), wt.BOOL, wt.DWORD]
kernel32.WaitForMultipleObjects.restype = wt.DWORD
kernel32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, ctypes.c_void_p]
kernel32.CreateFileW.restype = ctypes.c_void_p


class CaptureError(RuntimeError):
    pass


@dataclass
class Frame:
    seq: int
    wall_time: float          # time.time() when the capture returned
    native_size: tuple        # (width, height) of the remote session
    scale: float              # frame pixels = native pixels * scale
    bgr: np.ndarray           # H x W x 3 uint8, contiguous, already downsampled
    capture_ms: float         # WM_COPYDATA round trip + pipe transfer
    resize_ms: float

    @property
    def size(self):
        return (self.bgr.shape[1], self.bgr.shape[0])


def parse_target(target: str) -> str:
    """Window title the client uses: host only. Mirrors SendRdpInput.cs."""
    t = target.strip()
    if not t.startswith("[") and t.rfind(":") > 0:
        return t[:t.rfind(":")]
    if t.startswith("[") and "]:" in t:
        return t[1:t.index("]:")]
    return t


def _process_image(pid: int) -> str:
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        n = wt.DWORD(len(buf))
        return buf.value if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)) else ""
    finally:
        kernel32.CloseHandle(h)


def find_client_windows(title: str, exe_names=("wfreerdp.exe",)):
    """Top-level windows titled `title` that belong to an allowed client executable."""
    allowed = {n.lower() for n in exe_names}
    found = []

    def visit(hwnd, _):
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf, len(buf))
        if buf.value.lower() == title.lower():
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            image = _process_image(pid.value)
            if os.path.basename(image).lower() in allowed:
                found.append((hwnd, pid.value, image))
        return True

    user32.EnumWindows(WNDENUMPROC(visit), 0)
    return found


def parse_bmp_header(hdr: bytes):
    """Validate the header studio_capture_primary() writes; return (width, height, payload bytes)."""
    if len(hdr) < BMP_HEADER:
        raise CaptureError(f"short BMP header ({len(hdr)} bytes)")
    bf_type, bf_size, _, _, off = struct.unpack_from("<HIHHI", hdr, 0)
    bi_size, width, height, planes, bpp, compression = struct.unpack_from("<IiiHHI", hdr, 14)
    if bf_type != 0x4D42 or off != BMP_HEADER or bi_size != 40 or planes != 1:
        raise CaptureError("not a studio capture BMP")
    if bpp != 32 or compression != 0 or height >= 0:
        raise CaptureError(f"expected 32-bit top-down BI_RGB, got bpp={bpp} compression={compression} height={height}")
    height = -height
    if not (0 < width <= 16384 and 0 < height <= 16384):
        raise CaptureError(f"bad frame size {width}x{height}")
    payload = width * height * 4
    if bf_size != BMP_HEADER + payload:
        raise CaptureError(f"size field {bf_size} does not match {width}x{height}")
    return width, height, payload


class _PipeReader(threading.Thread):
    """Owns one overlapped pipe instance; reads exactly one BMP from the client."""

    def __init__(self, name: str, io_timeout_ms: int):
        super().__init__(daemon=True)
        self.name = name
        self.io_timeout_ms = io_timeout_ms
        self.error = None
        self.width = self.height = 0
        self.pixels = None
        self.handle = kernel32.CreateNamedPipeW(
            name, PIPE_ACCESS_INBOUND | FILE_FLAG_OVERLAPPED | FILE_FLAG_FIRST_PIPE_INSTANCE,
            PIPE_TYPE_BYTE | PIPE_REJECT_REMOTE_CLIENTS, 1, 0, PIPE_BUFFER, 0, None)
        if self.handle in (None, INVALID_HANDLE):
            raise CaptureError(f"CreateNamedPipe failed, Win32={ctypes.get_last_error()}")
        self.io_event = kernel32.CreateEventW(None, True, False, None)
        self.abort_event = kernel32.CreateEventW(None, True, False, None)
        self.client_done = kernel32.CreateEventW(None, True, False, None)

    # -- called from the capturing thread
    def abort(self):
        kernel32.SetEvent(self.abort_event)

    def sender_finished(self):
        kernel32.SetEvent(self.client_done)

    # -- reader thread
    def _wait(self, ov, extra):
        handles = (ctypes.c_void_p * (1 + len(extra)))(self.io_event, *extra)
        r = kernel32.WaitForMultipleObjects(len(handles), handles, False, self.io_timeout_ms)
        n = wt.DWORD()
        if r == WAIT_OBJECT_0:
            if kernel32.GetOverlappedResult(self.handle, ctypes.byref(ov), ctypes.byref(n), False):
                return n.value, 0
            return 0, ctypes.get_last_error()
        kernel32.CancelIoEx(self.handle, ctypes.byref(ov))
        kernel32.GetOverlappedResult(self.handle, ctypes.byref(ov), ctypes.byref(n), True)
        if r == WAIT_TIMEOUT:
            raise CaptureError("pipe I/O timed out")
        if r == WAIT_OBJECT_0 + 1:
            raise CaptureError("capture aborted")
        raise CaptureError("client reported success but never opened the pipe")

    def _overlapped(self):
        ov = OVERLAPPED()
        ov.hEvent = self.io_event
        return ov

    def _read_into(self, address: int, count: int):
        got = 0
        while got < count:
            ov = self._overlapped()
            chunk = min(count - got, PIPE_BUFFER)
            if kernel32.ReadFile(self.handle, address + got, chunk, None, ctypes.byref(ov)):
                n, err = self._wait(ov, [self.abort_event])
            else:
                err = ctypes.get_last_error()
                if err != ERROR_IO_PENDING:
                    n = 0
                else:
                    n, err = self._wait(ov, [self.abort_event])
            if err == ERROR_BROKEN_PIPE or (n == 0 and err == 0):
                raise CaptureError(f"client closed the pipe after {got} of {count} bytes")
            if err:
                raise CaptureError(f"ReadFile failed, Win32={err}")
            got += n

    def run(self):
        try:
            ov = self._overlapped()
            if not kernel32.ConnectNamedPipe(self.handle, ctypes.byref(ov)):
                err = ctypes.get_last_error()
                if err == ERROR_IO_PENDING:
                    _, err = self._wait(ov, [self.abort_event, self.client_done])
                    if err:
                        raise CaptureError(f"ConnectNamedPipe failed, Win32={err}")
                elif err != ERROR_PIPE_CONNECTED:
                    raise CaptureError(f"ConnectNamedPipe failed, Win32={err}")
            header = ctypes.create_string_buffer(BMP_HEADER)
            self._read_into(ctypes.addressof(header), BMP_HEADER)
            self.width, self.height, payload = parse_bmp_header(header.raw)
            pixels = np.empty((self.height, self.width, 4), dtype=np.uint8)
            self._read_into(pixels.ctypes.data, payload)
            self.pixels = pixels
        except Exception as exc:  # reported to the capturing thread
            self.error = exc
        finally:
            kernel32.CloseHandle(self.handle)
            for h in (self.io_event, self.abort_event, self.client_done):
                kernel32.CloseHandle(h)


def downsample(bgra: np.ndarray, scale: float) -> np.ndarray:
    if scale >= 1.0:
        return cv2.cvtColor(bgra, cv2.COLOR_BGRA2BGR)
    h, w = bgra.shape[:2]
    small = cv2.resize(bgra, (max(1, round(w * scale)), max(1, round(h * scale))), interpolation=cv2.INTER_AREA)
    return cv2.cvtColor(small, cv2.COLOR_BGRA2BGR)


class RdpClient:
    """One studio FreeRDP client window, addressed like SendRdpInput: --target HOST:PORT."""

    def __init__(self, target: str, exe_names=("wfreerdp.exe",), timeout_ms: int = 5000,
                 io_timeout_ms: int = 5000):
        self.target = target
        self.title = parse_target(target)
        self.exe_names = exe_names
        self.timeout_ms = timeout_ms
        self.io_timeout_ms = io_timeout_ms
        self._hwnd = None
        self._seq = 0

    def window(self):
        if self._hwnd and user32.IsWindow(self._hwnd):
            return self._hwnd
        found = find_client_windows(self.title, self.exe_names)
        if len(found) != 1:
            raise CaptureError(f"expected exactly one client window titled {self.title!r} "
                               f"({'/'.join(self.exe_names)}), found {len(found)}")
        self._hwnd = found[0][0]
        return self._hwnd

    def _send(self, kind: int, payload: bytes) -> int:
        buf = ctypes.create_string_buffer(payload, len(payload))
        cds = COPYDATASTRUCT(kind, len(payload), ctypes.cast(buf, ctypes.c_void_p))
        result = ctypes.c_size_t()
        if not user32.SendMessageTimeoutW(self.window(), WM_COPYDATA, 0, ctypes.byref(cds), SMTO_FLAGS,
                                          self.timeout_ms, ctypes.byref(result)):
            err = ctypes.get_last_error()
            self._hwnd = None  # re-resolve next time (client may have restarted)
            raise CaptureError(f"client did not answer WM_COPYDATA (Win32={err})")
        return result.value

    def status(self) -> dict:
        r = self._send(KIND_STATUS, b"\0\0\0\0")
        if not 1000 <= r <= 1007:
            raise CaptureError(f"status endpoint unavailable (result={r})")
        bits = r - 1000
        return {"connected": bool(bits & 1), "unicode": bool(bits & 2), "input_suspended": bool(bits & 4)}

    def capture(self, scale: float = 0.5) -> Frame:
        if not 0.05 <= scale <= 1.0:
            raise ValueError("scale must be within 0.05..1.0")
        name = rf"\\.\pipe\studio-rdpcap-{os.getpid()}-{secrets.token_hex(8)}"
        reader = _PipeReader(name, self.io_timeout_ms)
        reader.start()
        t0 = time.perf_counter()
        try:
            result = self._send(KIND_CAPTURE, name.encode("utf-8"))
        except BaseException:
            reader.abort()
            reader.join()
            raise
        if result != 1:
            reader.abort()
            reader.join()
            raise CaptureError(f"client rejected the capture (result={result}); see %TEMP%\\studio-capture.log")
        reader.sender_finished()
        reader.join()
        if reader.error:
            raise CaptureError(str(reader.error)) from reader.error
        capture_ms = (time.perf_counter() - t0) * 1000
        t1 = time.perf_counter()
        bgr = downsample(reader.pixels, scale)
        resize_ms = (time.perf_counter() - t1) * 1000
        self._seq += 1
        return Frame(self._seq, time.time(), (reader.width, reader.height), scale, bgr, capture_ms, resize_ms)
