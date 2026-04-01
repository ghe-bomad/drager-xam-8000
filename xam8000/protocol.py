"""Dräger X-am 8000 binary protocol: CRC, framing, and protobuf helpers.

Wire format: 55 C1 [LEN 2B] [19 00 01 00] [SEQ 4B] [CMD 2B] [PAYLOAD] [CRC16 2B]
"""

import struct
import time

import serial

# Frame constants
SYNC = 0x55
START = 0xC1
ADDR = b"\x19\x00\x01\x00"
CONNECT_PAYLOAD = b"\xFF\xFF\x00\xC2\x01\x00"
RESP_OK = 0x8000
RESP_ERR = 0xFFFF

# CRC-16/KERMIT lookup table
_CRC16_T = []
for _i in range(256):
    _c = _i
    for _ in range(8):
        _c = (_c >> 1) ^ 0x8408 if _c & 1 else _c >> 1
    _CRC16_T.append(_c)

def crc16(data: bytes) -> int:
    crc = 0
    for b in data:
        crc = _CRC16_T[(crc ^ b) & 0xFF] ^ (crc >> 8)
    return crc

# CRC-32/MPEG-2 tables for seed-to-key derivation
_CRC32_T = []
for _i in range(256):
    _c = _i << 24
    for _ in range(8):
        _c = ((_c << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if _c & 0x80000000 else (_c << 1) & 0xFFFFFFFF
    _CRC32_T.append(_c)

_REFLECT = [sum(((_i >> b) & 1) << (7 - b) for b in range(8)) for _i in range(256)]

def compute_key(seed: int, password: str) -> int:
    """CRC-32/MPEG-2 seed-to-key with bit-reflected input bytes."""
    acc = seed & 0xFFFFFFFF
    for b in password.encode("ascii"):
        acc = ((acc << 8) ^ _CRC32_T[(_REFLECT[b] ^ (acc >> 24)) & 0xFF]) & 0xFFFFFFFF
    return acc


# --- Frame building ---

def build_frame(seq: int, cmd: bytes, payload: bytes = b"") -> bytes:
    inner = bytes([START]) + struct.pack("<H", len(cmd) + len(payload)) + ADDR + struct.pack("<I", seq) + cmd + payload
    return bytes([SYNC]) + inner + struct.pack("<H", crc16(inner))

def frame_connect(seq):     return build_frame(seq, b"\x0A\x00", CONNECT_PAYLOAD)
def frame_keepalive(seq):   return build_frame(seq, b"\x04\x00")
def frame_seed(seq, mode):  return build_frame(seq, b"\x0D\x00", bytes([mode]))
def frame_key(seq, key):    return build_frame(seq, b"\x0E\x00", struct.pack("<I", key))
def frame_info(seq):        return build_frame(seq, b"\x02\x00")
def frame_status(seq):      return build_frame(seq, b"\x06\x00")
def frame_partno(seq):      return build_frame(seq, b"\x08\x00")
def frame_disconnect(seq):  return build_frame(seq, b"\x0B\x00")


# --- Response parsing ---

def read_response(ser: serial.Serial, timeout: float = 5.0) -> tuple[int, int, bytes]:
    """Read and parse a response frame. Returns (seq, cmd, payload)."""
    deadline = time.monotonic() + timeout
    old_to = ser.timeout
    ser.timeout = min(timeout, 1.0)
    try:
        state = 0
        while time.monotonic() < deadline:
            b = ser.read(1)
            if not b:
                continue
            if state == 0:
                if b[0] == SYNC:
                    state = 1
                elif b[0] == START:
                    break
            elif state == 1:
                if b[0] == START:
                    break
                elif b[0] != SYNC:
                    state = 0
        else:
            raise TimeoutError("No response frame")

        hdr = ser.read(2)
        if len(hdr) < 2:
            raise TimeoutError("Incomplete length")
        length = hdr[0] | (hdr[1] << 8)

        rest = ser.read(4 + 4 + length + 2)
        if len(rest) < 4 + 4 + length + 2:
            raise TimeoutError(f"Incomplete frame: {len(rest)}/{4 + 4 + length + 2}")

        expected = crc16(bytes([START]) + hdr + rest[:-2])
        actual = rest[-2] | (rest[-1] << 8)
        if expected != actual:
            raise ValueError(f"CRC mismatch: 0x{expected:04X} vs 0x{actual:04X}")

        return struct.unpack_from("<I", rest, 4)[0], rest[8] | (rest[9] << 8), rest[10:-2]
    finally:
        ser.timeout = old_to


# --- Protobuf encoding/decoding (no external dependency) ---

def pb_varint(val: int) -> bytes:
    out = []
    while val > 0x7F:
        out.append((val & 0x7F) | 0x80)
        val >>= 7
    out.append(val & 0x7F)
    return bytes(out)

def pb_field(num: int, data: bytes) -> bytes:
    return pb_varint((num << 3) | 2) + pb_varint(len(data)) + data

def pb_string(num: int, s: str) -> bytes:
    return pb_field(num, s.encode("utf-8"))

def pb_empty(num: int) -> bytes:
    return pb_varint((num << 3) | 2) + b"\x00"

def pb_uint(num: int, val: int) -> bytes:
    return pb_varint((num << 3) | 0) + pb_varint(val)

def pb_decode(data: bytes) -> list[tuple[int, int, bytes | int]]:
    """Decode protobuf fields. Returns [(field_num, wire_type, value), ...]."""
    fields, pos = [], 0
    while pos < len(data):
        tag, pos = _dec_vi(data, pos)
        wt = tag & 7
        if wt == 0:
            val, pos = _dec_vi(data, pos)
            fields.append((tag >> 3, 0, val))
        elif wt == 2:
            n, pos = _dec_vi(data, pos)
            fields.append((tag >> 3, 2, data[pos:pos + n]))
            pos += n
        elif wt == 5:
            fields.append((tag >> 3, 5, data[pos:pos + 4]))
            pos += 4
        elif wt == 1:
            fields.append((tag >> 3, 1, data[pos:pos + 8]))
            pos += 8
        else:
            break
    return fields

def _dec_vi(data, pos):
    r, s = 0, 0
    while pos < len(data):
        b = data[pos]; pos += 1
        r |= (b & 0x7F) << s
        if not (b & 0x80): break
        s += 7
    return r, pos
