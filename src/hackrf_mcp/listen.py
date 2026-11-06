"""Listening: audio out of the live service's samples, for the web UI.

While a listen runs, every block the source delivers also goes to an
AudioTap (one of the source's named taps, beside the spectrum history's):
up to MAX_CHANNELS channels inside the band the receiver is tuned to are cut
out of the stream at once (one forward FFT per block shared by all of them),
each demodulated (AM, NFM or WFM), squelched, and turned into 8-bit mu-law
audio, which goes to the audio socket (AudioServer) for hackrf-web to relay
to browsers. An AM channel in the airband (AIRBAND_HZ) also records each
transmission into the clip store and logs one row for it.

Audio only: the I/Q samples never leave the live service this way.

The live service owns the listen (what is heard, for how long, whether it
lent the receiver to it); this module only turns samples into audio. Nothing
here takes the live service's lock, and nothing here holds a lock while it
calls anything else.
"""

from __future__ import annotations

import math
import os
import queue
import re
import secrets
import shutil
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import scipy.fft as scipy_fft
from scipy import signal as scipy_signal

from hackrf_mcp import iq, live_feed, live_profiles
from hackrf_mcp.live_log import Row

MODES = ("am", "nfm", "wfm")
MAX_CHANNELS = 4
# A listen that retunes the receiver takes this window: the narrowest watch,
# the least CPU, and wide enough to scan several channels in.
WINDOW_RATE_HZ = live_profiles.WATCH_RATES_HZ[0]
# Clips are recorded on AM channels in the aeronautical voice band only.
AIRBAND_HZ = (118_000_000, 137_000_000)
# The automatic squelch opens this far above the channel's noise floor
# (measured across the whole window, so a channel that never goes quiet,
# like a weather broadcast, still opens); each channel's slider moves it by
# squelch_db within SQUELCH_RANGE_DB, and at the bottom of the range the
# squelch stays open.
AUTO_MARGIN_DB = 8.0
SQUELCH_RANGE_DB = (-20.0, 30.0)
HYSTERESIS_DB = 2.0
# Closed only after this long below the threshold, so a reply that follows
# at once joins the same clip.
HANG_S = 1.5
# Audio kept from before the squelch opened, and after the signal ended.
PRE_ROLL_S = 0.25
TAIL_S = 0.3
# Shorter transmissions (a squelch blip) are not kept as clips.
MIN_CLIP_S = 0.5
METER_S = 0.25
# Each block the channels are cut from spans sample_rate / BIN_HZ samples
# (40 ms), which is also how often the squelch decides.
BIN_HZ = 25
# Blocks the tap may hold for its worker: about two seconds at 10 MS/s.
QUEUE_BLOCKS = 64
# The audio socket: frames a client may fall behind by (about 10 s of four
# channels' audio and meters).
AUDIO_QUEUE = 512

# NOAA Weather Radio's national channels, built in (and public).
NOAA_CHANNELS = (
    (162_550_000, "NOAA WX1"),
    (162_400_000, "NOAA WX2"),
    (162_475_000, "NOAA WX3"),
    (162_425_000, "NOAA WX4"),
    (162_450_000, "NOAA WX5"),
    (162_500_000, "NOAA WX6"),
    (162_525_000, "NOAA WX7"),
)


@dataclass(frozen=True)
class ModeParams:
    channel_min_hz: int  # the channel's own complex rate is at least this
    bandwidth_hz: int  # cut out of the stream
    audio_min_hz: int  # the audio's rate is at least this
    audio_cut_hz: float  # the audio's low-pass
    deviation_hz: float | None = None  # FM: peak deviation, for the audio's level
    deemphasis_s: float | None = None  # FM broadcast's de-emphasis


MODE: dict[str, ModeParams] = {
    "am": ModeParams(16_000, 10_000, 8_000, 3_400),
    "nfm": ModeParams(32_000, 16_000, 8_000, 3_400, deviation_hz=5_000),
    "wfm": ModeParams(200_000, 180_000, 16_000, 7_500, deviation_hz=75_000, deemphasis_s=75e-6),
}


class ListenError(ValueError):
    pass


def mhz(freq_hz: int) -> str:
    """A channel's name: its frequency in MHz, to three or four decimals."""
    s = f"{freq_hz / 1e6:.4f}".rstrip("0")
    whole, _, frac = s.partition(".")
    return f"{whole}.{frac.ljust(3, '0')} MHz"


