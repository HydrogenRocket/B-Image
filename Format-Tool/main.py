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
        self.geometry("640x800")  # vertical stacking
        # theme toggle will be placed near status bar instead of a menu
        # no menu bar needed

        # make panels expand vertically
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure((0, 1), weight=1)

        # Left frame: Image -> .bimg
        left = ctk.CTkFrame(self, corner_radius=6)
        left.grid(row=0, column=0, padx=12, pady=12, sticky="nsew")
        left.grid_columnconfigure((0,1,2), weight=1)
        for r in range(8):
            left.grid_rowconfigure(r, weight=0)
        left.grid_rowconfigure(6, weight=1)  # spacer pushes button to bottom

        ctk.CTkLabel(left, text="Image -> .bimg", font=TITLE_FONT).grid(row=0, column=0, columnspan=3, pady=(4, 4))

        self.src_entry = ctk.CTkEntry(left, placeholder_text="Select source image...", font=FONT)
        self.src_entry.grid(row=1, column=0, columnspan=2, padx=8, pady=6, sticky="ew")
        ctk.CTkButton(left, text="Browse", font=FONT, command=self.browse_image).grid(row=1, column=2, padx=4, pady=4)

        self.out_entry = ctk.CTkEntry(left, placeholder_text="Output filename (optional)", font=FONT)
        self.out_entry.grid(row=2, column=0, columnspan=2, padx=8, pady=6, sticky="ew")
        ctk.CTkLabel(left, text=".bimg will be appended if missing").grid(row=2, column=2, padx=8, pady=6)

        self.alpha_var = ctk.BooleanVar()
        ctk.CTkCheckBox(left, text="Preserve alpha", variable=self.alpha_var, font=FONT).grid(row=3, column=0, padx=4, pady=4)

        self.flatten_var = ctk.BooleanVar()
        ctk.CTkCheckBox(left, text="Flatten pixels list", variable=self.flatten_var, font=FONT).grid(row=3, column=1, padx=4, pady=4)

        # Cluster threshold slider (K-means compression)
        ctk.CTkLabel(left, text="Compression:", font=FONT).grid(row=4, column=0, padx=4, pady=(8, 2), sticky="w")
        self.cluster_var = ctk.DoubleVar(value=0)
        self.cluster_slider = ctk.CTkSlider(left, from_=0, to=255, number_of_steps=256, variable=self.cluster_var)
        self.cluster_slider.grid(row=4, column=1, columnspan=2, padx=4, pady=(8, 2), sticky="ew")
        
        # Label to show current threshold value
        self.threshold_label = ctk.CTkLabel(left, text="0 (Lossless)", font=("Arial", 10))
        self.threshold_label.grid(row=5, column=0, columnspan=3, padx=4, pady=(0, 4))
        self.cluster_slider.bind("<B1-Motion>", self.update_threshold_label)
        self.cluster_slider.bind("<Button-1>", self.update_threshold_label)

        ctk.CTkButton(left, text="Create .bimg", fg_color="#1f6feb", font=FONT, command=self.create_bimg_thread).grid(row=7, column=0, columnspan=3, padx=20, pady=(10, 20), sticky="ew")

        # Right frame: .bimg -> Image
        right = ctk.CTkFrame(self, corner_radius=6)
        right.grid(row=1, column=0, padx=12, pady=12, sticky="nsew")
        right.grid_columnconfigure((0,1,2), weight=1)
        for r in range(11):
            right.grid_rowconfigure(r, weight=0)
        right.grid_rowconfigure(8, weight=1)  # spacer pushes button to bottom

        ctk.CTkLabel(right, text=".bimg -> Image", font=TITLE_FONT).grid(row=0, column=0, columnspan=3, pady=(4, 4))

        self.bimg_entry = ctk.CTkEntry(right, placeholder_text="Select .bimg file...", font=FONT)
        self.bimg_entry.grid(row=1, column=0, columnspan=2, padx=8, pady=6, sticky="ew")
        ctk.CTkButton(right, text="Browse", font=FONT, command=self.browse_bimg).grid(row=1, column=2, padx=4, pady=4)

        self.restore_entry = ctk.CTkEntry(right, placeholder_text="Output filename (e.g. restored.png)", font=FONT)
        self.restore_entry.grid(row=2, column=0, columnspan=2, padx=8, pady=6, sticky="ew")
        self.format_var = ctk.StringVar(value="PNG")
        ctk.CTkOptionMenu(right, variable=self.format_var, values=["PNG", "JPEG"]).grid(row=2, column=2, padx=8, pady=6)

        # Checkboxes side-by-side on the same row
        self.smooth_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(right, text="Smooth clustering artifacts", variable=self.smooth_var, font=FONT).grid(row=3, column=0, padx=4, pady=4, sticky="w")
        self.smooth_var.trace_add("write", lambda *_: self.update_smooth_state())

        self.blur_var = ctk.BooleanVar(value=False)
        self.blur_check = ctk.CTkCheckBox(right, text="Blur smoothed areas", variable=self.blur_var, font=FONT, state="disabled")
        self.blur_check.grid(row=3, column=1, columnspan=2, padx=4, pady=4, sticky="w")
        self.blur_var.trace_add("write", lambda *_: self.update_blur_state())

        # Sliders stacked directly above each other; label includes live value
        self.smooth_strength_var = ctk.DoubleVar(value=1.0)
        self.smooth_strength_label = ctk.CTkLabel(right, text="Smooth strength: 100%", font=FONT, text_color="gray")
        self.smooth_strength_label.grid(row=4, column=0, padx=4, pady=(8, 2), sticky="w")
        self.smooth_strength_slider = ctk.CTkSlider(right, from_=0.0, to=2.0, number_of_steps=200, variable=self.smooth_strength_var, state="disabled")
        self.smooth_strength_slider.grid(row=4, column=1, columnspan=2, padx=4, pady=(8, 2), sticky="ew")
        self.smooth_strength_slider.bind("<B1-Motion>", self.update_smooth_strength_label)
        self.smooth_strength_slider.bind("<Button-1>", self.update_smooth_strength_label)

        # Primary blur radius (row 5)
        self.blur_radius_var = ctk.DoubleVar(value=2)
        self.blur_radius_label = ctk.CTkLabel(right, text="Blur radius: 2px", font=FONT, text_color="gray")
        self.blur_radius_label.grid(row=5, column=0, padx=4, pady=(8, 2), sticky="w")
        self.blur_radius_slider = ctk.CTkSlider(right, from_=1, to=20, number_of_steps=19, variable=self.blur_radius_var, state="disabled")
        self.blur_radius_slider.grid(row=5, column=1, columnspan=2, padx=4, pady=(8, 2), sticky="ew")
        self.blur_radius_slider.bind("<B1-Motion>", self.update_blur_radius_label)
        self.blur_radius_slider.bind("<Button-1>", self.update_blur_radius_label)

        # Extra blur passes: count label + [−] [+] buttons (row 6)
        self.extra_blur_passes = []  # list of (DoubleVar, CTkLabel, CTkSlider)
        self.blur_extra_passes_label = ctk.CTkLabel(right, text="Extra blur passes: 0", font=FONT, text_color="gray")
        self.blur_extra_passes_label.grid(row=6, column=0, padx=4, pady=(8, 2), sticky="w")
        self.blur_pass_remove_btn = ctk.CTkButton(right, text="−", width=36, font=FONT, state="disabled", command=self.remove_blur_pass)
        self.blur_pass_remove_btn.grid(row=6, column=1, padx=(4, 2), pady=(8, 2), sticky="e")
        self.blur_pass_add_btn = ctk.CTkButton(right, text="+", width=36, font=FONT, state="disabled", command=self.add_blur_pass)
        self.blur_pass_add_btn.grid(row=6, column=2, padx=(2, 4), pady=(8, 2), sticky="w")

        # Inner frame that holds the dynamically added blur-pass sliders (row 7)
        self.blur_passes_frame = ctk.CTkFrame(right, fg_color="transparent")
        self.blur_passes_frame.grid(row=7, column=0, columnspan=3, padx=0, pady=0, sticky="ew")
        self.blur_passes_frame.grid_columnconfigure(0, weight=1)
        self.blur_passes_frame.grid_columnconfigure(1, weight=2)

        ctk.CTkButton(right, text="Restore Image", fg_color="#16a34a", font=FONT, command=self.restore_thread).grid(row=9, column=0, columnspan=3, padx=20, pady=(10, 20), sticky="ew")

        # Status bar
        self.status = ctk.CTkLabel(self, text="Ready", anchor="w")
        self.status.grid(row=2, column=0, sticky="ew", padx=12, pady=(0,12))
        # theme controls in a frame
        theme_frame = ctk.CTkFrame(self, fg_color="transparent")
        theme_frame.grid(row=2, column=0, sticky="e", padx=12, pady=(0,12))
        ctk.CTkLabel(theme_frame, text="Theme:", font=FONT).pack(side="left", padx=(0, 4))
        self.theme_var = ctk.BooleanVar(value=(ctk.get_appearance_mode().lower() == "light"))
        self.theme_switch = ctk.CTkSwitch(theme_frame, text="", variable=self.theme_var, command=self.toggle_theme)
        self.theme_switch.pack(side="left")

    def set_status(self, text):
        self.status.configure(text=text)
        logger.debug(f"Status: {text}")

    def toggle_theme(self):
        # use variable to determine target
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
        else:
            self.smooth_strength_slider.configure(state="disabled")
            self.smooth_strength_label.configure(text_color="gray")
            self.blur_check.configure(state="disabled")
            self._clear_blur_controls()

    def update_blur_state(self):
        """Enable or disable blur controls based on the blur checkbox."""
        if self.blur_var.get():
            self.blur_radius_slider.configure(state="normal")
            self.blur_radius_label.configure(text_color=("black", "white"))
            self.blur_extra_passes_label.configure(text_color=("black", "white"))
            self.blur_pass_add_btn.configure(state="normal")
        else:
            self._clear_blur_controls()

    def _clear_blur_controls(self):
        """Disable and reset all blur controls, destroying any extra pass sliders."""
        self.blur_radius_slider.configure(state="disabled")
        self.blur_radius_label.configure(text_color="gray")
        self.blur_extra_passes_label.configure(text="Extra blur passes: 0", text_color="gray")
        self.blur_pass_add_btn.configure(state="disabled")
        self.blur_pass_remove_btn.configure(state="disabled")
        for _, label, slider in self.extra_blur_passes:
            label.destroy()
            slider.destroy()
        self.extra_blur_passes.clear()

    def add_blur_pass(self):
        """Append a new blur-pass slider row to blur_passes_frame."""
        pass_num = len(self.extra_blur_passes) + 1
        default_radius = min(2 + pass_num * 2, 20)
        var = ctk.DoubleVar(value=default_radius)
        label = ctk.CTkLabel(self.blur_passes_frame, text=f"Pass {pass_num + 1} radius: {default_radius}px", font=FONT)
        slider = ctk.CTkSlider(self.blur_passes_frame, from_=1, to=20, number_of_steps=19, variable=var)
        row = len(self.extra_blur_passes)
        label.grid(row=row, column=0, padx=4, pady=(4, 2), sticky="w")
        slider.grid(row=row, column=1, padx=4, pady=(4, 2), sticky="ew")

        def make_updater(lbl, v, n):
            def _update(_=None):
                lbl.configure(text=f"Pass {n + 1} radius: {int(v.get())}px")
            return _update

        updater = make_updater(label, var, pass_num)
        slider.bind("<B1-Motion>", updater)
        slider.bind("<Button-1>", updater)
        self.extra_blur_passes.append((var, label, slider))
        self.blur_extra_passes_label.configure(text=f"Extra blur passes: {len(self.extra_blur_passes)}")
        self.blur_pass_remove_btn.configure(state="normal")

    def remove_blur_pass(self):
        """Remove the last blur-pass slider row."""
        if not self.extra_blur_passes:
            return
        _, label, slider = self.extra_blur_passes.pop()
        label.destroy()
        slider.destroy()
        self.blur_extra_passes_label.configure(text=f"Extra blur passes: {len(self.extra_blur_passes)}")
        if not self.extra_blur_passes:
            self.blur_pass_remove_btn.configure(state="disabled")

    def update_blur_radius_label(self, event=None):
        radius = int(self.blur_radius_var.get())
        self.blur_radius_label.configure(text=f"Blur radius: {radius}px")

    def update_smooth_strength_label(self, event=None):
        """Update the smooth strength label to show current slider value."""
        pct = int(self.smooth_strength_var.get() * 100)
        self.smooth_strength_label.configure(text=f"Smooth strength: {pct}%")
        logger.debug(f"Smooth strength updated to: {pct}%")

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
        p = self.system_file_dialog(title="Select image")
        if p is NotImplemented:
            p = filedialog.askopenfilename(parent=self, filetypes=[("Image files", ("*.png","*.jpg","*.jpeg","*.gif","*.bmp","*.tiff")), ("All files", ("*.*",))])
        elif p is None:
            return
        if p:
            self.src_entry.delete(0, "end")
            self.src_entry.insert(0, p)

    def browse_bimg(self):
        p = self.system_file_dialog(title="Select bundle")
        if p is NotImplemented:
            p = filedialog.askopenfilename(parent=self, filetypes=[("BIMG bundle", ("*.bimg","*.tar.gz")), ("All files", ("*.*",))])
        elif p is None:
            return
        if p:
            self.bimg_entry.delete(0, "end")
            self.bimg_entry.insert(0, p)

    def create_bimg_thread(self):
        t = threading.Thread(target=self.create_bimg)
        t.daemon = True
        t.start()

    def system_file_dialog(self, title="Select file"):
        # try zenity/kdialog for a native feel on Linux
        import shutil, subprocess, sys
        native_available = False
        if sys.platform.startswith("linux"):
            if shutil.which("zenity"):
                native_available = True
                try:
                    res = subprocess.run(["zenity","--file-selection","--title",title], capture_output=True, text=True)
                    if res.returncode == 0:
                        return res.stdout.strip()
                    else:
                        return None  # canceled
                except Exception:
                    pass
            if shutil.which("kdialog"):
                native_available = True
                try:
                    res = subprocess.run(["kdialog","--getopenfilename","",title], capture_output=True, text=True)
                    if res.returncode == 0:
                        return res.stdout.strip()
                    else:
                        return None
                except Exception:
                    pass
        # macOS apple script
        if sys.platform == "darwin":
            native_available = True
            try:
                script = 'POSIX path of (choose file with prompt "' + title + '")'
                res = subprocess.run(["osascript","-e",script], capture_output=True, text=True)
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
        # if user didn't supply a name, default to source base name
        if not out:
            out = os.path.splitext(os.path.basename(src))[0]
        # append extension based on option menu selection
        fmt = self.format_var.get().upper()
        if not os.path.splitext(out)[1]:
            out = out + "." + fmt.lower()
        smooth_gradients = bool(self.smooth_var.get())
        smooth_strength = float(self.smooth_strength_var.get())
        blur_passes = None
        if smooth_gradients and self.blur_var.get():
            blur_passes = [int(self.blur_radius_var.get())]
            blur_passes += [int(var.get()) for var, _, _ in self.extra_blur_passes]
        logger.info(f"Restoring image: {src} -> {out}")
        logger.info(f"Settings: format={fmt}, smooth={smooth_gradients}, strength={smooth_strength:.2f}, blur_passes={blur_passes}")
        self.set_status("Restoring image...")
        try:
            decompress.decompress_image(src, out, smooth_gradients=smooth_gradients, smooth_strength=smooth_strength, blur_passes=blur_passes)
            logger.info(f"Successfully restored: {out}")
            self.set_status(f"Restored: {out}")
        except Exception as e:
            logger.error(f"Error restoring image: {e}", exc_info=True)
            self.set_status(f"Error: {e}")


if __name__ == "__main__":
    app = App()
    app.mainloop()
