"""
Live thermal image viewer for Telops cameras.

Requires the 'gui' extra: pip install pyTelops[gui]

Can be used from code::

    with Camera() as cam:
        cam.live_view()

Or from the CLI::

    pytelops live
"""

import time
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from .camera import Camera

try:
    import tkinter as tk
    from tkinter import ttk
    from PIL import Image, ImageTk
    import matplotlib
    HAS_GUI_DEPS = True
except ImportError:
    HAS_GUI_DEPS = False

from . import registers as reg

COLORMAP_CHOICES = ["inferno", "hot", "plasma", "magma", "viridis", "gray"]


def _check_gui_deps():
    if not HAS_GUI_DEPS:
        raise ImportError(
            "GUI dependencies not installed. Run:\n"
            "  pip install pyTelops[gui]")


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

    def __init__(self, camera: "Camera", colormap: str = "inferno",
                 scale: int = 2):
        _check_gui_deps()

        self.cam = camera
        w, h = self.cam.resolution
        self.width = w
        self.height = h
        self.img_height = h - camera.HEADER_ROWS

        self.cmap_name = colormap
        self.lut = build_lut(self.cmap_name)
        self.scale = scale
        self.disp_w = self.width * scale
        self.disp_h = self.img_height * scale

        # Start streaming
        self.cam.start_stream()
        self.cam._gvcp.write_reg(reg.REG_ACQUISITION_START, 1)

        # Build GUI
        self.root = tk.Tk()
        self.root.title("pyTelops Live View")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.canvas = tk.Canvas(self.root, width=self.disp_w,
                                height=self.disp_h, bg="black")
        self.canvas.pack()

        bottom = tk.Frame(self.root)
        bottom.pack(fill="x", padx=5, pady=2)

        self.status_var = tk.StringVar(value="Starting...")
        self.status = tk.Label(bottom, textvariable=self.status_var,
                               font=("Consolas", 10), anchor="w")
        self.status.pack(side="left", fill="x", expand=True)

        self.cmap_var = tk.StringVar(value=self.cmap_name)
        cmap_menu = ttk.Combobox(bottom, textvariable=self.cmap_var,
                                 values=COLORMAP_CHOICES, width=10,
                                 state="readonly")
        cmap_menu.pack(side="right")
        cmap_menu.bind("<<ComboboxSelected>>", self._on_cmap_change)

        self.photo = None
        self.frame_count = 0
        self.fps_time = time.monotonic()
        self.fps = 0.0
        self.running = True

        self.root.after(1, self.update)

    def _on_cmap_change(self, event=None):
        self.cmap_name = self.cmap_var.get()
        self.lut = build_lut(self.cmap_name)

    def update(self):
        if not self.running:
            return

        result = self.cam._gvsp.get_frame_with_info(timeout=0.05)

        if result is not None:
            frame, info = result
            # Skip Telops header rows
            img = frame[self.cam.HEADER_ROWS:, :]

            # Percentile normalization
            vmin = np.percentile(img, 1)
            vmax = np.percentile(img, 99)
            if vmax > vmin:
                flt = (img.astype(np.float32) - vmin) / (vmax - vmin)
                np.clip(flt, 0, 1, out=flt)
                img16 = (flt * 65535).astype(np.uint16)
            else:
                img16 = np.zeros_like(img, dtype=np.uint16)

            colored = self.lut[img16.ravel()].reshape(
                self.img_height, self.width, 3)

            pil_img = Image.fromarray(colored)
            pil_img = pil_img.resize((self.disp_w, self.disp_h), Image.NEAREST)
            self.photo = ImageTk.PhotoImage(pil_img)

            self.canvas.delete("all")
            self.canvas.create_image(0, 0, anchor="nw", image=self.photo)

            # FPS counter
            self.frame_count += 1
            now = time.monotonic()
            elapsed = now - self.fps_time
            if elapsed >= 1.0:
                self.fps = self.frame_count / elapsed
                self.frame_count = 0
                self.fps_time = now

            self.status_var.set(
                f"{self.width}x{self.img_height}  |  "
                f"{self.fps:.1f} fps  |  "
                f"p1={vmin:.0f}  p99={vmax:.0f}  |  "
                f"block={info['block_id']}")

        self.root.after(1, self.update)

    def on_close(self):
        self.running = False
        self.cam.stop_stream()
        self.root.destroy()

    def run(self):
        """Run the viewer (blocks until window is closed)."""
        self.root.mainloop()
