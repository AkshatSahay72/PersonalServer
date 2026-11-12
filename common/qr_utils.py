#!/usr/bin/env python3
"""
Pure-Python QR Code SVG Generator (ISO/IEC 18004, Byte Mode, Versions 1-8, EC-L)
================================================================================
Generates scannable QR code SVGs without external dependencies so Termux, Windows,
and air-gapped environments can render private Tailscale enrollment links offline.
"""

from typing import List, Tuple

# Version capacity in bytes (EC Level L) and EC parameters:
# (version, total_data_codewords, ec_per_block, blocks_g1, data_per_g1, blocks_g2, data_per_g2, align_coords)
_QR_SPECS: List[Tuple[int, int, int, int, int, int, int, List[int]]] = [
    (1, 19, 7, 1, 19, 0, 0, []),
    (2, 34, 10, 1, 34, 0, 0, [6, 18]),
    (3, 55, 15, 1, 55, 0, 0, [6, 22]),
    (4, 80, 20, 1, 80, 0, 0, [6, 26]),
    (5, 108, 26, 1, 108, 0, 0, [6, 30]),
    (6, 136, 18, 2, 68, 0, 0, [6, 34]),
    (7, 156, 20, 2, 78, 0, 0, [6, 22, 38]),
    (8, 194, 24, 2, 97, 0, 0, [6, 24, 42]),
]

# Precompute GF(256) tables with primitive polynomial 0x11D
_GF_EXP = [0] * 512
_GF_LOG = [0] * 256
_val = 1
for _i in range(255):
    _GF_EXP[_i] = _val
    _GF_LOG[_val] = _i
    _val <<= 1
    if _val & 0x100:
        _val ^= 0x11D
for _i in range(255, 512):
    _GF_EXP[_i] = _GF_EXP[_i - 255]


def _gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]


def _rs_generator_poly(deg: int) -> List[int]:
    poly = [1]
    for i in range(deg):
        nxt = [0] * (len(poly) + 1)
        factor = _GF_EXP[i]
        for j, coef in enumerate(poly):
            nxt[j] ^= coef
            nxt[j + 1] ^= _gf_mul(coef, factor)
        poly = nxt
    return poly


def _rs_encode(data: List[int], ec_len: int) -> List[int]:
    gen = _rs_generator_poly(ec_len)
    buf = list(data) + [0] * ec_len
    for i in range(len(data)):
        coef = buf[i]
        if coef != 0:
            for j in range(1, len(gen)):
                buf[i + j] ^= _gf_mul(gen[j], coef)
    return buf[len(data):]


def _choose_spec(payload_len: int) -> Tuple[int, int, int, int, int, int, int, List[int]]:
    # Byte mode overhead: 4 bits mode + 8 bits length + 4 bits terminator = 2 bytes (for V1-V8)
    for spec in _QR_SPECS:
        if payload_len + 2 <= spec[1]:
            return spec
    raise ValueError(f"QR payload too long ({payload_len} bytes; max 192)")


def _build_codewords(payload: bytes) -> Tuple[Tuple[int, int, int, int, int, int, int, List[int]], List[int]]:
    spec = _choose_spec(len(payload))
    _, total_data, ec_per_block, b1, d1, b2, d2, _ = spec

    bits: List[int] = []

    def push_bits(val: int, count: int) -> None:
        for shift in range(count - 1, -1, -1):
            bits.append((val >> shift) & 1)

    push_bits(0b0100, 4)  # Byte mode indicator
    push_bits(len(payload), 8)
    for byte in payload:
        push_bits(byte, 8)

    max_bits = total_data * 8
    term_len = min(4, max_bits - len(bits))
    push_bits(0, term_len)
    while len(bits) % 8 != 0:
        bits.append(0)

    data_bytes = []
    for i in range(0, len(bits), 8):
        b = 0
        for bit in bits[i:i + 8]:
            b = (b << 1) | bit
        data_bytes.append(b)

    pad_idx = 0
    while len(data_bytes) < total_data:
        data_bytes.append(0xEC if pad_idx % 2 == 0 else 0x11)
        pad_idx += 1

    # Split blocks & generate RS error correction codewords
    data_blocks: List[List[int]] = []
    ec_blocks: List[List[int]] = []
    offset = 0
    for count, size in ((b1, d1), (b2, d2)):
        for _ in range(count):
            blk = data_bytes[offset:offset + size]
            offset += size
            data_blocks.append(blk)
            ec_blocks.append(_rs_encode(blk, ec_per_block))

    interleaved: List[int] = []
    max_data_blk = max(len(b) for b in data_blocks)
    for i in range(max_data_blk):
        for blk in data_blocks:
            if i < len(blk):
                interleaved.append(blk[i])
    for i in range(ec_per_block):
        for blk in ec_blocks:
            interleaved.append(blk[i])

    return spec, interleaved


