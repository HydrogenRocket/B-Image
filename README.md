# B-IMG

A utility for compressing images into `.bimg` bundles and decompressing them back into standard image formats.

## Features

- **Lossless or lossy** — K-means color clustering lets you trade image quality for smaller file sizes
- **Advanced compression** — multiple encoding strategies selected automatically per image:
  - Palette mode with nibble-packed 4-bit indices for images with ≤ 16 unique colors
  - Standard 8-bit palette indices with delta encoding for up to 256 colors
  - 16-bit palette indices for up to 65,535 unique colors
  - Planar channel storage + PNG-style prediction filters (None/Sub/Up/Average/Paeth) for photographic images
  - LZMA compression as the final pass on all formats
- **Post-processing** — optional decompression-side smoothing pipeline to recover visual quality from lossy compression:
  - BFS-based gradient fill blends each color region smoothly from its edges inward
  - Adjustable smooth strength (0–200%), blur radius (1–20 px), and region threshold (controls minimum region size — lower catches smaller K-means patches)
- **GUI, Viewer, and CLI** — a CustomTkinter desktop app, a standalone viewer, and command-line scripts
- **Alpha channel support** — optionally preserve transparency (RGBA)
- **Native file pickers** — uses `zenity` or `kdialog` on Linux, native dialogs on macOS, with file type filtering

## Installation

Requires Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## GUI

```bash
python Format-Tool/main.py
```

The window has two panels:

**Image → .bimg** (compress)
- Browse for any image file (PNG, JPEG, GIF, BMP, TIFF)
- Optionally set an output filename (`.bimg` is appended automatically)
- Toggle alpha channel preservation
- Adjust the **Compression** slider: `0` is lossless, `255` is extreme (K-means color reduction)
- Click **Create .bimg**

**.bimg → Image** (decompress)
- Browse for a `.bimg` file
- Choose an output filename and format (PNG, JPEG, etc.)
- Optionally enable **Smooth clustering artifacts** and configure the post-processing pipeline:
  - **Smooth strength** — how aggressively the gradient is blended (0–200%)
  - **Blur smoothed areas** — applies a box blur on top of the gradient fill
  - **Blur radius** — kernel size in pixels (1–20 px)
  - **Region threshold** — minimum region size as a percentage of total pixels; lower values catch smaller K-means patches (0–0.5%, default 0.10%)
- Click **Restore Image**

A theme toggle in the bottom-right switches between light and dark modes.

## Viewer

```bash
python Viewer/viewer.py
```

Opens `.bimg` files directly for preview. The sidebar exposes the full post-processing pipeline — smooth strength, blur radius, and region threshold — with an **Apply Changes** button to re-render with the current settings.

## Post-processing example

Lossy compression reduces the number of distinct colors via K-means clustering, which introduces flat color bands (posterization) in areas that originally had smooth gradients — most visible in soft bokeh backgrounds and sky gradients.

The post-processing pipeline blends each color region smoothly from its edges inward (BFS gradient fill), then optionally applies a box blur on top. The **region threshold** controls sensitivity: lower values process smaller color patches, which is important at high compression levels where many small K-means regions are created.

| Original | Compressed (40) + restored (smooth strength 75%, blur radius 20 px, region threshold 0.01%) |
|---|---|
| ![Original](origional.jpg) | ![Compressed and restored with post-processing](converted-restored.png) |

The restored image retains the overall composition and color palette while the gradient smoothing softens the K-means banding in the blue background. Fine high-frequency detail (sparkles, texture) is intentionally left unblurred — only large flat color regions are processed.

## CLI

### Compress

```bash
python Format-Tool/compress.py [options] path/to/image.png
```

| Option | Description |
|---|---|
| `-o, --output <file>` | Output path (use `.bimg` extension to create a bundle) |
| `--bundle` | Explicitly create a `.bimg` bundle |
| `--alpha` | Preserve alpha channel (RGBA) |
| `--flatten` | Output a flat pixel list instead of nested rows (JSON mode only) |

```bash
# Compress to .bimg
python Format-Tool/compress.py -o photo.bimg photo.png

# With alpha channel
python Format-Tool/compress.py --alpha -o photo.bimg photo.png
```

### Decompress

```bash
python Format-Tool/decompress.py input.bimg output.png
```

## File format

`.bimg` files start with a single format byte followed by LZMA-compressed binary data.

The binary header contains:

| Field | Size | Description |
|---|---|---|
| width | 4 bytes | Image width in pixels |
| height | 4 bytes | Image height in pixels |
| mode | 1 byte | `0`=RGB, `1`=RGBA, `2`=indexed |
| flags | 1 byte | Bitmask of active encodings (see below) |
| palette | variable | Present when flag `0x02` is set |
| pixel data | variable | Encoding depends on flags |

**Flags:**

| Bit | Meaning |
|---|---|
| `0x01` | Delta-encoded 8-bit palette indices |
| `0x02` | Palette mode |
| `0x04` | K-means clustering was applied |
| `0x08` | Planar channel storage + PNG prediction filters (photographic images) |
| `0x10` | Nibble-packed 4-bit palette indices (≤ 16 colors) |
| `0x20` | 16-bit palette indices (17–65,535 colors) |

**This is still very experimental**