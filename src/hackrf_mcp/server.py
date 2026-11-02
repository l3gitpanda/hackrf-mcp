"""MCP server wrapping the hackrf_tools CLI for controlling a HackRF SDR."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Literal

import numpy as np
from fastmcp import FastMCP
from fastmcp.utilities.types import Audio, Image
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from hackrf_mcp import __version__, decoders, iq, live_client, live_log, live_profiles, policy, settings, storage
from hackrf_mcp import protocols  # noqa: F401  (registers every decoder and live profile)
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
        "hackrf_iq_detect_bursts, hackrf_decode, hackrf_decode_ism, "
        "hackrf_list_captures and hackrf_server_status do not access the "
        "device and can be run anytime, including while the device is "
        "busy.\n\n"
        "LIVE DECODING: a separate live service can keep one profile "
        "(adsb, ais, acars, vdl2, flex, pocsag, aprs, eas) decoding "
        "continuously into a rolling log. hackrf_live_start starts or "
        "switches it (this is how to move the receiver to another band), "
        "hackrf_live_stop stops it, hackrf_live_status shows what it is "
        "doing, and hackrf_live_search / hackrf_live_summary read the log. "
        "While it runs, any device tool above pauses it for the length of "
        "the call and it resumes afterwards; the tool's result says so.\n\n"
        "DECODING: to find out what is on a frequency (aircraft, ships, "
        "pagers, ACARS, APRS, RDS, ISM sensors), capture with "
        "hackrf_transfer at the rate hackrf_decode's description gives for "
        "the protocol, then run hackrf_decode (or hackrf_decode_ism). The "
        "burst and spectrum tools measure power only and can miss signals "
        "much narrower than the capture.\n\n"
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

# The decoders and live profiles the protocol modules registered; the tools'
# parameter choices and descriptions come from these, so adding a protocol
# module is enough to add a decoder.
DecoderName = Literal[decoders.names()]
ProfileName = Literal[live_profiles.names()]


def _fill_doc(**sections: str):
    """Replace each <<NAME>> line in a tool's docstring with the given text,
    indented like the placeholder. FastMCP reads the docstring when the tool
    is registered, so this decorator goes below @mcp.tool."""

    def fill(fn):
        doc = fn.__doc__
        for name, text in sections.items():
            m = re.search(rf"^([ \t]*)<<{name}>>$", doc, re.M)
            if m is None:
                raise ValueError(f"{fn.__name__}'s docstring has no <<{name}>>")
            indent = m.group(1)
            block = "\n".join(indent + line if line else "" for line in text.splitlines())
            doc = doc[: m.start()] + block + doc[m.end() :]
        fn.__doc__ = doc
        return fn

    return fill


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
        # If the live decoder service holds the HackRF, it lets go for this
        # call; the lease brings it back even if this process dies first.
        try:
            hold = live_client.pause(binary, (timeout or 60) + 30)
        except live_client.Unavailable:
            hold = None
        except live_client.LiveError as exc:
            return _refused(
                f"the live decoder service holds the HackRF and did not release it ({exc}); "
                "stop it with hackrf_live_stop and try again"
            )
        started = time.monotonic()
        try:
            result = run(binary, args, timeout=timeout)
        finally:
            if hold is not None:
                live_client.resume(hold["token"])
        if hold is not None and hold.get("paused_profile"):
            result += (
                f"\n(Live decoding of {hold['paused_profile']} was paused for this call, "
                f"{time.monotonic() - started:.0f} s, and has resumed.)"
            )
        return result
    finally:
        _device_lock.release()


def _refused(reason: object) -> str:
    return f"REFUSED: {reason}"


BIAS_TEE_LOCKED = (
    "bias-tee power is locked on this server (HACKRF_MCP_ALLOW_BIAS_TEE is "
    "off); DC on the antenna port can damage equipment that isn't built for it"
)


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
            cannot tolerate DC bias is connected. Refused unless the server
            operator has allowed bias-tee power.
        repeat_tx: For tx, repeat the file continuously until timeout.
        device_serial: Serial number of a specific HackRF, if multiple are
            connected.
        timeout_seconds: Maximum time to let the transfer run before it is
            terminated.
    """
    s = settings.get()
    args: list[str] = []
    tx_record: dict | None = None
    if antenna_enable and not s.allow_bias_tee:
        return _refused(BIAS_TEE_LOCKED)
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
        tx_record["result"] = (
            "busy" if result == DEVICE_BUSY else "refused-live" if result.startswith("REFUSED:") else "ran"
        )
        storage.audit_tx(tx_record)
        if tx_record["result"] == "ran":
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
    doesn't expect it can damage that equipment. Unless the server operator
    has allowed bias-tee power, anything that would turn it on is refused;
    turning it off, "leave" and reset still work.

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
    turns_on = immediate_mode is True or "on" in (rx_mode, tx_mode, off_mode)
    if turns_on and not settings.get().allow_bias_tee:
        return _refused(BIAS_TEE_LOCKED)

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


# Default cap on samples that the in-memory demodulation in hackrf_iq_to_wav
# holds after decimation. The other file tools stream the capture in chunks
# (iq.iter_iq_chunks) and need no cap: their memory use doesn't grow with
# the capture's length.
MAX_IQ_SAMPLES = 50_000_000
MAX_DEMOD_SAMPLES = 15_000_000


