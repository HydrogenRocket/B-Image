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


# ---------------------------------------------------------------------------
# PNG-style prediction filter helpers
# ---------------------------------------------------------------------------

def _paeth_predictor(a, b, c):
    """PNG Paeth predictor function."""
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    elif pb <= pc:
        return b
    else:
        return c


def _apply_png_filter(filter_type, row, prev_row):
    """Apply one of the 5 PNG filter types to a row of bytes.

    filter_type: 0=None, 1=Sub, 2=Up, 3=Average, 4=Paeth
    row: bytes of current row
    prev_row: bytes of previous (original/decoded) row; same length as row
    Returns filtered bytes.
    """
    n = len(row)
    out = bytearray(n)
    if filter_type == 0:  # None
        out[:] = row
    elif filter_type == 1:  # Sub
        for i in range(n):
            a = row[i - 1] if i > 0 else 0
            out[i] = (row[i] - a) & 0xFF
    elif filter_type == 2:  # Up
        for i in range(n):
            out[i] = (row[i] - prev_row[i]) & 0xFF
    elif filter_type == 3:  # Average
        for i in range(n):
            a = row[i - 1] if i > 0 else 0
            b = prev_row[i]
            out[i] = (row[i] - ((a + b) >> 1)) & 0xFF
    elif filter_type == 4:  # Paeth
        for i in range(n):
            a = row[i - 1] if i > 0 else 0
            b = prev_row[i]
            c = prev_row[i - 1] if i > 0 else 0
            out[i] = (row[i] - _paeth_predictor(a, b, c)) & 0xFF
    return bytes(out)


def _best_png_filter(row, prev_row):
    """Try all 5 PNG filter types; return (filter_type, filtered_row) with lowest score.

    Score = sum of abs-as-signed-byte values (lower = better for compression).
    """
    best_type = 0
    best_row = bytes(row)
    best_score = float('inf')
    for ft in range(5):
        filtered = _apply_png_filter(ft, row, prev_row)
        score = sum(min(b, 256 - b) for b in filtered)
        if score < best_score:
            best_score = score
            best_type = ft
            best_row = filtered
    return best_type, best_row


def _encode_planar_png(pixels, width, height):
    """Encode RGB pixels using planar storage + PNG per-scanline filters.

    Stores channels separately (all R, then all G, then all B).
    For each channel plane, applies the best PNG filter per scanline.

    Output format per channel:
      height rows × (1 filter-type byte + width data bytes)
    Total: 3 × height × (1 + width) bytes before LZMA.
    """
    r_plane = bytearray(width * height)
    g_plane = bytearray(width * height)
    b_plane = bytearray(width * height)
    for i, px in enumerate(pixels):
        r_plane[i] = px[0]
        g_plane[i] = px[1]
        b_plane[i] = px[2]

    result = bytearray()
    for plane in (r_plane, g_plane, b_plane):
        prev_row = bytes(width)
        for y in range(height):
            row = bytes(plane[y * width:(y + 1) * width])
            ft, filtered = _best_png_filter(row, prev_row)
            result.append(ft)
            result.extend(filtered)
            prev_row = row
    return bytes(result)


def _pack_nibbles(indices, count):
    """Pack palette indices (0–15) into nibbles: two indices per byte.

    High nibble = first index, low nibble = second index.
    A zero-padding nibble is appended when count is odd.
    """
    result = bytearray()
    for i in range(0, count, 2):
        hi = indices[i] & 0xF
        lo = (indices[i + 1] & 0xF) if (i + 1) < count else 0
        result.append((hi << 4) | lo)
    return bytes(result)


