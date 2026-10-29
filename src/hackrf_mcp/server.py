"""MCP server wrapping the hackrf_tools CLI for controlling a HackRF SDR."""

from __future__ import annotations

import argparse
import os
import shutil
import tempfile
import threading
from dataclasses import replace
from pathlib import Path
from typing import Literal

import numpy as np
from fastmcp import FastMCP
from fastmcp.utilities.types import Audio, Image
from mcp.types import ToolAnnotations

from hackrf_mcp import __version__, iq, policy, settings, storage
from hackrf_mcp.cli import run, set_bin_dir

mcp = FastMCP(
    "hackrf",
    instructions=(
        "Tools for controlling a HackRF SDR via hackrf_tools.\n\n"
        "IMPORTANT: a HackRF device can only be accessed by one process at a "
        "time (it opens an exclusive USB connection). Only call one of "
        "hackrf_info, hackrf_sweep, hackrf_transfer, hackrf_clock, "
        "hackrf_operacake, or hackrf_biast at a time, and wait for it to "
        "finish before starting another. This server refuses a second "
        "device call while one is running and says the HackRF is busy. If a "
        "tool fails with a 'device is busy' / 'resource busy' / 'access "
        "denied' style error from hackrf_tools, another process outside "
        "this server is likely holding the device. hackrf_sweep and "
        "hackrf_transfer can run for up to their timeout_seconds, so avoid "
        "issuing another HackRF tool call until one of those returns.\n\n"
        "hackrf_iq_to_wav, hackrf_iq_spectrogram, hackrf_iq_info, "
        "hackrf_iq_detect_bursts, hackrf_list_captures and "
        "hackrf_server_status do not access the device and can be run "
        "anytime, including while the device is busy.\n\n"
        "FILES: every file path is relative to the server's capture "
        "directory (see hackrf_server_status); paths outside it are "
        "refused.\n\n"
        "TRANSMIT: whether hackrf_transfer may transmit, and on which "
        "frequencies, is set by the server operator (see "
        "hackrf_server_status). A refused transmission returns a message "
        "starting with REFUSED. Report it to the user; do not retry with "
        "altered parameters to get around it."
    ),
)

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
CONFIG = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
DANGEROUS = ToolAnnotations(readOnlyHint=False, destructiveHint=True)

# Calls from concurrent clients run in a thread pool, so this lock keeps two
# of them from opening the HackRF at once.
_device_lock = threading.Lock()
DEVICE_BUSY = (
    "HackRF is busy: another hackrf_* call on this server is still using "
    "the device. Wait for it to return, then try again."
)


def _run_on_device(binary: str, args: list[str], timeout: float | None = None) -> str:
    if not _device_lock.acquire(blocking=False):
        return DEVICE_BUSY
    try:
        return run(binary, args, timeout=timeout)
    finally:
        _device_lock.release()


def _refused(reason: object) -> str:
    return f"REFUSED: {reason}"


@mcp.tool(annotations=READ_ONLY)
def hackrf_info(device_serial: str | None = None) -> str:
    """Show information about connected HackRF devices: board ID, firmware
    version, part ID, and serial number.

    Args:
        device_serial: Serial number of a specific HackRF, if multiple are
            connected.
    """
    args = []
    if device_serial:
        args += ["-d", device_serial]
    return _run_on_device("hackrf_info", args, timeout=15)


@mcp.tool(annotations=READ_ONLY)
def hackrf_sweep(
    freq_min_mhz: int = 0,
    freq_max_mhz: int = 6000,
    bin_width_hz: int | None = None,
    one_shot: bool = True,
    num_sweeps: int | None = None,
    lna_gain_db: int | None = None,
    vga_gain_db: int | None = None,
    amp_enable: bool = False,
    device_serial: str | None = None,
    timeout_seconds: float = 30,
    output_image_path: str | None = None,
):
    """Sweep a frequency range with the HackRF and report measured power
    levels (dB) for each frequency bin.

    Output rows have the form:
    date, time, hz_low, hz_high, hz_bin_width, num_samples, dB, dB, ...

    NOTE: this holds the HackRF's exclusive USB connection for up to
    timeout_seconds. Do not call this or any other hackrf_* tool again until
    it returns.

    Args:
        freq_min_mhz: Start of the frequency range to sweep, in MHz.
        freq_max_mhz: End of the frequency range to sweep, in MHz.
        bin_width_hz: FFT bin width (frequency resolution) in Hz, 2445-5000000.
        one_shot: If true (default), perform a single sweep and stop. Set to
            false only if num_sweeps is also given, otherwise the sweep would
            run until timeout_seconds is reached.
        num_sweeps: Number of sweeps to perform when one_shot is false.
        lna_gain_db: RX LNA (IF) gain, 0-40dB in 8dB steps.
        vga_gain_db: RX VGA (baseband) gain, 0-62dB in 2dB steps.
        amp_enable: Enable the RX RF amplifier (~11-14dB extra gain). This is
            a receive-only operation, but a strong nearby transmitter
            combined with amp_enable can overdrive the receiver front end.
        device_serial: Serial number of a specific HackRF, if multiple are
            connected.
        timeout_seconds: Maximum time to let the sweep run before it is
            terminated.
        output_image_path: If given, also render a power-vs-frequency plot
            (PNG) of the sweep data to this path in the capture directory and
            return it as an image. Useful for visualizing wide sweeps where
            the raw CSV is hard to read.
    """
    if output_image_path:
        try:
            output_image_path = str(storage.resolve(output_image_path))
        except storage.PathNotAllowed as exc:
            return _refused(exc)

    args = ["-f", f"{freq_min_mhz}:{freq_max_mhz}"]
    if bin_width_hz is not None:
        args += ["-w", str(bin_width_hz)]
    if one_shot:
        args += ["-1"]
    elif num_sweeps is not None:
        args += ["-N", str(num_sweeps)]
    if lna_gain_db is not None:
        args += ["-l", str(lna_gain_db)]
    if vga_gain_db is not None:
        args += ["-g", str(vga_gain_db)]
    if amp_enable:
        args += ["-a", "1"]
    if device_serial:
        args += ["-d", device_serial]

    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        out_path = Path(tmp.name)

    try:
        args += ["-r", str(out_path)]
        log = _run_on_device("hackrf_sweep", args, timeout=timeout_seconds)
        if log == DEVICE_BUSY:
            return log
        data = out_path.read_text(errors="replace")
    finally:
        out_path.unlink(missing_ok=True)

    max_chars = 50_000
    if len(data) > max_chars:
        data = data[:max_chars] + "\n... (output truncated)"

    result = f"{log}\n--- sweep data ---\n{data}".strip()

    if output_image_path:
        freqs_hz, power_db = iq.parse_sweep_csv(data)
        if freqs_hz.size == 0:
            return f"{result}\n\nNo sweep data to plot."
        iq.render_sweep_png(
            output_image_path,
            freqs_hz,
            power_db,
            title=f"Sweep {freq_min_mhz}-{freq_max_mhz}MHz",
        )
        peak_idx = np.argmax(power_db)
        result += (
            f"\n\nWrote {output_image_path}. Peak power {power_db[peak_idx]:.1f}dB "
            f"at {freqs_hz[peak_idx] / 1e6:.4f}MHz."
        )
        return [result, Image(path=output_image_path)]

    return result


