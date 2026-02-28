#!/usr/bin/env python3
"""
B-IMG Viewer: A simple utility to view, zoom, and pan .bimg files.
"""

import os
import tkinter as tk
from tkinter import filedialog
import customtkinter as ctk
from PIL import Image, ImageTk
import logging
import threading

import decompress

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class BImageViewer(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("B-IMG Viewer")
        self.geometry("1200x800")

        # State
        self.current_path = None
        self.original_image = None
        self.display_image = None
        self.zoom_level = 1.0
        self.canvas_image_id = None
        self.extra_blur_passes = []  # list of (DoubleVar, CTkLabel, CTkSlider)

        # UI Setup
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # --- Sidebar ---
        self.sidebar = ctk.CTkFrame(self, width=250, corner_radius=0)
        self.sidebar.grid(row=0, column=0, rowspan=3, sticky="nsew", padx=0, pady=0)
        self.sidebar.grid_rowconfigure(10, weight=1) # spacer

        ctk.CTkLabel(self.sidebar, text="B-IMG Viewer", font=("Arial", 20, "bold")).grid(row=0, column=0, padx=20, pady=(20, 10))
        
        self.open_btn = ctk.CTkButton(self.sidebar, text="Open .bimg", command=self.open_file)
        self.open_btn.grid(row=1, column=0, padx=20, pady=10)

        # Smoothing Controls
        ctk.CTkLabel(self.sidebar, text="Post-Processing", font=("Arial", 14, "bold")).grid(row=2, column=0, padx=20, pady=(20, 5), sticky="w")
        
        self.smooth_var = ctk.BooleanVar(value=False)
        self.smooth_check = ctk.CTkCheckBox(self.sidebar, text="Smooth artifacts", variable=self.smooth_var, command=self.update_smooth_state)
        self.smooth_check.grid(row=3, column=0, padx=20, pady=5, sticky="w")

        self.smooth_strength_var = ctk.DoubleVar(value=1.0)
        self.smooth_strength_label = ctk.CTkLabel(self.sidebar, text="Strength: 100%", text_color="gray")
        self.smooth_strength_label.grid(row=4, column=0, padx=20, pady=(5, 0), sticky="w")
        self.smooth_strength_slider = ctk.CTkSlider(self.sidebar, from_=0.0, to=2.0, variable=self.smooth_strength_var, state="disabled", command=self.update_smooth_strength_label)
        self.smooth_strength_slider.grid(row=5, column=0, padx=20, pady=5, sticky="ew")

        self.blur_var = ctk.BooleanVar(value=False)
        self.blur_check = ctk.CTkCheckBox(self.sidebar, text="Blur smoothed areas", variable=self.blur_var, state="disabled", command=self.update_blur_state)
        self.blur_check.grid(row=6, column=0, padx=20, pady=5, sticky="w")

        self.blur_radius_var = ctk.DoubleVar(value=2)
        self.blur_radius_label = ctk.CTkLabel(self.sidebar, text="Blur radius: 2px", text_color="gray")
        self.blur_radius_label.grid(row=7, column=0, padx=20, pady=(5, 0), sticky="w")
        self.blur_radius_slider = ctk.CTkSlider(self.sidebar, from_=1, to=20, number_of_steps=19, variable=self.blur_radius_var, state="disabled", command=self.update_blur_radius_label)
        self.blur_radius_slider.grid(row=8, column=0, padx=20, pady=5, sticky="ew")

        self.apply_btn = ctk.CTkButton(self.sidebar, text="Apply Changes", command=self.reload_current_file, fg_color="#16a34a", state="disabled")
        self.apply_btn.grid(row=9, column=0, padx=20, pady=20)

        # --- Main Content ---
        # Toolbar
        self.toolbar = ctk.CTkFrame(self, height=40)
        self.toolbar.grid(row=0, column=1, sticky="ew", padx=10, pady=5)
        
        self.zoom_label = ctk.CTkLabel(self.toolbar, text="Zoom: 100%")
        self.zoom_label.pack(side="right", padx=15)

        # Canvas for Image
        self.canvas = tk.Canvas(self, bg="#1a1a1a", highlightthickness=0)
        self.canvas.grid(row=1, column=1, sticky="nsew", padx=10, pady=(0, 10))

        # Mouse Bindings for Panning
        self.canvas.bind("<ButtonPress-1>", self.on_pan_start)
        self.canvas.bind("<B1-Motion>", self.on_pan_move)
        
        # Mouse Bindings for Zooming
        self.canvas.bind("<MouseWheel>", self.on_zoom)  # Windows/macOS
        self.canvas.bind("<Button-4>", self.on_zoom)    # Linux zoom in
        self.canvas.bind("<Button-5>", self.on_zoom)    # Linux zoom out

        # Status Bar
        self.status = ctk.CTkLabel(self, text="Ready", anchor="w")
        self.status.grid(row=2, column=1, sticky="ew", padx=15, pady=(0, 5))

    def update_smooth_state(self):
        if self.smooth_var.get():
            self.smooth_strength_slider.configure(state="normal")
            self.smooth_strength_label.configure(text_color=("black", "white"))
            self.blur_check.configure(state="normal")
        else:
            self.smooth_strength_slider.configure(state="disabled")
            self.smooth_strength_label.configure(text_color="gray")
            self.blur_check.configure(state="disabled")
            self.blur_var.set(False)
            self.update_blur_state()
        self.update_apply_btn_state()

    def update_blur_state(self):
        if self.blur_var.get():
            self.blur_radius_slider.configure(state="normal")
            self.blur_radius_label.configure(text_color=("black", "white"))
        else:
            self.blur_radius_slider.configure(state="disabled")
            self.blur_radius_label.configure(text_color="gray")
        self.update_apply_btn_state()

    def update_apply_btn_state(self):
        if self.current_path:
            self.apply_btn.configure(state="normal")
        else:
            self.apply_btn.configure(state="disabled")

    def update_smooth_strength_label(self, value):
        pct = int(float(value) * 100)
        self.smooth_strength_label.configure(text=f"Strength: {pct}%")

    def update_blur_radius_label(self, value):
        radius = int(float(value))
        self.blur_radius_label.configure(text=f"Blur radius: {radius}px")

    def system_file_dialog(self, title="Select file"):
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
                        return None
                except Exception: pass
            if shutil.which("kdialog"):
                native_available = True
                try:
                    res = subprocess.run(["kdialog","--getopenfilename","",title], capture_output=True, text=True)
                    if res.returncode == 0:
                        return res.stdout.strip()
                    else:
                        return None
                except Exception: pass
        if sys.platform == "darwin":
            native_available = True
            try:
                script = 'POSIX path of (choose file with prompt "' + title + '")'
                res = subprocess.run(["osascript","-e",script], capture_output=True, text=True)
                if res.returncode == 0:
                    return res.stdout.strip()
                else:
                    return None
            except Exception: pass
        if not native_available:
            return NotImplemented
        return None

    def open_file(self):
        path = self.system_file_dialog(title="Open .bimg bundle")
        if path is NotImplemented:
            path = filedialog.askopenfilename(filetypes=[("BIMG bundle", "*.bimg"), ("All files", "*.*")])
        elif path is None:
            return

        if not path:
            return

        self.current_path = path
        self.reload_current_file()

    def reload_current_file(self):
        if not self.current_path:
            return

        self.status.configure(text=f"Processing: {os.path.basename(self.current_path)}...")
        self.update_idletasks()
        self._set_controls_state("disabled")
        threading.Thread(target=self._process_image_thread, daemon=True).start()

    def _set_controls_state(self, state):
        self.open_btn.configure(state=state)
        self.apply_btn.configure(state=state)
        self.smooth_check.configure(state=state)
        self.blur_check.configure(state=state)
        self.smooth_strength_slider.configure(state=state)
        self.blur_radius_slider.configure(state=state)
        
        if state == "normal":
            self.update_smooth_state()
            self.update_blur_state()

    def _process_image_thread(self):
        try:
            temp_out = "viewer_temp_load.png"
            smooth = self.smooth_var.get()
            strength = self.smooth_strength_var.get()
            blur_passes = [int(self.blur_radius_var.get())] if smooth and self.blur_var.get() else None

            decompress.decompress_image(self.current_path, temp_out, smooth_gradients=smooth, smooth_strength=strength, blur_passes=blur_passes)
            
            loaded_img = Image.open(temp_out)
            self.original_image = loaded_img.copy()
            loaded_img.close()
            if os.path.exists(temp_out): os.remove(temp_out)

            self.after(0, self._finish_loading)
        except Exception as e:
            logger.error(f"Failed to load .bimg: {e}", exc_info=True)
            self.after(0, lambda: self.status.configure(text=f"Error: {e}"))
            self.after(0, lambda: self._set_controls_state("normal"))

    def _finish_loading(self):
        self.zoom_level = 1.0
        self.show_image(reset_view=True)
        self.status.configure(text=f"Loaded: {os.path.basename(self.current_path)}")
        self.update_apply_btn_state()
        self._set_controls_state("normal")

    def show_image(self, reset_view=False):
        if not self.original_image:
            return

        orig_w, orig_h = self.original_image.size
        full_w = int(orig_w * self.zoom_level)
        full_h = int(orig_h * self.zoom_level)

        can_w = self.canvas.winfo_width()
        can_h = self.canvas.winfo_height()
        if can_w <= 1: can_w, can_h = 1200, 800

        # Virtual center and bounds
        virt_cx, virt_cy = can_w // 2, can_h // 2
        v_ix1, v_iy1 = virt_cx - full_w // 2, virt_cy - full_h // 2
        v_ix2, v_iy2 = v_ix1 + full_w, v_iy1 + full_h

        # Viewport bounds
        vx1 = self.canvas.canvasx(0)
        vy1 = self.canvas.canvasy(0)
        vx2, vy2 = vx1 + can_w, vy1 + can_h

        # Intersection
        inter_x1, inter_y1 = max(vx1, v_ix1), max(vy1, v_iy1)
        inter_x2, inter_y2 = min(vx2, v_ix2), min(vy2, v_iy2)

        if inter_x2 <= inter_x1 or inter_y2 <= inter_y1:
            if self.canvas_image_id: self.canvas.delete(self.canvas_image_id)
            self.canvas_image_id = None
        else:
            # Map intersection to original image
            crop_x1 = int((inter_x1 - v_ix1) / self.zoom_level)
            crop_y1 = int((inter_y1 - v_iy1) / self.zoom_level)
            crop_x2 = int((inter_x2 - v_ix1) / self.zoom_level) + 1
            crop_y2 = int((inter_y2 - v_iy1) / self.zoom_level) + 1
            
            crop_x1, crop_y1 = max(0, min(orig_w, crop_x1)), max(0, min(orig_h, crop_y1))
            crop_x2, crop_y2 = max(0, min(orig_w, crop_x2)), max(0, min(orig_h, crop_y2))

            # Crop and resize ONLY visible part
            resample = Image.Resampling.LANCZOS if self.zoom_level < 1.0 else Image.Resampling.NEAREST
            cropped = self.original_image.crop((crop_x1, crop_y1, crop_x2, crop_y2))
            
            rw, rh = int((crop_x2 - crop_x1) * self.zoom_level), int((crop_y2 - crop_y1) * self.zoom_level)
            if rw > 0 and rh > 0:
                resized = cropped.resize((rw, rh), resample)
                self.display_image = ImageTk.PhotoImage(resized)
                
                fx, fy = v_ix1 + crop_x1 * self.zoom_level, v_iy1 + crop_y1 * self.zoom_level
                if self.canvas_image_id is None:
                    self.canvas_image_id = self.canvas.create_image(fx, fy, anchor="nw", image=self.display_image)
                else:
                    self.canvas.itemconfig(self.canvas_image_id, image=self.display_image)
                    self.canvas.coords(self.canvas_image_id, fx, fy)

        # Update scrollregion based on FULL virtual image size
        pad = 2000
        self.canvas.config(scrollregion=(v_ix1 - pad, v_iy1 - pad, v_ix2 + pad, v_iy2 + pad))
        self.zoom_label.configure(text=f"Zoom: {int(self.zoom_level * 100)}%")

    def on_zoom(self, event):
        if not self.original_image: return
        if event.num == 4 or event.delta > 0: self.zoom_level *= 1.1
        elif event.num == 5 or event.delta < 0: self.zoom_level /= 1.1
        self.zoom_level = max(0.05, min(self.zoom_level, 50.0))
        self.show_image()

    def on_pan_start(self, event):
        self.canvas.scan_mark(event.x, event.y)

    def on_pan_move(self, event):
        self.canvas.scan_dragto(event.x, event.y, gain=1)
        self.show_image() # Update crop during pan

if __name__ == "__main__":
    ctk.set_appearance_mode("dark")
    app = BImageViewer()
    app.mainloop()
