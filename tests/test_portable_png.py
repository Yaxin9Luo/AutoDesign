from __future__ import annotations

import struct
import unittest
import zlib
from pathlib import Path

from agent_skills._shared import portable_png


REPO_ROOT = Path(__file__).resolve().parents[1]
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
EXPECTED_CROP = bytes(
    [
        10, 20, 30, 255, 40, 50, 60, 255, 70, 80, 90, 255,
        110, 120, 130, 255, 140, 150, 160, 255, 170, 180, 190, 255,
    ]
)


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _png(
    raw: bytes,
    *,
    width: int = 4,
    height: int = 3,
    bit_depth: int = 8,
    color_type: int = 6,
    interlace: int = 0,
    extra_chunks: tuple[tuple[bytes, bytes], ...] = (),
) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, bit_depth, color_type, 0, 0, interlace)
    return PNG_SIGNATURE + _chunk(b"IHDR", header) + b"".join(
        _chunk(kind, payload) for kind, payload in extra_chunks
    ) + _chunk(b"IDAT", zlib.compress(raw)) + _chunk(b"IEND", b"")


def _paeth(left: int, above: int, upper_left: int) -> int:
    estimate = left + above - upper_left
    distances = (abs(estimate - left), abs(estimate - above), abs(estimate - upper_left))
    return (left, above, upper_left)[distances.index(min(distances))]


