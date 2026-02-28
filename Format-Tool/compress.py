#!/usr/bin/env python3
"""Convert an image into a compressed .bimg file.

The output is a data structure containing the width, height and an array of
pixels ordered row by row. Each pixel is a list of three integers [R, G, B].

Usage: python compress.py path/to/file.png -o output.bimg
"""

import argparse
import json
import sys
import io
import tarfile
import os
import struct
import lzma
import shutil
import random
import logging

from PIL import Image

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def image_to_pixels(path, preserve_alpha=False):
    """Read an image file and return (width, height, pixels, mode).

    If `preserve_alpha` is True the image is converted to RGBA and each
    pixel is returned as [R, G, B, A]. Otherwise pixels are [R, G, B].
    """
    with Image.open(path) as img:
        if preserve_alpha:
            img = img.convert("RGBA")
            mode = "RGBA"
        else:
            img = img.convert("RGB")
            mode = "RGB"
        width, height = img.size
        data = list(img.getdata())  # flat list of tuples
        # convert tuples to lists
        pixels = [list(pixel) for pixel in data]
        return width, height, pixels, mode


def kmeans_cluster(pixels, threshold=0):
    """Cluster pixels using K-means based on color distance threshold.

    threshold: 0 (lossless, 256 colors) to 255 (extreme, 1-2 colors)
    Returns: (clustered_pixels, palette, threshold_used)
    """
    if threshold == 0 or len(pixels) == 0:
        # Lossless mode
        unique_colors = {}
        palette = []
        indexed = []
        for pixel in pixels:
            color = tuple(pixel[:3])
            if color not in unique_colors:
                unique_colors[color] = len(palette)
                palette.append(list(color))
            indexed.append(unique_colors[color])
        return indexed, palette, 0

    # Determine number of clusters k from threshold
    k = max(2, 256 - (threshold // 2))

    # Unique colors set for efficiency
    unique_colors_set = set(tuple(pixel[:3]) for pixel in pixels)
    unique_colors_list = list(unique_colors_set)

    if len(unique_colors_list) <= k:
        # Already few enough colors, return lossless result
        return kmeans_cluster(pixels, threshold=0)

    # Simple K-means: init centers randomly from unique colors, iterate
    centers = random.sample(unique_colors_list, k)

    for iteration in range(6):  # 6 iterations for convergence
        # Assign each unique color to nearest center
        assignments = {}
        for color in unique_colors_list:
            nearest = min(range(k), key=lambda i: sum((color[j] - centers[i][j])**2 for j in range(3)))
            if nearest not in assignments:
                assignments[nearest] = []
            assignments[nearest].append(color)

        # Recompute centers
        new_centers = []
        for i in range(k):
            if i in assignments and assignments[i]:
                avg = [int(sum(c[j] for c in assignments[i]) / len(assignments[i])) for j in range(3)]
                new_centers.append(avg)
            else:
                new_centers.append(centers[i])

        # Check for convergence
        if all(sum((n[j] - centers[i][j])**2 for j in range(3))**0.5 < 1 for i, n in enumerate(new_centers)):
            break
        centers = new_centers

    palette = [[int(c[j]) for j in range(3)] for c in centers]

    # map every pixel to nearest palette entry
    indexed = []
    for pixel in pixels:
        color = tuple(pixel[:3])
        nearest = min(range(len(palette)), key=lambda i: sum((color[j] - palette[i][j])**2 for j in range(3)))
        indexed.append(nearest)

    return indexed, palette, threshold


def pixels_to_binary(width, height, pixels, mode, use_palette=True, use_delta=True, cluster_threshold=0):
    """Convert pixels to compressed binary format with advanced optimizations.

    Format: width(4) + height(4) + mode(1) + flags(1) + [palette_data] + pixel_data

    Optimizations:
    - K-means clustering: reduce colors using lossy clustering (if cluster_threshold > 0)
    - Palette mode: use indexed color for images with <= 256 unique colors
    - Delta encoding: store differences between adjacent pixels
    - Hex encoding: variable-length encoding for small palettes (≤16 colors = 4-bit)
    """
    # K-means clustering if enabled
    clustering_applied = False
    indexed_pixels_list = None
    palette_list = None

    if cluster_threshold > 0 and mode == "RGB":
        # Apply K-means clustering
        indexed_pixels_list, palette_list, _ = kmeans_cluster(pixels, cluster_threshold)
        clustering_applied = True
        # palette_list is already [R,G,B] format

    # Try palette mode if enabled and not preserving alpha
    palette_pixels = pixels
    palette_data = b""
    mode_byte = 1 if mode == "RGBA" else 0
    flags = 0

    # If clustering was applied, use the clustered palette; otherwise try to build one
    if clustering_applied:
        # Use clustering palette
        mode_byte = 2  # INDEXED mode
        flags |= 0x02  # palette flag
        flags |= 0x04  # clustering flag
        palette_data = struct.pack("<H", len(palette_list))
        for r, g, b in palette_list:
            palette_data += bytes([r, g, b])
        palette_pixels = [[idx] for idx in indexed_pixels_list]
    elif use_palette and mode == "RGB":
        # Try to build a palette from actual colors
        unique_colors = {}
        palette_list_auto = []
        indexed_pixels = []
        can_use_palette = True

        flat = [val for pixel in pixels for val in pixel]
        for i in range(0, len(flat), 3):
            color = tuple(flat[i:i+3])
            if color not in unique_colors:
                if len(unique_colors) >= 256:
                    can_use_palette = False
                    break
                unique_colors[color] = len(palette_list_auto)
                palette_list_auto.append(color)
            indexed_pixels.append(unique_colors[color])

        if can_use_palette and len(palette_list_auto) < 256:
            # Use palette mode
            mode_byte = 2  # INDEXED mode
            flags |= 0x02  # palette flag
            palette_data = struct.pack("<H", len(palette_list_auto))
            for r, g, b in palette_list_auto:
                palette_data += bytes([r, g, b])
            palette_pixels = [[idx] for idx in indexed_pixels]

    # Apply delta encoding if enabled
    encoded_pixels = palette_pixels[:]
    if use_delta and len(palette_pixels) > 0:
        bytes_per_pixel = len(palette_pixels[0])
        delta_pixels = []
        for i, pixel in enumerate(palette_pixels):
            if i == 0:
                delta_pixels.append(pixel[:])
            else:
                prev_pixel = palette_pixels[i - 1]
                delta = []
                for j in range(bytes_per_pixel):
                    diff = pixel[j] - prev_pixel[j]
                    # Store as signed byte (-128 to 127)
                    delta.append(diff & 0xFF)
                delta_pixels.append(delta)
        encoded_pixels = delta_pixels
        flags |= 0x01  # delta flag

    # Build binary data
    binary = struct.pack("<II", width, height)
    binary += struct.pack("BB", mode_byte, flags)
    binary += palette_data

    # Add pixel data (for now, standard 1-byte per index; hex encoding can be added later)
    flat_pixels = [val for pixel in encoded_pixels for val in pixel]
    binary += bytes(flat_pixels)

    return binary


def create_smart_bundle(image_path, out_path, pixels, width, height, mode, cluster_threshold=0):
    """Create a .bimg bundle with smart format selection.

    Compares:
    - Optimized binary format (with LZMA preset=9, optional K-means clustering)
    - Original PNG file (if available)

    Uses whichever is smaller. Simple format: [format_byte][gzipped_data]
    - Format byte: 0 = PNG, 1 = Binary optimized

    Args:
        cluster_threshold: 0-255, where 0=lossless, 255=extreme compression (2 colors)
    """
    import gzip

    # Try optimized binary format with optional clustering
    binary_data = pixels_to_binary(width, height, pixels, mode, use_palette=True, use_delta=True, cluster_threshold=cluster_threshold)
    binary_compressed = lzma.compress(binary_data, preset=9)

    # Try original PNG if it's a PNG file
    use_original_png = False
    original_png_data = None

    if image_path.lower().endswith('.png'):
        try:
            with open(image_path, 'rb') as f:
                original_png_data = f.read()
        except Exception:
            original_png_data = None

    # Compare sizes: use original PNG if it's smaller
    if original_png_data and len(original_png_data) < len(binary_compressed):
        use_original_png = True
        data_to_store = original_png_data
        format_byte = 0
    else:
        data_to_store = binary_compressed
        format_byte = 1

    # Write with minimal format: [format_byte|data]
    with open(out_path, 'wb') as f:
        f.write(bytes([format_byte]))
        f.write(data_to_store)

    # Report what was used
    if use_original_png:
        print(f"Smart bundle: Using original PNG ({len(data_to_store)} bytes)")
    else:
        print(f"Smart bundle: Using optimized binary ({len(data_to_store)} bytes)")

    return


def main():
    parser = argparse.ArgumentParser(description="Image -> .bimg compressor")
    parser.add_argument("image", help="Path to the input image file")
    parser.add_argument(
        "-o",
        "--output",
        help="Write pixel data to this file instead of stdout",
        default=None,
    )
    parser.add_argument(
        "--flatten",
        action="store_true",
        help="Produce a flat list of pixels instead of nested rows",
    )
    parser.add_argument(
        "--alpha",
        action="store_true",
        help="Preserve alpha channel (output RGBA pixels).",
    )
    parser.add_argument(
        "--bundle",
        action="store_true",
        help="Bundle pixel data and write a .bimg file.",
    )
    args = parser.parse_args()

    width, height, pixels, mode = image_to_pixels(args.image, preserve_alpha=args.alpha)

    if not args.flatten:
        # convert to nested list [row][col]
        rows = [pixels[i * width : (i + 1) * width] for i in range(height)]
        output_data = {"width": width, "height": height, "pixels": rows, "mode": mode}
    else:
        output_data = {"width": width, "height": height, "pixels": pixels, "mode": mode}

    # If bundling requested or output ends with .bimg, write a smart bundle (.bimg)
    want_bundle = args.bundle or (args.output and args.output.endswith(".bimg"))
    if want_bundle:
        if not args.output:
            parser.error("--bundle requires an --output filename (recommended with .bimg extension)")

        out_path = args.output
        if not out_path.endswith(".bimg"):
            out_path = out_path + ".bimg"

        # Use smart hybrid bundle format
        create_smart_bundle(args.image, out_path, pixels, width, height, mode)
        print(f"Wrote bundle: {out_path}")
        return

    out_stream = open(args.output, "w") if args.output else sys.stdout
    json.dump(output_data, out_stream)
    if args.output:
        out_stream.close()


if __name__ == "__main__":
    main()