def _sample_cap(sample_rate_hz: int, max_duration_seconds: float | None) -> int | None:
    if max_duration_seconds is None:
        return None
    return max(0, int(max_duration_seconds * sample_rate_hz))


def _coverage(path: str | Path, sample_rate_hz: int, samples_read: int) -> str:
    """'' when the whole capture was read, else a note saying how much."""
    total = iq.iq_file_samples(path)
    if samples_read >= total:
        return ""
    return (
        f" NOTE: only the first {samples_read / sample_rate_hz:.2f}s of "
        f"{total / sample_rate_hz:.2f}s were processed (max_duration_seconds); "
        "pass max_duration_seconds=None for all of it."
    )


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
            input capture (from the start). None processes the whole file;
            the channel is decimated as it is read, so a long capture costs
            time rather than memory.
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

    if iq.iq_file_samples(iq_file_path) == 0:
        return f"'{iq_file_path}' contains no IQ samples."

    offset_hz = (target_freq_hz - center_freq_hz) if target_freq_hz is not None else 0
    bw = bandwidth_hz or iq.MODE_BANDWIDTH_HZ[mode]
    # Mix and decimate while reading, so only the channel is held in memory.
    dec = iq.pick_decimation(sample_rate_hz, max(1.25 * bw, audio_rate_hz))
    work_rate = sample_rate_hz // dec
    max_samples = _sample_cap(sample_rate_hz, max_duration_seconds)
    limit = MAX_DEMOD_SAMPLES * dec
    max_samples = limit if max_samples is None else min(max_samples, limit)
    baseband = iq.channelize_to_array(
        iq_file_path,
        sample_rate_hz,
        offset_hz,
        dec,
        max_samples=max_samples,
        bandwidth=min(0.95, 1.25 * bw / work_rate),
    )

    audio = iq.demodulate(baseband, work_rate, mode, bandwidth_hz=bandwidth_hz, denoise=denoise)
    audio = iq.resample_to_rate(audio, work_rate, audio_rate_hz)
    iq.write_wav(output_wav_path, audio, audio_rate_hz)

    samples_read = baseband.size * dec
    duration_s = samples_read / sample_rate_hz
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
        f"input at {sample_rate_hz}Hz.{warning}{_coverage(iq_file_path, sample_rate_hz, samples_read)}"
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
    max_duration_seconds: float | None = None,
    reduce: Literal["max", "mean"] = "max",
):
    """Render a spectrogram (waterfall) PNG from a raw IQ capture (from
    hackrf_transfer, 8-bit signed interleaved I/Q).

    Useful for visually identifying bursts, framing/timing patterns (e.g.
    keyfobs, trunked radio control channels), and occupied bandwidth.

    The capture is read in chunks and drawn as at most ~1200 time columns,
    each holding the peak (reduce="max", the default, which keeps short
    bursts visible) or average power of the FFT frames inside it, so the
    whole file can be drawn in bounded memory.

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
        max_duration_seconds: Only draw up to this many seconds of the
            capture (from the start). None (default) draws all of it.
        reduce: "max" (peak hold per column) or "mean".
    """
    try:
        iq_file_path = str(storage.resolve(iq_file_path, must_exist=True))
        output_image_path = str(storage.resolve(output_image_path))
    except storage.PathNotAllowed as exc:
        return [_refused(exc)]

    fft_size = max(16, min(int(fft_size), 8192))
    max_samples = _sample_cap(sample_rate_hz, max_duration_seconds)
    freqs_hz, times_s, power_db = iq.stream_spectrogram(
        iq_file_path, sample_rate_hz, fft_size=fft_size, max_samples=max_samples, reduce=reduce
    )
    if power_db.size == 0:
        return [f"'{iq_file_path}' contains no IQ samples (or fewer than fft_size)."]

    iq.render_spectrogram_png(
        output_image_path,
        freqs_hz,
        times_s,
        power_db,
        center_freq_hz,
        title=f"{Path(iq_file_path).name} ({reduce} per column)",
    )

    peak_idx = np.unravel_index(np.argmax(power_db), power_db.shape)
    peak_freq_hz = freqs_hz[peak_idx[0]] + center_freq_hz
    peak_time_s = times_s[peak_idx[1]]
    total = iq.iq_file_samples(iq_file_path)
    samples_read = total if max_samples is None else min(total, max_samples)
    duration_s = samples_read / sample_rate_hz

    summary = (
        f"Wrote {output_image_path} ({duration_s:.2f}s of input at "
        f"{sample_rate_hz}Hz, fft_size={fft_size}, {times_s.size} columns). Peak power "
        f"{power_db[peak_idx]:.1f}dB at {peak_freq_hz / 1e6:.4f}MHz, "
        f"t={peak_time_s:.3f}s.{_coverage(iq_file_path, sample_rate_hz, samples_read)}"
    )
    return [summary, Image(path=output_image_path)]