def _filtered_rows(rows: tuple[bytes, ...], filter_type: int, bytes_per_pixel: int) -> bytes:
    encoded = bytearray()
    previous = bytes(len(rows[0]))
    for row in rows:
        encoded.append(filter_type)
        for index, value in enumerate(row):
            left = row[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
            above = previous[index]
            upper_left = previous[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
            predictor = (0, left, above, (left + above) // 2, _paeth(left, above, upper_left))[filter_type]
            encoded.append((value - predictor) & 0xFF)
        previous = row
    return bytes(encoded)


def _output_pixels(data: bytes) -> tuple[dict[str, int], bytes]:
    position = len(PNG_SIGNATURE)
    chunks: list[tuple[bytes, bytes]] = []
    while position < len(data):
        length = struct.unpack(">I", data[position : position + 4])[0]
        kind = data[position + 4 : position + 8]
        payload = data[position + 8 : position + 8 + length]
        chunks.append((kind, payload))
        position += length + 12
    header = struct.unpack(">IIBBBBB", chunks[0][1])
    width, height, bit_depth, color_type, _compression, _filtering, interlace = header
    channels = {0: 1, 2: 3, 4: 2, 6: 4}[color_type]
    row_bytes = width * channels
    raw = zlib.decompress(b"".join(payload for kind, payload in chunks if kind == b"IDAT"))
    pixels = bytearray()
    for offset in range(0, len(raw), row_bytes + 1):
        assert raw[offset] == 0
        pixels.extend(raw[offset + 1 : offset + row_bytes + 1])
    return (
        {"width": width, "height": height, "bit_depth": bit_depth, "color_type": color_type, "channels": channels, "row_bytes": row_bytes, "interlace": interlace},
        bytes(pixels),
    )


class PortablePngTests(unittest.TestCase):
    def test_crops_each_png_row_filter_to_literal_pixels(self) -> None:
        rows = (
            bytes([1, 2, 3, 255, 10, 20, 30, 255, 40, 50, 60, 255, 70, 80, 90, 255]),
            bytes([101, 102, 103, 255, 110, 120, 130, 255, 140, 150, 160, 255, 170, 180, 190, 255]),
            bytes([201, 202, 203, 255, 210, 220, 230, 255, 240, 250, 251, 255, 252, 253, 254, 255]),
        )
        for filter_type in range(5):
            with self.subTest(filter_type=filter_type):
                fixture = _png(_filtered_rows(rows, filter_type, 4))
                result = portable_png.crop_png(fixture, (1, 0, 4, 2))
                info, pixels = _output_pixels(result)
                self.assertEqual((info["width"], info["height"]), (3, 2))
                self.assertEqual(pixels, EXPECTED_CROP)

    def test_crop_is_byte_identical_and_discards_untrusted_metadata(self) -> None:
        fixture = _png(
            _filtered_rows((bytes(range(16)), bytes(range(16, 32)), bytes(range(32, 48))), 0, 4),
            extra_chunks=((b"tEXt", b"untrusted\x00metadata"),),
        )
        first = portable_png.crop_png(fixture, (1, 0, 4, 2))
        self.assertEqual(portable_png.crop_png(fixture, (1, 0, 4, 2)), first)
        self.assertNotIn(b"tEXt", first)
        self.assertEqual(first[:8], PNG_SIGNATURE)
        info, _ = _output_pixels(first)
        self.assertEqual((info["width"], info["height"], info["bit_depth"], info["color_type"]), (3, 2, 8, 6))

    def test_inspect_reports_eight_bit_rgba_dimensions_and_row_size(self) -> None:
        info = portable_png.inspect_png(_png(_filtered_rows((bytes(range(16)),) * 3, 0, 4)))
        self.assertEqual(
            info,
            {"width": 4, "height": 3, "bit_depth": 8, "color_type": 6, "channels": 4, "row_bytes": 16},
        )

    def test_crops_each_supported_eight_bit_color_mode(self) -> None:
        for color_type, channels in ((0, 1), (2, 3), (4, 2), (6, 4)):
            with self.subTest(color_type=color_type):
                rows = tuple(bytes(range(offset, offset + 4 * channels)) for offset in (0, 20, 40))
                result = portable_png.crop_png(
                    _png(_filtered_rows(rows, 0, channels), color_type=color_type), (1, 0, 4, 2)
                )
                info, pixels = _output_pixels(result)
                self.assertEqual(info["channels"], channels)
                self.assertEqual(pixels, rows[0][channels:] + rows[1][channels:])

    def test_rejects_invalid_crc(self) -> None:
        fixture = bytearray(_png(_filtered_rows((bytes(range(16)),) * 3, 0, 4)))
        fixture[-5] ^= 1
        with self.assertRaises(portable_png.PNGError):
            portable_png.inspect_png(bytes(fixture))

    def test_rejects_truncated_idat_and_trailing_zlib_data(self) -> None:
        raw = _filtered_rows((bytes(range(16)),) * 3, 0, 4)
        with self.subTest("truncated"):
            compressed = zlib.compress(raw)[:-1]
            fixture = PNG_SIGNATURE + _chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 3, 8, 6, 0, 0, 0)) + _chunk(b"IDAT", compressed) + _chunk(b"IEND", b"")
            with self.assertRaises(portable_png.PNGError):
                portable_png.inspect_png(fixture)
        with self.subTest("trailing"):
            header = struct.pack(">IIBBBBB", 4, 3, 8, 6, 0, 0, 0)
            fixture = PNG_SIGNATURE + _chunk(b"IHDR", header) + _chunk(b"IDAT", zlib.compress(raw) + b"junk") + _chunk(b"IEND", b"")
            with self.assertRaises(portable_png.PNGError):
                portable_png.inspect_png(fixture)

    def test_rejects_unsupported_png_layouts_and_invalid_filter(self) -> None:
        cases = (
            _png(_filtered_rows((bytes(range(16)),) * 3, 0, 4), bit_depth=16),
            _png(_filtered_rows((bytes(range(16)),) * 3, 0, 4), color_type=3),
            _png(_filtered_rows((bytes(range(16)),) * 3, 0, 4), interlace=1),
            _png(bytes([5]) + bytes(range(16)) + _filtered_rows((bytes(range(16)),) * 2, 0, 4)),
        )
        for fixture in cases:
            with self.subTest(fixture=fixture[12:16]):
                with self.assertRaises(portable_png.PNGError):
                    portable_png.inspect_png(fixture)

    def test_rejects_empty_and_out_of_range_crop_boxes(self) -> None:
        fixture = _png(_filtered_rows((bytes(range(16)),) * 3, 0, 4))
        for box in ((0, 0, 0, 1), (1, 1, 1, 1), (-1, 0, 1, 1), (0, 0, 5, 1), (0, 0, 1, 4)):
            with self.subTest(box=box):
                with self.assertRaises(portable_png.PNGError):
                    portable_png.crop_png(fixture, box)

    def test_sync_keeps_only_poster_png_copy(self) -> None:
        canonical = REPO_ROOT / "agent_skills" / "_shared" / "portable_png.py"
        poster = REPO_ROOT / "agent_skills" / "autodesign-poster" / "scripts" / "portable_png.py"
        self.assertEqual(poster.read_bytes(), canonical.read_bytes())
        for name in ("autodesign-ppt", "autodesign-webpage", "autodesign-video"):
            self.assertFalse((REPO_ROOT / "agent_skills" / name / "scripts" / "portable_png.py").exists())


if __name__ == "__main__":
    unittest.main()
