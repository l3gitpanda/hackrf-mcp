"""Offline analysis helpers for HackRF IQ capture files (numpy/scipy/matplotlib).

These operate purely on files already written by hackrf_transfer (-r) and do
not touch the device.
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path
from typing import Literal

import numpy as np
from scipy import signal as scipy_signal
from scipy.io import wavfile

Mode = Literal["nfm", "wfm", "am", "usb", "lsb"]

# Reasonable default channel bandwidths per mode, in Hz.
MODE_BANDWIDTH_HZ: dict[Mode, float] = {
    "nfm": 16_000,
    "wfm": 180_000,
    "am": 10_000,
    "usb": 2_900,
    "lsb": 2_900,
}

# Audio low-pass cutoff applied to the FM discriminator output. Without this,
# the discriminator's triangular noise spectrum (power rises with frequency)
# dominates the signal and swamps percentile-based normalization.
AUDIO_LPF_HZ: dict[str, float] = {
    "nfm": 3_000,
    "wfm": 15_000,
}

# De-emphasis time constant (seconds) to undo transmitter pre-emphasis.
DEEMPHASIS_TAU_S: dict[str, float] = {
    "wfm": 75e-6,
}


def _pyplot():
    """matplotlib, imported on first use: only the PNG renderers need it,
    and the live decoder service (live_service.py) never does."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    return plt


def read_iq_int8(path: str | Path, max_samples: int | None = None) -> np.ndarray:
    """Read interleaved 8-bit signed I/Q samples (hackrf_transfer's default
    capture format) into a complex64 array scaled to roughly [-1, 1]."""
    count = -1 if max_samples is None else max_samples * 2
    raw = np.fromfile(path, dtype=np.int8, count=count)
    if raw.size % 2:
        raw = raw[:-1]
    raw = raw.astype(np.float32) / 128.0
    return (raw[0::2] + 1j * raw[1::2]).astype(np.complex64)


def mix(iq: np.ndarray, sample_rate_hz: float, shift_hz: float) -> np.ndarray:
    if shift_hz == 0:
        return iq
    t = np.arange(iq.size, dtype=np.float64) / sample_rate_hz
    return iq * np.exp(-2j * np.pi * shift_hz * t)


def _lowpass(iq: np.ndarray, sample_rate_hz: float, cutoff_hz: float, numtaps: int = 129) -> np.ndarray:
    nyq = sample_rate_hz / 2
    cutoff = max(1.0, min(cutoff_hz, nyq * 0.99))
    taps = scipy_signal.firwin(numtaps, cutoff / nyq).astype(np.float32)
    # Overlap-add works block by block, so it needs far less memory than
    # one FFT across a long capture.
    return scipy_signal.oaconvolve(iq, taps, mode="same").astype(np.complex64)


def demodulate(
    iq: np.ndarray,
    sample_rate_hz: float,
    mode: Mode,
    bandwidth_hz: float | None = None,
    denoise: bool = False,
) -> np.ndarray:
    """Demodulate a baseband-centered IQ array to a real-valued audio signal
    (still at sample_rate_hz; caller is responsible for resampling)."""
    bw = bandwidth_hz or MODE_BANDWIDTH_HZ[mode]

    if mode == "am":
        filtered = _lowpass(iq, sample_rate_hz, bw / 2)
        audio = np.abs(filtered)
        audio = audio - np.mean(audio)
    elif mode in ("usb", "lsb"):
        # Shift the desired sideband toward baseband, low-pass to its
        # bandwidth, then take the real part (phasing-method approximation).
        shift = bw / 2 if mode == "usb" else -bw / 2
        shifted = mix(iq, sample_rate_hz, shift)
        filtered = _lowpass(shifted, sample_rate_hz, bw / 2)
        audio = np.real(filtered)
    else:
        # nfm / wfm: quadrature (phase-difference) FM discriminator.
        filtered = _lowpass(iq, sample_rate_hz, bw / 2)
        audio = np.angle(filtered[1:] * np.conj(filtered[:-1]))

        # DC-block the discriminator output: a frequency offset between the
        # signal and baseband shows up as a constant bias in the FM
        # discriminator output, which sounds like a loud constant tone.
        sos_dc = scipy_signal.butter(2, 50, btype="high", fs=sample_rate_hz, output="sos")
        audio = scipy_signal.sosfilt(sos_dc, audio)

        # Drop the filter startup transient, which can otherwise dominate
        # peak-based normalization.
        trim = int(0.05 * sample_rate_hz)
        if audio.size > 2 * trim:
            audio = audio[trim:]

        # De-emphasis: undo transmitter pre-emphasis (high frequencies
        # boosted before transmission) via a one-pole RC low-pass.
        tau = DEEMPHASIS_TAU_S.get(mode)
        if tau:
            alpha = (1.0 / sample_rate_hz) / (tau + 1.0 / sample_rate_hz)
            audio = scipy_signal.lfilter([alpha], [1, -(1 - alpha)], audio)

        # Audio low-pass: the FM discriminator's noise power rises with
        # frequency, so without this the audio band is swamped by
        # out-of-band noise.
        audio_lpf = AUDIO_LPF_HZ[mode]
        sos_audio = scipy_signal.butter(6, audio_lpf, btype="low", fs=sample_rate_hz, output="sos")
        audio = scipy_signal.sosfilt(sos_audio, audio)

    if denoise:
        audio = spectral_denoise(audio, sample_rate_hz)

    return audio