@mcp.tool(annotations=CONFIG)
def hackrf_transfer(
    direction: Literal["rx", "tx"],
    file_path: str,
    frequency_hz: int,
    sample_rate_hz: int = 10_000_000,
    duration_seconds: float | None = None,
    num_samples: int | None = None,
    lna_gain_db: int | None = None,
    vga_gain_db: int | None = None,
    txvga_gain_db: int | None = None,
    amp_enable: bool = False,
    antenna_enable: bool = False,
    repeat_tx: bool = False,
    device_serial: str | None = None,
    timeout_seconds: float = 60,
) -> str:
    """Receive IQ samples from the HackRF into a file, or transmit IQ samples
    from a file.

    For rx, the capture is bounded by num_samples if given, otherwise by
    duration_seconds (default 5 seconds), converted to a sample count using
    sample_rate_hz. The server refuses captures longer than its
    max_rx_seconds, larger than its max_capture_mb, or that would fill the
    disk (see hackrf_server_status). For tx, the transfer ends when the input
    file is exhausted unless repeat_tx is set, in which case it runs until
    timeout_seconds; either way the server stops it after its
    max_tx_seconds.

    NOTE: this holds the HackRF's exclusive USB connection for up to
    timeout_seconds (longer for repeat_tx). Do not call this or any other
    hackrf_* tool again until it returns.

    SAFETY: Transmitting radio signals (direction="tx") puts the HackRF into
    TX mode and radiates RF on frequency_hz. This may be subject to
    regulations in your jurisdiction (e.g. FCC rules in the US) — only
    transmit on frequencies and at power levels you are authorized to use,
    and consider using a dummy load or shielded enclosure for testing.
    The server operator sets a transmit mode: "off" refuses every
    transmission; "allowed-bands" measures the occupied bandwidth of the
    file and transmits only if it and the carrier fit inside one allowed
    band; "unrestricted" skips the band check. The band check covers
    frequency only, not power or emissions. Every attempt, allowed or
    refused, is written to the server's transmit audit log. A refusal
    returns a message starting with REFUSED; do not retry with altered
    parameters to get around it.
    Enabling antenna_enable (bias-tee) applies DC power to the antenna port
    and can damage equipment connected to that port that does not expect DC
    bias. Enabling amp_enable adds significant additional gain (~11-14dB);
    combined with antenna_enable on a strong nearby signal it can overdrive
    the receiver front end.

    Args:
        direction: "rx" to receive into file_path, "tx" to transmit from
            file_path.
        file_path: Path of the IQ sample file to read from or write to,
            relative to the capture directory.
        frequency_hz: Center frequency in Hz.
        sample_rate_hz: Sample rate in Hz (2-20 MHz supported, default 10MHz).
        duration_seconds: For rx, how long to capture if num_samples is not
            given.
        num_samples: For rx, the exact number of samples to capture.
        lna_gain_db: RX LNA (IF) gain, 0-40dB in 8dB steps. Start around 16dB.
        vga_gain_db: RX VGA (baseband) gain, 0-62dB in 2dB steps. Start around
            16dB.
        txvga_gain_db: TX VGA (IF) gain, 0-47dB in 1dB steps.
        amp_enable: Enable the RX/TX RF amplifier (~11-14dB extra gain). See
            safety note above.
        antenna_enable: Enable antenna port power (bias-tee, ~3.3-5V DC). See
            safety note above — do not enable if an antenna/device that
            cannot tolerate DC bias is connected.
        repeat_tx: For tx, repeat the file continuously until timeout.
        device_serial: Serial number of a specific HackRF, if multiple are
            connected.
        timeout_seconds: Maximum time to let the transfer run before it is
            terminated.
    """
    s = settings.get()
    args: list[str] = []
    tx_record: dict | None = None
    try:
        path = storage.resolve(file_path, must_exist=(direction == "tx"))
    except storage.PathNotAllowed as exc:
        return _refused(exc)

    if direction == "rx":
        if num_samples is None:
            secs = duration_seconds if duration_seconds is not None else 5.0
            num_samples = int(secs * sample_rate_hz)
        if num_samples / sample_rate_hz > s.max_rx_seconds:
            return _refused(
                f"capture of {num_samples / sample_rate_hz:.1f}s is over this "
                f"server's {s.max_rx_seconds:g}s limit"
            )
        try:
            # hackrf_transfer writes 2 bytes (8-bit I and Q) per sample.
            storage.check_capture_space(2 * num_samples)
        except storage.CaptureRefused as exc:
            return _refused(exc)
        args += ["-r", str(path), "-n", str(num_samples)]
    else:
        occupied = None
        if s.tx_mode == "allowed-bands":
            try:
                freqs, power = iq.file_power_spectrum(path, sample_rate_hz)
                occupied = iq.power_bandwidth(freqs, power)
            except (OSError, ValueError):
                occupied = None  # fall back to assuming the full bandwidth
        # A few FFT bins of margin on each side of the measured span.
        margin_hz = 4 * sample_rate_hz / 4096
        try:
            decision = policy.check_tx(s, frequency_hz, sample_rate_hz, occupied, margin_hz)
        except ValueError as exc:  # misconfigured band sets
            decision = policy.TxDecision(False, f"transmit policy error: {exc}")
        tx_record = {
            "file": str(path.relative_to(storage.data_dir())),
            "file_sha256": storage.sha256_file(path),
            "frequency_hz": frequency_hz,
            "sample_rate_hz": sample_rate_hz,
            "span_hz": decision.span_hz,
            "tx_mode": s.tx_mode,
            "txvga_gain_db": txvga_gain_db,
            "amp_enable": amp_enable,
            "antenna_enable": antenna_enable,
            "repeat_tx": repeat_tx,
            "allowed": decision.allowed,
            "reason": decision.reason,
        }
        if not decision.allowed:
            storage.audit_tx(tx_record)
            return _refused(decision.reason)
        timeout_seconds = min(timeout_seconds, s.max_tx_seconds)
        args += ["-t", str(path)]
        if repeat_tx:
            args += ["-R"]

    args += ["-f", str(frequency_hz), "-s", str(sample_rate_hz)]
    if lna_gain_db is not None:
        args += ["-l", str(lna_gain_db)]
    if vga_gain_db is not None:
        args += ["-g", str(vga_gain_db)]
    if txvga_gain_db is not None:
        args += ["-x", str(txvga_gain_db)]
    if amp_enable:
        args += ["-a", "1"]
    if antenna_enable:
        args += ["-p", "1"]
    if device_serial:
        args += ["-d", device_serial]

    result = _run_on_device("hackrf_transfer", args, timeout=timeout_seconds)
    if tx_record is not None:
        tx_record["result"] = "busy" if result == DEVICE_BUSY else "ran"
        storage.audit_tx(tx_record)
        if result != DEVICE_BUSY:
            result = f"Transmit allowed: {tx_record['reason']}.\n{result}"
    return result


