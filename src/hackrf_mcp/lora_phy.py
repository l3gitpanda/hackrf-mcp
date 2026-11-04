"""The LoRa physical layer, receive side, in numpy: finding frames in a
channel sampled at its bandwidth (one sample per chip) and decoding them to
bytes.

Written from the published descriptions of the LoRa PHY, chiefly J.
Tapparel and A. Burg, "Design and Implementation of LoRa Physical Layer in
GNU Radio" (GRCon 2024), with the synchronisation of Bernier et al. (2020)
and Xhonneux et al. (2022) as that paper summarises them. No decoder's code
was copied. The steps, transmitter side first:

- whitening: payload bytes XORed with an LFSR sequence (x^8+x^6+x^5+x^4+1,
  seeded 0xFF, one byte per step); the header and the CRC are not whitened;
- nibbles: payload bytes low nibble first; the explicit header is five
  nibbles, high nibble first: length (2), coding rate and CRC flag, and a
  5-bit checksum (header_checksum);
- Hamming coding of each nibble to 4 + cr bits (cr = 1..4 parity bits, code
  rate 4/(4+cr)); cr = 1 is a single parity bit, no correction;
- diagonal interleaving of blocks of sf codewords into 4 + cr symbols;
- Gray mapping, plus an offset of one, to symbol values;
- the first block (8 symbols) always uses cr = 4 and the reduced rate
  (sf - 2 bits per symbol), as low data rate optimisation (LDRO) does for
  every block when the symbol time reaches 16 ms;
- a frame is a preamble of upchirps, two sync-word symbols, 2.25
  downchirps, then the data symbols.

The receiver dechirps windows of N = 2^sf samples and takes the strongest
FFT bin. A run of windows with the same bin is a preamble; the bin of the
upchirps (CFO + STO) and of the downchirps (CFO - STO) separate the
frequency offset from the timing offset, the phase from one preamble
symbol to the next gives the fractional frequency offset, and the
sampling clock offset follows from the frequency offset (both come from
the same crystals), so long frames stay in step.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# --- Coding ----------------------------------------------------------------


def whitening(n: int) -> np.ndarray:
    """The first n bytes of the whitening sequence."""
    out = np.empty(n, dtype=np.uint8)
    state = 0xFF
    for i in range(n):
        out[i] = state
        bit = ((state >> 7) ^ (state >> 5) ^ (state >> 4) ^ (state >> 3)) & 1
        state = ((state << 1) | bit) & 0xFF
    return out


def crc16(data: bytes) -> int:
    """CRC-16 with the CCITT polynomial x^16 + x^12 + x^5 + 1, initial value
    0, no reflection."""
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def payload_crc(payload: bytes) -> int:
    """The payload CRC as LoRa sends it: the CRC of all but the last two
    bytes, XORed with those two bytes."""
    if len(payload) < 2:
        return crc16(payload)
    return crc16(payload[:-2]) ^ payload[-1] ^ (payload[-2] << 8)


# Rows of the header checksum's generator matrix, as masks over the twelve
# header bits (first nibble's high bit = bit 11). Checksum bit 4 first.
_HEADER_ROWS = (0xF00, 0x8E1, 0x49A, 0x257, 0x12F)


def header_checksum(n0: int, n1: int, n2: int) -> int:
    h = (n0 << 8) | (n1 << 4) | n2
    out = 0
    for mask in _HEADER_ROWS:
        out = (out << 1) | (bin(h & mask).count("1") & 1)
    return out


def hamming_encode(nibble: int, cr: int) -> int:
    """The 4 + cr bit codeword of a nibble, first bit most significant: the
    nibble's bits from its most significant, then the parity bits."""
    d = [(nibble >> (3 - i)) & 1 for i in range(4)]
    if cr == 1:
        parity = [d[0] ^ d[1] ^ d[2] ^ d[3]]
    else:
        parity = [
            d[0] ^ d[1] ^ d[2],
            d[1] ^ d[2] ^ d[3],
            d[0] ^ d[1] ^ d[3],
            d[0] ^ d[2] ^ d[3],
        ][:cr]
    word = nibble
    for p in parity:
        word = (word << 1) | p
    return word


