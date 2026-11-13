#!/usr/bin/env python3
"""A 4 cm chameleon that pops up on one window.

Drag him onto any window and he perches on the nearest edge, facing in.
Scroll, or middle-drag, to turn him. He shifts colour toward the window
he is on. Right-click him to take him off the desktop.
"""

from __future__ import annotations

import json
import math
import os
import signal
import sys
import time
from pathlib import Path

def prefer_xwayland(max_wait_s: float = 25.0) -> str:
    """Use XWayland so the chameleon can perch on a window. Call before Gdk or Gtk."""
    if os.environ.get("CHAMELEON_NATIVE_WAYLAND"):
        os.environ["GDK_BACKEND"] = "wayland"
        return "wayland"
    forced = os.environ.get("GDK_BACKEND")
    if forced and forced != "x11":
        return forced
    display = os.environ.get("DISPLAY") or ":0"
    os.environ["DISPLAY"] = display
    screen = display.split(":")[-1].split(".")[0]
    if not screen.isdigit():
        screen = "0"
    sock = f"/tmp/.X11-unix/X{screen}"
    deadline = time.time() + max_wait_s
    while not os.path.exists(sock):
        if time.time() >= deadline:
            sys.stderr.write(
                "chameleon: XWayland is not up; the pet cannot perch on a window\n"
            )
            return forced or "wayland"
        time.sleep(0.2)
    os.environ["GDK_BACKEND"] = "x11"
    return "x11"


prefer_xwayland()

import gi

gi.require_version("Atspi", "2.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gtk", "3.0")

from gi.repository import Atspi, Gdk, GdkPixbuf, Gio, GLib, Gtk
from PIL import Image

HOME = Path.home()
ASSET = Path(__file__).resolve().parent
PID_FILE = HOME / ".cache" / "chameleon.pid"
LOG_FILE = Path("/tmp/chameleon.log")
PRGNAME = "chameleon"
LENGTH_MM = 40.0
POLL_MS = 40

_PLACE = ""
_EDGE_ANGLE = {
    "left": 0.0,
    "top": math.pi / 2,
    "right": math.pi,
    "bottom": -math.pi / 2,
}


def acc_path(win) -> str:
    path = getattr(win, "path", None)
    if path:
        return str(path)
    return f"id:{id(win)}"


def window_title(win) -> str:
    try:
        title = (win.get_name() or "").strip()
    except Exception:
        title = ""
    return title or "Window"


def log(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}\n"
    try:
        LOG_FILE.open("a").write(line)
    except OSError:
        pass


def claim_pid(replace: bool) -> None:
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    if PID_FILE.exists():
        try:
            old = int(PID_FILE.read_text().strip() or "0")
        except ValueError:
            old = 0
        if old and old != os.getpid() and Path(f"/proc/{old}").exists():
            if not replace:
                log(f"already running as {old}")
                raise SystemExit(0)
            os.kill(old, signal.SIGTERM)
            for _ in range(30):
                if not Path(f"/proc/{old}").exists():
                    break
                time.sleep(0.05)
    PID_FILE.write_text(str(os.getpid()))


def px_per_mm(x: int, y: int) -> float:
    display = Gdk.Display.get_default()
    mon = display.get_monitor_at_point(max(0, x), max(0, y))
    if mon is None:
        return 2048 / 340
    geo = mon.get_geometry()
    mm = mon.get_width_mm()
    if not mm or mm < 40:
        mm = 340
    return geo.width / float(mm)


def screen_size() -> tuple[int, int]:
    display = Gdk.Display.get_default()
    mon = display.get_monitor(0)
    geo = mon.get_geometry()
    return geo.width, geo.height


def wrap_angle(angle: float) -> float:
    return (angle + math.pi) % (2 * math.pi) - math.pi


def lerp_angle(a: float, b: float, u: float) -> float:
    return wrap_angle(a + wrap_angle(b - a) * u)