@mcp.tool(annotations=CONFIG)
def hackrf_clock(
    read_clock: int | None = None,
    read_all: bool = False,
    clkin_status: bool = False,
    clkout_enable: bool | None = None,
    p1_signal: str | None = None,
    p2_signal: str | None = None,
    clkin_ctrl: str | None = None,
    device_serial: str | None = None,
) -> str:
    """Read or configure the HackRF's clock input/output settings (CLKIN,
    CLKOUT, and the Pro/Pro+ P1/P2 SMA connectors).

    Args:
        read_clock: Read settings for this clock number.
        read_all: Read settings for all clocks.
        clkin_status: Get CLKIN lock status.
        clkout_enable: Enable or disable CLKOUT.
        p1_signal: Signal for the Pro P1 SMA connector. One of: clkin,
            trigger_in, trigger_out, p22_clkin, pps_out, aux_clk1, aux_clk2,
            off.
        p2_signal: Signal for the Pro P2 SMA connector. One of: clkout,
            trigger_in, trigger_out.
        clkin_ctrl: Select the Pro CLKIN signal source. One of: p1, p22.
        device_serial: Serial number of a specific HackRF, if multiple are
            connected.
    """
    args = []
    if read_clock is not None:
        args += ["-r", str(read_clock)]
    if read_all:
        args += ["-a"]
    if clkin_status:
        args += ["-i"]
    if clkout_enable is not None:
        args += ["-o", "1" if clkout_enable else "0"]
    if p1_signal:
        args += ["-1", p1_signal]
    if p2_signal:
        args += ["-2", p2_signal]
    if clkin_ctrl:
        args += ["-c", clkin_ctrl]
    if device_serial:
        args += ["-d", device_serial]
    return _run_on_device("hackrf_clock", args, timeout=15)


@mcp.tool(annotations=CONFIG)
def hackrf_operacake(
    list_boards: bool = False,
    address: int | None = None,
    mode: Literal["manual", "frequency", "time"] | None = None,
    port_a0: str | None = None,
    port_b0: str | None = None,
    frequency_ranges: list[str] | None = None,
    dwell_times: list[str] | None = None,
    default_dwell: int | None = None,
    gpio_test: bool = False,
    device_serial: str | None = None,
) -> str:
    """List or configure Opera Cake antenna switch boards attached to a
    HackRF.

    Args:
        list_boards: List available Opera Cake boards and exit.
        address: Address of the Opera Cake to configure (default 0).
        mode: Switching mode: "manual", "frequency", or "time".
        port_a0: Port connected to port A0 (e.g. "A1").
        port_b0: Port connected to port B0 (e.g. "B1").
        frequency_ranges: For frequency mode, entries of the form
            "port:min_mhz:max_mhz" to automatically assign a port for a
            frequency range. Can be repeated.
        dwell_times: For time mode, entries of the form "port:dwell" (or just
            "port" to use default_dwell) specifying how long to dwell on each
            port, in samples. Can be repeated.
        default_dwell: Default dwell time in samples for time mode.
        gpio_test: Test GPIO functionality of an Opera Cake.
        device_serial: Serial number of a specific HackRF, if multiple are
            connected.
    """
    args = []
    if list_boards:
        args += ["-l"]
    if gpio_test:
        args += ["-g"]
    if address is not None:
        args += ["-o", str(address)]
    if mode:
        args += ["-m", mode]
    if port_a0:
        args += ["-a", port_a0]
    if port_b0:
        args += ["-b", port_b0]
    for r in frequency_ranges or []:
        args += ["-f", r]
    for t in dwell_times or []:
        args += ["-t", t]
    if default_dwell is not None:
        args += ["-w", str(default_dwell)]
    if device_serial:
        args += ["-d", device_serial]
    return _run_on_device("hackrf_operacake", args, timeout=15)


