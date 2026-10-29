"""Offline analysis helpers for HackRF IQ capture files (numpy/scipy/matplotlib).

These operate purely on files already written by hackrf_transfer (-r) and do
not touch the device.
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path
from typing import Literal

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
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
    taps = scipy_signal.firwin(numtaps, cutoff / nyq)
    return scipy_signal.fftconvolve(iq, taps, mode="same")


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

    spans: list[tuple[int, int]] = []
    start_idx = 0
    in_burst = False
    for i, m in enumerate(mask):
        if m and not in_burst:
            start_idx = i
            in_burst = True
        elif not m and in_burst:
            spans.append((start_idx, i))
            in_burst = False
    if in_burst:
        spans.append((start_idx, len(mask)))

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