class _DragPointer:
    """Pointer position while a drag is held. Separate display, no menu overlay."""

    def __init__(self) -> None:
        self.ok = False
        self._listening = False
        try:
            import ctypes
            import ctypes.util

            self._c = ctypes
            x = ctypes.cdll.LoadLibrary(ctypes.util.find_library("X11"))
            xi = ctypes.cdll.LoadLibrary(ctypes.util.find_library("Xi"))
        except OSError:
            return
        x.XOpenDisplay.restype = ctypes.c_void_p
        x.XDefaultRootWindow.restype = ctypes.c_ulong
        x.XPending.restype = ctypes.c_int
        x.XPending.argtypes = [ctypes.c_void_p]
        x.XQueryPointer.restype = ctypes.c_int
        x.XQueryPointer.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_uint),
        ]
        self._x = x
        self._xi = xi
        self._dpy = x.XOpenDisplay(None)
        if not self._dpy:
            return
        self._root = x.XDefaultRootWindow(self._dpy)
        self._opcode = -1
        try:
            self._setup()
        except Exception as exc:
            log(f"pointer {exc}")
            return
        self.ok = True

    def _setup(self) -> None:
        c = self._c
        x = self._x
        xi = self._xi
        x.XQueryExtension.argtypes = [
            c.c_void_p,
            c.c_char_p,
            c.POINTER(c.c_int),
            c.POINTER(c.c_int),
            c.POINTER(c.c_int),
        ]
        x.XQueryExtension.restype = c.c_int
        opcode = c.c_int()
        event = c.c_int()
        error = c.c_int()
        if not x.XQueryExtension(self._dpy, b"XInputExtension", c.byref(opcode), c.byref(event), c.byref(error)):
            return
        self._opcode = int(opcode.value)
        xi.XIQueryVersion.argtypes = [c.c_void_p, c.POINTER(c.c_int), c.POINTER(c.c_int)]
        xi.XIQueryVersion.restype = c.c_int
        major = c.c_int(2)
        minor = c.c_int(2)
        xi.XIQueryVersion(self._dpy, c.byref(major), c.byref(minor))

        class XIEventMask(c.Structure):
            _fields_ = [("deviceid", c.c_int), ("mask_len", c.c_int), ("mask", c.POINTER(c.c_ubyte))]

        class XEvent(c.Union):
            _fields_ = [("pad", c.c_long * 24)]

        class XGenericEventCookie(c.Structure):
            _fields_ = [
                ("type", c.c_int),
                ("serial", c.c_ulong),
                ("send_event", c.c_int),
                ("display", c.c_void_p),
                ("extension", c.c_int),
                ("evtype", c.c_int),
                ("cookie", c.c_uint),
                ("data", c.c_void_p),
            ]

        class XIRaw(c.Structure):
            _fields_ = [
                ("type", c.c_int),
                ("serial", c.c_ulong),
                ("send_event", c.c_int),
                ("display", c.c_void_p),
                ("extension", c.c_int),
                ("evtype", c.c_int),
                ("time", c.c_ulong),
                ("deviceid", c.c_int),
                ("sourceid", c.c_int),
                ("detail", c.c_int),
            ]

        self._XIEventMask = XIEventMask
        self._XEvent = XEvent
        self._Cookie = XGenericEventCookie
        self._Raw = XIRaw
        xi.XISelectEvents.argtypes = [c.c_void_p, c.c_ulong, c.c_void_p, c.c_int]
        xi.XISelectEvents.restype = c.c_int
        x.XFlush.argtypes = [c.c_void_p]
        x.XNextEvent.argtypes = [c.c_void_p, c.c_void_p]
        x.XGetEventData.argtypes = [c.c_void_p, c.POINTER(XGenericEventCookie)]
        x.XGetEventData.restype = c.c_int
        x.XFreeEventData.argtypes = [c.c_void_p, c.POINTER(XGenericEventCookie)]

    def listen(self, on: bool) -> None:
        if not self.ok or self._opcode < 0 or on == self._listening:
            return
        c = self._c
        raw = bytearray(4)
        if on:
            for evtype in (15, 16):
                raw[evtype >> 3] |= 1 << (evtype & 7)
        mask = (c.c_ubyte * 4).from_buffer_copy(bytes(raw))
        self._mask_buf = mask
        select = self._XIEventMask(1, 4, mask)
        self._xi.XISelectEvents(self._dpy, self._root, c.byref(select), 1)
        self._x.XFlush(self._dpy)
        self._listening = on

    def poll(self) -> tuple[int, int, int, list[int]]:
        """x, y, button mask, released button numbers."""
        if not self.ok:
            return 0, 0, 0, []
        c = self._c
        root_ret = c.c_ulong()
        child = c.c_ulong()
        rx = c.c_int()
        ry = c.c_int()
        wx = c.c_int()
        wy = c.c_int()
        mask = c.c_uint()
        self._x.XQueryPointer(
            self._dpy,
            self._root,
            c.byref(root_ret),
            c.byref(child),
            c.byref(rx),
            c.byref(ry),
            c.byref(wx),
            c.byref(wy),
            c.byref(mask),
        )
        return int(rx.value), int(ry.value), int(mask.value), self._releases()

    def _releases(self) -> list[int]:
        if not self._listening or self._opcode < 0:
            return []
        x = self._x
        c = self._c
        found: list[int] = []
        while x.XPending(self._dpy):
            event = self._XEvent()
            x.XNextEvent(self._dpy, c.byref(event))
            cookie = c.cast(c.byref(event), c.POINTER(self._Cookie)).contents
            if cookie.type != 35 or cookie.extension != self._opcode:
                continue
            if not x.XGetEventData(self._dpy, c.byref(cookie)):
                continue
            try:
                raw = c.cast(cookie.data, c.POINTER(self._Raw)).contents
                if raw.evtype == 16 and raw.detail in (1, 2) and raw.detail not in found:
                    found.append(int(raw.detail))
            finally:
                x.XFreeEventData(self._dpy, c.byref(cookie))
        return found


def pil_to_pixbuf(im: Image.Image):
    data = im.tobytes()
    return GdkPixbuf.Pixbuf.new_from_bytes(
        GLib.Bytes.new(data),
        GdkPixbuf.Colorspace.RGB,
        True,
        8,
        im.width,
        im.height,
        im.width * 4,
    )


def tint_image(im: Image.Image, rgb: tuple[float, float, float]) -> Image.Image:
    src = im.tobytes()
    out = bytearray(src)
    tr, tg, tb = (rgb[0] / 255.0, rgb[1] / 255.0, rgb[2] / 255.0)
    for i in range(0, len(out), 4):
        alpha = out[i + 3]
        if alpha < 8:
            continue
        r = out[i] / 255.0
        g = out[i + 1] / 255.0
        b = out[i + 2] / 255.0
        y = 0.2126 * r + 0.7152 * g + 0.0722 * b
        scale = min(1.7, y / 0.42)
        mix = 0.9
        out[i] = int(min(255, (min(1.0, tr * scale) * mix + r * (1 - mix)) * 255))
        out[i + 1] = int(min(255, (min(1.0, tg * scale) * mix + g * (1 - mix)) * 255))
        out[i + 2] = int(min(255, (min(1.0, tb * scale) * mix + b * (1 - mix)) * 255))
    return Image.frombytes("RGBA", im.size, bytes(out))