@mcp.tool(annotations=CONFIG)
def hackrf_biast(
    immediate_mode: bool | None = None,
    rx_mode: Literal["leave", "on", "off"] | None = None,
    tx_mode: Literal["leave", "on", "off"] | None = None,
    off_mode: Literal["leave", "on", "off"] | None = None,
    reset: bool = False,
    device_serial: str | None = None,
) -> str:
    """Enable, disable, or configure the HackRF's bias-tee antenna port power,
    for compatibility with software that does not control it directly.

    SAFETY: enabling the bias tee applies DC power (~3.3-5V) to the antenna
    port. Only enable it when an antenna or accessory that expects bias-tee
    power (e.g. an LNA) is connected — applying DC bias to equipment that
    doesn't expect it can damage that equipment.

    Args:
        immediate_mode: Enable (true) or disable (false) the bias tee
            immediately.
        rx_mode: Default bias-tee power state when the device enters RX mode.
        tx_mode: Default bias-tee power state when the device enters TX mode.
        off_mode: Default bias-tee power state when the device enters OFF
            mode.
        reset: Reset all bias-tee settings to device defaults before applying
            any other options given.
        device_serial: Serial number of a specific HackRF, if multiple are
            connected.
    """
    args = []
    if reset:
        args += ["-R"]
    if immediate_mode is not None:
        args += ["-b", "1" if immediate_mode else "0"]
    if rx_mode:
        args += ["-r", rx_mode]
    if tx_mode:
        args += ["-t", tx_mode]
    if off_mode:
        args += ["-o", off_mode]
    if device_serial:
        args += ["-d", device_serial]
    return _run_on_device("hackrf_biast", args, timeout=15)


# Default cap on samples processed by the offline analysis tools below, to
# keep memory use and runtime bounded for large captures. 50M complex
# samples is ~100MB as complex64 (200MB raw int8 file at 8-bit I/Q).
MAX_IQ_SAMPLES = 50_000_000


@mcp.tool(annotations=CONFIG)
def hackrf_iq_to_wav(
    iq_file_path: str,
    sample_rate_hz: int,
    mode: Literal["nfm", "wfm", "am", "usb", "lsb"],
    output_wav_path: str,
    center_freq_hz: int = 0,
    target_freq_hz: int | None = None,
    bandwidth_hz: float | None = None,
    audio_rate_hz: int = 48_000,
    max_duration_seconds: float | None = 60.0,
    denoise: bool = False,
    return_audio: bool = False,
):
    """Demodulate a raw IQ capture (from hackrf_transfer, 8-bit signed
    interleaved I/Q) to a WAV audio file.

    This is an offline file-processing tool and does not access the HackRF
    device.

    IMPORTANT: capture with an LO offset. HackRF baseband has a DC spike at
    0Hz. If the signal of interest sits at (or very near) center_freq_hz, it
    demodulates as a loud constant tone ("Daffy Duck" effect) instead of
    audio. When recording with hackrf_transfer, tune frequency_hz ~25-50kHz
    away from the actual signal frequency, then pass the *actual* signal
    frequency here as target_freq_hz (with center_freq_hz set to the
    hackrf_transfer frequency_hz) so the signal gets mixed away from 0Hz
    before demodulation.

    Args:
        iq_file_path: Path to the raw IQ capture file (e.g. from
            `hackrf_transfer` with `direction="rx"`), relative to the
            capture directory.
        sample_rate_hz: Sample rate the capture was recorded at, in Hz.
        mode: Demodulation mode: "nfm" (narrowband FM, e.g. analog two-way
            radio), "wfm" (wideband FM, e.g. broadcast FM radio), "am"
            (amplitude modulation), "usb"/"lsb" (single-sideband voice).
        output_wav_path: Path to write the demodulated WAV file to,
            relative to the capture directory.
        center_freq_hz: Center frequency the capture was recorded at, in Hz.
            Used together with target_freq_hz to compute the tuning offset.
        target_freq_hz: Frequency of the signal of interest, in Hz, if it is
            not at center_freq_hz. The signal is mixed down to baseband
            before demodulation. Defaults to center_freq_hz (no shift) — see
            the offset-tuning note above before relying on this default.
        bandwidth_hz: Channel bandwidth to filter to before demodulation, in
            Hz. Defaults to a typical value for `mode` (e.g. 16kHz for nfm,
            180kHz for wfm).
        audio_rate_hz: Output WAV sample rate, in Hz.
        max_duration_seconds: Only process up to this many seconds of the
            input capture (from the start), to bound memory/runtime for
            large files. Set to None to process the whole file (use with
            caution on large captures).
        denoise: If True, apply STFT spectral-subtraction noise reduction
            (using the first second of audio as the noise profile). Useful
            for marginal-SNR signals; may introduce artifacts on clean audio.
        return_audio: If True, also return the WAV file as inline playable
            audio content (in addition to writing it to output_wav_path).
    """
    try:
        iq_file_path = str(storage.resolve(iq_file_path, must_exist=True))
        output_wav_path = str(storage.resolve(output_wav_path))
    except storage.PathNotAllowed as exc:
        return _refused(exc)

    max_samples = None
    if max_duration_seconds is not None:
        max_samples = min(int(max_duration_seconds * sample_rate_hz), MAX_IQ_SAMPLES)
    elif MAX_IQ_SAMPLES:
        max_samples = MAX_IQ_SAMPLES

    samples = iq.read_iq_int8(iq_file_path, max_samples=max_samples)
    if samples.size == 0:
        return f"'{iq_file_path}' contains no IQ samples."

    offset_hz = (target_freq_hz - center_freq_hz) if target_freq_hz is not None else 0
    baseband = iq.mix(samples, sample_rate_hz, offset_hz)

    audio = iq.demodulate(baseband, sample_rate_hz, mode, bandwidth_hz=bandwidth_hz, denoise=denoise)
    audio = iq.resample_to_rate(audio, sample_rate_hz, audio_rate_hz)
    iq.write_wav(output_wav_path, audio, audio_rate_hz)

    duration_s = samples.size / sample_rate_hz
    warning = ""
    if offset_hz == 0 and mode in ("nfm", "wfm"):
        warning = (
            " WARNING: target_freq_hz == center_freq_hz (no tuning offset), "
            "so the signal may sit on HackRF's baseband DC spike and "
            "demodulate as a loud constant tone. Recapture with an "
            "LO offset of ~25-50kHz and set target_freq_hz to the actual "
            "signal frequency."
        )

    result = (
        f"Wrote {output_wav_path} ({audio.size / audio_rate_hz:.2f}s @ "
        f"{audio_rate_hz}Hz) demodulated as {mode} from {duration_s:.2f}s of "
        f"input at {sample_rate_hz}Hz.{warning}"
    )

    if return_audio:
        return [result, Audio(path=output_wav_path)]
    return result