def spectral_denoise(
    audio: np.ndarray,
    sample_rate_hz: float,
    reduction_db: float = 12.0,
    noise_seconds: float = 1.0,
    nperseg: int = 1024,
) -> np.ndarray:
    """Reduce steady background noise via STFT spectral subtraction, using
    the first `noise_seconds` of audio as the noise profile."""
    if audio.size < nperseg:
        return audio

    noverlap = nperseg // 2
    hop = nperseg - noverlap
    freqs, times, z = scipy_signal.stft(audio, fs=sample_rate_hz, nperseg=nperseg, noverlap=noverlap)

    mag = np.abs(z)
    phase = np.angle(z)

    noise_frames = max(1, min(int(noise_seconds * sample_rate_hz / hop), mag.shape[1]))
    noise_profile = np.mean(mag[:, :noise_frames], axis=1, keepdims=True)

    floor = 10 ** (-reduction_db / 20)
    mag_denoised = np.maximum(mag - noise_profile, mag * floor)

    z_denoised = mag_denoised * np.exp(1j * phase)
    _, denoised = scipy_signal.istft(z_denoised, fs=sample_rate_hz, nperseg=nperseg, noverlap=noverlap)

    denoised = np.real(denoised)
    if denoised.size >= audio.size:
        return denoised[: audio.size]
    return np.pad(denoised, (0, audio.size - denoised.size))


def resample_to_rate(audio: np.ndarray, in_rate_hz: float, out_rate_hz: float) -> np.ndarray:
    if in_rate_hz == out_rate_hz:
        return audio
    frac = Fraction(out_rate_hz / in_rate_hz).limit_denominator(1000)
    return scipy_signal.resample_poly(audio, frac.numerator, frac.denominator)


def write_wav(path: str | Path, audio: np.ndarray, sample_rate_hz: int) -> None:
    # Normalize by a high percentile rather than the raw max, so that brief
    # transient spikes (e.g. filter startup) don't crush the overall volume.
    ref = np.percentile(np.abs(audio), 99.5) + 1e-9 if audio.size else 1.0
    pcm = np.clip(audio / ref * 32767, -32768, 32767).astype(np.int16)
    wavfile.write(str(path), int(sample_rate_hz), pcm)


