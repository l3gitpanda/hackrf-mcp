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
    "nfm": 12_500,
    "wfm": 180_000,
    "am": 10_000,
    "usb": 2_900,
    "lsb": 2_900,
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
) -> np.ndarray:
    """Demodulate a baseband-centered IQ array to a real-valued audio signal
    (still at sample_rate_hz; caller is responsible for resampling)."""
    bw = bandwidth_hz or MODE_BANDWIDTH_HZ[mode]

    if mode == "am":
        filtered = _lowpass(iq, sample_rate_hz, bw / 2)
        audio = np.abs(filtered)
        return audio - np.mean(audio)

    if mode in ("usb", "lsb"):
        # Shift the desired sideband toward baseband, low-pass to its
        # bandwidth, then take the real part (phasing-method approximation).
        shift = bw / 2 if mode == "usb" else -bw / 2
        shifted = mix(iq, sample_rate_hz, shift)
        filtered = _lowpass(shifted, sample_rate_hz, bw / 2)
        return np.real(filtered)

    # nfm / wfm: quadrature (phase-difference) FM discriminator.
    filtered = _lowpass(iq, sample_rate_hz, bw / 2)
    audio = np.angle(filtered[1:] * np.conj(filtered[:-1]))
    return audio


def resample_to_rate(audio: np.ndarray, in_rate_hz: float, out_rate_hz: float) -> np.ndarray:
    if in_rate_hz == out_rate_hz:
        return audio
    frac = Fraction(out_rate_hz / in_rate_hz).limit_denominator(1000)
    return scipy_signal.resample_poly(audio, frac.numerator, frac.denominator)


def write_wav(path: str | Path, audio: np.ndarray, sample_rate_hz: int) -> None:
    peak = np.max(np.abs(audio)) if audio.size else 0.0
    scale = 0.95 / peak if peak > 0 else 1.0
    pcm = np.clip(audio * scale * 32767, -32768, 32767).astype(np.int16)
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