def default_mode(freq_hz: int) -> str:
    if 108_000_000 <= freq_hz <= AIRBAND_HZ[1]:
        return "am"
    if 88_000_000 <= freq_hz < 108_000_000:
        return "wfm"
    return "nfm"


@dataclass(frozen=True)
class Channel:
    freq_hz: int
    mode: str
    squelch_db: float = 0.0
    label: str | None = None

    @property
    def key(self) -> tuple[int, str]:
        return (self.freq_hz, self.mode)

    @property
    def clips(self) -> bool:
        return self.mode == "am" and AIRBAND_HZ[0] <= self.freq_hz <= AIRBAND_HZ[1]

    @property
    def always_open(self) -> bool:
        return self.squelch_db <= SQUELCH_RANGE_DB[0]

    def describe(self) -> str:
        s = f"{self.freq_hz / 1e6:.4f} MHz {self.mode.upper()}"
        return f"{s} ({self.label})" if self.label else s

    def to_json(self) -> dict:
        return {
            "freq_hz": self.freq_hz,
            "mode": self.mode,
            "squelch_db": self.squelch_db,
            "label": self.label,
            "clips": self.clips,
        }


def parse_channels(items) -> tuple[Channel, ...]:
    """The channels of a listen request, checked."""
    if not isinstance(items, (list, tuple)) or not items:
        raise ListenError("give at least one channel to listen to")
    if len(items) > MAX_CHANNELS:
        raise ListenError(f"at most {MAX_CHANNELS} channels at once, not {len(items)}")
    out: list[Channel] = []
    for it in items:
        if not isinstance(it, dict):
            raise ListenError("each channel is an object with freq_hz (and mode, squelch_db, label)")
        freq = int(round(float(it["freq_hz"])))
        if not live_profiles.WATCH_MIN_HZ <= freq <= live_profiles.WATCH_MAX_HZ:
            raise ListenError(f"{freq / 1e6:.4f} MHz is outside what the HackRF receives")
        mode = str(it.get("mode") or default_mode(freq)).lower()
        if mode not in MODES:
            raise ListenError(f"mode {mode!r} is not one of {', '.join(MODES)}")
        sq = float(it.get("squelch_db") or 0.0)
        if not math.isfinite(sq):
            raise ListenError("squelch_db must be a number")
        sq = min(max(sq, SQUELCH_RANGE_DB[0]), SQUELCH_RANGE_DB[1])
        label = None
        if it.get("label") is not None:
            label = str(it["label"]).strip()[:40] or None
        if any(abs(c.freq_hz - freq) < 1_000 for c in out):
            raise ListenError(f"{freq / 1e6:.4f} MHz is given twice")
        out.append(Channel(freq, mode, sq, label))
    return tuple(out)


def same_channels(a: tuple[Channel, ...], b: tuple[Channel, ...]) -> bool:
    return [c.key for c in a] == [c.key for c in b]


def fits(center_hz: int, rate_hz: int, channels: tuple[Channel, ...]) -> bool:
    """Whether every channel lies whole inside the usable part of a tuning."""
    if not rate_hz or rate_hz % BIN_HZ:
        return False
    reach = live_profiles.usable_hz(rate_hz)
    for c in channels:
        p = MODE[c.mode]
        if abs(c.freq_hz - center_hz) + p.bandwidth_hz / 2 > reach:
            return False
        if rate_hz / _decimation_for(rate_hz, p.channel_min_hz) <= p.bandwidth_hz:
            return False
    return True


def window_center(channels: tuple[Channel, ...]) -> int:
    """The centre of a WINDOW_RATE_HZ tuning holding every channel, each
    clear of the DC spike at the centre."""
    half = max(MODE[c.mode].bandwidth_hz for c in channels) // 2
    try:
        return live_profiles.choose_center(
            tuple(c.freq_hz for c in channels), WINDOW_RATE_HZ, margin_hz=10_000 + half, avoid_hz=20_000 + half
        )
    except live_profiles.SpecError as exc:
        raise ListenError(f"those channels don't fit one {WINDOW_RATE_HZ / 1e6:g} MHz window: {exc}") from None


# --- mu-law and WAV -------------------------------------------------------------

_BIAS = 0x84
_CLIP = 32635


def ulaw_encode(x: np.ndarray) -> bytes:
    """G.711 mu-law bytes from samples in [-1, 1]."""
    pcm = np.clip(np.rint(np.asarray(x, dtype=np.float32) * 32767.0), -32768, 32767).astype(np.int32)
    sign = np.where(pcm < 0, 0x80, 0)
    mag = np.minimum(np.abs(pcm), _CLIP) + _BIAS
    exponent = np.clip(np.floor(np.log2(mag)).astype(np.int32) - 7, 0, 7)
    mantissa = (mag >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4) | mantissa) & 0xFF).astype(np.uint8).tobytes()