@mcp.tool(annotations=READ_ONLY)
def hackrf_iq_info(
    iq_file_path: str,
    sample_rate_hz: int,
    center_freq_hz: int = 0,
    fft_size: int = 4096,
    occupied_bw_threshold_db: float = 6.0,
    max_duration_seconds: float | None = None,
) -> str:
    """Summarize a raw IQ capture (from hackrf_transfer, 8-bit signed
    interleaved I/Q): duration, sample count, signal levels, and an estimate
    of which frequencies are occupied.

    This is an offline file-processing tool and does not access the HackRF
    device. Useful for a quick look at a capture before rendering a full
    spectrogram or demodulating it. The file is read in chunks.

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
            capture (from the start). None (default) reads all of it.
    """
    try:
        iq_file_path = str(storage.resolve(iq_file_path, must_exist=True))
    except storage.PathNotAllowed as exc:
        return _refused(exc)

    fft_size = max(16, min(int(fft_size), 65536))
    n, mean_power_db, freqs_hz, power_db = iq.stream_stats(
        iq_file_path, sample_rate_hz, fft_size=fft_size, max_samples=_sample_cap(sample_rate_hz, max_duration_seconds)
    )
    if n == 0:
        return f"'{iq_file_path}' contains no IQ samples."

    duration_s = n / sample_rate_hz
    peak_idx = np.argmax(power_db)
    peak_freq_hz = freqs_hz[peak_idx] + center_freq_hz
    peak_power_db = power_db[peak_idx]

    low_hz, high_hz, bw_hz = iq.occupied_bandwidth(freqs_hz, power_db, threshold_db=occupied_bw_threshold_db)

    return (
        f"'{iq_file_path}': {duration_s:.3f}s ({n} samples) at "
        f"{sample_rate_hz}Hz, center {center_freq_hz / 1e6:.4f}MHz.\n"
        f"Mean power: {mean_power_db:.1f}dB. Peak power: {peak_power_db:.1f}dB "
        f"at {peak_freq_hz / 1e6:.4f}MHz.\n"
        f"Occupied bandwidth (within {occupied_bw_threshold_db:.1f}dB of peak): "
        f"{bw_hz / 1e3:.1f}kHz, from {(low_hz + center_freq_hz) / 1e6:.4f}MHz to "
        f"{(high_hz + center_freq_hz) / 1e6:.4f}MHz.{_coverage(iq_file_path, sample_rate_hz, n)}"
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
    max_duration_seconds: float | None = None,
    channel_freq_hz: int | None = None,
    channel_bandwidth_hz: float | None = None,
    smoothing_ms: float | None = None,
) -> str:
    """Detect bursts of elevated signal power in a raw IQ capture (from
    hackrf_transfer, 8-bit signed interleaved I/Q), e.g. keyfob transmissions,
    packet preambles, or trunked radio control channel activity.

    This is an offline file-processing tool and does not access the HackRF
    device. The file is read in chunks.

    By default the whole capture bandwidth is examined, so a signal much
    narrower than the capture (e.g. a 25 kHz AIS or pager channel in a 2 MHz
    capture) may not raise the total power enough to be seen. Pass
    channel_freq_hz and channel_bandwidth_hz to look at one channel only.
    To actually decode a known protocol, use hackrf_decode instead.

    For each detected burst, reports its start/end time, duration, peak
    power, and dominant frequency (relative to the capture's noise floor).
    Start and end times are accurate to about the smoothing window.

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
            capture (from the start). None (default) reads all of it.
        channel_freq_hz: Absolute frequency of one channel to examine
            (needs channel_bandwidth_hz and center_freq_hz).
        channel_bandwidth_hz: Width of that channel, in Hz.
        smoothing_ms: Length of the power-smoothing window. Defaults to half
            of min_duration_ms, at most 0.5 ms.
    """
    try:
        iq_file_path = str(storage.resolve(iq_file_path, must_exist=True))
    except storage.PathNotAllowed as exc:
        return _refused(exc)

    offset = None
    if channel_freq_hz is not None or channel_bandwidth_hz is not None:
        if channel_freq_hz is None or not channel_bandwidth_hz:
            return "channel_freq_hz and channel_bandwidth_hz must be given together."
        offset = channel_freq_hz - center_freq_hz
        if abs(offset) + channel_bandwidth_hz / 2 > sample_rate_hz / 2:
            return (
                f"{channel_freq_hz / 1e6:.4f} MHz is outside this capture "
                f"({(center_freq_hz - sample_rate_hz / 2) / 1e6:.4f}-{(center_freq_hz + sample_rate_hz / 2) / 1e6:.4f} MHz)."
            )

    bursts, n, rate = iq.detect_bursts_file(
        iq_file_path,
        sample_rate_hz,
        threshold_db=threshold_db,
        min_duration_s=min_duration_ms / 1000,
        merge_gap_s=merge_gap_ms / 1000,
        smoothing_s=None if smoothing_ms is None else smoothing_ms / 1000,
        max_bursts=max_bursts,
        max_samples=_sample_cap(sample_rate_hz, max_duration_seconds),
        channel_offset_hz=offset,
        channel_bandwidth_hz=channel_bandwidth_hz,
    )
    if n == 0:
        return f"'{iq_file_path}' contains no IQ samples."

    duration_s = n / rate
    where = (
        f" in the {channel_bandwidth_hz / 1e3:g} kHz channel at {channel_freq_hz / 1e6:.4f} MHz"
        if offset is not None
        else ""
    )
    coverage = _coverage(iq_file_path, sample_rate_hz, int(round(duration_s * sample_rate_hz)))

    if not bursts:
        return (
            f"No bursts found{where} in {duration_s:.3f}s of input at "
            f"{sample_rate_hz}Hz (threshold {threshold_db:.1f}dB above median).{coverage}"
        )

    lines = [
        f"Found {len(bursts)} burst(s){where} in {duration_s:.3f}s of input at "
        f"{sample_rate_hz}Hz (threshold {threshold_db:.1f}dB above median):{coverage}"
    ]
    for b in bursts:
        freq_hz = center_freq_hz + b["peak_freq_offset_hz"]
        lines.append(
            f"  t={b['start_s']:.4f}-{b['end_s']:.4f}s "
            f"(dur {b['duration_s'] * 1000:.3f}ms), peak {b['peak_db']:.1f}dB, "
            f"freq {freq_hz / 1e6:.4f}MHz"
        )
    return "\n".join(lines)