def average_png(path: Path) -> tuple[float, float, float] | None:
    try:
        im = Image.open(path).convert("RGBA")
    except OSError:
        return None
    pixels = list(im.getdata())
    if not pixels:
        return None
    width = im.width
    r = g = b = n = 0.0
    step = max(1, min(width, im.height) // 40)
    for y in range(0, im.height, step):
        row = y * width
        for x in range(0, width, step):
            pr, pg, pb, pa = pixels[row + x]
            if pa < 20:
                continue
            r += pr
            g += pg
            b += pb
            n += 1
    if n < 8:
        return None
    return (r / n, g / n, b / n)


class Assets:
    def __init__(self) -> None:
        self.layout = json.loads((ASSET / "layout.json").read_text())
        self.body = Image.open(ASSET / "body.png").convert("RGBA")
        self.iris = Image.open(ASSET / "iris.png").convert("RGBA")
        self.tongue = Image.open(ASSET / "tongue.png").convert("RGBA")
        self.fly = Image.open(ASSET / "fly.png").convert("RGBA")


class Chameleon(Gtk.Window):
    def __init__(self, app: "App", accessible, assets: Assets) -> None:
        super().__init__(title="Chameleon", type=Gtk.WindowType.TOPLEVEL)
        self.app = app
        self.accessible = accessible
        self.assets = assets
        self.layout = assets.layout
        self.color: list[float] | None = None
        self.target: tuple[float, float, float] | None = None
        self._applied: tuple[int, int, int] | None = None
        self._body_pb = None
        self._next_sample = time.monotonic() + 0.4
        self._next_eye = time.monotonic() + 0.5
        self._next_tongue = time.monotonic() + 1.6
        self._eye = (0.0, 0.0)
        self._eye_to = (0.0, 0.0)
        self._tongue = 0.0
        self._tongue_dir = 0
        self._fly = 0.0
        self._born = time.monotonic()
        self._span = 160
        self._body_size = (160, 60)
        self._angle = 0.0
        self._perch_side = "left"
        self._perch_along = 0.5
        self._free = False
        self._dragging = False
        self._turning = False
        self._grab_dx = 0.0
        self._grab_dy = 0.0
        self._px = 0
        self._py = 0
        self._side = 220
        self._overlap = 18
        self._grip = (140.0, 40.0)
        self._at_x: int | None = None
        self._at_y: int | None = None
        self._gliding = False
        self._glide_t0 = 0.0
        self._glide_off = (0.0, 0.0)
        self._glide_ready = False
        self._spin_from: float | None = None
        self._spin_to: float | None = None
        self._frame_timer = 0
        self._saw_button = False
        self._glide_target: tuple[int, int] | None = None

        self.set_name(PRGNAME)
        self.set_app_paintable(True)
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_accept_focus(False)
        self.set_focus_on_map(False)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.set_keep_above(True)
        screen = self.get_screen()
        visual = screen.get_rgba_visual() if screen is not None else None
        if visual is not None:
            self.set_visual(visual)
        self.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.BUTTON1_MOTION_MASK
            | Gdk.EventMask.BUTTON2_MOTION_MASK
            | Gdk.EventMask.SCROLL_MASK
            | Gdk.EventMask.SMOOTH_SCROLL_MASK
        )
        self.connect("draw", self._draw)
        self.connect("button-press-event", self._press)
        self.connect("button-release-event", self._release)
        self.connect("motion-notify-event", self._motion)
        self.connect("scroll-event", self._scroll)
        self.connect("grab-broken-event", self._grab_broken)
        self.connect("realize", self._realized)
        self._fit()
        self.show_all()

    def _realized(self, *_args) -> None:
        gdkwin = self.get_window()
        if gdkwin is None:
            return
        try:
            gdkwin.set_override_redirect(True)
        except Exception as exc:
            log(f"redirect {exc}")
        gdkwin.set_events(
            gdkwin.get_events()
            | Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.BUTTON1_MOTION_MASK
            | Gdk.EventMask.BUTTON2_MOTION_MASK
            | Gdk.EventMask.SCROLL_MASK
            | Gdk.EventMask.SMOOTH_SCROLL_MASK
        )

    def _fit(self) -> None:
        ext = self._extents()
        origin = (ext.x, ext.y) if ext is not None else (200, 200)
        ppm = px_per_mm(*origin)
        span = max(80, int(round(LENGTH_MM * ppm)))
        bw, bh = self.assets.body.size
        body_h = max(24, int(round(span * bh / bw)))
        tongue_w = int(span * float(self.layout["tongueFrac"]))
        self._span = span
        self._body_size = (span, body_h)
        self._tongue_size = (
            tongue_w,
            max(8, int(tongue_w * self.assets.tongue.size[1] / self.assets.tongue.size[0])),
        )
        iris = max(8, int(span * float(self.layout["irisFrac"])))
        self._iris_size = iris
        fly_w = max(8, int(span * 0.055))
        self._fly_size = (
            fly_w,
            max(6, int(fly_w * self.assets.fly.size[1] / self.assets.fly.size[0])),
        )
        overlap = max(18, int(span * 0.22))
        perch = float(self.layout["perch"])
        gx = float(span - overlap)
        gy = float(body_h * perch)
        self._overlap = overlap
        self._grip = (gx, gy)
        half = max(gx, span - gx + tongue_w, gy, body_h - gy) + 16
        self._side = int(math.ceil(half * 2))
        self.set_size_request(self._side, self._side)
        self.resize(self._side, self._side)
        self._resize_art()
        self._shape_ready = False

    def _resize_art(self) -> None:
        span, body_h = self._body_size
        body = self.assets.body.resize((span, body_h), Image.Resampling.LANCZOS)
        self._body_im = body
        self._body_pb = pil_to_pixbuf(body)
        iris = self.assets.iris.resize((self._iris_size, self._iris_size), Image.Resampling.LANCZOS)
        self._iris_pb = pil_to_pixbuf(iris)
        tw, th = self._tongue_size
        tongue = self.assets.tongue.resize((tw, th), Image.Resampling.LANCZOS)
        self._tongue_pb = pil_to_pixbuf(tongue)
        fw, fh = self._fly_size
        fly = self.assets.fly.resize((fw, fh), Image.Resampling.LANCZOS)
        self._fly_pb = pil_to_pixbuf(fly)
        self._applied = None

    def _extents(self):
        if self.accessible is None:
            return None
        try:
            state = self.accessible.get_state_set()
            if state.contains(Atspi.StateType.DEFUNCT) or state.contains(Atspi.StateType.ICONIFIED):
                return None
            if not state.contains(Atspi.StateType.SHOWING):
                return None
            return window_extents(self.accessible)
        except Exception:
            return None

    def _anchor(self, ext) -> tuple[float, float]:
        along = min(0.9, max(0.1, self._perch_along))
        overlap = self._overlap
        if self._perch_side == "right":
            return ext.x + ext.width - overlap, ext.y + ext.height * along
        if self._perch_side == "top":
            return ext.x + ext.width * along, ext.y + overlap
        if self._perch_side == "bottom":
            return ext.x + ext.width * along, ext.y + ext.height - overlap
        return ext.x + overlap, ext.y + ext.height * along

    def _place(self, x: int, y: int, dead: int = 0) -> None:
        x = int(x)
        y = int(y)
        if self._at_x is not None and abs(x - self._at_x) <= dead and abs(y - self._at_y) <= dead:
            return
        self._at_x = x
        self._at_y = y
        self.move(x, y)
        display = self.get_display()
        if display is not None:
            display.flush()

    def _origin(self) -> tuple[int, int]:
        if self._at_x is not None and self._at_y is not None:
            return self._at_x, self._at_y
        origin = self.get_position()
        return int(origin[0]), int(origin[1])

    def follow(self) -> bool:
        if self._dragging or self._turning:
            return True
        if self._free or self.accessible is None:
            if self._gliding:
                self._step_glide(None)
            if not self.get_visible():
                self.show_all()
            return True
        ext = self._extents()
        if ext is None:
            try:
                gone = self.accessible.get_state_set().contains(Atspi.StateType.DEFUNCT)
            except Exception:
                gone = True
            if gone:
                self._free = True
                self.accessible = None
                self._gliding = False
                self.show_all()
                return True
            self.hide()
            return True
        if not self.get_visible():
            self.show_all()
        ax, ay = self._anchor(ext)
        sw, sh = screen_size()
        ax = max(4, min(sw - 4, ax))
        ay = max(4, min(sh - 4, ay))
        target = (int(ax - self._side / 2), int(ay - self._side / 2))
        self._glide_target = target
        if self._gliding:
            self._step_glide(target)
            return True
        self._place(target[0], target[1], dead=1)
        return True

    def _begin_glide(self, angle: float | None) -> None:
        self._gliding = True
        self._glide_t0 = time.monotonic()
        self._glide_ready = False
        self._glide_off = (0.0, 0.0)
        if angle is None:
            self._spin_from = None
            self._spin_to = None
        else:
            self._spin_from = self._angle
            self._spin_to = angle
        self._ensure_frame()

    def _step_glide(self, target: tuple[int, int] | None) -> None:
        now = time.monotonic()
        u = (now - self._glide_t0) / 0.16
        if u >= 1.0 or target is None:
            self._gliding = False
            if target is not None:
                self._place(target[0], target[1])
            if self._spin_to is not None:
                self._angle = self._spin_to
            self._spin_from = None
            self._spin_to = None
            self._shape_ready = False
            self.queue_draw()
            return
        su = u * u * (3.0 - 2.0 * u)
        if not self._glide_ready:
            ox, oy = self._origin()
            self._glide_off = (ox - target[0], oy - target[1])
            self._glide_ready = True
        offx, offy = self._glide_off
        self._place(int(target[0] + offx * (1.0 - su)), int(target[1] + offy * (1.0 - su)))
        if self._spin_from is not None and self._spin_to is not None:
            self._angle = lerp_angle(self._spin_from, self._spin_to, su)
            self.queue_draw()

    def tick(self, now: float) -> None:
        if self._dragging or self._turning:
            return
        if now >= self._next_eye:
            ang = (hash((int(now * 4), id(self))) % 628) / 100.0
            reach = self._iris_size * 0.16
            self._eye_to = (math.cos(ang) * reach, math.sin(ang) * reach * 0.8)
            self._next_eye = now + 0.45 + (hash(int(now * 10)) % 100) / 120.0
        self._eye = (
            self._eye[0] + (self._eye_to[0] - self._eye[0]) * 0.35,
            self._eye[1] + (self._eye_to[1] - self._eye[1]) * 0.35,
        )
        if self._tongue_dir == 0 and now >= self._next_tongue:
            self._tongue_dir = 1
            self._fly = 1.0
        if self._tongue_dir == 1:
            self._tongue = min(1.0, self._tongue + 0.34)
            if self._tongue >= 1.0:
                self._tongue_dir = -1
                self._fly = 0.0
        elif self._tongue_dir == -1:
            self._tongue = max(0.0, self._tongue - 0.28)
            if self._tongue <= 0.0:
                self._tongue_dir = 0
                self._next_tongue = now + 5.2 + (hash(int(now)) % 40) / 10.0
        if self.target is not None:
            if self.color is None:
                self.color = [self.target[0], self.target[1], self.target[2]]
            else:
                for i in range(3):
                    self.color[i] += (self.target[i] - self.color[i]) * 0.18
            self._apply_color()
        if now >= self._next_sample and not self._gliding:
            self._next_sample = now + 2.4
            self.app.sample(self)
        self.queue_draw()

    def _apply_color(self) -> None:
        if not self.color:
            return
        key = tuple(int(max(0, min(255, c))) for c in self.color)
        if self._applied is not None and all(abs(key[i] - self._applied[i]) < 3 for i in range(3)):
            return
        self._applied = key
        tinted = tint_image(self._body_im, key)
        self._body_pb = pil_to_pixbuf(tinted)

    def _draw(self, _widget, cr) -> bool:
        span, body_h = self._body_size
        perch = float(self.layout["perch"])
        breath = 1.0 + math.sin((time.monotonic() - self._born) * 1.7) * 0.012
        gx, gy = self._grip
        cr.save()
        cr.translate(self._side / 2, self._side / 2)
        cr.rotate(self._angle)
        cr.translate(-gx, -gy)
        cr.save()
        cr.translate(0, body_h * perch)
        cr.scale(1.0, breath)
        cr.translate(0, -body_h * perch)
        Gdk.cairo_set_source_pixbuf(cr, self._body_pb, 0, 0)
        cr.paint()
        eye_x = self.layout["eye"][0] * span
        eye_y = self.layout["eye"][1] * body_h
        iris = self._iris_size
        Gdk.cairo_set_source_pixbuf(cr, self._iris_pb, eye_x - iris / 2, eye_y - iris / 2)
        cr.paint()
        pupil = iris * 0.40
        cx = eye_x + self._eye[0]
        cy = eye_y + self._eye[1]
        cr.set_source_rgba(0.03, 0.03, 0.03, 1)
        cr.arc(cx, cy, pupil / 2, 0, math.tau)
        cr.fill()
        cr.set_source_rgba(1, 1, 1, 0.92)
        cr.arc(cx - pupil * 0.18, cy - pupil * 0.2, max(1.2, pupil * 0.14), 0, math.tau)
        cr.fill()
        cr.restore()

        if self._fly > 0.05 or self._tongue > 0.05:
            mouth_x = self.layout["mouth"][0] * span
            mouth_y = self.layout["mouth"][1] * body_h
            ang = math.radians(-16)
            reach = self._tongue_size[0] * 0.92
            if self._fly > 0.05:
                fx = mouth_x + math.cos(ang) * reach - self._fly_size[0] * 0.3
                fy = mouth_y + math.sin(ang) * reach - self._fly_size[1] * 0.2
                cr.save()
                cr.translate(fx, fy)
                Gdk.cairo_set_source_pixbuf(cr, self._fly_pb, 0, 0)
                cr.paint()
                cr.restore()
            if self._tongue > 0.02:
                cr.save()
                cr.translate(mouth_x, mouth_y)
                cr.rotate(ang)
                cr.scale(max(0.05, self._tongue), 0.45 + 0.55 * self._tongue)
                cr.translate(0, -self._tongue_size[1] * 0.42)
                Gdk.cairo_set_source_pixbuf(cr, self._tongue_pb, 0, 0)
                cr.paint()
                cr.restore()
        cr.restore()
        if not self._shape_ready and not self._dragging and not self._turning and not self._gliding:
            self._apply_shape()
            self._shape_ready = True
        return False

    def _apply_shape(self) -> None:
        gdkwin = self.get_window()
        if gdkwin is None or self._body_im is None:
            self._shape_ready = False
            return
        import cairo

        side = self._side
        span, body_h = self._body_size
        pixels = self._body_im.tobytes()
        gx, gy = self._grip
        cos = math.cos(self._angle)
        sin = math.sin(self._angle)
        cx = side / 2
        cy = side / 2
        mask = bytearray(side * side)
        stride = span * 4
        for ly in range(body_h):
            row = ly * stride
            for lx in range(span):
                if pixels[row + lx * 4 + 3] < 40:
                    continue
                dx = lx - gx
                dy = ly - gy
                x = int(dx * cos - dy * sin + cx)
                y = int(dx * sin + dy * cos + cy)
                for oy in (-1, 0, 1):
                    yy = y + oy
                    if yy < 0 or yy >= side:
                        continue
                    base = yy * side
                    for ox in (-1, 0, 1):
                        xx = x + ox
                        if 0 <= xx < side:
                            mask[base + xx] = 1
        region = cairo.Region()
        for y in range(side):
            base = y * side
            x = 0
            while x < side:
                if not mask[base + x]:
                    x += 1
                    continue
                start = x
                x += 1
                while x < side and mask[base + x]:
                    x += 1
                region.union(cairo.RectangleInt(start, y, x - start, 1))
        try:
            gdkwin.input_shape_combine_region(region, 0, 0)
        except Exception as exc:
            log(f"shape {exc}")

    def _grab_pointer(self, event) -> None:
        window = self.get_window()
        if window is None:
            return
        display = window.get_display()
        seat = display.get_default_seat()
        cursor = Gdk.Cursor.new_for_display(display, Gdk.CursorType.FLEUR)
        try:
            seat.grab(
                window,
                Gdk.SeatCapabilities.ALL_POINTING,
                False,
                cursor,
                event,
                None,
                None,
            )
        except Exception as exc:
            log(f"grab {exc}")

    def _ungrab(self) -> None:
        try:
            self.get_display().get_default_seat().ungrab()
        except Exception as exc:
            log(f"ungrab {exc}")

    def _pointer(self) -> tuple[int, int, int, list[int]]:
        display = Gdk.Display.get_default()
        _screen, x, y = display.get_default_seat().get_pointer().get_position()
        mask = 0
        releases: list[int] = []
        ptr = self.app.pointer
        if ptr.ok and ptr._listening:
            try:
                _rx, _ry, mask, releases = ptr.poll()
            except Exception as exc:
                log(f"pointer {exc}")
        return int(x), int(y), mask, releases

    def _ensure_frame(self) -> None:
        if not self._frame_timer:
            self._frame_timer = GLib.timeout_add(8, self._frame, priority=GLib.PRIORITY_HIGH)

    def _frame(self) -> bool:
        try:
            if self._dragging:
                self._step_drag()
            elif self._turning:
                self._step_turn()
            if self._gliding and not self._dragging and not self._turning and self._glide_target is not None:
                self._step_glide(self._glide_target)
        except Exception as exc:
            log(f"frame {exc}")
        if self._dragging or self._turning or self._gliding:
            return True
        self._frame_timer = 0
        return False

    def _step_drag(self) -> None:
        x, y, mask, releases = self._pointer()
        self._px, self._py = x, y
        self._place(int(round(x - self._grab_dx)), int(round(y - self._grab_dy)))
        if 1 in releases:
            self._finish_drag()

    def _step_turn(self) -> None:
        x, y, mask, releases = self._pointer()
        dx = x - self._px
        self._px, self._py = x, y
        if dx:
            self._angle = wrap_angle(self._angle + dx * 0.009)
            self.queue_draw()
        if 2 in releases:
            self._finish_turn()

    def _finish_drag(self) -> None:
        if not self._dragging:
            return
        self._dragging = False
        self._saw_button = False
        self.app.pointer.listen(False)
        self._ungrab()
        self.app.drop_on(self, self._px, self._py)

    def _finish_turn(self) -> None:
        if not self._turning:
            return
        self._turning = False
        self._saw_button = False
        self.app.pointer.listen(False)
        self._ungrab()
        self._shape_ready = False
        self.queue_draw()
        log(f"turned {math.degrees(self._angle):.0f}")

    def _press(self, _widget, event) -> bool:
        if event.button == 3:
            self.app.quit()
            return True
        if event.button == 2:
            self._turning = True
            self._gliding = False
            self._saw_button = False
            self._px = int(event.x_root)
            self._py = int(event.y_root)
            self.app.pointer.listen(True)
            self._grab_pointer(event)
            self._ensure_frame()
            return True
        if event.button == 1:
            ox, oy = self._origin()
            self._dragging = True
            self._gliding = False
            self._saw_button = False
            self._grab_dx = float(event.x_root) - ox
            self._grab_dy = float(event.y_root) - oy
            self._px = int(event.x_root)
            self._py = int(event.y_root)
            self.app.pointer.listen(True)
            self._grab_pointer(event)
            self._ensure_frame()
            return True
        return False

    def _motion(self, _widget, event) -> bool:
        if self._dragging or self._turning:
            return True
        return False

    def _release(self, _widget, event) -> bool:
        if event.button == 2 and self._turning:
            self._finish_turn()
            return True
        if event.button == 1 and self._dragging:
            self._px = int(event.x_root)
            self._py = int(event.y_root)
            self._finish_drag()
            return True
        return False

    def _grab_broken(self, *_args) -> bool:
        if self._dragging or self._turning:
            log("grab broken, still tracking")
        return False

    def _scroll(self, _widget, event) -> bool:
        direction = event.direction
        delta = 0.0
        if direction == Gdk.ScrollDirection.UP or direction == Gdk.ScrollDirection.LEFT:
            delta = -1.0
        elif direction == Gdk.ScrollDirection.DOWN or direction == Gdk.ScrollDirection.RIGHT:
            delta = 1.0
        elif direction == Gdk.ScrollDirection.SMOOTH:
            _ok, dx, dy = event.get_scroll_deltas()
            delta = dy if abs(dy) >= abs(dx) else dx
        if delta == 0.0 or self._dragging:
            return False
        self._angle = wrap_angle(self._angle + delta * math.radians(12))
        self._shape_ready = False
        self.queue_draw()
        return True