def compute_spectrogram(
    iq: np.ndarray,
    sample_rate_hz: float,
    fft_size: int = 1024,
    overlap: float = 0.5,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (freqs_hz, times_s, power_db) with freqs centered on 0 Hz."""
    noverlap = int(fft_size * overlap)
    freqs, times, sxx = scipy_signal.spectrogram(
        iq,
        fs=sample_rate_hz,
        nperseg=fft_size,
        noverlap=noverlap,
        return_onesided=False,
        scaling="spectrum",
        mode="magnitude",
        window="hann",
    )
    freqs = np.fft.fftshift(freqs)
    sxx = np.fft.fftshift(sxx, axes=0)
    power_db = 20 * np.log10(sxx + 1e-12)
    return freqs, times, power_db


def compute_spectrum(
    iq: np.ndarray,
    sample_rate_hz: float,
    fft_size: int = 4096,
) -> tuple[np.ndarray, np.ndarray]:
    """Return (freqs_hz, power_db): the average power spectrum across the
    whole capture, with freqs centered on 0 Hz."""
    noverlap = fft_size // 2 if iq.size > fft_size else 0
    nperseg = min(fft_size, iq.size)
    freqs, _, sxx = scipy_signal.spectrogram(
        iq,
        fs=sample_rate_hz,
        nperseg=nperseg,
        noverlap=noverlap,
        return_onesided=False,
        scaling="spectrum",
        mode="magnitude",
        window="hann",
    )
    freqs = np.fft.fftshift(freqs)
    sxx = np.fft.fftshift(sxx, axes=0)
    power_db = 20 * np.log10(sxx.mean(axis=1) + 1e-12)
    return freqs, power_db


def file_power_spectrum(
    path: str | Path,
    sample_rate_hz: float,
    fft_size: int = 4096,
    num_segments: int = 16,
    segment_samples: int = 1 << 18,
) -> tuple[np.ndarray, np.ndarray]:
    """Return (freqs_hz, power) for an int8 IQ file: the linear power
    spectrum averaged over `num_segments` evenly spaced segments, so a long
    file is sampled from start to end without being read in full. freqs are
    centered on 0 Hz. Both arrays are empty for an empty file."""
    raw = np.memmap(path, dtype=np.int8, mode="r") if Path(path).stat().st_size else np.zeros(0, np.int8)
    total = raw.size // 2
    if total == 0:
        return np.zeros(0), np.zeros(0)
    seg_len = min(segment_samples, total)
    nperseg = min(fft_size, seg_len)
    starts = np.unique(np.linspace(0, total - seg_len, num=num_segments).astype(np.int64))
    acc = None
    for start in starts:
        seg = np.asarray(raw[2 * start : 2 * (start + seg_len)], dtype=np.float32) / 128.0
        x = seg[0::2] + 1j * seg[1::2]
        freqs, pxx = scipy_signal.welch(
            x,
            fs=sample_rate_hz,
            nperseg=nperseg,
            return_onesided=False,
            scaling="spectrum",
            window="hann",
        )
        acc = pxx if acc is None else acc + pxx
    return np.fft.fftshift(freqs), np.fft.fftshift(acc / len(starts))


def power_bandwidth(
    freqs_hz: np.ndarray,
    power: np.ndarray,
    fraction: float = 0.99,
) -> tuple[float, float] | None:
    """Edges (low_hz, high_hz) of the band holding `fraction` of the total
    power, cutting (1 - fraction) / 2 from each side (the usual definition
    of occupied bandwidth). None if the spectrum holds no power."""
    total = float(np.sum(power))
    if freqs_hz.size == 0 or total <= 0:
        return None
    cumulative = np.cumsum(power) / total
    tail = (1.0 - fraction) / 2
    low_idx = int(np.searchsorted(cumulative, tail))
    high_idx = int(np.searchsorted(cumulative, 1.0 - tail))
    high_idx = min(high_idx, freqs_hz.size - 1)
    bin_hz = float(freqs_hz[1] - freqs_hz[0]) if freqs_hz.size > 1 else 0.0
    # Widen by half a bin on each side: a bin covers +-bin/2 around its center.
    return float(freqs_hz[low_idx]) - bin_hz / 2, float(freqs_hz[high_idx]) + bin_hz / 2


def occupied_bandwidth(
    freqs_hz: np.ndarray,
    power_db: np.ndarray,
    threshold_db: float = 6.0,
) -> tuple[float, float, float]:
    """Estimate the occupied frequency span as the range of frequencies
    within `threshold_db` of the peak power. Returns (low_hz, high_hz,
    bandwidth_hz), relative to whatever frame `freqs_hz` is in."""
    peak = power_db.max()
    mask = power_db >= (peak - threshold_db)
    occ_freqs = freqs_hz[mask]
    low, high = occ_freqs.min(), occ_freqs.max()
    return float(low), float(high), float(high - low)


def parse_sweep_csv(data: str) -> tuple[np.ndarray, np.ndarray]:
    """Parse hackrf_sweep -r CSV output into (freqs_hz, power_db), sorted by
    frequency. Each row has the form:
    date,time,hz_low,hz_high,hz_bin_width,num_samples,dB,dB,..."""
    freqs: list[float] = []
    powers: list[float] = []
    for line in data.splitlines():
        parts = line.strip().split(",")
        if len(parts) < 7:
            continue
        try:
            hz_low = float(parts[2])
            hz_bin_width = float(parts[4])
            db_values = [float(x) for x in parts[6:]]
        except ValueError:
            continue
        for i, db in enumerate(db_values):
            freqs.append(hz_low + (i + 0.5) * hz_bin_width)
            powers.append(db)

    freqs_arr = np.array(freqs, dtype=np.float64)
    powers_arr = np.array(powers, dtype=np.float64)
    order = np.argsort(freqs_arr)
    return freqs_arr[order], powers_arr[order]


def render_sweep_png(
    path: str | Path,
    freqs_hz: np.ndarray,
    power_db: np.ndarray,
    title: str | None = None,
) -> None:
    plt = _pyplot()
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(freqs_hz / 1e6, power_db, linewidth=0.7)
    ax.set_xlabel("Frequency (MHz)")
    ax.set_ylabel("Power (dB)")
    ax.grid(True, alpha=0.3)
    if title:
        ax.set_title(title)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def detect_bursts(
    iq: np.ndarray,
    sample_rate_hz: float,
    threshold_db: float = 10.0,
    smoothing_s: float = 0.0005,
    min_duration_s: float = 0.0005,
    merge_gap_s: float = 0.001,
    max_bursts: int = 200,
) -> list[dict[str, float]]:
    """Detect bursts of elevated power in an IQ capture using a smoothed
    power envelope, relative to the capture's median (noise floor) power.

    Returns a list of dicts with start_s, end_s, duration_s, peak_db, and
    peak_freq_offset_hz (the dominant frequency within the burst, relative to
    baseband / center_freq_hz)."""
    if iq.size == 0:
        return []

    window = max(1, int(sample_rate_hz * smoothing_s))
    power = np.abs(iq) ** 2
    if window > 1:
        kernel = np.ones(window) / window
        power = np.convolve(power, kernel, mode="same")
    power_db = 10 * np.log10(power + 1e-12)

    noise_floor_db = float(np.median(power_db))
    mask = power_db >= (noise_floor_db + threshold_db)

    edges = np.diff(np.concatenate([[0], mask.astype(np.int8), [0]]))
    spans = list(zip(np.flatnonzero(edges == 1).tolist(), np.flatnonzero(edges == -1).tolist()))

    merge_samples = int(sample_rate_hz * merge_gap_s)
    merged: list[list[int]] = []
    for span in spans:
        if merged and span[0] - merged[-1][1] <= merge_samples:
            merged[-1][1] = span[1]
        else:
            merged.append(list(span))

    min_samples = sample_rate_hz * min_duration_s
    results: list[dict[str, float]] = []
    for start, end in merged:
        if (end - start) < min_samples:
            continue

        seg = iq[start:end]
        peak_freq_offset_hz = 0.0
        if seg.size >= 8:
            spec = np.fft.fftshift(np.fft.fft(seg))
            freqs = np.fft.fftshift(np.fft.fftfreq(seg.size, 1 / sample_rate_hz))
            peak_freq_offset_hz = float(freqs[np.argmax(np.abs(spec))])

        results.append(
            {
                "start_s": start / sample_rate_hz,
                "end_s": end / sample_rate_hz,
                "duration_s": (end - start) / sample_rate_hz,
                "peak_db": float(power_db[start:end].max()),
                "peak_freq_offset_hz": peak_freq_offset_hz,
            }
        )
        if len(results) >= max_bursts:
            break

    return results


def render_spectrogram_png(
    path: str | Path,
    freqs_hz: np.ndarray,
    times_s: np.ndarray,
    power_db: np.ndarray,
    center_freq_hz: float,
    title: str | None = None,
) -> None:
    # The figure is about 1000 px tall, so more frequency rows than that
    # only cost memory; keep each group's peak.
    if freqs_hz.size > 1024:
        group = -(-freqs_hz.size // 1024)
        rows = freqs_hz.size // group
        freqs_hz = freqs_hz[: rows * group].reshape(rows, group).mean(axis=1)
        power_db = power_db[: rows * group].reshape(rows, group, -1).max(axis=1)
    plt = _pyplot()
    fig, ax = plt.subplots(figsize=(10, 6))
    freqs_mhz = (freqs_hz + center_freq_hz) / 1e6
    mesh = ax.pcolormesh(times_s, freqs_mhz, power_db, shading="auto", cmap="viridis")
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Frequency (MHz)")
    if title:
        ax.set_title(title)
    fig.colorbar(mesh, ax=ax, label="Power (dB)")
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


# --- Streaming helpers ------------------------------------------------------
#
# Captures can be gigabytes, and the server may run on a small box: a 60 s
# capture at 2 MS/s is 120M samples, ~1 GB as complex64 and several GB once
# scipy has made its intermediates. Everything below reads the file in
# fixed-size chunks through a memmap, so memory stays bounded by the chunk
# size no matter how long the capture is.

CHUNK_SAMPLES = 1 << 20


def iq_file_samples(path: str | Path) -> int:
    """Number of complex samples in an int8 IQ file."""
    return Path(path).stat().st_size // 2


def iter_iq_chunks(
    path: str | Path,
    chunk_samples: int = CHUNK_SAMPLES,
    max_samples: int | None = None,
):
    """Yield successive complex64 chunks (scaled to roughly [-1, 1]) of an
    int8 IQ file, reading at most `max_samples` samples in total."""
    total = iq_file_samples(path)
    if max_samples is not None:
        total = min(total, max_samples)
    if total == 0:
        return
    raw = np.memmap(path, dtype=np.int8, mode="r", shape=(2 * total,))
    for start in range(0, total, chunk_samples):
        seg = np.asarray(raw[2 * start : 2 * min(start + chunk_samples, total)], dtype=np.float32)
        seg *= 1.0 / 128.0
        yield seg.view(np.complex64)


def iter_cu8_chunks(path: str | Path, chunk_samples: int = CHUNK_SAMPLES, max_samples: int | None = None):
    """Yield the file's int8 IQ bytes converted to unsigned 8-bit (cu8, as
    rtl_sdr writes it), the input format of dump1090 and dumpvdl2. The
    conversion is a flip of each byte's top bit."""
    total = iq_file_samples(path)
    if max_samples is not None:
        total = min(total, max_samples)
    if total == 0:
        return
    raw = np.memmap(path, dtype=np.uint8, mode="r", shape=(2 * total,))
    for start in range(0, total, chunk_samples):
        yield np.bitwise_xor(raw[2 * start : 2 * min(start + chunk_samples, total)], 0x80).tobytes()