@mcp.tool(annotations=CONFIG)
@_fill_doc(DECODERS=decoders.tool_help())
def hackrf_decode(
    decoder: DecoderName,
    iq_file_path: str,
    sample_rate_hz: int,
    center_freq_hz: int,
    frequencies_hz: list[int] | None = None,
    max_events: int = 100,
    max_duration_seconds: float | None = None,
    timeout_seconds: float = 240,
    output_path: str | None = None,
    receiver_lat: float | None = None,
    receiver_lon: float | None = None,
) -> str:
    """Decode a protocol in a raw IQ capture (from hackrf_transfer, 8-bit
    signed interleaved I/Q) with an external decoder program, and summarise
    what it found. For ISM-band sensors, remotes and meters use
    hackrf_decode_ism (rtl_433).

    This is an offline file-processing tool and does not access the HackRF
    device. hackrf_server_status lists which decoders are installed.

    Capture first with hackrf_transfer (direction="rx"); each decoder needs
    a particular frequency and sample rate:
    <<DECODERS>>
    Capture 30-60 s for intermittent traffic (AIS, ACARS, pagers, APRS).
    A capture at the wrong rate is refused with what to change.

    Args:
        decoder: Which protocol to decode (see above).
        iq_file_path: Path to the raw IQ capture file, relative to the
            capture directory.
        sample_rate_hz: Sample rate the capture was recorded at, in Hz.
        center_freq_hz: Center frequency the capture was recorded at, in Hz.
        frequencies_hz: Channel frequencies to decode, in Hz, for the
            channelised decoders (acars, vdl2, pocsag, flex, aprs, eas,
            dtmf, rds). Defaults to the protocol's usual US channels where
            it has them; channels outside the capture are skipped.
        max_events: Maximum number of aircraft/ships/messages listed.
        max_duration_seconds: Only decode up to this many seconds of the
            capture (from the start). None (default) decodes all of it.
        timeout_seconds: Maximum time to let the decoder run.
        output_path: If given, also save the decoder program's full output
            to this file in the capture directory.
        receiver_lat: Receiver latitude, for adsb (optional).
        receiver_lon: Receiver longitude, for adsb (optional).
    """
    try:
        path = storage.resolve(iq_file_path, must_exist=True)
        out = storage.resolve(output_path) if output_path else None
    except storage.PathNotAllowed as exc:
        return _refused(exc)
    if iq.iq_file_samples(path) == 0:
        return f"'{path}' contains no IQ samples."
    receiver = None
    if receiver_lat is not None and receiver_lon is not None:
        receiver = (float(receiver_lat), float(receiver_lon))
    max_samples = _sample_cap(sample_rate_hz, max_duration_seconds)
    try:
        result = decoders.decode(
            decoder,
            path,
            sample_rate_hz,
            center_freq_hz,
            frequencies_hz,
            max_samples,
            timeout_seconds,
            storage.data_dir() / ".decode-tmp",
            receiver,
            pager_content=settings.get_live().pager_content,
        )
    except decoders.DecodeError as exc:
        return f"Cannot decode: {exc}"
    text = result.format(max(1, max_events))
    total = iq.iq_file_samples(path)
    read = total if max_samples is None else min(total, max_samples)
    text += _coverage(path, sample_rate_hz, read)
    if out is not None:
        out.write_text(result.raw)
        text += f"\nFull decoder output saved to {out.relative_to(storage.data_dir())}."
    return text


