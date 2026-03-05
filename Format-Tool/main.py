#!/usr/bin/env python3
"""Simple CustomTkinter UI to convert images <-> .bimg bundles.

Requires: customtkinter, Pillow

Run: python main.py
"""

import threading
import os
import io
import json
import tarfile
import struct
import lzma
import logging

import customtkinter as ctk
from tkinter import filedialog, Menu

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

import compress
import decompress

# default to dark appearance; user can switch at runtime if desired
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

APP_TITLE = "B-IMG Converter"

# font definitions
FONT = ("Arial", 12)
TITLE_FONT = ("Arial", 14, "bold")


def write_bimg_from_image(src_path, out_path, preserve_alpha=False, flatten=False, cluster_threshold=0):
    width, height, pixels, mode = compress.image_to_pixels(src_path, preserve_alpha=preserve_alpha)
    if not flatten:
        rows = [pixels[i * width : (i + 1) * width] for i in range(height)]
        output_data = {"width": width, "height": height, "pixels": rows, "mode": mode}
    else:
        output_data = {"width": width, "height": height, "pixels": pixels, "mode": mode}

    if not out_path.endswith(".bimg"):
        out_path = out_path + ".bimg"

    # Use smart hybrid bundle (picks best between optimized binary and original PNG)
    # Pass cluster_threshold for lossy compression
    compress.create_smart_bundle(src_path, out_path, pixels, width, height, mode, cluster_threshold=cluster_threshold)
    return out_path


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("640x800")

        # Top frame gets 1 share, bottom frame gets 2 shares (more content)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=2)

        # ── Top frame: Image -> .bimg ──────────────────────────────────────
        left = ctk.CTkFrame(self, corner_radius=6)
        left.grid(row=0, column=0, padx=12, pady=(12, 6), sticky="nsew")

        # Button packed at bottom FIRST — pack reserves its space before anything else,
        # so it is always visible and flush with the frame border.
        ctk.CTkButton(
            left, text="Create .bimg", fg_color="#1f6feb", font=FONT,
            command=self.create_bimg_thread,
        ).pack(side="bottom", fill="x", padx=16, pady=(8, 16))

        # Content area fills all remaining space above the button
        lc = ctk.CTkFrame(left, fg_color="transparent")
        lc.pack(side="top", fill="both", expand=True)
        lc.grid_columnconfigure((0, 1, 2), weight=1)

        ctk.CTkLabel(lc, text="Image -> .bimg", font=TITLE_FONT).grid(
            row=0, column=0, columnspan=3, pady=(8, 4))

        self.src_entry = ctk.CTkEntry(lc, placeholder_text="Select source image...", font=FONT)
        self.src_entry.grid(row=1, column=0, columnspan=2, padx=8, pady=6, sticky="ew")
        ctk.CTkButton(lc, text="Browse", font=FONT, command=self.browse_image).grid(
            row=1, column=2, padx=4, pady=4)

        self.out_entry = ctk.CTkEntry(lc, placeholder_text="Output filename (optional)", font=FONT)
        self.out_entry.grid(row=2, column=0, columnspan=2, padx=8, pady=6, sticky="ew")
        ctk.CTkLabel(lc, text=".bimg will be appended if missing").grid(
            row=2, column=2, padx=8, pady=6)

        self.alpha_var = ctk.BooleanVar()
        ctk.CTkCheckBox(lc, text="Preserve alpha", variable=self.alpha_var, font=FONT).grid(
            row=3, column=0, padx=4, pady=4)

        self.flatten_var = ctk.BooleanVar()
        ctk.CTkCheckBox(lc, text="Flatten pixels list", variable=self.flatten_var, font=FONT).grid(
            row=3, column=1, padx=4, pady=4)

        ctk.CTkLabel(lc, text="Compression:", font=FONT).grid(
            row=4, column=0, padx=4, pady=(8, 2), sticky="w")
        self.cluster_var = ctk.DoubleVar(value=0)
        self.cluster_slider = ctk.CTkSlider(
            lc, from_=0, to=255, number_of_steps=256, variable=self.cluster_var)
        self.cluster_slider.grid(row=4, column=1, columnspan=2, padx=4, pady=(8, 2), sticky="ew")

        self.threshold_label = ctk.CTkLabel(lc, text="0 (Lossless)", font=("Arial", 10))
        self.threshold_label.grid(row=5, column=0, columnspan=3, padx=4, pady=(0, 4))
        self.cluster_slider.bind("<B1-Motion>", self.update_threshold_label)
        self.cluster_slider.bind("<Button-1>", self.update_threshold_label)

        # ── Bottom frame: .bimg -> Image ───────────────────────────────────
        right = ctk.CTkFrame(self, corner_radius=6)
        right.grid(row=1, column=0, padx=12, pady=(6, 12), sticky="nsew")

        # Button packed at bottom FIRST
        ctk.CTkButton(
            right, text="Restore Image", fg_color="#16a34a", font=FONT,
            command=self.restore_thread,
        ).pack(side="bottom", fill="x", padx=16, pady=(8, 16))

        # Content area
        rc = ctk.CTkFrame(right, fg_color="transparent")
        rc.pack(side="top", fill="both", expand=True)
        rc.grid_columnconfigure((0, 1, 2), weight=1)

        ctk.CTkLabel(rc, text=".bimg -> Image", font=TITLE_FONT).grid(
            row=0, column=0, columnspan=3, pady=(8, 4))

        self.bimg_entry = ctk.CTkEntry(rc, placeholder_text="Select .bimg file...", font=FONT)
        self.bimg_entry.grid(row=1, column=0, columnspan=2, padx=8, pady=6, sticky="ew")
        ctk.CTkButton(rc, text="Browse", font=FONT, command=self.browse_bimg).grid(
            row=1, column=2, padx=4, pady=4)

        self.restore_entry = ctk.CTkEntry(
            rc, placeholder_text="Output filename (e.g. restored.png)", font=FONT)
        self.restore_entry.grid(row=2, column=0, columnspan=2, padx=8, pady=6, sticky="ew")
        self.format_var = ctk.StringVar(value="PNG")
        ctk.CTkOptionMenu(rc, variable=self.format_var, values=["PNG", "JPEG"]).grid(
            row=2, column=2, padx=8, pady=6)

        self.smooth_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            rc, text="Smooth clustering artifacts", variable=self.smooth_var, font=FONT,
        ).grid(row=3, column=0, padx=4, pady=4, sticky="w")
        self.smooth_var.trace_add("write", lambda *_: self.update_smooth_state())

        self.blur_var = ctk.BooleanVar(value=False)
        self.blur_check = ctk.CTkCheckBox(
            rc, text="Blur smoothed areas", variable=self.blur_var, font=FONT, state="disabled")
        self.blur_check.grid(row=3, column=1, columnspan=2, padx=4, pady=4, sticky="w")
        self.blur_var.trace_add("write", lambda *_: self.update_blur_state())

        self.smooth_strength_var = ctk.DoubleVar(value=1.0)
        self.smooth_strength_label = ctk.CTkLabel(
            rc, text="Smooth strength: 100%", font=FONT, text_color="gray")
        self.smooth_strength_label.grid(row=4, column=0, padx=4, pady=(8, 2), sticky="w")
        self.smooth_strength_slider = ctk.CTkSlider(
            rc, from_=0.0, to=2.0, number_of_steps=200,
            variable=self.smooth_strength_var, state="disabled")
        self.smooth_strength_slider.grid(row=4, column=1, columnspan=2, padx=4, pady=(8, 2), sticky="ew")
        self.smooth_strength_slider.bind("<B1-Motion>", self.update_smooth_strength_label)
        self.smooth_strength_slider.bind("<Button-1>", self.update_smooth_strength_label)

        self.blur_radius_var = ctk.DoubleVar(value=2)
        self.blur_radius_label = ctk.CTkLabel(rc, text="Blur radius: 2px", font=FONT, text_color="gray")
        self.blur_radius_label.grid(row=5, column=0, padx=4, pady=(8, 2), sticky="w")
        self.blur_radius_slider = ctk.CTkSlider(
            rc, from_=1, to=20, number_of_steps=19,
            variable=self.blur_radius_var, state="disabled")
        self.blur_radius_slider.grid(row=5, column=1, columnspan=2, padx=4, pady=(8, 2), sticky="ew")
        self.blur_radius_slider.bind("<B1-Motion>", self.update_blur_radius_label)
        self.blur_radius_slider.bind("<Button-1>", self.update_blur_radius_label)

        self.smooth_sensitivity_var = ctk.DoubleVar(value=0.1)
        self.smooth_sensitivity_label = ctk.CTkLabel(
            rc, text="Region threshold: 0.10%", font=FONT, text_color="gray")
        self.smooth_sensitivity_label.grid(row=6, column=0, padx=4, pady=(8, 2), sticky="w")
        self.smooth_sensitivity_slider = ctk.CTkSlider(
            rc, from_=0.0, to=0.5, number_of_steps=500,
            variable=self.smooth_sensitivity_var, state="disabled")
        self.smooth_sensitivity_slider.grid(row=6, column=1, columnspan=2, padx=4, pady=(8, 2), sticky="ew")
        self.smooth_sensitivity_slider.bind("<B1-Motion>", self.update_sensitivity_label)
        self.smooth_sensitivity_slider.bind("<Button-1>", self.update_sensitivity_label)

        # Status bar
        self.status = ctk.CTkLabel(self, text="Ready", anchor="w")
        self.status.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 12))
        theme_frame = ctk.CTkFrame(self, fg_color="transparent")
        theme_frame.grid(row=2, column=0, sticky="e", padx=12, pady=(0, 12))
        ctk.CTkLabel(theme_frame, text="Theme:", font=FONT).pack(side="left", padx=(0, 4))
        self.theme_var = ctk.BooleanVar(value=(ctk.get_appearance_mode().lower() == "light"))
        self.theme_switch = ctk.CTkSwitch(
            theme_frame, text="", variable=self.theme_var, command=self.toggle_theme)
        self.theme_switch.pack(side="left")

    def set_status(self, text):
        self.status.configure(text=text)
        logger.debug(f"Status: {text}")

    def toggle_theme(self):
        if self.theme_var.get():
            ctk.set_appearance_mode("light")
            logger.info("Theme switched to: light")
        else:
            ctk.set_appearance_mode("dark")
            logger.info("Theme switched to: dark")

    def update_smooth_state(self):
        """Enable or disable the strength slider and blur sub-option based on the smooth checkbox."""
        if self.smooth_var.get():
            self.smooth_strength_slider.configure(state="normal")
            self.smooth_strength_label.configure(text_color=("black", "white"))
            self.blur_check.configure(state="normal")
            self.smooth_sensitivity_slider.configure(state="normal")
            self.smooth_sensitivity_label.configure(text_color=("black", "white"))
        else:
            self.smooth_strength_slider.configure(state="disabled")
            self.smooth_strength_label.configure(text_color="gray")
            self.blur_check.configure(state="disabled")
            self.smooth_sensitivity_slider.configure(state="disabled")
            self.smooth_sensitivity_label.configure(text_color="gray")
            self._clear_blur_controls()

    def update_blur_state(self):
        """Enable or disable blur controls based on the blur checkbox."""
        if self.blur_var.get():
            self.blur_radius_slider.configure(state="normal")
            self.blur_radius_label.configure(text_color=("black", "white"))
        else:
            self._clear_blur_controls()

    def _clear_blur_controls(self):
        """Disable and reset blur controls."""
        self.blur_radius_slider.configure(state="disabled")
        self.blur_radius_label.configure(text_color="gray")

    def update_blur_radius_label(self, event=None):
        radius = int(self.blur_radius_var.get())
        self.blur_radius_label.configure(text=f"Blur radius: {radius}px")

    def update_smooth_strength_label(self, event=None):
        """Update the smooth strength label to show current slider value."""
        pct = int(self.smooth_strength_var.get() * 100)
        self.smooth_strength_label.configure(text=f"Smooth strength: {pct}%")
        logger.debug(f"Smooth strength updated to: {pct}%")

    def update_sensitivity_label(self, event=None):
        """Update the region threshold label to show current slider value."""
        val = self.smooth_sensitivity_var.get()
        self.smooth_sensitivity_label.configure(text=f"Region threshold: {val:.2f}%")

    def update_threshold_label(self, event=None):
        """Update the threshold label to show current slider value."""
        threshold = int(self.cluster_var.get())
        if threshold == 0:
            self.threshold_label.configure(text="0 (Lossless)")
        elif threshold == 255:
            self.threshold_label.configure(text="255 (Extreme)")
        else:
            self.threshold_label.configure(text=f"{threshold}")
        logger.debug(f"Cluster threshold updated to: {threshold}")

    def browse_image(self):
        exts = ["png", "jpg", "jpeg", "gif", "bmp", "tiff"]
        p = self.system_file_dialog(title="Select image", exts=exts)
        if p is NotImplemented:
            p = filedialog.askopenfilename(
                parent=self,
                filetypes=[("Image files", tuple(f"*.{e}" for e in exts))])
        elif p is None:
            return
        if p:
            self.src_entry.delete(0, "end")
            self.src_entry.insert(0, p)

    def browse_bimg(self):
        exts = ["bimg"]
        p = self.system_file_dialog(title="Select .bimg bundle", exts=exts)
        if p is NotImplemented:
            p = filedialog.askopenfilename(
                parent=self,
                filetypes=[("BIMG bundle", "*.bimg")])
        elif p is None:
            return
        if p:
            self.bimg_entry.delete(0, "end")
            self.bimg_entry.insert(0, p)

    def create_bimg_thread(self):
        t = threading.Thread(target=self.create_bimg)
        t.daemon = True
        t.start()

    def system_file_dialog(self, title="Select file", exts=None):
        """Open a native file dialog. exts is a list of extensions without dots, e.g. ['png','jpg'].
        Returns the chosen path, None if cancelled, or NotImplemented if no native dialog found."""
        import shutil, subprocess, sys
        native_available = False
        if sys.platform.startswith("linux"):
            if shutil.which("zenity"):
                native_available = True
                try:
                    cmd = ["zenity", "--file-selection", "--title", title]
                    if exts:
                        pattern = " ".join(f"*.{e}" for e in exts)
                        cmd += ["--file-filter", pattern]
                    res = subprocess.run(cmd, capture_output=True, text=True)
                    if res.returncode == 0:
                        return res.stdout.strip()
                    else:
                        return None  # canceled
                except Exception:
                    pass
            if shutil.which("kdialog"):
                native_available = True
                try:
                    filter_str = " ".join(f"*.{e}" for e in exts) if exts else "*"
                    res = subprocess.run(
                        ["kdialog", "--getopenfilename", "", filter_str],
                        capture_output=True, text=True)
                    if res.returncode == 0:
                        return res.stdout.strip()
                    else:
                        return None
                except Exception:
                    pass
        # macOS AppleScript — no 'of type' filter: AppleScript expects UTIs or
        # 4-char HFS codes, not bare extensions, so passing "bimg"/"png" greys
        # out every file instead of filtering to them.
        if sys.platform == "darwin":
            native_available = True
            try:
                script = f'POSIX path of (choose file with prompt "{title}")'
                res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
                if res.returncode == 0:
                    return res.stdout.strip()
                else:
                    return None
            except Exception:
                pass
        if not native_available:
            return NotImplemented
        return None

    def create_bimg(self):
        src = self.src_entry.get().strip()
        if not src or not os.path.exists(src):
            self.set_status("Source image not found")
            logger.error(f"Source image not found: {src}")
            return
        out = self.out_entry.get().strip() or os.path.splitext(os.path.basename(src))[0] + ".bimg"
        preserve_alpha = bool(self.alpha_var.get())
        flatten = bool(self.flatten_var.get())
        cluster_threshold = int(self.cluster_var.get())
        logger.info(f"Creating .bimg: {src} -> {out}")
        logger.info(f"Settings: preserve_alpha={preserve_alpha}, flatten={flatten}, cluster_threshold={cluster_threshold}")
        self.set_status("Creating .bimg...")
        try:
            out_path = write_bimg_from_image(src, out, preserve_alpha=preserve_alpha, flatten=flatten, cluster_threshold=cluster_threshold)
            logger.info(f"Successfully created: {out_path}")
            self.set_status(f"Created: {out_path}")
        except Exception as e:
            logger.error(f"Error creating .bimg: {e}", exc_info=True)
            self.set_status(f"Error: {e}")

    def restore_thread(self):
        t = threading.Thread(target=self.restore_image)
        t.daemon = True
        t.start()

    def restore_image(self):
        src = self.bimg_entry.get().strip()
        if not src or not os.path.exists(src):
            self.set_status("Bundle not found")
            logger.error(f"Bundle not found: {src}")
            return
        out = self.restore_entry.get().strip()
        if not out:
            out = os.path.splitext(os.path.basename(src))[0]
        fmt = self.format_var.get().upper()
        if not os.path.splitext(out)[1]:
            out = out + "." + fmt.lower()
        smooth_gradients = bool(self.smooth_var.get())
        smooth_strength = float(self.smooth_strength_var.get())
        smooth_sensitivity = float(self.smooth_sensitivity_var.get())
        blur_passes = None
        if smooth_gradients and self.blur_var.get():
            blur_passes = [int(self.blur_radius_var.get())]
        logger.info(f"Restoring image: {src} -> {out}")
        logger.info(f"Settings: format={fmt}, smooth={smooth_gradients}, strength={smooth_strength:.2f}, sensitivity={smooth_sensitivity:.2f}, blur_passes={blur_passes}")
        self.set_status("Restoring image...")
        try:
            decompress.decompress_image(src, out, smooth_gradients=smooth_gradients, smooth_strength=smooth_strength, blur_passes=blur_passes, smooth_sensitivity=smooth_sensitivity)
            logger.info(f"Successfully restored: {out}")
            self.set_status(f"Restored: {out}")
        except Exception as e:
            logger.error(f"Error restoring image: {e}", exc_info=True)
            self.set_status(f"Error: {e}")


if __name__ == "__main__":
    app = App()
    app.mainloop()
