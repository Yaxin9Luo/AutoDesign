"""Deterministic PNG region cropping for standalone Agent Skills."""

from __future__ import annotations

import binascii
import struct
import zlib


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_CHANNELS_BY_COLOR_TYPE = {0: 1, 2: 3, 4: 2, 6: 4}


class PNGError(ValueError):
    """Raised when PNG bytes are outside the supported portable subset."""


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", binascii.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _paeth(left: int, above: int, upper_left: int) -> int:
    estimate = left + above - upper_left
    left_distance = abs(estimate - left)
    above_distance = abs(estimate - above)
    upper_left_distance = abs(estimate - upper_left)
    if left_distance <= above_distance and left_distance <= upper_left_distance:
        return left
    if above_distance <= upper_left_distance:
        return above
    return upper_left


def _parse_png(data: bytes) -> tuple[dict[str, int], bytes]:
    if not isinstance(data, bytes) or not data.startswith(_PNG_SIGNATURE):
        raise PNGError("invalid PNG signature")

    position = len(_PNG_SIGNATURE)
    header: tuple[int, int, int, int, int, int, int] | None = None
    idat_parts: list[bytes] = []
    idat_closed = False
    palette_seen = False
    while position < len(data):
        if len(data) - position < 12:
            raise PNGError("truncated PNG chunk")
        length = struct.unpack(">I", data[position : position + 4])[0]
        kind = data[position + 4 : position + 8]
        payload_end = position + 8 + length
        chunk_end = payload_end + 4
        if chunk_end > len(data):
            raise PNGError("truncated PNG chunk")
        payload = data[position + 8 : payload_end]
        actual_crc = struct.unpack(">I", data[payload_end:chunk_end])[0]
        expected_crc = binascii.crc32(kind + payload) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            raise PNGError(f"invalid PNG chunk CRC: {kind.decode('latin1')}")

        if header is None:
            if kind != b"IHDR" or length != 13:
                raise PNGError("IHDR must be the first PNG chunk")
            header = struct.unpack(">IIBBBBB", payload)
        elif kind == b"IHDR":
            raise PNGError("duplicate IHDR chunk")
        elif kind == b"IDAT":
            if idat_closed:
                raise PNGError("PNG IDAT chunks must be consecutive")
            idat_parts.append(payload)
        elif kind == b"PLTE":
            if idat_parts or palette_seen:
                raise PNGError("PNG PLTE must precede IDAT and appear once")
            if header[3] not in {2, 6}:
                raise PNGError("PNG PLTE is not allowed for this color type")
            if not length or length % 3 or length > 768:
                raise PNGError("invalid PNG PLTE length")
            palette_seen = True
        elif kind == b"IEND":
            if length != 0 or not idat_parts:
                raise PNGError("invalid PNG IEND chunk")
            if chunk_end != len(data):
                raise PNGError("trailing bytes after PNG IEND")
            break
        else:
            if not kind[0] & 0x20:
                raise PNGError(f"unsupported critical PNG chunk: {kind.decode('latin1')}")
            if idat_parts:
                idat_closed = True
        position = chunk_end
    else:
        raise PNGError("missing PNG IEND chunk")

    assert header is not None
    width, height, bit_depth, color_type, compression, filtering, interlace = header
    if not width or not height:
        raise PNGError("PNG dimensions must be positive")
    if bit_depth != 8 or color_type not in _CHANNELS_BY_COLOR_TYPE:
        raise PNGError("unsupported PNG bit depth or color type")
    if compression != 0 or filtering != 0:
        raise PNGError("unsupported PNG compression or filter method")
    if interlace != 0:
        raise PNGError("interlaced PNGs are not supported")

    channels = _CHANNELS_BY_COLOR_TYPE[color_type]
    row_bytes = width * channels
    expected_length = height * (row_bytes + 1)
    decompressor = zlib.decompressobj()
    try:
        raw = decompressor.decompress(b"".join(idat_parts), expected_length + 1)
    except zlib.error as error:
        raise PNGError("invalid PNG IDAT stream") from error
    if (
        not decompressor.eof
        or decompressor.unused_data
        or decompressor.unconsumed_tail
        or len(raw) != expected_length
    ):
        raise PNGError("invalid PNG IDAT stream length")

    pixels = bytearray(width * height * channels)
    previous = bytes(row_bytes)
    source_offset = 0
    target_offset = 0
    for _row in range(height):
        filter_type = raw[source_offset]
        source_offset += 1
        filtered = raw[source_offset : source_offset + row_bytes]
        source_offset += row_bytes
        row = pixels[target_offset : target_offset + row_bytes]
        for index, value in enumerate(filtered):
            left = row[index - channels] if index >= channels else 0
            above = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            if filter_type == 0:
                predictor = 0
            elif filter_type == 1:
                predictor = left
            elif filter_type == 2:
                predictor = above
            elif filter_type == 3:
                predictor = (left + above) // 2
            elif filter_type == 4:
                predictor = _paeth(left, above, upper_left)
            else:
                raise PNGError("unsupported PNG scanline filter")
            row[index] = (value + predictor) & 0xFF
        pixels[target_offset : target_offset + row_bytes] = row
        previous = bytes(row)
        target_offset += row_bytes

    return (
        {
            "width": width,
            "height": height,
            "bit_depth": bit_depth,
            "color_type": color_type,
            "channels": channels,
            "row_bytes": row_bytes,
        },
        bytes(pixels),
    )


def _stored_zlib(data: bytes) -> bytes:
    result = bytearray(b"\x78\x01")
    for offset in range(0, len(data) or 1, 65535):
        block = data[offset : offset + 65535]
        final = offset + len(block) >= len(data)
        result.append(1 if final else 0)
        result.extend(struct.pack("<HH", len(block), 0xFFFF - len(block)))
        result.extend(block)
    result.extend(struct.pack(">I", zlib.adler32(data) & 0xFFFFFFFF))
    return bytes(result)


def inspect_png(data: bytes) -> dict[str, int]:
    """Validate a supported PNG and return its pixel format."""

    info, _pixels = _parse_png(data)
    return info


def crop_png(data: bytes, box: tuple[int, int, int, int]) -> bytes:
    """Return a deterministic PNG crop for integer ``(left, top, right, bottom)``."""

    info, pixels = _parse_png(data)
    if not isinstance(box, tuple) or len(box) != 4 or any(type(value) is not int for value in box):
        raise PNGError("crop box must contain four integer coordinates")
    left, top, right, bottom = box
    if not (0 <= left < right <= info["width"] and 0 <= top < bottom <= info["height"]):
        raise PNGError("crop box is outside PNG bounds")

    channels = info["channels"]
    row_bytes = info["row_bytes"]
    crop_row_bytes = (right - left) * channels
    cropped = bytearray()
    for row in range(top, bottom):
        cropped.append(0)
        start = row * row_bytes + left * channels
        cropped.extend(pixels[start : start + crop_row_bytes])
    header = struct.pack(">IIBBBBB", right - left, bottom - top, 8, info["color_type"], 0, 0, 0)
    return _PNG_SIGNATURE + _chunk(b"IHDR", header) + _chunk(b"IDAT", _stored_zlib(bytes(cropped))) + _chunk(b"IEND", b"")