@mcp.tool(annotations=CONFIG)
def hackrf_iq_spectrogram(
    iq_file_path: str,
    sample_rate_hz: int,
    output_image_path: str,
    center_freq_hz: int = 0,
    fft_size: int = 1024,
    max_duration_seconds: float | None = 10.0,
):
    """Render a spectrogram (waterfall) PNG from a raw IQ capture (from
    hackrf_transfer, 8-bit signed interleaved I/Q).

    Useful for visually identifying bursts, framing/timing patterns (e.g.
    keyfobs, trunked radio control channels), and occupied bandwidth.

    This is an offline file-processing tool and does not access the HackRF
    device.

    Args:
        iq_file_path: Path to the raw IQ capture file (e.g. from
            `hackrf_transfer` with `direction="rx"`), relative to the
            capture directory.
        sample_rate_hz: Sample rate the capture was recorded at, in Hz.
        output_image_path: Path to write the spectrogram PNG to,
            relative to the capture directory.
        center_freq_hz: Center frequency the capture was recorded at, in Hz.
            Used to label the frequency axis in absolute terms.
        fft_size: FFT size (frequency resolution) for the spectrogram.
        max_duration_seconds: Only process up to this many seconds of the
            input capture (from the start), to bound memory/runtime for
            large files. Set to None to process the whole file (use with
            caution on large captures).
    """
    try:
        iq_file_path = str(storage.resolve(iq_file_path, must_exist=True))
        output_image_path = str(storage.resolve(output_image_path))
    except storage.PathNotAllowed as exc:
        return [_refused(exc)]

    max_samples = None
    if max_duration_seconds is not None:
        max_samples = min(int(max_duration_seconds * sample_rate_hz), MAX_IQ_SAMPLES)
    elif MAX_IQ_SAMPLES:
        max_samples = MAX_IQ_SAMPLES

    samples = iq.read_iq_int8(iq_file_path, max_samples=max_samples)
    if samples.size == 0:
        return [f"'{iq_file_path}' contains no IQ samples."]

    freqs_hz, times_s, power_db = iq.compute_spectrogram(samples, sample_rate_hz, fft_size=fft_size)
    iq.render_spectrogram_png(
        output_image_path,
        freqs_hz,
        times_s,
        power_db,
        center_freq_hz,
        title=Path(iq_file_path).name,
    )

    peak_idx = np.unravel_index(np.argmax(power_db), power_db.shape)
    peak_freq_hz = freqs_hz[peak_idx[0]] + center_freq_hz
    peak_time_s = times_s[peak_idx[1]]
    duration_s = samples.size / sample_rate_hz

    summary = (
        f"Wrote {output_image_path} ({duration_s:.2f}s of input at "
        f"{sample_rate_hz}Hz, fft_size={fft_size}). Peak power "
        f"{power_db[peak_idx]:.1f}dB at {peak_freq_hz / 1e6:.4f}MHz, "
        f"t={peak_time_s:.3f}s."
    )
    return [summary, Image(path=output_image_path)]


