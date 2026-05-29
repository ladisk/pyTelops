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

        # Bind mouse events
        self.canvas.bind("<Motion>", self._on_mouse_move)
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Button-3>", self._on_right_click)  # right-click clears markers

        self.photo = None
        self.cbar_photo = None
        self.frame_count = 0
        self.fps_time = time.monotonic()
        self.fps = 0.0
        self.running = True

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