def _ulaw_table() -> np.ndarray:
    u = ~np.arange(256, dtype=np.int32) & 0xFF
    sign = u & 0x80
    exponent = (u >> 4) & 0x07
    mantissa = u & 0x0F
    mag = (((mantissa << 3) + _BIAS) << exponent) - _BIAS
    return (np.where(sign, -mag, mag) / 32768.0).astype(np.float32)


ULAW_TABLE = _ulaw_table()


def ulaw_decode(data: bytes) -> np.ndarray:
    return ULAW_TABLE[np.frombuffer(data, dtype=np.uint8)]


def ulaw_wav(data: bytes, rate_hz: int) -> bytes:
    """A mono mu-law WAV file (format 7) of `data`."""
    fmt = struct.pack("<HHIIHHH", 7, 1, int(rate_hz), int(rate_hz), 1, 8, 0)
    fact = struct.pack("<I", len(data))
    pad = b"\0" if len(data) % 2 else b""
    body = (
        b"WAVE"
        + b"fmt " + struct.pack("<I", len(fmt)) + fmt
        + b"fact" + struct.pack("<I", len(fact)) + fact
        + b"data" + struct.pack("<I", len(data)) + data + pad
    )  # fmt: skip
    return b"RIFF" + struct.pack("<I", len(body)) + body


# --- cutting channels out ------------------------------------------------------------