def cs8_to_complex(buf: bytes) -> np.ndarray:
    """Complex64 samples (scaled to roughly [-1, 1]) from a block of int8 I/Q
    bytes as hackrf_transfer writes them. The block must hold whole samples."""
    seg = np.frombuffer(buf, dtype=np.int8).astype(np.float32)
    seg *= 1.0 / 128.0
    return seg.view(np.complex64)


def cs8_to_cu8(buf: bytes) -> bytes:
    """int8 I/Q bytes as unsigned 8-bit (cu8), by flipping each top bit."""
    return np.bitwise_xor(np.frombuffer(buf, dtype=np.uint8), 0x80).tobytes()


def complex_to_cu8(x: np.ndarray, scale: float = 127.0) -> bytes:
    """Interleave complex samples (roughly [-1, 1]) as unsigned 8-bit I/Q."""
    out = np.empty(2 * x.size, dtype=np.float32)
    out[0::2] = x.real
    out[1::2] = x.imag
    return np.clip(np.rint(out * scale + 127.5), 0, 255).astype(np.uint8).tobytes()


def complex_to_cs8(x: np.ndarray, scale: float = 127.0) -> bytes:
    """Interleave complex samples (roughly [-1, 1]) as signed 8-bit I/Q."""
    out = np.empty(2 * x.size, dtype=np.float32)
    out[0::2] = x.real
    out[1::2] = x.imag
    return np.clip(np.rint(out * scale), -127, 127).astype(np.int8).tobytes()