def _place_finder(grid: List[List[int]], reserved: List[List[bool]], top: int, left: int, size: int) -> None:
    for r in range(-1, 8):
        for c in range(-1, 8):
            rr, cc = top + r, left + c
            if 0 <= rr < size and 0 <= cc < size:
                reserved[rr][cc] = True
                if 0 <= r <= 6 and 0 <= c <= 6:
                    is_border = r in (0, 6) or c in (0, 6)
                    is_center = 2 <= r <= 4 and 2 <= c <= 4
                    grid[rr][cc] = 1 if (is_border or is_center) else 0
                else:
                    grid[rr][cc] = 0


def _place_alignment(grid: List[List[int]], reserved: List[List[bool]], cr: int, cc: int) -> None:
    for r in range(-2, 3):
        for c in range(-2, 3):
            rr, cc2 = cr + r, cc + c
            if reserved[rr][cc2]:
                return
    for r in range(-2, 3):
        for c in range(-2, 3):
            rr, cc2 = cr + r, cc + c
            reserved[rr][cc2] = True
            grid[rr][cc2] = 1 if (abs(r) == 2 or abs(c) == 2 or (r == 0 and c == 0)) else 0


def _format_bits(mask: int) -> int:
    # EC Level L = 01
    fmt_data = (0b01 << 3) | mask
    rem = fmt_data << 10
    gen = 0b10100110111
    for bit in range(14, 9, -1):
        if rem & (1 << bit):
            rem ^= gen << (bit - 10)
    return ((fmt_data << 10) | rem) ^ 0b101010000010010


def _version_bits(version: int) -> int:
    rem = version << 12
    gen = 0b1111100100101
    for bit in range(17, 11, -1):
        if rem & (1 << bit):
            rem ^= gen << (bit - 12)
    return (version << 12) | rem


def _apply_format_and_version(grid: List[List[int]], version: int, mask: int, size: int) -> None:
    fmt = _format_bits(mask)
    for i in range(15):
        bit = (fmt >> i) & 1
        # Copy 1: around top-left finder
        if i < 6:
            grid[8][i] = bit
        elif i < 8:
            grid[8][i + 1] = bit
        elif i == 8:
            grid[7][8] = bit
        else:
            grid[14 - i][8] = bit

        # Copy 2: bottom-left + top-right
        if i < 8:
            grid[size - 1 - i][8] = bit
        else:
            grid[8][size - 15 + i] = bit

    if version >= 7:
        ver = _version_bits(version)
        for i in range(18):
            bit = (ver >> i) & 1
            r, c = i // 3, size - 11 + (i % 3)
            grid[r][c] = bit
            grid[c][r] = bit


def _penalty_score(grid: List[List[int]], size: int) -> int:
    score = 0
    # Horizontal & vertical runs
    for r in range(size):
        run_c = 1
        run_v = grid[r][0]
        for c in range(1, size):
            if grid[r][c] == run_v:
                run_c += 1
            else:
                if run_c >= 5:
                    score += 3 + (run_c - 5)
                run_v = grid[r][c]
                run_c = 1
        if run_c >= 5:
            score += 3 + (run_c - 5)

    for c in range(size):
        run_c = 1
        run_v = grid[0][c]
        for r in range(1, size):
            if grid[r][c] == run_v:
                run_c += 1
            else:
                if run_c >= 5:
                    score += 3 + (run_c - 5)
                run_v = grid[r][c]
                run_c = 1
        if run_c >= 5:
            score += 3 + (run_c - 5)

    # 2x2 blocks
    for r in range(size - 1):
        for c in range(size - 1):
            v = grid[r][c]
            if v == grid[r][c + 1] == grid[r + 1][c] == grid[r + 1][c + 1]:
                score += 3
    return score