@mcp.tool(annotations=READ_ONLY)
def hackrf_iq_info(
    iq_file_path: str,
    sample_rate_hz: int,
    center_freq_hz: int = 0,
    fft_size: int = 4096,
    occupied_bw_threshold_db: float = 6.0,
    max_duration_seconds: float | None = 10.0,
) -> str:
    """Summarize a raw IQ capture (from hackrf_transfer, 8-bit signed
    interleaved I/Q): duration, sample count, signal levels, and an estimate
    of which frequencies are occupied.

    This is an offline file-processing tool and does not access the HackRF
    device. Useful for a quick look at a capture before rendering a full
    spectrogram or demodulating it.

    Args:
        iq_file_path: Path to the raw IQ capture file (e.g. from
            `hackrf_transfer` with `direction="rx"`), relative to the
            capture directory.
        sample_rate_hz: Sample rate the capture was recorded at, in Hz.
        center_freq_hz: Center frequency the capture was recorded at, in Hz.
            Used to report occupied frequencies in absolute terms.
        fft_size: FFT size used to compute the average power spectrum.
        occupied_bw_threshold_db: A frequency is considered "occupied" if its
            average power is within this many dB of the peak.
        max_duration_seconds: Only process up to this many seconds of the
            input capture (from the start), to bound memory/runtime for
            large files. Set to None to process the whole file (use with
            caution on large captures).
    """
    try:
        iq_file_path = str(storage.resolve(iq_file_path, must_exist=True))
    except storage.PathNotAllowed as exc:
        return _refused(exc)

    max_samples = None
    if max_duration_seconds is not None:
        max_samples = min(int(max_duration_seconds * sample_rate_hz), MAX_IQ_SAMPLES)
    elif MAX_IQ_SAMPLES:
        max_samples = MAX_IQ_SAMPLES

    samples = iq.read_iq_int8(iq_file_path, max_samples=max_samples)
    if samples.size == 0:
        return f"'{iq_file_path}' contains no IQ samples."

    duration_s = samples.size / sample_rate_hz
    mean_power_db = float(10 * np.log10(np.mean(np.abs(samples) ** 2) + 1e-12))

    freqs_hz, power_db = iq.compute_spectrum(samples, sample_rate_hz, fft_size=fft_size)
    peak_idx = np.argmax(power_db)
    peak_freq_hz = freqs_hz[peak_idx] + center_freq_hz
    peak_power_db = power_db[peak_idx]

    low_hz, high_hz, bw_hz = iq.occupied_bandwidth(freqs_hz, power_db, threshold_db=occupied_bw_threshold_db)

    return (
        f"'{iq_file_path}': {duration_s:.3f}s ({samples.size} samples) at "
        f"{sample_rate_hz}Hz, center {center_freq_hz / 1e6:.4f}MHz.\n"
        f"Mean power: {mean_power_db:.1f}dB. Peak power: {peak_power_db:.1f}dB "
        f"at {peak_freq_hz / 1e6:.4f}MHz.\n"
        f"Occupied bandwidth (within {occupied_bw_threshold_db:.1f}dB of peak): "
        f"{bw_hz / 1e3:.1f}kHz, from {(low_hz + center_freq_hz) / 1e6:.4f}MHz to "
        f"{(high_hz + center_freq_hz) / 1e6:.4f}MHz."
    )


@mcp.tool(annotations=READ_ONLY)
def hackrf_iq_detect_bursts(
    iq_file_path: str,
    sample_rate_hz: int,
    center_freq_hz: int = 0,
    threshold_db: float = 10.0,
    min_duration_ms: float = 0.5,
    merge_gap_ms: float = 1.0,
    max_bursts: int = 50,
    max_duration_seconds: float | None = 10.0,
) -> str:
    """Detect bursts of elevated signal power in a raw IQ capture (from
    hackrf_transfer, 8-bit signed interleaved I/Q), e.g. keyfob transmissions,
    packet preambles, or trunked radio control channel activity.

    This is an offline file-processing tool and does not access the HackRF
    device.

    For each detected burst, reports its start/end time, duration, peak
    power, and dominant frequency (relative to the capture's noise floor).

    Args:
        iq_file_path: Path to the raw IQ capture file (e.g. from
            `hackrf_transfer` with `direction="rx"`), relative to the
            capture directory.
        sample_rate_hz: Sample rate the capture was recorded at, in Hz.
        center_freq_hz: Center frequency the capture was recorded at, in Hz.
            Used to report burst frequencies in absolute terms.
        threshold_db: How many dB above the capture's median power a sample
            must be to be considered part of a burst.
        min_duration_ms: Minimum burst duration, in milliseconds, to be
            reported. Shorter excursions are treated as noise.
        merge_gap_ms: Bursts separated by less than this many milliseconds
            are merged into a single burst.
        max_bursts: Maximum number of bursts to report.
        max_duration_seconds: Only process up to this many seconds of the
            input capture (from the start), to bound memory/runtime for
            large files. Set to None to process the whole file (use with
            caution on large captures).
    """
    try:
        iq_file_path = str(storage.resolve(iq_file_path, must_exist=True))
    except storage.PathNotAllowed as exc:
        return _refused(exc)

    max_samples = None
    if max_duration_seconds is not None:
        max_samples = min(int(max_duration_seconds * sample_rate_hz), MAX_IQ_SAMPLES)
    elif MAX_IQ_SAMPLES:
        max_samples = MAX_IQ_SAMPLES

    samples = iq.read_iq_int8(iq_file_path, max_samples=max_samples)
    if samples.size == 0:
        return f"'{iq_file_path}' contains no IQ samples."

    duration_s = samples.size / sample_rate_hz
    bursts = iq.detect_bursts(
        samples,
        sample_rate_hz,
        threshold_db=threshold_db,
        min_duration_s=min_duration_ms / 1000,
        merge_gap_s=merge_gap_ms / 1000,
        max_bursts=max_bursts,
    )

    if not bursts:
        return (
            f"No bursts found in {duration_s:.3f}s of input at "
            f"{sample_rate_hz}Hz (threshold {threshold_db:.1f}dB above median)."
        )

    lines = [
        f"Found {len(bursts)} burst(s) in {duration_s:.3f}s of input at "
        f"{sample_rate_hz}Hz (threshold {threshold_db:.1f}dB above median):"
    ]
    for b in bursts:
        freq_hz = center_freq_hz + b["peak_freq_offset_hz"]
        lines.append(
            f"  t={b['start_s']:.4f}-{b['end_s']:.4f}s "
            f"(dur {b['duration_s'] * 1000:.2f}ms), peak {b['peak_db']:.1f}dB, "
            f"freq {freq_hz / 1e6:.4f}MHz"
        )
    return "\n".join(lines)