class Mixer:
    """Frequency shift that keeps its phase from one chunk to the next."""

    def __init__(self, sample_rate_hz: float, shift_hz: float):
        self.step = -2 * np.pi * shift_hz / sample_rate_hz
        self.phase = 0.0

    def __call__(self, x: np.ndarray) -> np.ndarray:
        if self.step == 0 or x.size == 0:
            return x
        # Phase in float64 (it grows over a chunk), wrapped, then the
        # rotation itself in single precision, which is plenty for 8-bit
        # samples and much cheaper.
        ph = np.mod(self.phase + self.step * np.arange(x.size, dtype=np.float64), 2 * np.pi).astype(np.float32)
        self.phase = float((self.phase + self.step * x.size) % (2 * np.pi))
        rot = np.empty(x.size, dtype=np.complex64)
        rot.real = np.cos(ph)
        rot.imag = np.sin(ph)
        return x * rot


def _decimation_stages(factor: int) -> list[int]:
    """Split an integer decimation factor into stages of at most 10, so no
    single filter needs thousands of taps."""
    stages = []
    remaining = factor
    while remaining > 1:
        for d in range(min(10, remaining), 1, -1):
            if remaining % d == 0:
                stages.append(d)
                remaining //= d
                break
        else:
            # A prime factor above 10: take it in one stage.
            stages.append(remaining)
            remaining = 1
    return stages


