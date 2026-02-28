#!/usr/bin/env python3
"""Convert a compressed .bimg bundle back into an image file.

The bundle should match the format produced by `compress.py`.

Usage: python decompress.py input.bimg output.png
"""

import argparse
import json
import sys
import tarfile
import io
import logging
import struct
import lzma

from PIL import Image

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def smooth_clustering_artifacts(pixels, width, height, strength=1.0, blur_passes=None):
    """Smooth K-means banding with a full gradient across each large color region.

    Instead of blending only the 1-pixel boundary ring, this computes each pixel's
    BFS distance from the nearest region boundary and applies a linear gradient:
      - boundary pixel (dist=0)  → blends fully toward the adjacent region's color
      - centre pixel (dist=max)  → keeps its original color
      - pixels in between        → blend proportional to (1 - dist/max_dist)

    This makes every large K-means band fade smoothly from its own color at the
    centre to the neighbouring band's color at the edge, across the full width.
    Small regions (fine detail, text) are skipped entirely so sharpness is preserved.

    Args:
        pixels:   flat list of [R,G,B] values
        width, height: image dimensions
        strength: 0.0 = no effect, 1.0 = full gradient, >1.0 = amplified blend width
    """
    from collections import deque

    logger.info(f"Smoothing clustering artifacts: {width}x{height}, strength={strength:.2f}")

    pixel_tuples = [tuple(p) for p in pixels]
    total = width * height
    NEIGHBORS = ((-1, 0), (1, 0), (0, -1), (0, 1))

    # --- Step 1: Connected components via BFS flood fill ---
    logger.info("Computing connected color regions...")
    region_id = [-1] * total
    region_sizes = []
    visited = [False] * total

    for start in range(total):
        if visited[start]:
            continue
        color = pixel_tuples[start]
        queue = deque([start])
        visited[start] = True
        region = [start]
        while queue:
            idx = queue.popleft()
            x, y = idx % width, idx // width
            for dx, dy in NEIGHBORS:
                nx, ny = x + dx, y + dy
                if 0 <= nx < width and 0 <= ny < height:
                    nidx = ny * width + nx
                    if not visited[nidx] and pixel_tuples[nidx] == color:
                        visited[nidx] = True
                        queue.append(nidx)
                        region.append(nidx)
        rid = len(region_sizes)
        region_sizes.append(len(region))
        for idx in region:
            region_id[idx] = rid

    # 0.1% of total pixels, minimum 30 — skips small regions (genuine detail)
    min_region = max(30, total // 1000)
    logger.info(f"Region size threshold: {min_region}px")

    # --- Step 2: BFS inward from region boundaries ---
    # For every pixel in a large region we compute:
    #   dist[i]     – BFS distance to the nearest differently-coloured pixel
    #   target_r/g/b[i] – averaged colour of those differently-coloured neighbours
    #                     at the boundary source (propagated inward unchanged)
    dist = [-1] * total
    target_r = [0.0] * total
    target_g = [0.0] * total
    target_b = [0.0] * total

    queue = deque()

    for y in range(height):
        for x in range(width):
            idx = y * width + x
            if region_sizes[region_id[idx]] < min_region:
                continue
            current_color = pixel_tuples[idx]
            diff = []
            for dx, dy in NEIGHBORS:
                nx, ny = x + dx, y + dy
                if 0 <= nx < width and 0 <= ny < height:
                    nidx = ny * width + nx
                    if pixel_tuples[nidx] != current_color:
                        diff.append(pixels[nidx])
            if diff:
                dist[idx] = 0
                n = len(diff)
                target_r[idx] = sum(p[0] for p in diff) / n
                target_g[idx] = sum(p[1] for p in diff) / n
                target_b[idx] = sum(p[2] for p in diff) / n
                queue.append(idx)

    # Propagate inward: each unvisited same-region pixel inherits the nearest
    # boundary's colour and gets distance = parent_dist + 1
    while queue:
        idx = queue.popleft()
        x, y = idx % width, idx // width
        nd = dist[idx] + 1
        for dx, dy in NEIGHBORS:
            nx, ny = x + dx, y + dy
            if 0 <= nx < width and 0 <= ny < height:
                nidx = ny * width + nx
                if region_id[nidx] != region_id[idx]:
                    continue  # never cross into another region
                if region_sizes[region_id[nidx]] < min_region:
                    continue
                if dist[nidx] != -1:
                    continue  # already reached at a shorter or equal distance
                dist[nidx] = nd
                target_r[nidx] = target_r[idx]
                target_g[nidx] = target_g[idx]
                target_b[nidx] = target_b[idx]
                queue.append(nidx)

    # Max depth reached inside each region — used to normalise the gradient so it
    # spans the full width of the band rather than stopping at an arbitrary depth
    max_dist_per_region = {}
    for idx in range(total):
        d = dist[idx]
        if d > 0:
            rid = region_id[idx]
            if d > max_dist_per_region.get(rid, 0):
                max_dist_per_region[rid] = d

    # --- Step 3: Apply the gradient ---
    # blend_amount = strength * (1 - dist/max_dist)
    #   → 'strength' at the boundary, 0 at the centre
    smoothed = [list(p) for p in pixels]
    blended = 0

    for idx in range(total):
        d = dist[idx]
        if d == -1:
            continue
        rid = region_id[idx]
        max_d = max_dist_per_region.get(rid, 1) or 1
        blend_amount = max(0.0, min(1.0, strength * (1.0 - d / max_d)))
        if blend_amount <= 0.0:
            continue
        r, g, b = pixels[idx]
        smoothed[idx][0] = max(0, min(255, int(r + blend_amount * (target_r[idx] - r))))
        smoothed[idx][1] = max(0, min(255, int(g + blend_amount * (target_g[idx] - g))))
        smoothed[idx][2] = max(0, min(255, int(b + blend_amount * (target_b[idx] - b))))
        blended += 1

    logger.info(f"Gradient smoothing complete: {blended} pixels blended across {len(max_dist_per_region)} regions")

    # --- Optional blur passes (one per entry in blur_passes) ---
    # Each pass operates on the result of the previous one.
    # Only pixels inside large smoothed regions are blurred; unsmoothed detail
    # pixels are left untouched. Sampling from the full array (including
    # unsmoothed neighbours) gives a natural feather at region boundaries.
    for pass_num, pass_radius in enumerate(blur_passes or [], start=1):
        if pass_radius <= 0:
            continue
        logger.info(f"Applying box blur pass {pass_num} (radius={pass_radius}px)...")
        blurred = [row[:] for row in smoothed]
        for idx in range(total):
            if dist[idx] == -1:
                continue
            x, y = idx % width, idx // width
            sr = sg = sb = count = 0
            for by in range(max(0, y - pass_radius), min(height, y + pass_radius + 1)):
                row_base = by * width
                for bx in range(max(0, x - pass_radius), min(width, x + pass_radius + 1)):
                    p = smoothed[row_base + bx]
                    sr += p[0]
                    sg += p[1]
                    sb += p[2]
                    count += 1
            blurred[idx][0] = sr // count
            blurred[idx][1] = sg // count
            blurred[idx][2] = sb // count
        smoothed = blurred
        logger.info(f"Box blur pass {pass_num} applied")

    return smoothed


def binary_to_pixels(binary_data, decompressed=False):
    """Convert binary data back to (width, height, pixels, mode, clustering_applied).

    Binary format: width(4) + height(4) + mode(1) + flags(1) + [palette] + pixel_data

    Flags:
    - 0x01: delta encoded
    - 0x02: palette mode
    - 0x04: K-means clustering applied
    """
    logger.info(f"Decoding binary data (decompressed={decompressed})")
    if not decompressed:
        logger.info(f"LZMA decompressing {len(binary_data)} bytes...")
        binary_data = lzma.decompress(binary_data)
        logger.info(f"Decompressed to {len(binary_data)} bytes")

    width = struct.unpack("<I", binary_data[0:4])[0]
    height = struct.unpack("<I", binary_data[4:8])[0]
    mode_byte = struct.unpack("B", binary_data[8:9])[0]
    flags = struct.unpack("B", binary_data[9:10])[0]

    has_delta = bool(flags & 0x01)
    has_palette = bool(flags & 0x02)
    has_clustering = bool(flags & 0x04)

    logger.info(f"Image: {width}x{height}, flags: delta={has_delta}, palette={has_palette}, clustering={has_clustering}")

    offset = 10
    palette = None

    # Read palette if present
    if has_palette:
        palette_size = struct.unpack("<H", binary_data[offset:offset+2])[0]
        offset += 2
        palette = []
        for _ in range(palette_size):
            r, g, b = binary_data[offset:offset+3]
            palette.append([r, g, b])
            offset += 3
        logger.info(f"Palette loaded: {palette_size} colors")

    # Determine mode and bytes per pixel
    if has_palette:
        mode = "RGB"
        bytes_per_pixel = 1
    elif mode_byte == 1:
        mode = "RGBA"
        bytes_per_pixel = 4
    else:
        mode = "RGB"
        bytes_per_pixel = 3

    # Read pixel data
    pixel_bytes = binary_data[offset:]
    expected_size = width * height * bytes_per_pixel
    if len(pixel_bytes) != expected_size:
        raise ValueError(f"Pixel data size mismatch: got {len(pixel_bytes)}, expected {expected_size}")

    # Decode pixels
    pixels = []
    for i in range(0, len(pixel_bytes), bytes_per_pixel):
        pixel = list(pixel_bytes[i:i+bytes_per_pixel])
        pixels.append(pixel)

    # Reverse delta encoding if applied
    if has_delta:
        logger.info("Reversing delta encoding...")
        delta_pixels = pixels
        pixels = []
        for i, delta in enumerate(delta_pixels):
            if i == 0:
                pixels.append(delta[:])
            else:
                prev_pixel = pixels[i - 1]
                actual = []
                for j in range(bytes_per_pixel):
                    val = (prev_pixel[j] + delta[j]) & 0xFF
                    actual.append(val)
                pixels.append(actual)

    # Map palette indices back to RGB
    if has_palette and palette:
        logger.info("Mapping palette indices to RGB...")
        rgb_pixels = []
        for idx_list in pixels:
            idx = idx_list[0]
            rgb_pixels.append(palette[idx])
        pixels = rgb_pixels

    logger.info(f"Binary decode complete: {len(pixels)} pixels, mode={mode}")
    return width, height, pixels, mode, has_clustering


def decompress_image(input_path, out_path, smooth_gradients=False, smooth_strength=1.0, blur_passes=None):
    # Support:
    # 1. .bimg smart bundles (format_byte + data)
    # 2. Raw PNG files (starting with 0x89)
    # 3. Plain tar bundles (legacy format)
    # 4. Plain JSON data files (legacy format)

    logger.info(f"Decompressing: {input_path} -> {out_path}")
    logger.info(f"Gradient smoothing enabled: {smooth_gradients}")

    data_dict = None

    # Try reading as smart bundle or raw PNG first
    try:
        with open(input_path, 'rb') as f:
            first_byte = f.read(1)
            if not first_byte:
                raise ValueError("Empty file")
                
            format_byte = first_byte[0]
            
            # Format 0x89 is the start of a raw PNG signature
            if format_byte == 0x89:
                logger.info("Raw PNG detected - copying directly")
                with open(out_path, 'wb') as out:
                    out.write(first_byte + f.read())
                return

            # Smart bundle: Format 0 = PNG, 1 = Binary optimized
            data = f.read()
            if format_byte == 0:
                # Direct PNG (wrapped in smart bundle)
                logger.info("Format: Direct PNG (wrapped) - copying directly")
                with open(out_path, 'wb') as out:
                    # data already has the format byte stripped because we read it as first_byte
                    out.write(data)
                return
            elif format_byte == 1:
                # Binary format (LZMA compressed)
                logger.info("Format: Optimized binary with LZMA compression")
                width, height, pixels, mode, has_clustering = binary_to_pixels(data, decompressed=False)
                # Apply optional gradient smoothing if clustering was used and smoothing enabled
                if has_clustering and smooth_gradients:
                    logger.info("Applying gradient smoothing to clustering artifacts...")
                    pixels = smooth_clustering_artifacts(pixels, width, height, strength=smooth_strength, blur_passes=blur_passes)
                    logger.info("Gradient smoothing applied")
                data_dict = {"width": width, "height": height, "pixels": pixels, "mode": mode}
            else:
                # Not a smart bundle, maybe legacy
                raise ValueError(f"Unknown format byte {format_byte}")

    except (ValueError, struct.error, lzma.LZMAError) as e:
        logger.info(f"Not a smart bundle or raw PNG ({e}), trying legacy formats...")

        # Try tarfile first (legacy .bimg?)
        if tarfile.is_tarfile(input_path):
            logger.info("Detected tarfile format (legacy)")
            with tarfile.open(input_path, "r:*") as tar:
                members = tar.getmembers()
                if not members:
                    raise ValueError("Bundle contains no files")

                # Try to find image.png first (smart bundle legacy), then pixels.bin, then pixels.dat
                member = None
                is_binary = False
                is_png = False

                for m in members:
                    if m.name == "image.png":
                        member = m
                        is_png = True
                        break

                if not member:
                    for m in members:
                        if m.name == "pixels.bin":
                            member = m
                            is_binary = True
                            break

                if not member:
                    for m in members:
                        if m.name == "pixels.dat":
                            member = m
                            break

                if not member:
                    member = members[0]
                    is_binary = member.name.endswith(".bin")
                    is_png = member.name.endswith(".png")

                fobj = tar.extractfile(member)
                if fobj is None:
                    raise ValueError("Failed to extract data from bundle")
                raw = fobj.read()

                if is_png:
                    with open(out_path, 'wb') as f:
                        f.write(raw)
                    return

                if is_binary:
                    width, height, pixels, mode, has_clustering = binary_to_pixels(raw, decompressed=False)
                    # Apply optional gradient smoothing if clustering was used and smoothing enabled
                    if has_clustering and smooth_gradients:
                        pixels = smooth_clustering_artifacts(pixels, width, height, strength=smooth_strength, blur_passes=blur_passes)
                    data_dict = {"width": width, "height": height, "pixels": pixels, "mode": mode}
                else:
                    try:
                        data_dict = json.loads(raw.decode("utf-8"))
                    except UnicodeDecodeError:
                        raise ValueError("File is not a valid smart bundle, PNG, Tar, or JSON")
        else:
            # Try plain JSON data file
            try:
                with open(input_path, "r") as f:
                    data_dict = json.load(f)
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise ValueError("File format not recognized as .bimg (tried Smart Bundle, PNG, Tar, JSON)")

    if data_dict is None:
        raise ValueError("Failed to load image data")

    width = data_dict.get("width")
    height = data_dict.get("height")
    pixels = data_dict.get("pixels")
    mode_field = data_dict.get("mode")
    if width is None or height is None or pixels is None:
        raise ValueError("Data must contain width, height and pixels fields")

    # flatten if rows provided
    if len(pixels) == height and isinstance(pixels[0], list):
        # pixels is nested rows
        flat = [tuple(p) for row in pixels for p in row]
    else:
        # assume already flat list
        flat = [tuple(p) for p in pixels]

    if len(flat) != width * height:
        raise ValueError("Pixel count does not match width*height")

    # determine mode: prefer explicit mode field, otherwise infer from pixel length
    if mode_field:
        mode = mode_field
    else:
        mode = "RGBA" if len(flat[0]) == 4 else "RGB"

    img = Image.new(mode, (width, height))
    img.putdata(flat)
    img.save(out_path)


def main():
    parser = argparse.ArgumentParser(description="Decompress .bimg -> image")
    parser.add_argument("input", help="Path to compressed .bimg file")
    parser.add_argument("output", help="Output image path")
    args = parser.parse_args()

    decompress_image(args.input, args.output)


if __name__ == "__main__":
    main()
