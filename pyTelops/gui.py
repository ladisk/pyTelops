"""
Live thermal image viewer for Telops cameras.

Requires the ``gui`` extra::

    pip install pyTelops[gui]

Can be used from code::

    with Camera() as cam:
        cam.live_view()

Or from the CLI::

    pytelops live

Features:

- Real-time thermal display with colormap selection
- Colorbar showing temperature scale
- Cursor temperature readout in status bar
- Click to place persistent marker with temperature
- Min/Max/Mean stats in status bar
- Focus assist: sharpness score with best-so-far hold, focus peaking, and a
  zoom loupe around the cursor. The score covers the full frame or only a
  focus box: Shift+click places the box, Shift+right-click goes back to the
  full frame.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from .camera import Camera

try:
    import tkinter as tk
    from tkinter import ttk

    import matplotlib
    from PIL import Image, ImageDraw, ImageTk

    HAS_GUI_DEPS = True
except ImportError:
    HAS_GUI_DEPS = False

COLORMAP_CHOICES = ["inferno", "hot", "plasma", "magma", "viridis", "gray"]

# Colorbar width in pixels
COLORBAR_WIDTH = 60

# Focus assist
FOCUS_BOX = 64  # side of the Shift+click focus box, image pixels
PEAKING_TOP = 0.05  # fraction of pixels with the strongest edges to color
PEAKING_COLOR = (0, 255, 0)
LOUPE_SRC = 32  # side of the magnified region, image pixels
LOUPE_SIZE = 192  # side of the loupe on screen, display pixels
SHARPNESS_SMOOTHING = 0.15  # weight of the newest frame in the shown score
_EDGE = 2  # pixels lost at each border by the 3x3 smoothing plus the gradient


def _gradient_sq(img: np.ndarray) -> np.ndarray:
    """Squared gradient magnitude of the 3x3-smoothed image.

    The smoothing removes most single-pixel sensor noise, which otherwise
    dominates the gradient of a low-contrast thermal scene. The result is
    ``_EDGE`` pixels smaller than *img* on every side.
    """
    img = np.asarray(img, dtype=np.float32)
    h, w = img.shape
    img = sum(img[dy : h - 2 + dy, dx : w - 2 + dx] for dy in range(3) for dx in range(3)) / 9.0
    gx = img[1:-1, 2:] - img[1:-1, :-2]
    gy = img[2:, 1:-1] - img[:-2, 1:-1]
    return gx * gx + gy * gy


def sharpness(img: np.ndarray, box: tuple[int, int, int, int] | None = None) -> float:
    """Focus measure: mean squared gradient (Tenengrad) of *img*.

    A sharper image of the same scene gives a larger value, so turn the lens
    until the value peaks. Only compare values of the same scene and box.

    Parameters
    ----------
    img : numpy.ndarray
        2-D image (temperature or counts).
    box : tuple of int, optional
        ``(x0, y0, x1, y1)`` region in image pixels. ``None`` uses the full frame.
    """
    if box is not None:
        x0, y0, x1, y1 = box
        img = img[y0:y1, x0:x1]
    if img.shape[0] < 2 * _EDGE + 1 or img.shape[1] < 2 * _EDGE + 1:
        return 0.0
    return float(_gradient_sq(img).mean())


def peaking_mask(img: np.ndarray, top: float = PEAKING_TOP) -> np.ndarray:
    """Boolean mask of the strongest edges in *img*, for focus peaking.

    Marks the *top* fraction of pixels by gradient magnitude. Pixels below four
    times the median gradient are never marked, so a flat or noisy scene shows
    no false edges.
    """
    mask = np.zeros(img.shape, dtype=bool)
    if img.shape[0] < 2 * _EDGE + 1 or img.shape[1] < 2 * _EDGE + 1:
        return mask
    g = _gradient_sq(img)
    threshold = max(float(np.quantile(g, 1.0 - top)), 16.0 * float(np.median(g)))
    mask[_EDGE:-_EDGE, _EDGE:-_EDGE] = (g >= threshold) & (g > 0)
    return mask


def focus_box(ix: int, iy: int, width: int, height: int, side: int = FOCUS_BOX):
    """``(x0, y0, x1, y1)`` box of *side* pixels centred on (ix, iy), kept inside the frame."""
    side_x, side_y = min(side, width), min(side, height)
    x0 = min(max(ix - side_x // 2, 0), width - side_x)
    y0 = min(max(iy - side_y // 2, 0), height - side_y)
    return (x0, y0, x0 + side_x, y0 + side_y)


def _check_gui_deps() -> None:
    """Raise ImportError if optional GUI dependencies (tkinter, matplotlib, Pillow) are absent."""
    if not HAS_GUI_DEPS:
        raise ImportError("GUI dependencies not installed. Run:\n  pip install pyTelops[gui]")


def build_lut(cmap_name: str) -> np.ndarray:
    """Build a 65536-entry RGB LUT from a matplotlib colormap."""
    _check_gui_deps()
    cmap = matplotlib.colormaps[cmap_name]
    return (cmap(np.linspace(0, 1, 65536))[:, :3] * 255).astype(np.uint8)


class LiveView:
    """Tkinter-based live thermal image viewer.

    Args:
        camera: Connected Camera instance.
        colormap: Initial matplotlib colormap name.
        scale: Display upscale factor.
    """

    def __init__(self, camera: Camera, colormap: str = "inferno", scale: int = 2):
        _check_gui_deps()

        self.cam = camera
        w, h = self.cam.resolution
        self.width = w
        self.height = h
        self.img_height = h  # resolution already returns usable pixels

        self.cmap_name = colormap
        self.lut = build_lut(self.cmap_name)
        self.scale = scale
        self.disp_w = self.width * scale
        self.disp_h = self.img_height * scale

        # Current frame data for cursor readout
        self._current_temp = None  # calibrated temperature array (Celsius)
        self._vmin = 0.0
        self._vmax = 1.0

        # Markers: list of (img_x, img_y) in image coordinates
        self._markers = []

        # Mouse position in image coordinates
        self._mouse_img_x = -1
        self._mouse_img_y = -1

        # Focus assist state
        self._box = focus_box(w // 2, h // 2, w, h)  # (x0, y0, x1, y1), image pixels
        self._sharp = None  # smoothed sharpness score
        self._sharp_best = 0.0

        # Start continuous acquisition
        self.cam.acquisition_start()

        # Build GUI
        self.root = tk.Tk()
        self.root.title("pyTelops Live View")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        # Main frame: image + colorbar
        main = tk.Frame(self.root)
        main.pack(fill="both", expand=True)

        self.canvas = tk.Canvas(main, width=self.disp_w, height=self.disp_h, bg="black")
        self.canvas.pack(side="left")

        # Colorbar canvas
        self.cbar_canvas = tk.Canvas(main, width=COLORBAR_WIDTH, height=self.disp_h, bg="black")
        self.cbar_canvas.pack(side="left", fill="y")

        # Bottom bar: status + controls
        bottom = tk.Frame(self.root)
        bottom.pack(fill="x", padx=5, pady=2)

        self.status_var = tk.StringVar(value="Starting...")
        self.status = tk.Label(
            bottom, textvariable=self.status_var, font=("Consolas", 10), anchor="w"
        )
        self.status.pack(side="left", fill="x", expand=True)

        # Cursor readout label
        self.cursor_var = tk.StringVar(value="")
        self.cursor_label = tk.Label(
            bottom,
            textvariable=self.cursor_var,
            font=("Consolas", 10),
            anchor="e",
            fg="yellow",
            bg="black",
            padx=5,
        )
        self.cursor_label.pack(side="right")

        self.cmap_var = tk.StringVar(value=self.cmap_name)
        cmap_menu = ttk.Combobox(
            bottom, textvariable=self.cmap_var, values=COLORMAP_CHOICES, width=10, state="readonly"
        )
        cmap_menu.pack(side="right")
        cmap_menu.bind("<<ComboboxSelected>>", self._on_cmap_change)

        # Focus assist row
        focus = tk.Frame(self.root)
        focus.pack(fill="x", padx=5, pady=2)
        tk.Label(focus, text="Focus:").pack(side="left")
        self.sharp_on = tk.BooleanVar(value=False)
        self.peaking_on = tk.BooleanVar(value=False)
        self.loupe_on = tk.BooleanVar(value=False)
        tk.Checkbutton(focus, text="Sharpness", variable=self.sharp_on).pack(side="left")
        tk.Checkbutton(focus, text="Peaking", variable=self.peaking_on).pack(side="left")
        tk.Checkbutton(focus, text="Loupe", variable=self.loupe_on).pack(side="left")
        tk.Label(focus, text="Area:").pack(side="left", padx=(5, 0))
        self.area_var = tk.StringVar(value="frame")
        area_menu = ttk.Combobox(
            focus, textvariable=self.area_var, values=["frame", "box"], width=6, state="readonly"
        )
        area_menu.pack(side="left")
        area_menu.bind("<<ComboboxSelected>>", lambda e: self._reset_sharpness())
        tk.Button(focus, text="Reset best", command=self._reset_sharpness).pack(side="left", padx=5)
        self.sharp_bar = tk.Canvas(focus, width=150, height=14, bg="black", highlightthickness=0)
        self.sharp_bar.pack(side="left", padx=5)
        self.sharp_var = tk.StringVar(value="")
        tk.Label(focus, textvariable=self.sharp_var, font=("Consolas", 10), anchor="w").pack(
            side="left", fill="x", expand=True
        )

        # Bind mouse events
        self.canvas.bind("<Motion>", self._on_mouse_move)
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Button-3>", self._on_right_click)  # right-click clears markers
        self.canvas.bind("<Shift-Button-1>", self._on_shift_click)  # set focus box
        self.canvas.bind("<Shift-Button-3>", self._on_shift_right_click)  # clear focus box

        self.photo = None
        self.cbar_photo = None
        self.frame_count = 0
        self.fps_time = time.monotonic()
        self.fps = 0.0
        self.running = True
        self._last_frame_t = time.monotonic()
        self._stall_warned = False

        self.root.after(1, self.update)

    def _on_cmap_change(self, event: object = None) -> None:
        """Rebuild the LUT when the user selects a different colormap."""
        self.cmap_name = self.cmap_var.get()
        self.lut = build_lut(self.cmap_name)

    def _on_mouse_move(self, event):
        """Track mouse position for cursor readout."""
        self._mouse_img_x = event.x // self.scale
        self._mouse_img_y = event.y // self.scale

    def _on_click(self, event):
        """Place a persistent marker at click position."""
        ix = event.x // self.scale
        iy = event.y // self.scale
        if 0 <= ix < self.width and 0 <= iy < self.img_height:
            self._markers.append((ix, iy))

    def _on_right_click(self, event):
        """Clear all markers."""
        self._markers.clear()

    def _on_shift_click(self, event):
        """Place the focus box around the click and score only the box."""
        ix, iy = event.x // self.scale, event.y // self.scale
        if 0 <= ix < self.width and 0 <= iy < self.img_height:
            self._box = focus_box(ix, iy, self.width, self.img_height)
            self.area_var.set("box")
            self._reset_sharpness()
        return "break"  # do not also place a marker

    def _on_shift_right_click(self, event):
        """Score the full frame again."""
        self.area_var.set("frame")
        self._reset_sharpness()
        return "break"

    def _reset_sharpness(self):
        """Forget the smoothed score and the best-so-far value."""
        self._sharp = None
        self._sharp_best = 0.0

    def _update_sharpness(self, img):
        """Update the sharpness score, its best-so-far value and the bar."""
        box = self._box if self.area_var.get() == "box" else None
        value = sharpness(img, box)
        a = SHARPNESS_SMOOTHING
        self._sharp = value if self._sharp is None else a * value + (1 - a) * self._sharp
        self._sharp_best = max(self._sharp_best, self._sharp)
        frac = self._sharp / self._sharp_best if self._sharp_best > 0 else 0.0
        where = "box" if box else "frame"
        self.sharp_var.set(f"{self._sharp:.4g}  best {self._sharp_best:.4g}  ({frac:.0%}, {where})")
        w = int(self.sharp_bar["width"])
        h = int(self.sharp_bar["height"])
        color = "#00ff00" if frac >= 0.95 else "#ffaa00"
        self.sharp_bar.delete("all")
        self.sharp_bar.create_rectangle(0, 0, int(frac * w), h, fill=color, width=0)

    def _draw_loupe(self, pil_img, colored):
        """Paste a magnified view of the region around the cursor into a corner."""
        ix, iy = self._mouse_img_x, self._mouse_img_y
        if not (0 <= ix < self.width and 0 <= iy < self.img_height):
            return
        x0, y0, x1, y1 = focus_box(ix, iy, self.width, self.img_height, LOUPE_SRC)
        crop = Image.fromarray(np.ascontiguousarray(colored[y0:y1, x0:x1]))
        side = min(LOUPE_SIZE, self.disp_w // 2, self.disp_h)
        zoom = crop.resize((side, side * crop.height // max(crop.width, 1)), Image.NEAREST)
        # Put the loupe in the corner away from the cursor.
        px = 4 if ix * self.scale > self.disp_w // 2 else self.disp_w - zoom.width - 4
        py = 4
        pil_img.paste(zoom, (px, py))
        draw = ImageDraw.Draw(pil_img)
        draw.rectangle([px - 1, py - 1, px + zoom.width, py + zoom.height], outline="white")
        s = self.scale
        draw.rectangle([x0 * s, y0 * s, x1 * s - 1, y1 * s - 1], outline="white")

    def _get_temp_at(self, ix, iy):
        """Get temperature at image coordinates."""
        if (
            self._current_temp is not None
            and 0 <= iy < self._current_temp.shape[0]
            and 0 <= ix < self._current_temp.shape[1]
        ):
            return self._current_temp[iy, ix]
        return None

    def update(self) -> None:
        """Fetch the next frame, update the canvas, and schedule the next tick."""
        if not self.running:
            return

        # Pull raw frame with headers - the viewer reads cal_mode and
        # calibration parameters out of the header bytes itself.
        frame = self.cam.read_frame(timeout=0.05, convert=False, strip_header=False)

        if frame is None:
            stalled_s = time.monotonic() - self._last_frame_t
            if stalled_s >= 3.0 and not self._stall_warned:
                self._stall_warned = True
                self.cam._warn_if_stream_stalled(stalled_s)
        else:
            self._last_frame_t = time.monotonic()
            self._stall_warned = False

        if frame is not None:
            # Read calibration mode from header
            header_bytes = frame[: self.cam.HEADER_ROWS, :].tobytes()
            cal_mode = header_bytes[self.cam._HDR_CAL_MODE]
            self._unit = {0: "RAW", 1: "NUC", 2: "°C", 3: "W/m²sr", 4: "W/m²", 255: "RAW"}.get(
                cal_mode, "?"
            )

            # Apply calibration to get temperature (reads header)
            self._current_temp = self.cam._apply_calibration(frame)

            # Use calibrated data for display
            img = self._current_temp
            if img.dtype != np.float32:
                # NUC/RAW: no calibration applied, strip headers manually
                img = frame[self.cam.HEADER_ROWS :, :].astype(np.float32)

            # Percentile normalization
            vmin = float(np.percentile(img, 1))
            vmax = float(np.percentile(img, 99))
            self._vmin = vmin
            self._vmax = vmax

            if vmax > vmin:
                flt = (img - vmin) / (vmax - vmin)
                np.clip(flt, 0, 1, out=flt)
                img16 = (flt * 65535).astype(np.uint16)
            else:
                img16 = np.zeros((self.img_height, self.width), dtype=np.uint16)

            colored = self.lut[img16.ravel()].reshape(self.img_height, self.width, 3)

            # Focus assist
            if self.peaking_on.get():
                colored = colored.copy()
                colored[peaking_mask(img)] = PEAKING_COLOR
            if self.sharp_on.get():
                self._update_sharpness(img)
            elif self.sharp_var.get():
                self.sharp_var.set("")
                self.sharp_bar.delete("all")

            pil_img = Image.fromarray(colored)
            pil_img = pil_img.resize((self.disp_w, self.disp_h), Image.NEAREST)

            # Draw markers on the image
            if self._markers:
                draw = ImageDraw.Draw(pil_img)
                for mx, my in self._markers:
                    dx, dy = mx * self.scale, my * self.scale
                    r = 4
                    draw.ellipse([dx - r, dy - r, dx + r, dy + r], outline="white", width=2)
                    temp = self._get_temp_at(mx, my)
                    if temp is not None:
                        label = f"{temp:.1f}"
                        draw.text((dx + r + 2, dy - 8), label, fill="white")

            if self.sharp_on.get() and self.area_var.get() == "box":
                x0, y0, x1, y1 = self._box
                s = self.scale
                ImageDraw.Draw(pil_img).rectangle(
                    [x0 * s, y0 * s, x1 * s - 1, y1 * s - 1], outline="cyan", width=2
                )
            if self.loupe_on.get():
                self._draw_loupe(pil_img, colored)

            self.photo = ImageTk.PhotoImage(pil_img)

            self.canvas.delete("all")
            self.canvas.create_image(0, 0, anchor="nw", image=self.photo)

            # Update colorbar
            self._draw_colorbar(vmin, vmax)

            # FPS counter
            self.frame_count += 1
            now = time.monotonic()
            elapsed = now - self.fps_time
            if elapsed >= 1.0:
                self.fps = self.frame_count / elapsed
                self.frame_count = 0
                self.fps_time = now

            # Stats
            mean_val = float(img.mean())
            u = self._unit
            self.status_var.set(
                f"{self.width}x{self.img_height}  |  "
                f"{self.fps:.1f} fps  |  "
                f"min={vmin:.1f}  max={vmax:.1f}  mean={mean_val:.1f} {u}"
            )

            # Cursor readout
            temp = self._get_temp_at(self._mouse_img_x, self._mouse_img_y)
            if temp is not None:
                self.cursor_var.set(
                    f"({self._mouse_img_x},{self._mouse_img_y}) {temp:.2f} {self._unit}"
                )
            else:
                self.cursor_var.set("")

        self.root.after(1, self.update)

    def _draw_colorbar(self, vmin: float, vmax: float):
        """Draw a vertical colorbar with temperature labels."""
        bar_w = 20
        bar_h = self.disp_h
        margin_left = 5
        pad_top = 10
        pad_bot = 14
        usable_h = max(bar_h - pad_top - pad_bot, 1)
        n_ticks = 5

        # Build gradient for usable region only
        gradient = np.linspace(65535, 0, usable_h, dtype=np.uint16)
        bar_rgb = self.lut[gradient]
        bar_img = np.repeat(bar_rgb[:, np.newaxis, :], bar_w, axis=1)

        full_w = COLORBAR_WIDTH
        pil_bar = Image.new("RGB", (full_w, bar_h), (0, 0, 0))
        pil_bar.paste(Image.fromarray(bar_img), (margin_left, pad_top))

        # Tick labels (drawn after gradient so they're on top)
        draw = ImageDraw.Draw(pil_bar)
        unit = getattr(self, "_unit", "")
        for i in range(n_ticks + 1):
            frac = i / n_ticks
            y = pad_top + int(frac * usable_h)
            val = vmax - frac * (vmax - vmin)
            label = f"{val:.1f}"
            if i == n_ticks:
                label += f" {unit}"
            draw.text((margin_left + bar_w + 3, y - 6), label, fill="white")

        self.cbar_photo = ImageTk.PhotoImage(pil_bar)
        self.cbar_canvas.delete("all")
        self.cbar_canvas.create_image(0, 0, anchor="nw", image=self.cbar_photo)

    def on_close(self) -> None:
        """Handle window close: stop acquisition, release stream, destroy the Tk root."""
        self.running = False
        self.cam.acquisition_stop()
        self.cam.stop_stream()
        self.root.destroy()

    def run(self):
        """Run the viewer (blocks until window is closed)."""
        self.root.mainloop()