class _DecimatorStage:
    """One FIR low-pass + decimate-by-`factor` stage. It computes only the
    samples it keeps (polyphase, via upfirdn), and carries the last
    len(taps) - 1 input samples and its sample phase into the next chunk,
    so the output is the same as filtering the whole capture at once."""

    TAPS_PER_PHASE = 10

    def __init__(self, factor: int, cutoff: float):
        self.factor = factor
        # len(taps) - 1 is a multiple of factor, which keeps the output
        # indexing below simple.
        self.taps = scipy_signal.firwin(self.TAPS_PER_PHASE * factor + 1, cutoff / factor).astype(np.float32)
        self.history = np.zeros(self.taps.size - 1, dtype=np.complex64)
        self.offset = 0  # index into the next chunk of the next sample to keep

    def __call__(self, x: np.ndarray) -> np.ndarray:
        n, f, o = x.size, self.factor, self.offset
        ext = np.concatenate([self.history, x])
        count = max(0, -(-(n - o) // f))
        # upfirdn's output i is the filter at ext[o + i*f]; the first output
        # that lands on x itself is i = (len(taps) - 1) / f.
        first = self.TAPS_PER_PHASE
        y = scipy_signal.upfirdn(self.taps, ext[o:], 1, f)[first : first + count]
        self.history = ext[ext.size - (self.taps.size - 1) :]
        self.offset = (o - n) % f
        return y.astype(np.complex64, copy=False)


class Decimator:
    """Low-pass filter and integer decimation that carries its filter state
    and sample phase from one chunk to the next, so chunked output matches
    processing the whole capture at once. `bandwidth` is the fraction of the
    output Nyquist band kept (0-1)."""

    def __init__(self, factor: int, bandwidth: float = 0.8):
        if factor < 1:
            raise ValueError("decimation factor must be at least 1")
        self.factor = factor
        stages = _decimation_stages(factor)
        # Earlier stages only need to protect the final band from aliasing,
        # so they can be wider; the last stage sets the bandwidth.
        self.stages = [
            _DecimatorStage(d, bandwidth if i == len(stages) - 1 else 0.9) for i, d in enumerate(stages)
        ]

    def __call__(self, x: np.ndarray) -> np.ndarray:
        for stage in self.stages:
            x = stage(x)
        return x


def channelize(
    path: str | Path,
    sample_rate_hz: int,
    shift_hz: float,
    decimation: int,
    max_samples: int | None = None,
    bandwidth: float = 0.8,
    chunk_samples: int = CHUNK_SAMPLES,
):
    """Yield chunks of the capture mixed down by `shift_hz` (so a signal at
    center + shift_hz lands on 0 Hz) and decimated by `decimation`."""
    mixer = Mixer(sample_rate_hz, shift_hz)
    decimator = Decimator(decimation, bandwidth) if decimation > 1 else None
    for chunk in iter_iq_chunks(path, chunk_samples, max_samples):
        y = mixer(chunk)
        if decimator is not None:
            y = decimator(y)
        if y.size:
            yield y


class FFTChannelizer:
    """Several narrow channels out of one wideband stream at once, by
    overlap-save fast convolution: one forward FFT per block, shared by all
    channels, then a small inverse FFT per channel that keeps only the bins
    around it. A Mixer and Decimator per channel costs a full-rate pass for
    every channel; this costs about one, however many channels there are.

    Channel offsets are rounded to `bin_hz`, so the sample rate must be a
    multiple of it, and `decimation` must divide sample_rate_hz / bin_hz.
    Each channel's output is continuous from block to block (the mixer
    phase is carried), so it can be FM-demodulated. Call it with successive
    complex64 chunks of any size; it returns one array per channel, empty
    until a whole block has arrived.
    """

    def __init__(
        self,
        sample_rate_hz: int,
        offsets_hz: list[float] | tuple[float, ...],
        decimation: int,
        bandwidth_hz: float,
        bin_hz: int = 25,
    ):
        if sample_rate_hz % bin_hz:
            raise ValueError(f"sample rate {sample_rate_hz} is not a multiple of {bin_hz} Hz")
        n = sample_rate_hz // bin_hz
        if n % decimation:
            raise ValueError(f"decimation {decimation} does not divide the {n}-point block")
        out_rate = sample_rate_hz / decimation
        if bandwidth_hz >= out_rate:
            raise ValueError(f"bandwidth {bandwidth_hz:g} Hz does not fit the {out_rate:g} S/s output")
        self.sample_rate_hz = sample_rate_hz
        self.n = n
        self.decimation = decimation
        self.m = n // decimation
        # The overlap holds the filter's memory (its length - 1); about a
        # tenth of the block gives a transition band near 1 kHz.
        self.overlap = decimation * max(4, -(-n // (10 * decimation)))
        self.step = n - self.overlap
        taps = scipy_signal.firwin(
            self.overlap + 1, bandwidth_hz / 2, fs=sample_rate_hz, window=("kaiser", 8.0)
        )
        response = np.fft.fft(taps, n)
        j = np.fft.fftfreq(self.m, 1.0 / self.m).astype(np.int64)  # bins in ifft order
        self.bins = [int(round(f / bin_hz)) for f in offsets_hz]
        self.offsets_hz = [k * bin_hz for k in self.bins]
        self._index = [(k + j) % n for k in self.bins]
        self._response = (response[j % n] / decimation).astype(np.complex64)
        self._history = np.zeros(self.overlap, dtype=np.complex64)
        self._pending: list[np.ndarray] = []
        self._pending_samples = 0
        self._block = 0
        # Mean power of each channel's last block, in dB relative to full scale.
        self.power_db = [float("-inf")] * len(self.bins)

    def __call__(self, x: np.ndarray) -> list[np.ndarray]:
        if x.size:
            self._pending.append(x.astype(np.complex64, copy=False))
            self._pending_samples += x.size
        outs: list[list[np.ndarray]] = [[] for _ in self.bins]
        if self._pending_samples < self.step:
            return [np.zeros(0, np.complex64) for _ in self.bins]
        data = np.concatenate(self._pending)
        used = 0
        keep = self.overlap // self.decimation
        while data.size - used >= self.step:
            block = np.concatenate([self._history, data[used : used + self.step]])
            used += self.step
            spectrum = np.fft.fft(block)
            start = self._block * self.step - self.overlap
            for i, (k, idx) in enumerate(zip(self.bins, self._index)):
                y = np.fft.ifft(spectrum[idx] * self._response)[keep:]
                # The bins were taken relative to this block's first sample;
                # rotate to the stream's phase so blocks join seamlessly.
                turn = (k * start) % self.n
                y = (y * np.complex64(np.exp(-2j * np.pi * turn / self.n))).astype(np.complex64)
                outs[i].append(y)
                p = float(np.mean(np.abs(y) ** 2)) if y.size else 0.0
                self.power_db[i] = 10 * np.log10(p) if p > 0 else float("-inf")
            self._history = block[-self.overlap :]
            self._block += 1
        rest = data[used:]
        self._pending = [rest] if rest.size else []
        self._pending_samples = rest.size
        return [np.concatenate(o) for o in outs]


def channelize_to_array(
    path: str | Path,
    sample_rate_hz: int,
    shift_hz: float,
    decimation: int,
    max_samples: int | None = None,
    bandwidth: float = 0.8,
) -> np.ndarray:
    parts = list(channelize(path, sample_rate_hz, shift_hz, decimation, max_samples, bandwidth))
    return np.concatenate(parts) if parts else np.zeros(0, np.complex64)


def pick_decimation(sample_rate_hz: int, min_out_rate_hz: float) -> int:
    """Largest integer factor that divides the sample rate exactly and
    leaves at least `min_out_rate_hz`."""
    best = 1
    for d in range(1, int(sample_rate_hz // max(min_out_rate_hz, 1)) + 1):
        if sample_rate_hz % d == 0:
            best = d
    return best


def fm_discriminate(x: np.ndarray, prev: complex | None = None) -> tuple[np.ndarray, complex]:
    """Quadrature FM discriminator (phase step per sample, radians). Pass the
    previous chunk's last sample as `prev` to keep chunks continuous."""
    if x.size == 0:
        return np.zeros(0, np.float32), prev if prev is not None else 0j
    ext = x if prev is None else np.concatenate([[np.complex64(prev)], x])
    out = np.angle(ext[1:] * np.conj(ext[:-1])).astype(np.float32)
    if prev is None:
        out = np.concatenate([[np.float32(0)], out])
    return out, complex(x[-1])


def stream_spectrogram(
    path: str | Path,
    sample_rate_hz: float,
    fft_size: int = 1024,
    max_samples: int | None = None,
    max_time_bins: int = 1200,
    reduce: Literal["max", "mean"] = "max",
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Spectrogram of an int8 IQ file in bounded memory: the capture is cut
    into at most `max_time_bins` columns and each column holds the max (or
    mean) power of the non-overlapping FFT frames inside it. Max keeps short
    bursts visible however long the capture is. Returns (freqs_hz, times_s,
    power_db) with freqs centered on 0 Hz, like compute_spectrogram."""
    total = iq_file_samples(path)
    if max_samples is not None:
        total = min(total, max_samples)
    n_frames = total // fft_size
    if n_frames == 0:
        return np.zeros(0), np.zeros(0), np.zeros((0, 0))
    n_bins = min(max_time_bins, n_frames)
    window = scipy_signal.get_window("hann", fft_size).astype(np.float32)
    window /= window.sum()
    acc = np.full((n_bins, fft_size), -np.inf if reduce == "max" else 0.0, dtype=np.float64)
    counts = np.zeros(n_bins, dtype=np.int64)
    frames_per_chunk = max(1, CHUNK_SAMPLES // fft_size)
    frame_idx = 0
    for chunk in iter_iq_chunks(path, frames_per_chunk * fft_size, n_frames * fft_size):
        nf = chunk.size // fft_size
        if nf == 0:
            break
        frames = chunk[: nf * fft_size].reshape(nf, fft_size) * window
        power = np.abs(np.fft.fft(frames, axis=1)) ** 2
        cols = (np.arange(frame_idx, frame_idx + nf) * n_bins) // n_frames
        if reduce == "max":
            np.maximum.at(acc, cols, power)
        else:
            np.add.at(acc, cols, power)
        np.add.at(counts, cols, 1)
        frame_idx += nf
    if reduce == "mean":
        acc /= np.maximum(counts, 1)[:, None]
    freqs = np.fft.fftshift(np.fft.fftfreq(fft_size, 1 / sample_rate_hz))
    power_db = 10 * np.log10(np.fft.fftshift(acc, axes=1).T + 1e-12)
    edges = (np.arange(n_bins + 1) * n_frames) // n_bins
    times = (edges[:-1] + edges[1:]) / 2 * fft_size / sample_rate_hz
    return freqs, times, power_db


def stream_stats(
    path: str | Path,
    sample_rate_hz: float,
    fft_size: int = 4096,
    max_samples: int | None = None,
) -> tuple[int, float, np.ndarray, np.ndarray]:
    """Return (samples_read, mean_power_db, freqs_hz, power_db): the mean
    power and the average power spectrum over the whole (capped) capture,
    read in chunks."""
    window = scipy_signal.get_window("hann", fft_size).astype(np.float32)
    window /= window.sum()
    spec = np.zeros(fft_size, dtype=np.float64)
    frames = 0
    power_sum = 0.0
    n = 0
    tail = np.zeros(0, np.complex64)
    for chunk in iter_iq_chunks(path, max_samples=max_samples):
        n += chunk.size
        power_sum += float(np.sum(chunk.real.astype(np.float64) ** 2 + chunk.imag.astype(np.float64) ** 2))
        x = np.concatenate([tail, chunk]) if tail.size else chunk
        nf = x.size // fft_size
        if nf:
            f = x[: nf * fft_size].reshape(nf, fft_size) * window
            spec += np.sum(np.abs(np.fft.fft(f, axis=1)) ** 2, axis=0)
            frames += nf
        tail = x[nf * fft_size :].copy()
    if n == 0:
        return 0, float("-inf"), np.zeros(0), np.zeros(0)
    if frames == 0:
        # Shorter than one FFT: use what there is.
        f = np.fft.fft(tail * scipy_signal.get_window("hann", tail.size) / max(tail.size, 1))
        spec, frames, fft_size = np.abs(f) ** 2, 1, tail.size
    freqs = np.fft.fftshift(np.fft.fftfreq(fft_size, 1 / sample_rate_hz))
    power_db = 10 * np.log10(np.fft.fftshift(spec / frames) + 1e-12)
    return n, float(10 * np.log10(power_sum / n + 1e-12)), freqs, power_db


def detect_bursts_file(
    path: str | Path,
    sample_rate_hz: int,
    threshold_db: float = 10.0,
    min_duration_s: float = 0.0005,
    merge_gap_s: float = 0.001,
    smoothing_s: float | None = None,
    max_bursts: int = 200,
    max_samples: int | None = None,
    channel_offset_hz: float | None = None,
    channel_bandwidth_hz: float | None = None,
) -> tuple[list[dict[str, float]], int, float]:
    """Burst detection over a whole int8 IQ file in bounded memory.

    With `channel_offset_hz` and `channel_bandwidth_hz`, only that channel is
    examined (mixed to 0 Hz, filtered and decimated first), so a narrow
    signal such as a 25 kHz AIS burst stands out from the noise of the whole
    capture bandwidth.

    The power envelope is smoothed over `smoothing_s` (default: half of
    `min_duration_s`, at most 0.5 ms), so reported start and end times are
    accurate to about that much. The threshold is relative to the median
    smoothed power, estimated as the median of per-chunk medians.

    Returns (bursts, samples_read, analysed_rate_hz). Each burst has
    start_s, end_s, duration_s, peak_db and peak_freq_offset_hz (relative to
    center_freq_hz, i.e. including channel_offset_hz)."""
    if channel_bandwidth_hz:
        offset = channel_offset_hz or 0.0
        dec = pick_decimation(sample_rate_hz, 2.5 * channel_bandwidth_hz)
        bw = min(0.95, channel_bandwidth_hz / (sample_rate_hz / dec))
        source = channelize(path, sample_rate_hz, offset, dec, max_samples, bandwidth=bw)
        rate = sample_rate_hz / dec
    else:
        offset = 0.0
        source = iter_iq_chunks(path, max_samples=max_samples)
        rate = float(sample_rate_hz)

    if smoothing_s is None:
        smoothing_s = min(0.0005, min_duration_s / 2)
    window = max(1, int(round(rate * smoothing_s)))
    delay = (window - 1) // 2

    # Pass 1 keeps the smoothed envelope per chunk only long enough to take
    # its median; pass 2 (below) recomputes it. Two passes over the file
    # cost less than holding the envelope of a long capture in memory.
    def envelopes(chunks):
        carry = np.zeros(window - 1, dtype=np.float64)
        pos = 0
        for x in chunks:
            p = x.real.astype(np.float64) ** 2 + x.imag.astype(np.float64) ** 2
            ext = np.concatenate([carry, p])
            c = np.cumsum(np.concatenate([[0.0], ext]))
            smooth = (c[window:] - c[:-window]) / window
            carry = ext[ext.size - (window - 1) :] if window > 1 else carry
            yield pos, x, 10 * np.log10(smooth + 1e-12)
            pos += x.size

    medians = []
    samples_read = 0
    for _, x, env in envelopes(source):
        medians.append(float(np.median(env)))
        samples_read += x.size
    if not medians:
        return [], 0, rate
    floor_db = float(np.median(medians))
    level = floor_db + threshold_db

    if channel_bandwidth_hz:
        source = channelize(path, sample_rate_hz, offset, dec, max_samples, bandwidth=bw)
    else:
        source = iter_iq_chunks(path, max_samples=max_samples)

    spans: list[list] = []  # [start, end, peak_db, freq_offset]
    open_start: int | None = None
    open_peak = -np.inf
    open_samples: list[np.ndarray] = []
    max_fft = 1 << 16

    def dominant_freq(parts: list[np.ndarray]) -> float:
        seg = np.concatenate(parts)[:max_fft] if parts else np.zeros(0)
        if seg.size < 8:
            return 0.0
        spec = np.abs(np.fft.fft(seg))
        freqs = np.fft.fftfreq(seg.size, 1 / rate)
        return float(freqs[np.argmax(spec)])

    for pos, x, env in envelopes(source):
        mask = env >= level
        d = np.diff(np.concatenate([[0], mask.astype(np.int8), [0]]))
        starts = np.flatnonzero(d == 1)
        ends = np.flatnonzero(d == -1)
        idx = 0
        # A burst still open from the previous chunk continues if this
        # chunk starts above the threshold.
        if open_start is not None:
            if mask.size and mask[0]:
                end = ends[0]
                open_peak = max(open_peak, float(env[:end].max()))
                if sum(p.size for p in open_samples) < max_fft:
                    open_samples.append(x[:end])
                if end < mask.size:
                    spans.append([open_start, pos + end, open_peak, dominant_freq(open_samples)])
                    open_start = None
                    open_samples = []
                idx = 1
            else:
                spans.append([open_start, pos, open_peak, dominant_freq(open_samples)])
                open_start = None
                open_samples = []
        for s, e in zip(starts[idx:], ends[idx:]):
            if e == mask.size:
                open_start, open_peak = pos + s, float(env[s:e].max())
                open_samples = [x[s:e]]
            else:
                spans.append([pos + s, pos + e, float(env[s:e].max()), dominant_freq([x[s:e]])])
        if len(spans) > 50 * max_bursts:
            break
    if open_start is not None:
        spans.append([open_start, samples_read, open_peak, dominant_freq(open_samples)])

    merge = int(rate * merge_gap_s)
    merged: list[list] = []
    for span in spans:
        if merged and span[0] - merged[-1][1] <= merge:
            last = merged[-1]
            last[1] = span[1]
            if span[2] > last[2]:
                last[2], last[3] = span[2], span[3]
        else:
            merged.append(list(span))

    min_samples = rate * min_duration_s
    results = []
    for start, end, peak_db, freq in merged:
        if end - start < min_samples:
            continue
        # The envelope is a trailing average; shift by its group delay so
        # the edges land where the burst starts and ends.
        start_s = max(0, start - delay) / rate
        end_s = max(0, end - delay) / rate
        results.append(
            {
                "start_s": start_s,
                "end_s": end_s,
                "duration_s": end_s - start_s,
                "peak_db": peak_db,
                "peak_freq_offset_hz": freq + offset,
            }
        )
        if len(results) >= max_bursts:
            break
    return results, samples_read, rate
