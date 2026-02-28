
# B-IMG

A utility for compressing images into `.bimg` bundles and decompressing them back into standard image formats.

## Features

- **Lossless or lossy** — K-means color clustering lets you trade image quality for smaller file sizes
- **Binary format** — palette indexing, delta encoding, and LZMA compression squeeze files as small as possible
- **GUI and CLI** — a CustomTkinter desktop app and standalone command-line scripts
- **Alpha channel support** — optionally preserve transparency (RGBA)

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
- Browse for any image file (PNG, JPEG, etc.)
- Optionally set an output filename (`.bimg` is appended automatically)
- Toggle alpha channel preservation
- Adjust the **Compression** slider: `0` is lossless, `255` is extreme (K-means color reduction)
- Click **Create .bimg**

**.bimg → Image** (decompress)
- Browse for a `.bimg` file
- Choose an output filename and format (PNG or JPEG)
- Optionally enable **Smooth clustering artifacts** to reduce banding from lossy compression
- Click **Restore Image**

A theme toggle in the bottom-right switches between light and dark modes. On Linux, `zenity` or `kdialog` are used for native file pickers if available.

## CLI

### Compress

```bash
python compress.py [options] path/to/image.png
```

| Option | Description |
|---|---|
| `-o, --output <file>` | Output path (use `.bimg` extension to create a bundle) |
| `--bundle` | Explicitly create a `.bimg` bundle |
| `--alpha` | Preserve alpha channel (RGBA) |
| `--flatten` | Output a flat pixel list instead of nested rows |

```bash
# compress to .bimg
python compress.py -o photo.bimg photo.png

# lossless bundle
python compress.py --bundle -o photo.bimg photo.png

# with alpha
python compress.py --alpha -o photo.bimg photo.png
```

### Decompress

```bash
python decompress.py input.bimg output.png
```

Automatically detects whether the bundle contains a direct PNG or an optimized binary stream, and reconstructs the image accordingly.

## .bimg Format

The `.bimg` file begins with a single format byte:

| Byte | Meaning |
|---|---|
| `0` | Raw PNG data follows (used when PNG is already smaller) |
| `1` | LZMA-compressed binary pixel stream |

The binary pixel stream includes:
- Width and height (4 bytes each, little-endian)
- Mode and flags (1 byte each)
- Optional palette (up to 256 RGB entries)
- Pixel data, optionally delta-encoded

Flags encode which optimizations were applied: delta encoding, palette mode, and K-means clustering.