@mcp.tool(annotations=READ_ONLY)
def hackrf_decode_ism(
    iq_file_path: str,
    sample_rate_hz: int,
    center_freq_hz: int,
    protocols: list[int] | None = None,
    max_events: int = 100,
    timeout_seconds: float = 120,
) -> str:
    """Decode ISM-band devices in a raw IQ capture (from hackrf_transfer,
    8-bit signed interleaved I/Q) with rtl_433, which knows 200+ protocols:
    weather stations, temperature/humidity sensors, tire-pressure sensors,
    doorbells, remotes, energy meters and more.

    This is an offline file-processing tool and does not access the HackRF
    device. It needs rtl_433 installed on the server.

    Capture first with hackrf_transfer (direction="rx"). These devices
    transmit in short bursts, often every 30-60 seconds, so capture for at
    least a minute at 2 MHz sample rate (the HackRF minimum) on the band's
    usual frequency: 433.92 MHz (sensors, remotes), 315 MHz (US remotes,
    tire sensors), 345 MHz (US security sensors) or 915 MHz (US ISM).
    Decoders are frequency-tolerant, so the device need only fall inside
    the captured bandwidth.

    Args:
        iq_file_path: Path to the raw IQ capture file, relative to the
            capture directory.
        sample_rate_hz: Sample rate the capture was recorded at, in Hz.
        center_freq_hz: Center frequency the capture was recorded at, in Hz.
        protocols: rtl_433 protocol numbers to enable (rtl_433 -R). Defaults
            to rtl_433's default set.
        max_events: Maximum number of decoded messages to return.
        timeout_seconds: Maximum time to let rtl_433 run.
    """
    try:
        path = storage.resolve(iq_file_path, must_exist=True)
    except storage.PathNotAllowed as exc:
        return _refused(exc)

    args = ["-r", f"cs8:{path}", "-s", str(sample_rate_hz), "-f", str(center_freq_hz)]
    for p in protocols or []:
        args += ["-R", str(int(p))]
    args += ["-F", "json", "-M", "level", "-M", "protocol"]
    output = run("rtl_433", args, timeout=timeout_seconds)

    events = []
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
    if not events:
        tail = output[-1500:]
        return f"rtl_433 decoded nothing in {path.name}.\n--- rtl_433 output (tail) ---\n{tail}"

    models: dict[str, int] = {}
    for e in events:
        models[str(e.get("model", "unknown"))] = models.get(str(e.get("model", "unknown")), 0) + 1
    lines = [
        f"rtl_433 decoded {len(events)} message(s) from {path.name}: "
        + ", ".join(f"{m} x{n}" for m, n in sorted(models.items(), key=lambda kv: -kv[1]))
    ]
    lines += [json.dumps(e, sort_keys=True) for e in events[: max(1, max_events)]]
    if len(events) > max_events:
        lines.append(f"... {len(events) - max_events} more not shown")
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
        f"Bias-tee power: {'allowed' if s.allow_bias_tee else 'locked'}",
        f"Device: {'busy' if _device_lock.locked() else 'idle'}",
        f"Transmit audit log: {s.tx_log_path}",
        "Decoders (hackrf_decode): "
        + ", ".join(
            f"{name} ({Path(prog).name})" if prog else f"{name} (not installed)"
            for name, prog in decoders.available().items()
        ),
        f"ISM decoder (hackrf_decode_ism): {'rtl_433' if shutil.which('rtl_433') else 'rtl_433 not installed'}",
        "Live profiles (hackrf_live_start): "
        + ", ".join(
            f"{name} ({Path(prog).name})" if prog else f"{name} (not installed)"
            for name, prog in live_profiles.available().items()
        ),
        f"Pager message text (FLEX/POCSAG): {'shown and logged' if settings.get_live().pager_content else 'hidden; only metadata is shown or logged'}",
        f"Live decoding: {_live_one_line()}",
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


# --- Live decoding ------------------------------------------------------------
#
# The live service (live_service.py, its own systemd unit) owns the HackRF
# while a profile runs. These tools talk to it over its socket and read its
# log (live_log.py) directly, read-only.

LIVE_NOT_RUNNING = (
    "The live decoder service is not running on this server (hackrf-live.service), "
    "so there is no live decoding. The other tools work as usual."
)


def _utc(ts: float | None) -> str:
    if ts is None:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(ts))


def _ago(ts: float | None, now: float) -> str:
    if ts is None:
        return "never"
    s = max(0, now - ts)
    if s < 120:
        return f"{s:.0f} s ago"
    if s < 7200:
        return f"{s / 60:.0f} min ago"
    return f"{s / 3600:.1f} h ago"