def pixels_to_binary(width, height, pixels, mode, use_palette=True, use_delta=True, cluster_threshold=0):
    """Convert pixels to compressed binary format with advanced optimizations.

    Format: width(4) + height(4) + mode(1) + flags(1) + [palette_data] + pixel_data

    Flags:
    - 0x01: delta encoded (8-bit palette indices only)
    - 0x02: palette mode
    - 0x04: K-means clustering applied
    - 0x08: planar channel storage + PNG per-scanline filters (raw RGB)
    - 0x10: nibble-packed 4-bit palette indices (≤16 colors)
    - 0x20: 16-bit palette indices (17–65535 colors)
    """
    # K-means clustering if enabled
    clustering_applied = False
    indexed_pixels_list = None
    palette_list = None

    if cluster_threshold > 0 and mode == "RGB":
        indexed_pixels_list, palette_list, _ = kmeans_cluster(pixels, cluster_threshold)
        clustering_applied = True

    mode_byte = 1 if mode == "RGBA" else 0
    flags = 0
    palette_data = b""
    final_pixel_bytes = b""

    if clustering_applied:
        # Use the K-means palette
        n_colors = len(palette_list)
        mode_byte = 2  # INDEXED mode
        flags |= 0x02 | 0x04  # palette + clustering
        palette_data = struct.pack("<H", n_colors)
        for color in palette_list:
            palette_data += bytes([int(color[0]), int(color[1]), int(color[2])])

        indices = list(indexed_pixels_list)
        if n_colors <= 16:
            flags |= 0x10  # nibble packing
            final_pixel_bytes = _pack_nibbles(indices, len(indices))
        else:
            # 8-bit indices with optional delta
            if use_delta:
                delta = [indices[0]]
                for i in range(1, len(indices)):
                    delta.append((indices[i] - indices[i - 1]) & 0xFF)
                indices = delta
                flags |= 0x01
            final_pixel_bytes = bytes(indices)

    elif use_palette and mode == "RGB":
        # Build a palette from unique colors (up to 65535)
        unique_colors = {}
        palette_list_auto = []
        indexed_pixels = []
        can_use_palette = True

        for pixel in pixels:
            color = tuple(pixel[:3])
            if color not in unique_colors:
                if len(palette_list_auto) >= 65535:
                    can_use_palette = False
                    break
                new_idx = len(palette_list_auto)
                unique_colors[color] = new_idx
                palette_list_auto.append(color)
            indexed_pixels.append(unique_colors[color])

        if can_use_palette:
            n_colors = len(palette_list_auto)
            mode_byte = 2  # INDEXED mode
            flags |= 0x02  # palette flag
            palette_data = struct.pack("<H", n_colors)
            for r, g, b in palette_list_auto:
                palette_data += bytes([r, g, b])

            if n_colors <= 16:
                flags |= 0x10  # nibble packing
                final_pixel_bytes = _pack_nibbles(indexed_pixels, len(indexed_pixels))
            elif n_colors <= 256:
                # 8-bit indices with optional delta
                indices = indexed_pixels
                if use_delta:
                    delta = [indices[0]]
                    for i in range(1, len(indices)):
                        delta.append((indices[i] - indices[i - 1]) & 0xFF)
                    indices = delta
                    flags |= 0x01
                final_pixel_bytes = bytes(indices)
            else:
                # 16-bit palette indices (no delta)
                flags |= 0x20
                buf = bytearray(len(indexed_pixels) * 2)
                for i, idx in enumerate(indexed_pixels):
                    buf[i * 2] = idx & 0xFF
                    buf[i * 2 + 1] = (idx >> 8) & 0xFF
                final_pixel_bytes = bytes(buf)
        else:
            # Too many unique colors — use planar + PNG filters
            flags |= 0x08
            final_pixel_bytes = _encode_planar_png(pixels, width, height)

    elif mode == "RGB":
        # No palette requested — use planar + PNG filters
        flags |= 0x08
        final_pixel_bytes = _encode_planar_png(pixels, width, height)

    else:
        # RGBA: keep delta encoding on raw bytes (existing behavior)
        encoded_pixels = pixels
        if use_delta and pixels:
            bytes_per_pixel = len(pixels[0])
            delta_pixels = [list(pixels[0])]
            for i in range(1, len(pixels)):
                prev = pixels[i - 1]
                delta_pixels.append([(pixels[i][j] - prev[j]) & 0xFF for j in range(bytes_per_pixel)])
            encoded_pixels = delta_pixels
            flags |= 0x01
        flat = [val for px in encoded_pixels for val in px]
        final_pixel_bytes = bytes(flat)

    binary = struct.pack("<II", width, height)
    binary += struct.pack("BB", mode_byte, flags)
    binary += palette_data
    binary += final_pixel_bytes
    return binary


def create_smart_bundle(image_path, out_path, pixels, width, height, mode, cluster_threshold=0):
    """Create a .bimg bundle using the optimized binary format with LZMA compression.

    Format: [format_byte=1][lzma_data]

    Args:
        cluster_threshold: 0-255, where 0=lossless, 255=extreme compression (2 colors)
    """
    binary_data = pixels_to_binary(width, height, pixels, mode, use_palette=True, use_delta=True, cluster_threshold=cluster_threshold)
    binary_compressed = lzma.compress(binary_data, preset=9)

    with open(out_path, 'wb') as f:
        f.write(bytes([1]))  # format_byte=1: binary optimized
        f.write(binary_compressed)

    print(f"Smart bundle: {len(binary_compressed)} bytes")


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