_CODEBOOK = {cr: [hamming_encode(v, cr) for v in range(16)] for cr in (1, 2, 3, 4)}


def hamming_decode(word: int, cr: int) -> tuple[int, int]:
    """(nibble, errors): errors is 0 for a valid codeword, 1 for one that was
    corrected (cr 3 and 4 correct one bit error), 2 for one that wasn't (its
    data bits are kept as received)."""
    book = _CODEBOOK[cr]
    data = word >> cr
    if book[data] == word:
        return data, 0
    if cr >= 3:
        for v, code in enumerate(book):
            if bin(code ^ word).count("1") == 1:
                return v, 1
    return data, 2


def gray(v: int) -> int:
    return v ^ (v >> 1)


def symbol_rows(symbols: list[int], sf: int, reduced: bool) -> list[int]:
    """Undo the transmitter's Gray mapping (and its offset of one): each
    symbol back to a row of the interleaver block, sf - 2 bits wide at the
    reduced rate, where the two dropped bits were a parity bit and a zero
    chosen so the value is a multiple of four (so rounding absorbs a +-1
    error)."""
    n = 1 << sf
    rows = []
    for s in symbols:
        v = (int(s) - 1) % n
        if reduced:
            v = int(round(v / 4)) % (n >> 2)
        rows.append(gray(v))
    return rows


def deinterleave(rows: list[int], width: int) -> list[int]:
    """A block of len(rows) = 4 + cr interleaved symbols, `width` bits each,
    back to `width` codewords of len(rows) bits. Bit j (from the most
    significant) of row i is bit i of codeword (i - j - 1) mod width."""
    cw_len = len(rows)
    words = [0] * width
    for i, row in enumerate(rows):
        for j in range(width):
            bit = (row >> (width - 1 - j)) & 1
            r = (i - j - 1) % width
            words[r] |= bit << (cw_len - 1 - i)
    return words