def parse_when(value: str | float | None, now: float) -> float | None:
    """A time for the live tools: "30m", "2h", "1d" or "90s" ago; an ISO 8601
    time (UTC unless it carries an offset); or Unix seconds."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    v = value.strip().lower()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if v[-1:] in units and v[:-1].replace(".", "", 1).isdigit():
        return now - float(v[:-1]) * units[v[-1]]
    if v.replace(".", "", 1).isdigit():
        return float(v)
    from datetime import datetime, timezone

    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"can't read the time {value!r}: use e.g. 30m, 2h, 1d, or 2026-10-29T21:00:00Z"
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _live_one_line() -> str:
    try:
        st = live_client.request({"cmd": "status"}, timeout=3)
    except live_client.Unavailable:
        return "service not running"
    except live_client.LiveError as exc:
        return f"service not answering ({exc})"
    return st["state"] + (f", {st['profile']}" if st.get("profile") else "") + " (see hackrf_live_status)"


def _format_live_status(st: dict) -> str:
    now = st.get("now") or time.time()
    lines = []
    state = st["state"]
    if state == "running":
        lines.append(f"Live decoding: RUNNING {st['profile']} since {_utc(st['running_since'])} ({_ago(st['running_since'], now)}).")
        lines.append(f"  Tuning: {st['tuning']}")
    elif state == "paused":
        h = st["hold"] or {}
        lines.append(
            f"Live decoding: PAUSED by {h.get('owner')} since {_utc(h.get('since'))}; resumes when it finishes "
            f"or at {_utc(h.get('until'))} at the latest" + (f" (was running {h['stopped']})." if h.get("stopped") else ".")
        )
    elif state == "waiting":
        lines.append(f"Live decoding: WAITING to retry {st['target']} at {_utc(st.get('retry_at'))} after an error.")
    elif state == "starting":
        lines.append(f"Live decoding: STARTING {st['target']}.")
    else:
        lines.append("Live decoding: IDLE (no profile running; the HackRF is free).")
    d = st.get("desired")
    if d and "schedule" in d:
        parts = [f"{e['profile']} {e['minutes']:g} min" for e in d["schedule"]]
        lines.append(f"  Rotation: {' -> '.join(parts)}, repeating; next switch {_utc(st.get('next_switch'))}.")
    if state != "idle" or st.get("decoded_total"):
        lines.append(
            f"  Decoded messages: {st['decoded_1m']} in the last minute, {st['decoded_10m']} in 10 min, "
            f"{st['decoded_total']} since the service started."
        )
        lines.append(
            f"  Log rows: {st['rows_10m']} in 10 min, {st['rows_total']} since the service started; "
            f"last row {_ago(st.get('last_row_at'), now)}."
        )
    h = st.get("health") or {}
    if h.get("input_ratio") is not None or h.get("input_power_dbfs") is not None:
        bits = []
        if h.get("input_ratio") is not None:
            bits.append(f"samples arriving at {h['input_ratio']:.0%} of the sample rate")
        if h.get("input_power_dbfs") is not None:
            bits.append(f"input power {h['input_power_dbfs']:.1f} dBFS")
        lines.append("  Input: " + ", ".join(bits) + (" -- FALLING BEHIND, samples are being lost" if st.get("falling_behind") else "") + ".")
    if h.get("channel_levels_dbfs"):
        lines.append(
            "  Channel levels (last ~40 ms, dBFS): "
            + ", ".join(f"{f} MHz {v if v is not None else '-'}" for f, v in h["channel_levels_dbfs"].items())
        )
    if st.get("last_error"):
        lines.append(f"  Last error ({_utc(st.get('last_error_at'))}): {st['last_error']}")
    if st.get("restarts"):
        lines.append(f"  Restarts after errors since the service started: {st['restarts']}")
    if st.get("stderr_tail") and (state != "running" or st.get("last_error")):
        lines.append("  Decoder output (tail): " + " | ".join(st["stderr_tail"]))
    log = st.get("log") or {}
    if log:
        lines.append(
            f"  Log: {log.get('path')}, {log.get('file_bytes', 0) / storage.MIB:.1f} MiB of {log.get('max_db_mb'):g} MiB, "
            f"rows kept {log.get('max_age_days'):g} days; pager message text "
            f"{'IS logged' if log.get('pager_content') else 'is not logged (metadata only)'} (HACKRF_MCP_PAGER_CONTENT)."
        )
        if log.get("rows_dropped"):
            lines.append(f"  Rows dropped: {log['rows_dropped']}" + (" (free disk below the floor)" if log.get("low_disk") else ""))
    sp = st.get("spectrum") or {}
    if sp and not sp.get("enabled"):
        lines.append("  Spectrum history: off (HACKRF_LIVE_SPECTRUM).")
    elif sp:
        seg = sp.get("segment")
        lines.append(
            f"  Spectrum history: {sp.get('path')}, {sp.get('file_bytes', 0) / storage.MIB:.1f} MiB of "
            f"{sp.get('max_mb'):g} MiB, second rows kept {sp.get('max_age_days'):g} days and minute rows "
            f"{sp.get('minute_days'):g} days; {sp.get('rows_recorded', 0)} seconds recorded since the service "
            f"started, last {_ago(sp.get('last_row_at'), now)}"
            + (
                f"; recording {seg['profile']} at {seg['center_hz'] / 1e6:.4f} MHz, "
                f"{seg['sample_rate_hz'] / 1e6:g} MS/s wide."
                if seg
                else (
                    f"; not recording: {st['profile']}'s decoder opens the HackRF itself."
                    if state == "running"
                    else "; not recording while nothing is decoding."
                )
            )
        )
        if sp.get("rows_dropped"):
            lines.append(
                f"  Spectrum rows dropped: {sp['rows_dropped']}" + (" (free disk below the floor)" if sp.get("low_disk") else "")
            )
    if h.get("spectrum_tap_errors"):
        lines.append(f"  Spectrum tap errors: {h['spectrum_tap_errors']}, last: {h.get('spectrum_tap_error')}")
    lines.append(f"  Service started {_utc(st.get('service_started'))}, pid {st.get('pid')}.")
    return "\n".join(lines)


class LiveScheduleEntry(BaseModel):
    profile: ProfileName
    minutes: float = Field(description="How long this entry runs each time round, 1-1440.")
    frequencies_hz: list[int] | None = Field(default=None, description="Channels, for flex/pocsag (required) or to narrow the others.")
    lna_gain_db: int | None = None
    vga_gain_db: int | None = None
    amp_enable: bool = False


@mcp.tool(annotations=READ_ONLY)
def hackrf_live_status() -> str:
    """Show what the live decoder service is doing: running, paused, idle or
    waiting to retry after an error; the profile and its tuning and since
    when; decoded-message and log-row rates; whether samples are arriving
    at the full rate; the last error; a rotation schedule if one is set;
    the size and limits of the log and of the spectrum history (one row of
    mean and peak power per bin per second of the band being decoded); and
    recent events (starts, stops, pauses, errors) from the log.

    This does not access the HackRF device.
    """
    try:
        st = live_client.request({"cmd": "status"}, timeout=5)
    except live_client.Unavailable:
        return LIVE_NOT_RUNNING
    except live_client.LiveError as exc:
        return f"The live decoder service did not answer: {exc}"
    out = _format_live_status(st)
    try:
        conn = live_log.open_reader(settings.get_live().db_path)
        try:
            ev = live_log.events(conn, limit=8)
        finally:
            conn.close()
        if ev:
            out += "\nRecent events (newest first):\n" + "\n".join(
                f"  {_utc(e['ts'])} {e['event']}" + (f" [{e['profile']}]" if e["profile"] else "")
                + (f": {e['detail']}" if e["detail"] else "")
                for e in ev
            )
    except (live_log.LogNotFound, sqlite3.Error):
        pass
    return out


@mcp.tool(annotations=CONFIG)
@_fill_doc(PROFILES=live_profiles.tool_help())
def hackrf_live_start(
    profile: ProfileName | None = None,
    frequencies_hz: list[int] | None = None,
    lna_gain_db: int | None = None,
    vga_gain_db: int | None = None,
    amp_enable: bool = False,
    schedule: list[LiveScheduleEntry] | None = None,
) -> str:
    """Start live decoding, or switch it to another profile: this is how to
    move the receiver to another band. The live service decodes the profile
    continuously into a rolling log (see hackrf_live_search and
    hackrf_live_summary) until it is stopped or switched, and resumes it
    after a restart. The profiles, and what each logs:
    <<PROFILES>>

    schedule rotates through two or more profiles instead, e.g.
    [{"profile": "adsb", "minutes": 10}, {"profile": "ais", "minutes": 10}],
    repeating until stopped; leave profile out when giving one.

    NOTE: while a profile runs, the live service holds the HackRF. The other
    device tools (hackrf_transfer, hackrf_sweep, hackrf_info, ...) still
    work: each pauses live decoding for the length of its call, and it
    resumes by itself afterwards.

    SAFETY: amp_enable adds ~11-14 dB of gain and can overload the receiver
    on strong nearby signals. The live service never turns on bias-tee
    power and never transmits.

    Args:
        profile: The profile to run (see above).
        frequencies_hz: Channel frequencies in Hz: required for flex and
            pocsag; for acars, vdl2, aprs and eas they narrow the default
            channels. Not used for adsb and ais.
        lna_gain_db: RX LNA gain, 0-40 dB in 8 dB steps (default per profile).
        vga_gain_db: RX VGA gain, 0-62 dB in 2 dB steps (default per profile).
        amp_enable: Turn on the HackRF's RF amplifier.
        schedule: A rotation, instead of profile.
    """
    if schedule:
        if profile is not None:
            return "Give either profile or schedule, not both."
        req = {
            "cmd": "start",
            "schedule": [
                {
                    "profile": e.profile,
                    "minutes": e.minutes,
                    "freqs_hz": e.frequencies_hz or [],
                    "lna_gain_db": e.lna_gain_db,
                    "vga_gain_db": e.vga_gain_db,
                    "amp": e.amp_enable,
                }
                for e in schedule
            ],
        }
    elif profile is not None:
        req = {
            "cmd": "start",
            "profile": profile,
            "freqs_hz": frequencies_hz or [],
            "lna_gain_db": lna_gain_db,
            "vga_gain_db": vga_gain_db,
            "amp": amp_enable,
        }
    else:
        return "Give a profile to start (or a schedule)."
    try:
        st = live_client.request(req, timeout=30)
    except live_client.Unavailable:
        return LIVE_NOT_RUNNING
    except live_client.LiveError as exc:
        return f"Not started: {exc}"
    return _format_live_status(st)


@mcp.tool(annotations=CONFIG)
def hackrf_live_stop() -> str:
    """Stop live decoding and release the HackRF. The service stays up,
    idle, and the log is kept; hackrf_live_start starts it again.
    """
    try:
        st = live_client.request({"cmd": "stop"}, timeout=30)
    except live_client.Unavailable:
        return LIVE_NOT_RUNNING
    except live_client.LiveError as exc:
        return f"Not stopped: {exc}"
    return _format_live_status(st)


_FIELD_KINDS = {"acars", "vdl2", "ais"}


def _row_line(r) -> str:
    head = (
        f"{_utc(r['ts'])}"
        + (f"..{time.strftime('%H:%M:%SZ', time.gmtime(r['ts_end']))}" if r["ts_end"] and r["ts_end"] - r["ts"] >= 1 else "")
        + f" {r['profile']}/{r['kind']}"
        + (f" {r['subject']}" if r["subject"] else "")
        + (f" ({r['label']})" if r["label"] else "")
        + (f" {r['freq_hz'] / 1e6:.4f} MHz" if r["freq_hz"] else "")
        + (f" {r['level_db']:.1f} dB" if r["level_db"] is not None else "")
    )
    body = r["fields"] if r["kind"] in _FIELD_KINDS else (r["raw"] or r["fields"])
    body = body.replace("\n", " / ")
    if len(body) > 400:
        body = body[:400] + "..."
    return f"  {head} | {body}"


@mcp.tool(annotations=READ_ONLY)
def hackrf_live_search(
    since: str | None = "24h",
    until: str | None = None,
    profile: ProfileName | None = None,
    subject: str | None = None,
    text: str | None = None,
    frequency_hz: int | None = None,
    kind: str | None = None,
    limit: int = 50,
) -> str:
    """Search the live decoding log, newest first.

    Rows are: one per aircraft per minute (adsb), one per ship per minute
    for AIS position reports and one per other AIS message (ais), and one
    per decoded message for the other profiles. Each shows its time (a
    range for per-minute rows), profile/kind, subject (ICAO address, MMSI,
    capcode, registration or APRS source), label (callsign, flight or ship
    name), frequency, signal level, and the decoded text or fields.

    This does not access the HackRF device.

    Args:
        since: Start of the time range: "30m", "2h", "1d" ago, an ISO 8601
            time (UTC unless it has an offset), or Unix seconds. Default 24h.
        until: End of the time range, in the same forms. Default now.
        profile: Only rows from this profile.
        subject: An exact identity, case-insensitive: ICAO address
            (A1B2C3), MMSI, pager capcode (leading zeros optional), APRS
            callsign, aircraft registration, or a label such as a
            callsign, flight number or ship name.
        text: Free text, matched as a substring of the decoded text and
            fields.
        frequency_hz: Only rows heard within 5 kHz of this frequency.
        kind: Only rows of this kind (aircraft, vessel, base-station,
            aid-to-navigation, ais, acars, vdl2, page, aprs, eas).
        limit: Maximum rows to return (default 50, at most 500).
    """
    now = time.time()
    try:
        t0, t1 = parse_when(since, now), parse_when(until, now)
    except ValueError as exc:
        return str(exc)
    try:
        conn = live_log.open_reader(settings.get_live().db_path)
    except live_log.LogNotFound as exc:
        return str(exc)
    try:
        rows = live_log.search(
            conn,
            since=t0,
            until=t1,
            profile=profile,
            kind=kind,
            subject=subject,
            text=text,
            freq_hz=frequency_hz,
            limit=min(max(1, limit), 500),
        )
    finally:
        conn.close()
    what = f"from {_utc(t0)}" if t0 else "from the start of the log"
    what += f" to {_utc(t1)}" if t1 else " to now"
    if not rows:
        return f"No rows {what} match."
    lines = [f"{len(rows)} row(s) {what}, newest first" + (" (limit reached)" if len(rows) >= limit else "") + ":"]
    lines += [_row_line(r) for r in rows]
    return "\n".join(lines)


@mcp.tool(annotations=READ_ONLY)
def hackrf_live_summary(
    profile: ProfileName,
    since: str | None = "1h",
    until: str | None = None,
    limit: int = 100,
) -> str:
    """Summarise the live decoding log by subject over a time range: e.g.
    the aircraft seen in the last hour (profile adsb), ships today (ais,
    since "1d"), or which pager capcodes were active (flex). One line per
    aircraft, ship, capcode or station, most recently heard first, with
    first and last time heard, rows, frequencies, strongest signal and the
    latest values of its main fields (summed message counts, altitude range).

    This does not access the HackRF device.

    Args:
        profile: Which profile's rows to summarise.
        since: Start of the range ("30m", "2h", "1d", ISO 8601 or Unix
            seconds). Default 1h.
        until: End of the range. Default now.
        limit: Maximum subjects listed (default 100).
    """
    now = time.time()
    try:
        t0 = parse_when(since, now) or 0.0
        t1 = parse_when(until, now)
    except ValueError as exc:
        return str(exc)
    try:
        conn = live_log.open_reader(settings.get_live().db_path)
    except live_log.LogNotFound as exc:
        return str(exc)
    try:
        spec = live_profiles.PROFILES[profile]
        entries = live_log.summarise(conn, profile, spec.summary_kinds, t0, t1, limit=min(max(1, limit), 1000) + 1)
    finally:
        conn.close()
    what = f"{profile} from {_utc(t0)} to {_utc(t1) if t1 else 'now'}"
    if not entries:
        return f"Nothing logged for {what}."
    more = len(entries) > limit
    entries = entries[:limit]
    noun = {"adsb": "aircraft", "ais": "stations"}.get(profile, "subjects")
    lines = [f"{len(entries)}{'+' if more else ''} {noun} for {what}, most recently heard first:"]
    for e in entries:
        parts = [e["subject"]]
        if e["labels"]:
            parts.append("(" + ", ".join(e["labels"][:3]) + ")")
        parts.append(f"{_utc(e['first'])} to {time.strftime('%H:%M:%SZ', time.gmtime(e['last']))}")
        parts.append(f"rows {e['rows']}")
        if e["freqs"]:
            parts.append("on " + ", ".join(f"{f / 1e6:.4f}" for f in sorted(e["freqs"])[:4]) + " MHz")
        if e["max_level_db"] is not None:
            parts.append(f"max {e['max_level_db']:.1f} dB")
        f = e["fields"]
        for key in spec.summary_fields:
            if key in f:
                v = f[key]
                if isinstance(v, list):
                    v = ",".join(str(x) for x in v[:6])
                parts.append(f"{key}={v}")
        lines.append("  " + " ".join(str(p) for p in parts))
    if more:
        lines.append(f"  ... more not shown; raise limit or narrow the range.")
    return "\n".join(lines)


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
            "HACKRF_MCP_TOKEN_FILE, or the token itself in HACKRF_MCP_TOKEN. "
            "HACKRF_MCP_TOKEN_SHA256 adds comma-separated SHA-256 hex digests "
            "of further accepted tokens."
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
        live = settings.LiveSettings.from_env()
    except (ValueError, OSError) as exc:
        parser.error(f"invalid configuration: {exc}")
    settings.configure(s)
    settings.configure_live(live)

    if args.transport == "stdio":
        mcp.run()
        return

    from starlette.middleware import Middleware

    from hackrf_mcp.auth import BearerTokenMiddleware, load_token_digests

    try:
        digests = load_token_digests(args.token_file)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    if not digests:
        parser.error(
            "http transport needs a bearer token: pass --token-file, or set "
            "HACKRF_MCP_TOKEN_FILE, HACKRF_MCP_TOKEN or HACKRF_MCP_TOKEN_SHA256"
        )
    mcp.run(
        transport="http",
        host=args.host,
        port=args.port,
        path=args.path,
        middleware=[Middleware(BearerTokenMiddleware, digests=digests)],
    )


if __name__ == "__main__":
    main()