@mcp.tool(annotations=READ_ONLY)
def hackrf_server_status() -> str:
    """Report how this server is configured: the transmit mode and the
    bands it allows, the capture directory and its free space, the capture
    and transmit limits, and the most recent transmit-audit entries.

    This does not access the HackRF device. The transmit mode is set by the
    server operator when the server starts; no tool can change it.
    """
    s = settings.get()
    root = storage.data_dir()
    free_mb = shutil.disk_usage(root).free / storage.MIB
    lines = [
        f"hackrf-mcp {__version__}",
        f"Transmit mode: {s.tx_mode}",
    ]
    if s.tx_mode == "allowed-bands":
        try:
            bands = policy.allowed_bands(s)
            lines.append(f"Band sets: {', '.join(s.tx_band_sets) or 'none'}")
            lines += [f"  {b.describe()}" for b in bands] or ["  (no bands: every transmission is refused)"]
        except ValueError as exc:
            lines.append(f"Band configuration error, so every transmission is refused: {exc}")
        lines.append(
            "  The band check covers frequency only. Power, modulation, emissions "
            "and license-class privileges are the operator's responsibility."
        )
    lines += [
        f"Capture directory: {root} ({free_mb:.0f} MiB free)",
        f"Limits: rx {s.max_rx_seconds:g}s and {s.max_capture_mb:g} MiB per capture, "
        f"{s.min_free_mb:g} MiB kept free; tx {s.max_tx_seconds:g}s per transmission",
        f"Device: {'busy' if _device_lock.locked() else 'idle'}",
        f"Transmit audit log: {s.tx_log_path}",
    ]
    recent = storage.recent_tx()
    if recent:
        lines.append("Recent transmit attempts (oldest first):")
        for r in recent:
            outcome = "allowed" if r.get("allowed") else "refused"
            lines.append(
                f"  {r.get('time_utc')} {r.get('frequency_hz', 0) / 1e6:.6f} MHz "
                f"{r.get('file')} {outcome}: {r.get('reason')}"
            )
    return "\n".join(lines)


@mcp.tool(annotations=READ_ONLY)
def hackrf_list_captures(limit: int = 50) -> str:
    """List files in the capture directory, newest first, with their sizes,
    plus the total size and the free disk space.

    This does not access the HackRF device.

    Args:
        limit: Maximum number of files to list.
    """
    entries, total = storage.list_captures(limit=max(1, limit))
    free_mb = shutil.disk_usage(storage.data_dir()).free / storage.MIB
    lines = [f"Capture directory {storage.data_dir()}: {total / storage.MIB:.1f} MiB used, {free_mb:.0f} MiB free."]
    if not entries:
        lines.append("No captures yet.")
    for e in entries:
        lines.append(f"  {e['modified_utc']}  {e['bytes'] / storage.MIB:10.2f} MiB  {e['path']}")
    return "\n".join(lines)


@mcp.tool(annotations=DANGEROUS)
def hackrf_delete_capture(file_path: str) -> str:
    """Delete one file from the capture directory, e.g. to free disk space
    for new captures. Directories and the transmit audit log cannot be
    deleted.

    This does not access the HackRF device.

    Args:
        file_path: Path of the file to delete, relative to the capture
            directory.
    """
    try:
        path = storage.resolve(file_path, must_exist=True)
    except storage.PathNotAllowed as exc:
        return _refused(exc)
    if path == settings.get().tx_log_path.expanduser().resolve():
        return _refused("the transmit audit log cannot be deleted through this server")
    size = path.stat().st_size
    path.unlink()
    return f"Deleted {path.relative_to(storage.data_dir())} ({size / storage.MIB:.2f} MiB)."


# @mcp.tool(annotations=DANGEROUS)
# def hackrf_debug(
#     target: Literal["max283x", "si5351c", "rffc5072", "gateware", "radio"],
#     register: int | None = None,
#     bank: int | None = None,
#     read: bool = False,
#     write_value: int | None = None,
#     config: bool = False,
#     state: bool = False,
#     selftest: bool = False,
#     device_serial: str | None = None,
# ) -> str:
#     """Read or write low-level HackRF hardware registers for debugging.

#     WARNING: writing incorrect register values can put the device into an
#     unusable state until it is reset or re-flashed. Prefer read-only options
#     unless you know exactly what you are doing.

#     Args:
#         target: Which chip/subsystem to target: max283x, si5351c, rffc5072,
#             gateware (FPGA registers), or radio (M0 radio registers).
#         register: Register number for read/write operations.
#         bank: Register bank for read/write operations (default 0).
#         read: Read the register specified by `register`, or all registers if
#             not given.
#         write_value: Write this value to the register specified by
#             `register`.
#         config: Print SI5351C multisynth configuration information.
#         state: Display M0 state.
#         selftest: Read the self-test report.
#         device_serial: Serial number of a specific HackRF, if multiple are
#             connected.
#     """
#     target_flags = {
#         "max283x": "-m",
#         "si5351c": "-s",
#         "rffc5072": "-f",
#         "gateware": "-g",
#         "radio": "-i",
#     }
#     args = [target_flags[target]]
#     if bank is not None:
#         args += ["-b", str(bank)]
#     if register is not None:
#         args += ["-n", str(register)]
#     if read:
#         args += ["-r"]
#     if write_value is not None:
#         args += ["-w", str(write_value)]
#     if config:
#         args += ["-c"]
#     if state:
#         args += ["-S"]
#     if selftest:
#         args += ["-t"]
#     if device_serial:
#         args += ["-d", device_serial]
#     return run("hackrf_debug", args, timeout=15)