def generate_qr_matrix(text: str) -> List[List[int]]:
    """Encodes text into a QR boolean matrix (1=dark, 0=light)."""
    payload = text.encode("utf-8")
    spec, codewords = _build_codewords(payload)
    version = spec[0]
    align_coords = spec[7]
    size = 17 + 4 * version

    base = [[0] * size for _ in range(size)]
    reserved = [[False] * size for _ in range(size)]

    _place_finder(base, reserved, 0, 0, size)
    _place_finder(base, reserved, 0, size - 7, size)
    _place_finder(base, reserved, size - 7, 0, size)

    # Timing patterns
    for i in range(8, size - 8):
        bit = 1 if i % 2 == 0 else 0
        reserved[6][i] = True
        base[6][i] = bit
        reserved[i][6] = True
        base[i][6] = bit

    # Dark module
    reserved[size - 8][8] = True
    base[size - 8][8] = 1

    # Alignment patterns
    for cr in align_coords:
        for cc in align_coords:
            _place_alignment(base, reserved, cr, cc)

    # Reserve format info cells
    for i in range(9):
        reserved[8][i] = True
        reserved[i][8] = True
    for i in range(8):
        reserved[8][size - 8 + i] = True
        reserved[size - 8 + i][8] = True

    if version >= 7:
        for i in range(6):
            for j in range(3):
                reserved[i][size - 11 + j] = True
                reserved[size - 11 + j][i] = True

    # Place data bits
    data_bits = []
    for cw in codewords:
        for shift in range(7, -1, -1):
            data_bits.append((cw >> shift) & 1)

    bit_idx = 0
    up = True
    col = size - 1
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(size - 1, -1, -1) if up else range(size)
        for r in rows:
            for dc in (0, -1):
                c = col + dc
                if not reserved[r][c]:
                    base[r][c] = data_bits[bit_idx] if bit_idx < len(data_bits) else 0
                    bit_idx += 1
        col -= 2
        up = not up

    mask_fns = [
        lambda r, c: (r + c) % 2 == 0,
        lambda r, c: r % 2 == 0,
        lambda r, c: c % 3 == 0,
        lambda r, c: (r + c) % 3 == 0,
        lambda r, c: ((r // 2) + (c // 3)) % 2 == 0,
        lambda r, c: ((r * c) % 2) + ((r * c) % 3) == 0,
        lambda r, c: (((r * c) % 2) + ((r * c) % 3)) % 2 == 0,
        lambda r, c: (((r + c) % 2) + ((r * c) % 3)) % 2 == 0,
    ]

    best_grid = None
    best_score = 10**9
    for mask_id, fn in enumerate(mask_fns):
        cand = [row[:] for row in base]
        for r in range(size):
            for c in range(size):
                if not reserved[r][c] and fn(r, c):
                    cand[r][c] ^= 1
        _apply_format_and_version(cand, version, mask_id, size)
        sc = _penalty_score(cand, size)
        if sc < best_score:
            best_score = sc
            best_grid = cand

    return best_grid


def generate_qr_svg(text: str, box_size: int = 4, quiet_zone: int = 2) -> str:
    """Renders text into a compact, scannable inline SVG QR code."""
    matrix = generate_qr_matrix(text)
    size = len(matrix)
    dim = (size + quiet_zone * 2) * box_size
    paths: List[str] = []
    for r, row in enumerate(matrix):
        for c, cell in enumerate(row):
            if cell:
                x = (c + quiet_zone) * box_size
                y = (r + quiet_zone) * box_size
                paths.append(f"M{x},{y}h{box_size}v{box_size}h-{box_size}z")
    d = "".join(paths)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {dim} {dim}" '
        f'width="168" height="168" shape-rendering="crispEdges" role="img" '
        f'aria-label="Enrollment QR Code">'
        f'<rect width="{dim}" height="{dim}" fill="#ffffff"/>'
        f'<path d="{d}" fill="#0d1117"/></svg>'
    )


__all__ = ["generate_qr_matrix", "generate_qr_svg"]