class App:
    def __init__(self) -> None:
        self.assets = Assets()
        self.pointer = _DragPointer()
        self.pets: dict[str, Chameleon] = {}
        self._sampling = False
        self._sample_queue: list[Chameleon] = []
        self._bus_ready = False
        self._quitting = False
        Gio.bus_own_name(
            Gio.BusType.SESSION,
            "org.gnome.Screenshot",
            Gio.BusNameOwnerFlags.ALLOW_REPLACEMENT,
            None,
            self._bus_acquired,
            None,
        )
        css = Gtk.CssProvider()
        css.load_from_data(
            b"""
            window#chameleon,
            window#chameleon.background {
              background-color: transparent;
              background-image: none;
              border: none;
              box-shadow: none;
            }
            """
        )
        screen = Gdk.Screen.get_default()
        if screen is not None:
            Gtk.StyleContext.add_provider_for_screen(
                screen, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
        Atspi.init()
        GLib.timeout_add(POLL_MS, self._poll)
        if _PLACE:
            GLib.timeout_add(600, self._place_once)
        else:
            GLib.timeout_add(600, self._place_active)

    def _bus_acquired(self, _conn, name) -> None:
        self._bus_ready = True
        log(f"bus {name}")

    def _place_once(self) -> bool:
        needle = _PLACE.lower()
        exact = None
        partial = None
        for _app_name, win, ext in iter_windows():
            title = window_title(win)
            folded = title.lower()
            if folded == needle and exact is None:
                exact = (win, ext, title)
            elif needle in folded and partial is None:
                partial = (win, ext, title)
        chosen = exact or partial
        if chosen is None:
            log(f"no window matching {_PLACE!r}")
            return False
        win, ext, title = chosen
        node = self._shell_match(ext) or win
        self.attach(node)
        log(f"placed on {title}")
        return False

    def _place_active(self) -> bool:
        if self.pets:
            return False
        fallback = None
        for _app_name, win, ext in iter_windows():
            if fallback is None:
                fallback = (win, ext)
            try:
                state = win.get_state_set()
            except Exception:
                continue
            if state.contains(Atspi.StateType.ACTIVE):
                node = self._shell_match(ext) or win
                self.attach(node)
                log(f"perched on {window_title(win)}")
                return False
        if fallback is not None:
            win, ext = fallback
            node = self._shell_match(ext) or win
            self.attach(node)
            log(f"perched on {window_title(win)}")
            return False

    def _shell_match(self, ext):
        """Shell actor with this window's real screen rectangle.

        Several programs, including the terminal, report every frame at 0, 0.
        The shell actor is the one that moves when the window is dragged.
        """
        hits = []
        positioned = ext.x > 2 or ext.y > 2
        for node, rx, ry, rw, rh in shell_windows():
            if _backdrop(rw, rh):
                continue
            if abs(rw - ext.width) > 16 or abs(rh - ext.height) > 16:
                continue
            if positioned:
                if abs(rx - ext.x) < 48 and abs(ry - ext.y) < 48:
                    return node
                continue
            hits.append(node)
        if len(hits) == 1:
            return hits[0]
        if hits:
            return hits[-1]
        return None

    def attach(self, accessible) -> bool:
        """Pop the one chameleon onto this window."""
        if self._quitting:
            return False
        key = acc_path(accessible)
        current = self.pets.get(key)
        if current is not None and not current._free:
            current.follow()
            return False
        for pet in list(self.pets.values()):
            self.dismiss(pet)
        try:
            pet = Chameleon(self, accessible, self.assets)
        except Exception as exc:
            log(f"attach failed {exc}")
            return False
        self.pets[key] = pet
        pet.follow()
        ext = pet._extents()
        if ext is not None:
            log(f"attached {int(ext.x)},{int(ext.y)} {int(ext.width)}x{int(ext.height)}")
        else:
            log(f"attached {window_title(accessible)}")
        return False

    def _rekey(self, pet: Chameleon, accessible) -> None:
        for key, item in list(self.pets.items()):
            if item is pet:
                self.pets.pop(key, None)
        pet.accessible = accessible
        pet._free = accessible is None
        self.pets["free" if accessible is None else acc_path(accessible)] = pet

    def drop_on(self, pet: Chameleon, x: int, y: int) -> None:
        """Perch the chameleon on the window under the pointer, or leave him there."""
        hit = self._window_at(x, y)
        if hit is None:
            self._rekey(pet, None)
            pet._next_sample = 0
            log(f"free {x},{y}")
            return
        node, rx, ry, rw, rh = hit
        side, along = nearest_side(rx, ry, rw, rh, x, y)
        pet._perch_side = side
        pet._perch_along = along
        self._rekey(pet, node)
        pet._next_sample = 0
        pet._begin_glide(_EDGE_ANGLE[side])
        pet.follow()
        log(f"dropped {side} on {rx},{ry} {rw}x{rh}")

    def dismiss(self, pet: Chameleon) -> None:
        key = acc_path(pet.accessible) if pet.accessible is not None else "free"
        self.pets.pop(key, None)
        for found, item in list(self.pets.items()):
            if item is pet:
                self.pets.pop(found, None)
        self._sample_queue = [item for item in self._sample_queue if item is not pet]
        pet.destroy()

    def quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        log("quit")
        for pet in list(self.pets.values()):
            self.dismiss(pet)
        Gtk.main_quit()

    def _poll(self) -> bool:
        if self._quitting:
            return False
        for pet in self.pets.values():
            if pet._dragging or pet._turning:
                return True
        now = time.monotonic()
        dead = []
        for key, pet in self.pets.items():
            try:
                if not pet.follow():
                    dead.append(key)
                    continue
                pet.tick(now)
            except Exception as exc:
                log(f"tick {exc}")
                dead.append(key)
        for key in dead:
            pet = self.pets.pop(key, None)
            if pet is not None:
                pet.destroy()
        return True

    def _skip(self, node, rx: int, ry: int, rw: int, rh: int) -> bool:
        if _backdrop(rw, rh):
            return True
        if rw < 36 or rh < 20:
            return True
        try:
            name = node.get_name() or ""
        except Exception:
            name = ""
        if name in ("Chameleon", PRGNAME, PRGNAME):
            return True
        for pet in self.pets.values():
            try:
                origin = pet.get_position()
                size = pet.get_size()
            except Exception:
                continue
            cx = rx + rw / 2
            cy = ry + rh / 2
            if origin[0] - 8 <= cx <= origin[0] + size[0] + 8 and origin[1] - 8 <= cy <= origin[1] + size[1] + 8:
                if abs(rw - size[0]) < 80 and abs(rh - size[1]) < 80:
                    return True
        return False

    def _window_at(self, x: int, y: int):
        """Topmost real window under this point. Later shell windows are above."""
        hits = []
        try:
            windows = shell_windows()
        except Exception as exc:
            log(f"windows {exc}")
            return None
        for node, rx, ry, rw, rh in windows:
            if not (rx <= x <= rx + rw and ry <= y <= ry + rh):
                continue
            if self._skip(node, rx, ry, rw, rh):
                continue
            hits.append((node, rx, ry, rw, rh))
        if not hits:
            return None
        return hits[-1]

    def sample(self, pet: Chameleon) -> None:
        if not self._bus_ready:
            return
        if pet not in self._sample_queue:
            self._sample_queue.append(pet)
        if self._sampling:
            return
        self._sampling = True
        GLib.idle_add(self._sample_one)

    def _sample_rect(self, pet: Chameleon):
        if pet._dragging:
            hit = self._window_at(pet._px, pet._py)
            if hit is not None:
                _node, rx, ry, rw, rh = hit
                return rect_inside(rx, ry, rw, rh)
        if not pet._free and pet.accessible is not None:
            ext = pet._extents()
            if ext is not None:
                return rect_inside(ext.x, ext.y, ext.width, ext.height)
        try:
            origin = pet.get_position()
        except Exception:
            return None
        sw, sh = screen_size()
        x = max(0, int(origin[0] + pet._side * 0.3))
        y = max(0, int(origin[1] + pet._side * 0.3))
        w = min(180, sw - x)
        h = min(120, sh - y)
        if w < 20 or h < 20:
            return None
        return x, y, w, h

    def _sample_one(self) -> bool:
        if not self._sample_queue:
            self._sampling = False
            return False
        pet = self._sample_queue.pop(0)
        if pet not in self.pets.values():
            return True
        rect = self._sample_rect(pet)
        if rect is None:
            self._sampling = bool(self._sample_queue)
            return bool(self._sample_queue)
        x, y, w, h = rect
        path = Path("/tmp/chameleon-sample.png")
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            bus.call_sync(
                "org.gnome.Shell.Screenshot",
                "/org/gnome/Shell/Screenshot",
                "org.gnome.Shell.Screenshot",
                "ScreenshotArea",
                GLib.Variant("(iiiibs)", (x, y, w, h, False, str(path))),
                GLib.VariantType("(bs)"),
                Gio.DBusCallFlags.NONE,
                2500,
                None,
            )
            color = average_png(path)
            if color is not None and pet in self.pets.values():
                pet.target = color
        except Exception as exc:
            log(f"sample {exc}")
        finally:
            try:
                path.unlink()
            except OSError:
                pass
        if self._sample_queue:
            GLib.idle_add(self._sample_one)
        else:
            self._sampling = False
        return False


def rect_inside(ex, ey, ew, eh):
    sw, sh = screen_size()
    x = max(0, int(ex + ew * 0.45))
    y = max(0, int(ey + 6))
    w = min(240, int(ew * 0.5), sw - x)
    h = min(160, int(eh - 12), sh - y)
    if w < 20 or h < 20:
        return None
    return x, y, w, h


def nearest_side(x: int, y: int, w: int, h: int, px: int, py: int) -> tuple[str, float]:
    distances = (
        ("left", px - x),
        ("right", x + w - px),
        ("top", py - y),
        ("bottom", y + h - py),
    )
    side = min(distances, key=lambda item: item[1])[0]
    if side in ("left", "right"):
        along = (py - y) / max(1, h)
    else:
        along = (px - x) / max(1, w)
    return side, min(0.9, max(0.1, along))


def window_extents(win):
    comp = win.get_component_iface()
    if comp is None:
        return None
    return comp.get_extents(Atspi.CoordType.SCREEN)


def _backdrop(width: int, height: int) -> bool:
    return width >= 1900 and height >= 1100


def _shell_group():
    """The shell panel whose children are the on-screen windows."""
    cached = getattr(_shell_group, "node", None)
    if cached is not None:
        try:
            if cached.get_child_count() >= 6:
                return cached
        except Exception:
            _shell_group.node = None
    try:
        desktop = Atspi.get_desktop(0)
    except Exception:
        return None
    shell = None
    for index in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(index)
        if app is not None and (app.get_name() or "") == "gnome-shell":
            shell = app
            break
    if shell is None:
        return None
    try:
        group = shell.get_child_at_index(0).get_child_at_index(0).get_child_at_index(0)
    except Exception:
        group = None
    if group is None:
        return None
    try:
        if group.get_role_name() == "panel" and group.get_child_count() >= 6:
            _shell_group.node = group
            return group
    except Exception:
        return None
    return None


def shell_windows() -> list[tuple]:
    """Real rectangles of the windows on this desktop."""
    group = _shell_group()
    if group is None:
        return []
    rows = []
    try:
        count = group.get_child_count()
    except Exception:
        _shell_group.node = None
        return []
    for index in range(count):
        child = group.get_child_at_index(index)
        if child is None:
            continue
        try:
            state = child.get_state_set()
            if not state.contains(Atspi.StateType.SHOWING) or not state.contains(Atspi.StateType.VISIBLE):
                continue
            ext = window_extents(child)
        except Exception:
            continue
        if ext is None or ext.width < 36 or ext.height < 20:
            continue
        if ext.x < -200 or ext.y < -80:
            continue
        rows.append((child, int(ext.x), int(ext.y), int(ext.width), int(ext.height)))
    return rows


def iter_windows():
    desktop = Atspi.get_desktop(0)
    for i in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(i)
        if app is None:
            continue
        try:
            app_name = app.get_name() or ""
        except Exception:
            continue
        if app_name in ("gnome-shell", PRGNAME, PRGNAME):
            continue
        count = app.get_child_count()
        for j in range(count):
            win = app.get_child_at_index(j)
            if win is None:
                continue
            try:
                role = win.get_role_name()
            except Exception:
                continue
            if role not in ("frame", "window", "dialog"):
                continue
            try:
                state = win.get_state_set()
                if state.contains(Atspi.StateType.DEFUNCT) or state.contains(Atspi.StateType.ICONIFIED):
                    continue
                if not state.contains(Atspi.StateType.SHOWING):
                    continue
                ext = window_extents(win)
            except Exception:
                continue
            if ext is None or ext.width < 36 or ext.height < 20:
                continue
            if ext.y < -200 or ext.x < -200:
                continue
            yield app_name, win, ext


def main(argv: list[str]) -> int:
    global _PLACE
    replace = "--replace" in argv
    if "--place" in argv:
        index = argv.index("--place")
        if index + 1 < len(argv):
            _PLACE = argv[index + 1]
    claim_pid(replace)
    GLib.set_prgname(PRGNAME)
    try:
        Gdk.set_program_class(PRGNAME)
    except Exception:
        pass

    def stop(_signum, _frame) -> None:
        Gtk.main_quit()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log("start")
    app = App()
    try:
        Gtk.main()
    finally:
        try:
            if PID_FILE.exists() and int(PID_FILE.read_text().strip() or "0") == os.getpid():
                PID_FILE.unlink()
        except (OSError, ValueError):
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