def payload_blocks(length: int, sf: int, cr: int, has_crc: bool, ldro: bool) -> int:
    """How many interleaver blocks follow the first one, for an explicit
    header frame."""
    first = (sf - 2 if sf >= 7 else sf) - 5  # payload nibbles in the first block
    need = 2 * length + (4 if has_crc else 0) - first
    per = sf - 2 if ldro else sf
    return max(0, -(-need // per))


def frame_symbols(length: int, sf: int, cr: int, has_crc: bool, ldro: bool) -> int:
    """Data symbols (header block included) of an explicit-header frame."""
    return 8 + payload_blocks(length, sf, cr, has_crc, ldro) * (4 + cr)


def ldro_default(sf: int, bw_hz: float) -> bool:
    """Low data rate optimisation as Semtech radios (and Meshtastic) turn it
    on: when a symbol lasts 16 ms or more."""
    return (1 << sf) / bw_hz >= 0.016


@dataclass
class Header:
    length: int
    cr: int
    has_crc: bool
    checksum_ok: bool
    errors: int  # codewords that failed their parity check


def decode_header(symbols: list[int], sf: int) -> tuple[Header, list[int]]:
    """The first block: (header, payload nibbles it also carries)."""
    width = sf - 2 if sf >= 7 else sf
    words = deinterleave(symbol_rows(symbols[:8], sf, sf >= 7), width)
    nibbles, errors = [], 0
    for w in words:
        v, e = hamming_decode(w, 4)
        nibbles.append(v)
        errors += e == 2
    n0, n1, n2, n3, n4 = nibbles[:5]
    check = ((n3 & 1) << 4) | n4
    header = Header(
        length=(n0 << 4) | n1,
        cr=n2 >> 1,
        has_crc=bool(n2 & 1),
        checksum_ok=check == header_checksum(n0, n1, n2) and 1 <= (n2 >> 1) <= 4,
        errors=errors,
    )
    return header, nibbles[5:]


@dataclass
class Decoded:
    header: Header
    payload: bytes
    crc_ok: bool | None  # None when the frame carries no CRC
    errors: int  # codewords that failed their parity check, header included
    corrected: int


def decode_symbols(symbols: list[int], sf: int, ldro: bool) -> Decoded | None:
    """Decode an explicit-header frame's data symbols. None if the header
    checksum fails or there are too few symbols."""
    if len(symbols) < 8:
        return None
    header, nibbles = decode_header(symbols, sf)
    if not header.checksum_ok or header.length == 0:
        return None
    cw = 4 + header.cr
    width = sf - 2 if ldro else sf
    blocks = payload_blocks(header.length, sf, header.cr, header.has_crc, ldro)
    if len(symbols) < 8 + blocks * cw:
        return None
    errors, corrected = header.errors, 0
    for b in range(blocks):
        block = symbols[8 + b * cw : 8 + (b + 1) * cw]
        for w in deinterleave(symbol_rows(block, sf, ldro), width):
            v, e = hamming_decode(w, header.cr)
            nibbles.append(v)
            errors += e == 2
            corrected += e == 1
    n = header.length
    raw = bytes((nibbles[2 * i] | (nibbles[2 * i + 1] << 4)) for i in range(n))
    payload = bytes(np.frombuffer(raw, np.uint8) ^ whitening(n))
    crc_ok = None
    if header.has_crc:
        lo = nibbles[2 * n] | (nibbles[2 * n + 1] << 4)
        hi = nibbles[2 * n + 2] | (nibbles[2 * n + 3] << 4)
        crc_ok = payload_crc(payload) == (lo | (hi << 8))
    return Decoded(header, payload, crc_ok, errors, corrected)


# --- Demodulation ----------------------------------------------------------


def upchirp(sf: int) -> np.ndarray:
    """The base upchirp (symbol 0) at one sample per chip."""
    n = 1 << sf
    k = np.arange(n, dtype=np.float64)
    return np.exp(2j * np.pi * (k * k / (2 * n) - k / 2)).astype(np.complex64)


@dataclass
class Frame:
    """One LoRa frame found in a channel."""

    sample: int  # where its data symbols start, in channel samples
    sf: int
    bw_hz: float
    cfo_hz: float
    snr_db: float
    sync: tuple[int, int]  # the two sync-word symbols as received
    preamble: int  # preamble windows seen
    header: Header | None
    payload: bytes = b""
    crc_ok: bool | None = None
    errors: int = 0
    corrected: int = 0
    symbols: list[int] = field(default_factory=list, repr=False)

    @property
    def ok(self) -> bool:
        return self.header is not None and self.crc_ok is not False

    @property
    def seconds(self) -> float:
        return self.sample / self.bw_hz


def _rank(f: Frame) -> tuple:
    return (f.crc_ok is True, f.header is not None, -f.errors, f.snr_db)


def sync_symbols(sync_word: int) -> tuple[int, int]:
    """The two symbol values that carry a sync word (0x2B -> 16, 88)."""
    return ((sync_word >> 4) * 8, (sync_word & 0x0F) * 8)


def _circ(a: float, n: int) -> float:
    """a wrapped to [-n/2, n/2)."""
    return (a + n / 2) % n - n / 2


def _peak(power: np.ndarray) -> float:
    """Fractional index of the strongest bin (parabolic fit, circular)."""
    n = power.size
    k = int(np.argmax(power))
    a, b, c = power[(k - 1) % n], power[k], power[(k + 1) % n]
    den = a - 2 * b + c
    return float(k + (0.5 * (a - c) / den if den else 0.0))


class Demodulator:
    """Find and decode LoRa frames of one spreading factor in a channel
    sampled at `os` times its bandwidth (os = 4 resolves the timing to a
    quarter chip, which a frequency correction can't stand in for: a chirp
    sampled between chips has a phase step where its frequency folds).

    fc_hz (the carrier) sets the sampling clock offset from the frequency
    offset; ldro defaults to what Semtech radios use for this sf and
    bandwidth. Positions are in chips (samples at the bandwidth) from the
    start of the array given to find()."""

    def __init__(
        self,
        sf: int,
        bw_hz: float,
        fc_hz: float,
        os: int = 4,
        ldro: bool | None = None,
        min_preamble: int = 5,
    ):
        self.sf = sf
        self.n = 1 << sf
        self.bw_hz = bw_hz
        self.fc_hz = fc_hz
        self.os = os
        self.ldro = ldro_default(sf, bw_hz) if ldro is None else ldro
        self.min_preamble = min_preamble
        # A window may hold a chirp when its peak bin has this many times the
        # mean power of the others. The strongest of N bins of noise alone
        # averages about ln(N) + 0.6 times the mean, so noise passes this in
        # about one window in three; what rejects it is a run of
        # min_preamble windows whose peaks sit within a bin of each other,
        # which noise practically never makes ((3/N)^4 a run).
        self.threshold = np.log(self.n) + 1
        # Once there is a preamble, a downchirp must stand out more.
        self.confirm = np.log(self.n) + 2
        self.up = upchirp(sf)
        self.down = np.conj(self.up)
        self._ramp = np.arange(self.n, dtype=np.float64) / self.n

    def max_frame_chips(self) -> int:
        """The longest possible frame (255 bytes at cr 4/8) with a 16-symbol
        preamble, in chips."""
        return (frame_symbols(255, self.sf, 4, True, self.ldro) + 21) * self.n

    def _power(self, x: np.ndarray, t: float, ref: np.ndarray, shift: float) -> np.ndarray | None:
        """The dechirped power spectrum of the symbol starting t chips into
        x, at the nearest sample phase; `shift` (bins) is removed first,
        and so is what's left of the timing error, which moves an upchirp's
        bin down (a downchirp's up) by as much as the window starts early."""
        q = int(round(t * self.os))
        end = q + self.n * self.os
        if q < 0 or end > x.size:
            return None
        early = t - q / self.os
        shift = shift - early if ref is self.down else shift + early
        w = x[q:end:self.os] * ref
        if shift:
            w = w * np.exp(-2j * np.pi * shift * self._ramp).astype(np.complex64)
        return np.abs(np.fft.fft(w)) ** 2

    def _strong(self, p: np.ndarray) -> bool:
        top = p.max()
        return bool(top > self.confirm * (p.sum() - top) / (self.n - 1))

    def find(self, x: np.ndarray, deadline: float | None = None, stop: float | None = None) -> list[Frame]:
        """Frames whose preamble starts before `stop` chips (default:
        anywhere in x). Frames running past the end of x are left out.

        The search runs at two sample phases half a chip apart: windows
        sampled between chips lose up to 4 dB where a chirp folds, half a
        chip nearer loses well under 1 dB, and each frame is kept once."""
        phases = (0, self.os // 2) if self.os >= 2 else (0,)
        found: list[Frame] = []
        for ph in phases:
            found += self._scan(x, ph, deadline, stop)
        found.sort(key=lambda f: f.sample)
        frames: list[Frame] = []
        for f in found:
            if frames and abs(f.sample - frames[-1].sample) < self.n / 2:
                if _rank(f) > _rank(frames[-1]):
                    frames[-1] = f
                continue
            frames.append(f)
        return frames

    def _scan(self, x: np.ndarray, ph: int, deadline: float | None, stop: float | None) -> list[Frame]:
        import time

        n = self.n
        xs = x[ph :: self.os]
        m = xs.size // n
        if m < self.min_preamble + 4:
            return []
        spec = np.abs(np.fft.fft(xs[: m * n].reshape(m, n) * self.down, axis=1)) ** 2
        bins = np.argmax(spec, axis=1)
        peak = spec[np.arange(m), bins]
        rest = (spec.sum(axis=1) - peak) / (n - 1)
        strong = peak > self.threshold * np.maximum(rest, 1e-30)
        offset = ph / self.os  # where window 0 starts, in chips
        frames: list[Frame] = []
        i = 1
        limit = m if stop is None else min(m, int(np.ceil(stop / n)))
        while i < limit:
            if deadline is not None and time.monotonic() > deadline:
                break
            if not (strong[i] and strong[i - 1] and abs(_circ(float(bins[i]) - bins[i - 1], n)) <= 1):
                i += 1
                continue
            a, b = i - 1, i
            # Compared with the previous window, so a slow drift (a clock
            # offset) doesn't end the run.
            while b + 1 < m and strong[b + 1] and abs(_circ(float(bins[b + 1]) - bins[b], n)) <= 1:
                b += 1
            if b - a + 1 < self.min_preamble:
                i = b + 1
                continue
            frame = self._frame(x, xs, spec, a, b, offset)
            if frame is None:
                i = b + 1
                continue
            frames.append(frame)
            i = max(b + 1, int((frame.sample + len(frame.symbols) * n) // n))
        return frames

    def _align(self, x: np.ndarray, inner: list[int], a: int, last: int, k_up: float, cfo: float, offset: float):
        """Given the CFO (bins), the symbol grid: (g, k_d), where g(k) is the
        chip where symbol boundary k starts and k_d the first downchirp's.
        None if no downchirp follows."""
        n = self.n
        sto = (k_up - cfo) % n  # the windows start this far into a symbol
        eps = cfo * self.bw_hz / n / self.fc_hz if self.fc_hz else 0.0
        step = n * (1 - eps)  # the transmitter's symbol, in our chips
        mid = (inner[0] + inner[-1]) / 2  # the window where the estimate holds
        # Windows sampled between chips misplace the bins near a fold, so
        # the STO from them can be most of a chip out (the CFO, their mean,
        # isn't). Settle it on the sample phase that puts the most of the
        # aligned, CFO-corrected preamble in bin 0.
        probe = inner[1:-1][:8] or inner
        best_d, best_e = 0.0, -1.0
        for d in np.arange(-6, 7) / self.os:
            e = 0.0
            for w in probe:
                p = self._power(x, offset + mid * n - sto + (w - mid) * step + d, self.down, cfo)
                if p is not None:
                    e += float(p[0])
            if e > best_e:
                best_e, best_d = e, float(d)
        origin = offset + mid * n - sto + best_d

        def g(k: float) -> float:
            return origin + (k - mid) * step

        # Walk the boundaries from inside the preamble to the first one
        # that holds a downchirp.
        for k in range(a + 2, last + 2):
            dp = self._power(x, g(k), self.up, cfo)
            up = self._power(x, g(k), self.down, cfo)
            if dp is None or up is None:
                return None
            if dp.max() > up.max() and self._strong(dp):
                return g, k
        return None

    def _frame(
        self, x: np.ndarray, xs: np.ndarray, spec: np.ndarray, a: int, b: int, offset: float
    ) -> Frame | None:
        n = self.n
        # The upchirps' bin (CFO + STO), finer than a bin, from the windows
        # inside the run (the first may be partly something else).
        inner = list(range(a + 1, b)) if b - a >= 3 else list(range(a, b + 1))
        pre = spec[inner].sum(axis=0)
        k_up = _peak(pre)
        # The fractional CFO from the phase turn from one preamble window to
        # the next: an offset of d bins turns it by 2*pi*d per symbol.
        kk = int(round(k_up)) % n
        idx = [(kk + p) % n for p in range(-2, 3)]
        acc, prev = 0j, None
        for w in inner:
            y = np.fft.fft(xs[w * n : (w + 1) * n] * self.down)[idx]
            if prev is not None:
                acc += complex(np.sum(y * np.conj(prev)))
            prev = y
        frac_cfo = float(np.angle(acc) / (2 * np.pi)) if acc else 0.0
        # The downchirps: of the next few windows dechirped with an upchirp,
        # the one whose peak stands out most gives CFO - STO.
        best, best_w, k_down = 0.0, -1, 0.0
        for w in range(b + 1, min(b + 7, xs.size // n)):
            p = np.abs(np.fft.fft(xs[w * n : (w + 1) * n] * self.up)) ** 2
            top = float(p.max())
            ratio = top * (n - 1) / max(float(p.sum()) - top, 1e-30)
            if ratio > best:
                best, best_w, k_down = ratio, w, _peak(p)
        if best_w < 0 or best < self.confirm:
            return None
        # The fraction comes from the phase turn, which nothing biases; the
        # integer part from the up and down bins together.
        approx = _circ((k_up + k_down) % n, n) / 2
        cfo = frac_cfo + round(approx - frac_cfo)
        aligned = self._align(x, inner, a, best_w, k_up, cfo, offset)
        if aligned is not None:
            # Check the integer on the aligned downchirps: with the CFO e
            # bins out, aligning the upchirps moves the timing by -e, which
            # puts the downchirps at 2e.
            g, k_d = aligned
            p0 = self._power(x, g(k_d), self.up, cfo)
            p1 = self._power(x, g(k_d + 1), self.up, cfo)
            if p0 is not None and p1 is not None:
                fix = round(_circ(_peak(p0 + p1), n) / 2)
                if fix:
                    cfo += fix
                    aligned = self._align(x, inner, a, best_w, k_up, cfo, offset)
        if aligned is None:
            return None
        g, k_d = aligned
        cfo_hz = cfo * self.bw_hz / n
        eps = cfo_hz / self.fc_hz if self.fc_hz else 0.0
        step = n * (1 - eps)  # the transmitter's symbol, in our chips
        s0 = self._power(x, g(k_d - 2), self.down, cfo)
        s1 = self._power(x, g(k_d - 1), self.down, cfo)
        sync = (int(np.argmax(s0)), int(np.argmax(s1))) if s0 is not None and s1 is not None else (-1, -1)
        data0 = g(k_d + 2.25)
        noise = (pre.sum() - pre.max()) / (n - 1)
        snr_db = float(10 * np.log10(max(pre.max() / max(noise, 1e-30) - 1, 1e-6) / n))
        frame = Frame(
            sample=int(round(data0)),
            sf=self.sf,
            bw_hz=self.bw_hz,
            cfo_hz=cfo_hz,
            snr_db=snr_db,
            sync=sync,
            preamble=b - a + 1,
            header=None,
        )

        def symbols(first: int, count: int) -> list[int] | None:
            out = []
            for k in range(first, first + count):
                p = self._power(x, data0 + k * step, self.down, cfo)
                if p is None:
                    return None
                out.append(int(np.argmax(p)))
            return out

        head = symbols(0, 8)
        if head is None:
            return None
        frame.symbols = head
        header, _ = decode_header(head, self.sf)
        if not header.checksum_ok or header.length == 0:
            return frame
        total = frame_symbols(header.length, self.sf, header.cr, header.has_crc, self.ldro)
        rest = symbols(8, total - 8)
        if rest is None:
            return None  # it runs past the end of what we have
        frame.symbols = head + rest
        decoded = decode_symbols(frame.symbols, self.sf, self.ldro)
        if decoded is None:
            return frame
        frame.header = decoded.header
        frame.payload = decoded.payload
        frame.crc_ok = decoded.crc_ok
        frame.errors = decoded.errors
        frame.corrected = decoded.corrected
        return frame


# --- Scanning a capture ----------------------------------------------------


@dataclass(frozen=True)
class Channel:
    """A frequency to search, at one bandwidth, for the spreading factors
    given."""

    freq_hz: float
    bw_hz: float
    sfs: tuple[int, ...]


@dataclass
class Found:
    channel: Channel
    frame: Frame
    seconds: float  # from the start of the capture to the data symbols


OS = 4  # samples per chip the channels are taken at


def rate_fits(sample_rate_hz: int, bw_hz: float) -> bool:
    """Whether a capture's rate gives the channel at OS samples a chip
    exactly (the FFT channeliser also needs it to be a multiple of 25 Hz)."""
    out = OS * bw_hz
    return sample_rate_hz % 25 == 0 and out == int(out) and sample_rate_hz % int(out) == 0 and (
        (sample_rate_hz // 25) % (sample_rate_hz // int(out)) == 0
    )


class _Track:
    """One channel's samples and its demodulators, processed in segments
    that overlap by the longest frame, so memory stays bounded."""

    def __init__(self, channel: Channel, fc_hz: float):
        self.channel = channel
        self.demods = [Demodulator(sf, channel.bw_hz, fc_hz, os=OS) for sf in channel.sfs]
        longest = max(d.max_frame_chips() for d in self.demods)
        self.overlap = longest * OS  # samples
        self.segment = max(self.overlap, 1 << 21)
        self.parts: list[np.ndarray] = []
        self.size = 0
        self.start = 0  # samples before parts[0]
        self.seen: list[tuple[int, int]] = []  # (sf, chip) of frames found
        self.found: list[Found] = []

    def feed(self, y: np.ndarray, deadline: float | None) -> None:
        if y.size:
            self.parts.append(y)
            self.size += y.size
        while self.size >= self.segment + self.overlap:
            self._process(deadline, final=False)

    def finish(self, deadline: float | None) -> None:
        if self.size:
            self._process(deadline, final=True)

    def _process(self, deadline: float | None, final: bool) -> None:
        x = np.concatenate(self.parts) if len(self.parts) > 1 else self.parts[0]
        stop = None if final else self.segment / OS
        base = self.start / OS
        for d in self.demods:
            for f in d.find(x, deadline=deadline, stop=stop):
                chip = int(base + f.sample)
                if any(sf == d.sf and abs(chip - c) < d.n for sf, c in self.seen):
                    continue
                self.seen.append((d.sf, chip))
                self.found.append(Found(self.channel, f, chip / self.channel.bw_hz))
        if final:
            self.parts, self.size = [], 0
            return
        rest = x[self.segment :]
        self.parts, self.size = [rest], rest.size
        self.start += self.segment


def scan(
    chunks,
    sample_rate_hz: int,
    center_hz: float,
    channels: list[Channel],
    deadline: float | None = None,
) -> list[Found]:
    """Every frame in a stream of complex sample chunks (a capture, read
    once), on the channels given, in time order. Each bandwidth is taken
    out by one FFT channeliser for all its channels."""
    import time

    groups: dict[float, list[_Track]] = {}
    for ch in channels:
        groups.setdefault(ch.bw_hz, []).append(_Track(ch, ch.freq_hz))
    from hackrf_mcp import iq

    splitters = {
        bw: iq.FFTChannelizer(
            sample_rate_hz,
            [t.channel.freq_hz - center_hz for t in tracks],
            sample_rate_hz // int(OS * bw),
            1.05 * bw,
        )
        for bw, tracks in groups.items()
    }
    for chunk in chunks:
        if deadline is not None and time.monotonic() > deadline:
            break
        for bw, tracks in groups.items():
            for t, y in zip(tracks, splitters[bw](chunk)):
                t.feed(y, deadline)
    found = []
    for tracks in groups.values():
        for t in tracks:
            t.finish(deadline)
            found += t.found
    found.sort(key=lambda f: f.seconds)
    return found