# @mcp.tool(annotations=DANGEROUS)
# def hackrf_cpldjtag(xsvf_file_path: str, device_serial: str | None = None) -> str:
#     """Program the HackRF's CPLD with an XSVF bitstream file over JTAG.

#     WARNING: this overwrites the CPLD configuration. Only use XSVF files from
#     a trusted HackRF firmware release, and do not disconnect the device while
#     this is running.

#     Args:
#         xsvf_file_path: Path to the XSVF file to write to the CPLD.
#         device_serial: Serial number of a specific HackRF, if multiple are
#             connected.
#     """
#     args = ["-x", xsvf_file_path]
#     if device_serial:
#         args += ["-d", device_serial]
#     return run("hackrf_cpldjtag", args, timeout=120)


# @mcp.tool(annotations=DANGEROUS)
# def hackrf_spiflash(
#     read_file: str | None = None,
#     write_file: str | None = None,
#     address: int | None = None,
#     length: int | None = None,
#     read_status: bool = False,
#     clear_status: bool = False,
#     reset: bool = False,
#     no_check: bool = False,
#     verbose: bool = False,
#     device_serial: str | None = None,
# ) -> str:
#     """Read or write the HackRF's SPI flash (firmware) memory.

#     WARNING: writing firmware can render the device unbootable if interrupted
#     or given an image incompatible with the hardware. Only write images from a
#     trusted HackRF release, and do not disconnect the device while this is
#     running.

#     Args:
#         read_file: Read SPI flash contents into this file.
#         write_file: Write the contents of this file to SPI flash.
#         address: Starting address for the operation (default 0).
#         length: Number of bytes to read (default: all).
#         read_status: Read SPI flash status registers before other operations.
#         clear_status: Clear SPI flash status registers before other
#             operations.
#         reset: Reset the HackRF after other operations complete.
#         no_check: Skip the firmware compatibility check for the target
#             device. Use with caution.
#         verbose: Enable verbose output.
#         device_serial: Serial number of a specific HackRF, if multiple are
#             connected.
#     """
#     args = []
#     if address is not None:
#         args += ["-a", str(address)]
#     if length is not None:
#         args += ["-l", str(length)]
#     if read_file:
#         args += ["-r", read_file]
#     if write_file:
#         args += ["-w", write_file]
#     if no_check:
#         args += ["-i"]
#     if read_status:
#         args += ["-s"]
#     if clear_status:
#         args += ["-c"]
#     if reset:
#         args += ["-R"]
#     if verbose:
#         args += ["-v"]
#     if device_serial:
#         args += ["-d", device_serial]
#     return run("hackrf_spiflash", args, timeout=120)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bin-dir",
        default=os.environ.get("HACKRF_TOOLS_DIR"),
        help=(
            "Directory containing the hackrf_tools binaries "
            "(hackrf_info, hackrf_sweep, etc). Falls back to PATH if not "
            "given. Defaults to the HACKRF_TOOLS_DIR environment variable."
        ),
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default=os.environ.get("HACKRF_MCP_TRANSPORT", "stdio"),
        help="stdio (default) or http (streamable HTTP; needs a token). Env: HACKRF_MCP_TRANSPORT.",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("HACKRF_MCP_HOST", "127.0.0.1"),
        help="Address to listen on in http mode (default 127.0.0.1). Env: HACKRF_MCP_HOST.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("HACKRF_MCP_PORT", "8765")),
        help="Port to listen on in http mode (default 8765). Env: HACKRF_MCP_PORT.",
    )
    parser.add_argument(
        "--path",
        default=os.environ.get("HACKRF_MCP_PATH", "/mcp"),
        help="URL path of the MCP endpoint in http mode (default /mcp). Env: HACKRF_MCP_PATH.",
    )
    parser.add_argument(
        "--token-file",
        default=None,
        help=(
            "File holding the bearer token http clients must send. Env: "
            "HACKRF_MCP_TOKEN_FILE, or the token itself in HACKRF_MCP_TOKEN."
        ),
    )
    parser.add_argument(
        "--tx-mode",
        choices=settings.TX_MODES,
        default=None,
        help="Transmit mode: off (default), allowed-bands or unrestricted. Env: HACKRF_MCP_TX_MODE.",
    )
    parser.add_argument(
        "--tx-bands",
        default=None,
        help='Band sets allowed-bands mode permits, e.g. "ism,amateur" (default ism). Env: HACKRF_MCP_TX_BANDS.',
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help="Capture directory every file path is confined to (default ~/hackrf-captures). Env: HACKRF_MCP_DATA_DIR.",
    )
    args = parser.parse_args(argv)

    set_bin_dir(args.bin_dir)

    try:
        s = settings.Settings.from_env()
        if args.tx_mode:
            s = replace(s, tx_mode=args.tx_mode)
        if args.tx_bands is not None:
            s = replace(s, tx_band_sets=settings.parse_band_set_names(args.tx_bands))
        if args.data_dir:
            s = replace(s, data_dir=Path(args.data_dir).expanduser())
        if s.tx_mode == "allowed-bands":
            policy.allowed_bands(s)  # fail at startup on an unknown band set
    except (ValueError, OSError) as exc:
        parser.error(f"invalid configuration: {exc}")
    settings.configure(s)

    if args.transport == "stdio":
        mcp.run()
        return

    from starlette.middleware import Middleware

    from hackrf_mcp.auth import BearerTokenMiddleware, load_token

    try:
        token = load_token(args.token_file)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    if not token:
        parser.error(
            "http transport needs a bearer token: pass --token-file, or set "
            "HACKRF_MCP_TOKEN_FILE or HACKRF_MCP_TOKEN"
        )
    mcp.run(
        transport="http",
        host=args.host,
        port=args.port,
        path=args.path,
        middleware=[Middleware(BearerTokenMiddleware, token=token)],
    )


if __name__ == "__main__":
    main()