def _decimation_for(rate_hz: int, min_out_hz: int, bin_hz: int = BIN_HZ) -> int:
    """The largest factor dividing the block (rate / bin_hz samples) that
    leaves a channel rate of at least min_out_hz."""
    n = rate_hz // bin_hz
    best = 1
    for d in range(1, max(1, rate_hz // max(min_out_hz, 1)) + 1):
        if n % d == 0:
            best = d
    return best


class Channelizer:
    """Several channels, each with its own width and rate, out of one
    stream at once: overlap-save fast convolution like iq.FFTChannelizer,
    one forward FFT per block for all of them, and a small inverse FFT per
    channel. Also measures each block's noise floor across the band (the
    median bin, which a few busy channels don't move), so a squelch can
    open on a channel that is never quiet.

    Call it with complex64 chunks; it returns, per whole block, a list of
    (samples per channel, power per channel, noise floor per channel), all
    powers linear (full scale = 1)."""

    def __init__(self, sample_rate_hz: int, offsets_hz, decimations, bandwidths_hz, bin_hz: int = BIN_HZ):
        if sample_rate_hz % bin_hz:
            raise ValueError(f"sample rate {sample_rate_hz} is not a multiple of {bin_hz} Hz")
        n = sample_rate_hz // bin_hz
        decs = [int(d) for d in decimations]
        for d, bw in zip(decs, bandwidths_hz):
            if n % d:
                raise ValueError(f"decimation {d} does not divide the {n}-point block")
            if bw >= sample_rate_hz / d:
                raise ValueError(f"bandwidth {bw:g} Hz does not fit {sample_rate_hz / d:g} S/s")
        lcm = 1
        for d in decs:
            lcm = lcm * d // math.gcd(lcm, d)
        overlap = lcm * max(1, round(n / 10 / lcm))
        if overlap >= n:
            raise ValueError("those channel rates leave no room in the block")
        self.rate = sample_rate_hz
        self.n = n
        self.overlap = overlap
        self.step = n - overlap
        self.decimations = decs
        self.rates_hz = [sample_rate_hz / d for d in decs]
        self.bins = [int(round(f / bin_hz)) for f in offsets_hz]
        self._index, self._response, self._keep, self._noise_gain = [], [], [], []
        for k, d, bw in zip(self.bins, decs, bandwidths_hz):
            m = n // d
            taps = scipy_signal.firwin(overlap + 1, bw / 2, fs=sample_rate_hz, window=("kaiser", 8.0))
            response = scipy_fft.fft(taps.astype(np.float64), n)
            j = np.fft.fftfreq(m, 1.0 / m).astype(np.int64)
            r = (response[j % n] / d).astype(np.complex64)
            self._index.append((k + j) % n)
            self._response.append(r)
            self._keep.append(overlap // d)
            # A channel's mean power for white noise of mean bin power D is
            # D * sum|r|^2 / m^2 (the inverse FFT divides by m).
            self._noise_gain.append(float(np.sum(np.abs(r) ** 2)) / m**2)
        # Bins the floor is measured on: the usable band, less the DC spike.
        f = np.abs(np.fft.fftfreq(n, 1.0 / sample_rate_hz))
        self._floor_bins = np.flatnonzero((f <= live_profiles.usable_hz(sample_rate_hz)) & (f >= 20_000))[::4]
        self._history = np.zeros(overlap, dtype=np.complex64)
        self._pending: list[np.ndarray] = []
        self._pending_samples = 0
        self._block = 0

    def __call__(self, x: np.ndarray):
        if x.size:
            self._pending.append(x.astype(np.complex64, copy=False))
            self._pending_samples += x.size
        blocks = []
        if self._pending_samples < self.step:
            return blocks
        data = np.concatenate(self._pending)
        used = 0
        while data.size - used >= self.step:
            block = np.concatenate([self._history, data[used : used + self.step]])
            used += self.step
            spectrum = scipy_fft.fft(block)
            start = self._block * self.step - self.overlap
            power = spectrum[self._floor_bins]
            power = power.real**2 + power.imag**2
            # The median of exponentially distributed bin powers is ln 2
            # times their mean.
            density = float(np.median(power)) / math.log(2) if power.size else 0.0
            outs, powers, floors = [], [], []
            for k, idx, r, keep, gain in zip(self.bins, self._index, self._response, self._keep, self._noise_gain):
                y = scipy_fft.ifft(spectrum[idx] * r)[keep:]
                turn = (k * start) % self.n
                y = (y * np.complex64(np.exp(-2j * np.pi * turn / self.n))).astype(np.complex64)
                outs.append(y)
                powers.append(float(np.mean(y.real**2 + y.imag**2)) if y.size else 0.0)
                floors.append(density * gain)
            blocks.append((outs, powers, floors))
            self._history = block[-self.overlap :]
            self._block += 1
        rest = data[used:]
        self._pending = [rest] if rest.size else []
        self._pending_samples = rest.size
        return blocks


def db(p: float) -> float:
    return 10 * math.log10(p) if p > 0 else -150.0


# --- demodulation --------------------------------------------------------------------


class Demod:
    """One channel's samples (at its channel rate) to audio, keeping every
    filter's state from one block to the next."""

    def __init__(self, mode: str, channel_rate_hz: float):
        self.mode = mode
        p = MODE[mode]
        self.p = p
        self.d = max(1, int(channel_rate_hz // p.audio_min_hz))
        self.audio_rate_hz = channel_rate_hz / self.d
        self.decimator = iq.Decimator(self.d, min(0.95, p.audio_cut_hz / (self.audio_rate_hz / 2))) if self.d > 1 else None
        self.prev: complex | None = None
        if mode == "am":
            # The carrier's level, smoothed over about a quarter second: the
            # audio is the envelope relative to it, so its level follows the
            # modulation, not the signal strength.
            a = 1 - math.exp(-1 / (0.25 * channel_rate_hz))
            self._carrier = ([a], [1, -(1 - a)])
            self._carrier_zi: np.ndarray | None = None
        if p.deviation_hz:
            self.fm_scale = channel_rate_hz / (2 * math.pi * p.deviation_hz)
        if p.deemphasis_s:
            a = 1 - math.exp(-1 / (p.deemphasis_s * channel_rate_hz))
            self._deemph = ([a], [1, -(1 - a)])
            self._deemph_zi = np.zeros(1)

    def __call__(self, y: np.ndarray) -> np.ndarray:
        if y.size == 0:
            return np.zeros(0, np.float32)
        if self.mode == "am":
            env = np.abs(y).astype(np.float64)
            b, a = self._carrier
            if self._carrier_zi is None:
                self._carrier_zi = scipy_signal.lfiltic(b, a, [env[0]], [env[0]])
            carrier, self._carrier_zi = scipy_signal.lfilter(b, a, env, zi=self._carrier_zi)
            audio = env / np.maximum(carrier, 1e-6) - 1.0
        else:
            d, self.prev = iq.fm_discriminate(y, self.prev)
            audio = d.astype(np.float64) * self.fm_scale
            if self.p.deemphasis_s:
                b, a = self._deemph
                audio, self._deemph_zi = scipy_signal.lfilter(b, a, audio, zi=self._deemph_zi)
        audio = np.clip(audio, -2.0, 2.0).astype(np.float32)
        if self.decimator is not None:
            audio = self.decimator(audio.astype(np.complex64)).real
        return np.clip(audio * 0.8, -1.0, 1.0).astype(np.float32)


class Squelch:
    """Open while the channel's power is AUTO_MARGIN_DB + offset above the
    floor; closed after HANG_S below it (less HYSTERESIS_DB)."""

    def __init__(self, offset_db: float = 0.0):
        self.offset_db = offset_db
        self.open = False
        self.active = False  # the signal is above the threshold now
        self.below_s = 0.0
        self.threshold_db = -150.0

    def update(self, power_db: float, floor_db: float, dt: float) -> bool:
        self.threshold_db = floor_db + AUTO_MARGIN_DB + self.offset_db
        if self.offset_db <= SQUELCH_RANGE_DB[0]:
            self.open, self.active = True, power_db >= self.threshold_db
            return self.open
        self.active = power_db >= self.threshold_db
        if self.active:
            self.open = True
            self.below_s = 0.0
        elif self.open:
            if power_db < self.threshold_db - HYSTERESIS_DB:
                self.below_s += dt
            if self.below_s >= HANG_S:
                self.open = False
                self.below_s = 0.0
        return self.open


# --- clips ---------------------------------------------------------------------------

ID_RE = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{4}")


class ClipStore:
    """clips/ under the live directory: <id>.wav, mu-law. Kept keep_days or
    until the folder passes keep_mb, oldest first; nothing is written while
    free disk space is under min_free_mb."""

    def __init__(self, root: Path, keep_days: float = 7.0, keep_mb: float = 1024.0, min_free_mb: float = 0.0, clock=time.time):
        self.root = Path(root)
        self.keep_s = keep_days * 86400
        self.keep_bytes = int(keep_mb * 1024 * 1024)
        self.min_free = int(min_free_mb * 1024 * 1024)
        self.keep_days = keep_days
        self.keep_mb = keep_mb
        self.clock = clock
        self._status: tuple[float, dict] | None = None

    @staticmethod
    def check_id(cid: str) -> str:
        if not isinstance(cid, str) or not ID_RE.fullmatch(cid):
            raise ValueError(f"not a clip id: {cid!r}")
        return cid

    def path(self, cid: str) -> Path:
        return self.root / f"{self.check_id(cid)}.wav"

    def new_id(self, ts: float) -> str:
        while True:
            cid = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(ts)) + "-" + secrets.token_hex(2)
            if not self.path(cid).exists():
                return cid

    def disk_ok(self) -> bool:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            return shutil.disk_usage(self.root).free >= self.min_free
        except OSError:
            return False

    def write(self, cid: str, data: bytes) -> int:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.path(cid)
        tmp = path.with_suffix(".wav.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        self._status = None
        return len(data)

    def _files(self) -> list[tuple[str, int, float]]:
        out = []
        try:
            entries = list(os.scandir(self.root))
        except FileNotFoundError:
            return out
        for e in entries:
            cid, _, ext = e.name.partition(".")
            if ext == "wav" and ID_RE.fullmatch(cid):
                try:
                    st = e.stat()
                except OSError:
                    continue
                out.append((cid, st.st_size, st.st_mtime))
        return sorted(out)

    def delete(self, cid: str) -> int:
        path = self.path(cid)
        try:
            size = path.stat().st_size
            path.unlink()
        except FileNotFoundError:
            return 0
        self._status = None
        return size

    def prune(self, need_bytes: int = 0) -> list[str]:
        now = self.clock()
        files = self._files()
        used = sum(s for _, s, _ in files)
        gone = []
        for cid, size, mtime in files:  # the id starts with its UTC time
            if now - mtime > self.keep_s or used + need_bytes > self.keep_bytes:
                used -= self.delete(cid)
                gone.append(cid)
        return gone

    def status(self) -> dict:
        now = time.monotonic()
        if self._status is not None and now - self._status[0] < 10:
            return self._status[1]
        files = self._files()
        st = {"count": len(files), "bytes": sum(s for _, s, _ in files), "keep_days": self.keep_days, "keep_mb": self.keep_mb}
        self._status = (now, st)
        return st


class ClipRecorder:
    """One channel's transmissions as clips: from the squelch opening (with
    PRE_ROLL_S before it) to the signal's end (with TAIL_S after it), cut
    at max_s. Calls on_clip(ts, ts_end, audio, peak_db, continued) with the
    clip's mu-law bytes."""

    def __init__(self, rate_hz: float, max_s: float, on_clip):
        self.rate = rate_hz
        self.max_samples = int(max_s * rate_hz)
        self.pre = int(PRE_ROLL_S * rate_hz)
        self.tail = int(TAIL_S * rate_hz)
        self.on_clip = on_clip
        self._ring = np.zeros(0, np.float32)
        self._parts: list[np.ndarray] | None = None
        self._samples = 0
        self._signal_end = 0  # samples into the clip where the signal was last above the threshold
        self._start_ts = 0.0
        self._peak_db = -150.0
        self._continued = False

    @property
    def recording(self) -> bool:
        return self._parts is not None

    def feed(self, audio: np.ndarray, ts: float, open_: bool, active: bool, power_db: float) -> None:
        if self._parts is None and open_ and active:
            pre = self._ring[-self.pre :] if self.pre else self._ring[:0]
            self._parts = [pre]
            self._samples = pre.size
            self._start_ts = ts - pre.size / self.rate
            self._peak_db = power_db
            self._signal_end = self._samples
        if self._parts is not None:
            self._parts.append(audio)
            self._samples += audio.size
            if active:
                self._signal_end = self._samples
                self._peak_db = max(self._peak_db, power_db)
            if not open_:
                self.finish()
            elif self._samples >= self.max_samples:
                self.finish(cut=True)
                if active:  # still talking: carry straight on in a new clip
                    self._parts = []
                    self._samples = 0
                    self._signal_end = 0
                    self._start_ts = ts + audio.size / self.rate
                    self._peak_db = power_db
                    self._continued = True
        self._ring = np.concatenate([self._ring, audio])[-max(self.pre, 1) :]

    def finish(self, cut: bool = False) -> None:
        if self._parts is None:
            return
        audio = np.concatenate(self._parts) if self._parts else np.zeros(0, np.float32)
        end = audio.size if cut else min(audio.size, self._signal_end + self.tail)
        audio = audio[: min(end, self.max_samples)]
        self._parts = None
        continued, self._continued = self._continued, False
        if audio.size >= MIN_CLIP_S * self.rate:
            self.on_clip(self._start_ts, self._start_ts + audio.size / self.rate, ulaw_encode(audio), self._peak_db, continued)


# --- frames on the audio socket ------------------------------------------------------

# Little-endian: magic, type, channel, flags, audio rate (Hz), sequence (or,
# for DROPPED, how many frames were lost), time of the first sample, the
# channel's level, its squelch threshold and its noise floor (dBFS). AUDIO
# frames carry mu-law samples after the header; METER frames come every
# METER_S for each channel, squelched or not; DROPPED tells a client that
# fell behind how many frames it lost.
FRAME = struct.Struct("<4sBBHIIdfff")
MAGIC = b"HKA1"
AUDIO, METER, DROPPED = 1, 2, 3
FLAG_OPEN, FLAG_ACTIVE, FLAG_RECORDING = 1, 2, 4


def frame(kind: int, channel: int, flags: int, rate_hz: float, seq: int, ts: float, level_db: float, threshold_db: float, floor_db: float, payload: bytes = b"") -> bytes:
    return FRAME.pack(MAGIC, kind, channel, flags, int(round(rate_hz)), seq & 0xFFFFFFFF, ts, level_db, threshold_db, floor_db) + payload


def parse_frame(data: bytes) -> dict:
    magic, kind, channel, flags, rate, seq, ts, level, threshold, floor = FRAME.unpack_from(data)
    if magic != MAGIC:
        raise ValueError("not an audio frame")
    return {
        "type": kind, "channel": channel, "flags": flags, "rate_hz": rate, "seq": seq, "ts": ts,
        "level_db": level, "threshold_db": threshold, "floor_db": floor, "payload": data[FRAME.size :],
    }  # fmt: skip


def length_prefixed(data: bytes) -> bytes:
    return struct.pack("<I", len(data)) + data


def dropped_frame(n: int) -> bytes:
    return length_prefixed(frame(DROPPED, 0, 0, 0, n, time.time(), 0.0, 0.0, 0.0))


class AudioServer(live_feed.FeedServer):
    """The audio socket (LiveSettings.audio_socket_path): each client gets
    every frame, length-prefixed (a little-endian u32, then the frame),
    with the feed's bounded queue per client, so a slow one loses frames
    (and is told how many) instead of holding anything up."""

    def __init__(self, path: Path, maxsize: int = AUDIO_QUEUE):
        super().__init__(path, maxsize, dropped=dropped_frame, name="audio")

    def send(self, data: bytes) -> None:
        self.publish_bytes(length_prefixed(data))


def read_frames(path: Path, timeout: float = 5.0):
    """Connect to an audio socket now, and iterate over its frames as dicts
    (for tests and debugging)."""
    import socket

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(str(path))

    def frames():
        buf = b""
        try:
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    return
                buf += chunk
                while len(buf) >= 4:
                    (n,) = struct.unpack_from("<I", buf)
                    if len(buf) < 4 + n:
                        break
                    yield parse_frame(buf[4 : 4 + n])
                    buf = buf[4 + n :]
        finally:
            sock.close()

    return frames()


# --- the tap -------------------------------------------------------------------------


class AudioTap:
    """Called with every block of int8 I/Q bytes the source delivers, from
    the source's thread: the blocks are queued (dropped, and counted, if
    the worker is QUEUE_BLOCKS behind) and turned into audio by a thread
    of the tap's own, so listening never slows the decoder sharing the
    source. close() ends it, finishing any clip in progress.

    `send` takes each frame (AudioServer.send); `has_listeners` says whether
    anyone takes audio now (meters and clips go on regardless); `on_clip`
    gets (channel, ts, ts_end, mu-law bytes, rate, peak_db, continued) for
    each clip of a channel that records them, or None for no clips.
    `threaded=False` processes each block in the caller (tests)."""

    def __init__(
        self,
        center_hz: int,
        sample_rate_hz: int,
        channels: tuple[Channel, ...],
        *,
        send: Callable[[bytes], None] | None = None,
        has_listeners: Callable[[], bool] = lambda: True,
        on_clip=None,
        clip_max_s: float = 60.0,
        clock: Callable[[], float] = time.time,
        threaded: bool = True,
    ):
        if not fits(center_hz, sample_rate_hz, channels):
            raise ListenError("those channels are not inside the band the receiver is tuned to")
        self.center_hz = center_hz
        self.rate = sample_rate_hz
        self.channels = channels
        self.send = send
        self.has_listeners = has_listeners
        self.clock = clock
        decs = [_decimation_for(sample_rate_hz, MODE[c.mode].channel_min_hz) for c in channels]
        self.channelizer = Channelizer(
            sample_rate_hz, [c.freq_hz - center_hz for c in channels], decs, [MODE[c.mode].bandwidth_hz for c in channels]
        )
        self.demods = [Demod(c.mode, r) for c, r in zip(channels, self.channelizer.rates_hz)]
        self.squelches = [Squelch(c.squelch_db) for c in channels]
        self.recorders: list[ClipRecorder | None] = []
        for i, (c, d) in enumerate(zip(channels, self.demods)):
            if on_clip is not None and c.clips:
                self.recorders.append(
                    ClipRecorder(d.audio_rate_hz, clip_max_s, lambda *a, i=i, r=d.audio_rate_hz: on_clip(i, a[0], a[1], a[2], r, a[3], a[4]))
                )
            else:
                self.recorders.append(None)
        self.levels_db = [-150.0] * len(channels)
        self.floors_db = [-150.0] * len(channels)
        self.block_s = self.channelizer.step / sample_rate_hz
        self.seq = 0
        self.dropped_blocks = 0
        self.blocks = 0
        self.cpu_s = 0.0
        self.started = time.monotonic()
        self.error: str | None = None
        self._meter_due = 0.0
        self._odd = b""
        self._closed = False
        self.threaded = threaded
        self._q: queue.Queue = queue.Queue(maxsize=QUEUE_BLOCKS)
        self._thread: threading.Thread | None = None
        if threaded:
            self._thread = threading.Thread(target=self._run, daemon=True, name="audio-tap")
            self._thread.start()

    # Called from the source's thread.
    def __call__(self, buf: bytes) -> None:
        if self._closed:
            return
        if not self.threaded:
            self.process(buf, self.clock())
            return
        try:
            self._q.put_nowait((buf, self.clock()))
        except queue.Full:
            self.dropped_blocks += 1

    def set_squelch(self, offsets_db) -> None:
        for s, o in zip(self.squelches, offsets_db):
            s.offset_db = float(o)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.threaded:
            try:
                self._q.put_nowait(None)
            except queue.Full:
                pass  # the worker sees _closed after its next block
        else:
            self._finish()

    def _finish(self) -> None:
        for r in self.recorders:
            if r is not None:
                try:
                    r.finish()
                except Exception as exc:
                    self.error = f"finishing a clip: {exc!r}"

    def _run(self) -> None:
        try:
            while True:
                item = self._q.get()
                if item is None or (self._closed and self._q.empty()):
                    break
                t0 = time.thread_time()
                try:
                    self.process(*item)
                except Exception as exc:  # counted, shown in status; listening goes on
                    self.error = repr(exc)
                self.cpu_s += time.thread_time() - t0
        finally:
            self._finish()

    def process(self, buf: bytes, arrived: float) -> None:
        if self._odd:
            buf = self._odd + buf
            self._odd = b""
        if len(buf) % 2:
            buf, self._odd = buf[:-1], buf[-1:]
        x = iq.cs8_to_complex(buf)
        blocks = self.channelizer(x)
        if not blocks:
            return
        listening = self.send is not None and self.has_listeners()
        # The blocks just completed end about when this buffer arrived.
        t = arrived - len(blocks) * self.block_s
        audio_out: list[list[np.ndarray]] = [[] for _ in self.channels]
        flags = [0] * len(self.channels)
        first_ts: list[float | None] = [None] * len(self.channels)
        for outs, powers, floors in blocks:
            self.blocks += 1
            for i, (y, p, f) in enumerate(zip(outs, powers, floors)):
                level, floor = db(p), db(f)
                self.levels_db[i], self.floors_db[i] = level, floor
                sq = self.squelches[i]
                open_ = sq.update(level, floor, self.block_s)
                audio = self.demods[i](y)
                r = self.recorders[i]
                if r is not None:
                    r.feed(audio, t, open_, sq.active, level)
                if open_ and listening:
                    if first_ts[i] is None:
                        first_ts[i] = t
                    audio_out[i].append(audio)
                flags[i] = (FLAG_OPEN if open_ else 0) | (FLAG_ACTIVE if sq.active else 0) | (FLAG_RECORDING if r is not None and r.recording else 0)
            t += self.block_s
        if self.send is None:
            return
        meter = t >= self._meter_due
        if meter:
            self._meter_due = t + METER_S
        for i, parts in enumerate(audio_out):
            sq = self.squelches[i]
            rate = self.demods[i].audio_rate_hz
            if parts:
                self.seq += 1
                self.send(frame(AUDIO, i, flags[i], rate, self.seq, first_ts[i], self.levels_db[i], sq.threshold_db, self.floors_db[i], ulaw_encode(np.concatenate(parts))))
            if meter:
                self.seq += 1
                self.send(frame(METER, i, flags[i], rate, self.seq, t, self.levels_db[i], sq.threshold_db, self.floors_db[i]))

    def status(self) -> list[dict]:
        """Each channel's state now (read without a lock: plain values the
        worker overwrites)."""
        out = []
        for i, c in enumerate(self.channels):
            sq = self.squelches[i]
            r = self.recorders[i]
            out.append(
                {
                    **c.to_json(),
                    "squelch_db": sq.offset_db,
                    "level_db": round(self.levels_db[i], 1),
                    "floor_db": round(self.floors_db[i], 1),
                    "threshold_db": round(sq.threshold_db, 1),
                    "open": sq.open,
                    "recording": bool(r is not None and r.recording),
                    "audio_rate_hz": round(self.demods[i].audio_rate_hz, 1),
                }
            )
        return out

    def health(self) -> dict:
        up = max(time.monotonic() - self.started, 1e-6)
        return {
            "cpu_pct": round(100 * self.cpu_s / up, 1),
            "dropped_blocks": self.dropped_blocks,
            "blocks": self.blocks,
            "error": self.error,
        }


# --- presets -------------------------------------------------------------------------


def parse_presets(text: str) -> list[dict]:
    """Named channels, one per line or ';'-separated: FREQ_MHZ [MODE] [LABEL],
    '#' starts a comment. Lines that don't parse are skipped."""
    out = []
    for raw in re.split(r"[;\n]", text or ""):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        try:
            freq = int(round(float(parts[0]) * 1e6))
        except ValueError:
            continue
        mode = parts[1].lower() if len(parts) > 1 and parts[1].lower() in MODES else None
        label = " ".join(parts[2 if mode else 1 :])[:40] or None
        if live_profiles.WATCH_MIN_HZ <= freq <= live_profiles.WATCH_MAX_HZ:
            out.append({"freq_hz": freq, "mode": mode or default_mode(freq), "label": label})
    return out[:200]


def builtin_presets() -> list[dict]:
    return [{"freq_hz": f, "mode": "nfm", "label": name} for f, name in sorted(NOAA_CHANNELS)]
