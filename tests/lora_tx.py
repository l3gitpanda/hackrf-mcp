"""A LoRa transmitter for the tests: the coding chain and a chirp modulator
that samples the continuous-time signal at any instants, so a test can give
a frame a frequency offset, a fractional timing offset and the sampling
clock offset that comes with them, as a real radio would.

Written from the same published description as hackrf_mcp.lora_phy, but in
the transmit direction (encode, interleave, inverse Gray), so the tests
check the receiver against the description rather than against itself.
"""

from __future__ import annotations

import numpy as np

from hackrf_mcp.lora_phy import header_checksum, payload_crc, sync_symbols, whitening


def _gray_inverse(v: int) -> int:
    out = 0
    while v:
        out ^= v
        v >>= 1
    return out


def _codeword(nibble: int, cr: int) -> list[int]:
    """Bits of the codeword, first bit first: data bits w0..w3 (w0 the
    nibble's most significant bit) times the generator matrices G0/G1."""
    w = [(nibble >> (3 - i)) & 1 for i in range(4)]
    if cr == 1:
        return w + [w[0] ^ w[1] ^ w[2] ^ w[3]]
    g1 = [  # columns 4..7 of G1, one row per data bit
        (1, 0, 1, 1),
        (1, 1, 1, 0),
        (1, 1, 0, 1),
        (0, 1, 1, 1),
    ]
    parity = [sum(w[r] * g1[r][c] for r in range(4)) % 2 for c in range(4)]
    return w + parity[:cr]


def _interleave(nibbles: list[int], sf: int, width: int, cr: int, reduced: bool) -> list[int]:
    """One block: `width` codewords -> 4 + cr symbol values."""
    d = [_codeword(v, cr) for v in nibbles]
    n = 1 << sf
    symbols = []
    for i in range(4 + cr):
        # I[i][j] = D[(i - (j + 1)) mod width][i], j = 0 first (most significant).
        row = [d[(i - (j + 1)) % width][i] for j in range(width)]
        if reduced:
            row = row + [sum(row) % 2, 0]
        value = 0
        for bit in row:
            value = (value << 1) | bit
        symbols.append((_gray_inverse(value) + 1) % n)
    return symbols


def encode(payload: bytes, sf: int, cr: int, has_crc: bool = True, ldro: bool = False) -> list[int]:
    """Data symbols of an explicit-header frame."""
    length = len(payload)
    white = bytes(np.frombuffer(payload, np.uint8) ^ whitening(length)) if length else b""
    n0, n1, n2 = length >> 4, length & 0xF, (cr << 1) | int(has_crc)
    chk = header_checksum(n0, n1, n2)
    nibbles = [n0, n1, n2, chk >> 4, chk & 0xF]
    for b in white:
        nibbles += [b & 0xF, b >> 4]
    if has_crc:
        crc = payload_crc(payload)
        for b in (crc & 0xFF, crc >> 8):
            nibbles += [b & 0xF, b >> 4]
    width0 = sf - 2
    first, rest = nibbles[:width0], nibbles[width0:]
    first += [0] * (width0 - len(first))
    symbols = _interleave(first, sf, width0, 4, True)
    width = sf - 2 if ldro else sf
    while rest:
        block, rest = rest[:width], rest[width:]
        block += [0] * (width - len(block))
        symbols += _interleave(block, sf, width, cr, ldro)
    return symbols


def _segments(data: list[int], sf: int, preamble: int, sync_word: int):
    """(kind, value, length in chips) for the whole frame."""
    n = 1 << sf
    segs = [("up", 0, n)] * preamble
    segs += [("up", s, n) for s in sync_symbols(sync_word)]
    segs += [("down", 0, n), ("down", 0, n), ("down", 0, n // 4)]
    segs += [("up", s, n) for s in data]
    return segs


def _phase(u: np.ndarray, kind: str, s: int, n: int) -> np.ndarray:
    """Phase (in cycles) u chips into a symbol: the integral of the
    instantaneous frequency ((u + s) mod n) / n - 1/2, which folds from
    +B/2 to -B/2 at u = n - s. Every whole symbol ends at a whole cycle."""
    if kind == "down":
        return -(u * u / (2 * n) - u / 2)
    folded = u >= n - s
    return (u * u / 2 + s * u) / n - u / 2 - np.where(folded, u - (n - s), 0.0)


def modulate(
    data: list[int],
    sf: int,
    *,
    preamble: int = 16,
    sync_word: int = 0x2B,
    lead: float = 3000.5,
    tail: int = 3000,
    cfo_hz: float = 0.0,
    bw_hz: float = 250_000.0,
    fc_hz: float = 906_875_000.0,
    snr_db: float | None = None,
    amplitude: float = 0.5,
    seed: int = 1,
    os: int = 4,
) -> np.ndarray:
    """The frame sampled at `os` times the bandwidth, starting `lead` chips
    (any fraction) into the output, with a frequency offset and the
    sampling clock offset that matches it (cfo / fc), and complex white
    noise at a per-sample SNR of snr_db."""
    n = 1 << sf
    segs = _segments(data, sf, preamble, sync_word)
    starts = np.cumsum([0] + [length for _, _, length in segs])
    total = (int(np.ceil(lead + starts[-1])) + tail) * os
    k = np.arange(total, dtype=np.float64) / os  # in our chips
    eps = cfo_hz / fc_hz
    # Our sample k falls this many transmitter chips into the frame.
    u = (k - lead) * (1 + eps)
    x = np.zeros(total, dtype=np.complex128)
    for (kind, s, length), t0 in zip(segs, starts[:-1]):
        sel = (u >= t0) & (u < t0 + length)
        if sel.any():
            x[sel] = np.exp(2j * np.pi * _phase(u[sel] - t0, kind, s, n))
    x *= amplitude * np.exp(2j * np.pi * cfo_hz / bw_hz * k)
    if snr_db is not None:
        rng = np.random.default_rng(seed)
        sigma = amplitude / np.sqrt(2 * 10 ** (snr_db / 10))
        x += sigma * (rng.standard_normal(total) + 1j * rng.standard_normal(total))
    return x.astype(np.complex64)
