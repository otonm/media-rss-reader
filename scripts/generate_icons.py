"""Generate placeholder PWA icons — pure stdlib, no dependencies."""

import struct
import zlib
from pathlib import Path


def build_png(width: int, height: int, bg_r: int, bg_g: int, bg_b: int) -> bytes:
    """Create a solid-colour PNG."""
    raw = b""
    for _ in range(height):
        raw += b"\x00" + bytes([bg_r, bg_g, bg_b]) * width

    def chunk(chunk_type: bytes, data: bytes) -> bytes:
        c = chunk_type + data
        crc = struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
        return struct.pack(">I", len(data)) + c + crc

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def triangle_png(size: int, bg_r: int, bg_g: int, bg_b: int, fg_r: int, fg_g: int, fg_b: int) -> bytes:
    """Create a square PNG with a centred play-triangle."""
    m = max(size // 5, 1)
    x0, y0 = m * 2, m * 2
    x1, y1 = size - m * 2, size // 2
    x2, y2 = m * 2, size - m * 2

    def in_triangle(px: int, py: int) -> bool:
        def sign(ax: int, ay: int, bx: int, by: int, cx: int, cy: int) -> int:
            return (ax - cx) * (by - cy) - (bx - cx) * (ay - cy)

        d1 = sign(px, py, x0, y0, x1, y1)
        d2 = sign(px, py, x1, y1, x2, y2)
        d3 = sign(px, py, x2, y2, x0, y0)
        neg = d1 < 0 or d2 < 0 or d3 < 0
        pos = d1 > 0 or d2 > 0 or d3 > 0
        return not (neg and pos)

    raw_rows: list[bytes] = []
    for py in range(size):
        row = bytearray(1 + size * 3)
        row[0] = 0
        for px in range(size):
            off = 1 + px * 3
            if in_triangle(px, py):
                row[off : off + 3] = bytes([fg_r, fg_g, fg_b])
            else:
                row[off : off + 3] = bytes([bg_r, bg_g, bg_b])
        raw_rows.append(bytes(row))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(b"".join(raw_rows)))
        + chunk(b"IEND", b"")
    )


def chunk(chunk_type: bytes, data: bytes) -> bytes:
    c = chunk_type + data
    crc = struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
    return struct.pack(">I", len(data)) + c + crc


OUT = Path(__file__).resolve().parent.parent / "src" / "static"
for size in (192, 512):
    png = triangle_png(size, 17, 17, 17, 255, 255, 255)
    (OUT / f"icon-{size}.png").write_bytes(png)

# Also generate a maskable padding variant at 512 (smaller icon area)
maskable_png = triangle_png(512, 17, 17, 17, 255, 255, 255)
(OUT / "icon-512-maskable.png").write_bytes(maskable_png)

print("Icons generated.")
