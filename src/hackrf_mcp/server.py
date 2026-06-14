"""MCP server wrapping the hackrf_tools CLI for controlling a HackRF SDR."""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path
from typing import Literal

import numpy as np
from fastmcp import FastMCP
from fastmcp.utilities.types import Image
from mcp.types import ToolAnnotations

from hackrf_mcp import iq
from hackrf_mcp.cli import run, set_bin_dir

mcp = FastMCP(
    "hackrf",
    instructions=(
        "Tools for controlling a HackRF SDR via hackrf_tools.\n\n"
        "IMPORTANT: a HackRF device can only be accessed by one process at a "
        "time (it opens an exclusive USB connection). Only call one of "
        "hackrf_info, hackrf_sweep, hackrf_transfer, hackrf_clock, "
        "hackrf_operacake, or hackrf_biast at a time, and wait for it to "
        "finish before starting another. If a tool fails with a "
        "'device is busy' / 'resource busy' / 'access denied' style error, "
        "another hackrf_tools process (including a previous call that is "
        "still running or was not cleanly terminated) is likely still "
        "holding the device. hackrf_sweep and hackrf_transfer can run for "
        "up to their timeout_seconds, so avoid issuing another HackRF tool "
        "call until one of those returns.\n\n"
        "hackrf_iq_to_wav and hackrf_iq_spectrogram are offline analysis "
        "tools that operate on capture files already written by "
        "hackrf_transfer. They do not access the device and can be run "
        "anytime, including while the device is busy."
    ),
)

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
CONFIG = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
DANGEROUS = ToolAnnotations(readOnlyHint=False, destructiveHint=True)


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
    return run("hackrf_info", args, timeout=15)


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
) -> str:
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
    """
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
        log = run("hackrf_sweep", args, timeout=timeout_seconds)
        data = out_path.read_text(errors="replace")
    finally:
        out_path.unlink(missing_ok=True)

    max_chars = 50_000
    if len(data) > max_chars:
        data = data[:max_chars] + "\n... (output truncated)"

    return f"{log}\n--- sweep data ---\n{data}".strip()


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
    sample_rate_hz. For tx, the transfer ends when the input file is exhausted
    unless repeat_tx is set, in which case it runs until timeout_seconds.

    NOTE: this holds the HackRF's exclusive USB connection for up to
    timeout_seconds (longer for repeat_tx). Do not call this or any other
    hackrf_* tool again until it returns.

    SAFETY: Transmitting radio signals (direction="tx") puts the HackRF into
    TX mode and radiates RF on frequency_hz. This may be subject to
    regulations in your jurisdiction (e.g. FCC rules in the US) — only
    transmit on frequencies and at power levels you are authorized to use,
    and consider using a dummy load or shielded enclosure for testing.
    Enabling antenna_enable (bias-tee) applies DC power to the antenna port
    and can damage equipment connected to that port that does not expect DC
    bias. Enabling amp_enable adds significant additional gain (~11-14dB);
    combined with antenna_enable on a strong nearby signal it can overdrive
    the receiver front end.

    Args:
        direction: "rx" to receive into file_path, "tx" to transmit from
            file_path.
        file_path: Path to the IQ sample file to read from or write to.
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
    args: list[str] = []
    if direction == "rx":
        args += ["-r", file_path]
        if num_samples is None:
            secs = duration_seconds if duration_seconds is not None else 5.0
            num_samples = int(secs * sample_rate_hz)
        args += ["-n", str(num_samples)]
    else:
        args += ["-t", file_path]
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

    return run("hackrf_transfer", args, timeout=timeout_seconds)


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
    return run("hackrf_clock", args, timeout=15)


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
    return run("hackrf_operacake", args, timeout=15)


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
    return run("hackrf_biast", args, timeout=15)


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
) -> str:
    """Demodulate a raw IQ capture (from hackrf_transfer, 8-bit signed
    interleaved I/Q) to a WAV audio file.

    This is an offline file-processing tool and does not access the HackRF
    device.

    Args:
        iq_file_path: Path to the raw IQ capture file (e.g. from
            `hackrf_transfer` with `direction="rx"`).
        sample_rate_hz: Sample rate the capture was recorded at, in Hz.
        mode: Demodulation mode: "nfm" (narrowband FM, e.g. analog two-way
            radio), "wfm" (wideband FM, e.g. broadcast FM radio), "am"
            (amplitude modulation), "usb"/"lsb" (single-sideband voice).
        output_wav_path: Path to write the demodulated WAV file to.
        center_freq_hz: Center frequency the capture was recorded at, in Hz.
            Used together with target_freq_hz to compute the tuning offset.
        target_freq_hz: Frequency of the signal of interest, in Hz, if it is
            not at center_freq_hz. The signal is mixed down to baseband
            before demodulation. Defaults to center_freq_hz (no shift).
        bandwidth_hz: Channel bandwidth to filter to before demodulation, in
            Hz. Defaults to a typical value for `mode` (e.g. 12.5kHz for
            nfm, 180kHz for wfm).
        audio_rate_hz: Output WAV sample rate, in Hz.
        max_duration_seconds: Only process up to this many seconds of the
            input capture (from the start), to bound memory/runtime for
            large files. Set to None to process the whole file (use with
            caution on large captures).
    """
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

    audio = iq.demodulate(baseband, sample_rate_hz, mode, bandwidth_hz=bandwidth_hz)
    audio = iq.resample_to_rate(audio, sample_rate_hz, audio_rate_hz)
    iq.write_wav(output_wav_path, audio, audio_rate_hz)

    duration_s = samples.size / sample_rate_hz
    return (
        f"Wrote {output_wav_path} ({audio.size / audio_rate_hz:.2f}s @ "
        f"{audio_rate_hz}Hz) demodulated as {mode} from {duration_s:.2f}s of "
        f"input at {sample_rate_hz}Hz."
    )


@mcp.tool(annotations=CONFIG)
def hackrf_iq_spectrogram(
    iq_file_path: str,
    sample_rate_hz: int,
    output_image_path: str,
    center_freq_hz: int = 0,
    fft_size: int = 1024,
    max_duration_seconds: float | None = 10.0,
) -> list:
    """Render a spectrogram (waterfall) PNG from a raw IQ capture (from
    hackrf_transfer, 8-bit signed interleaved I/Q).

    Useful for visually identifying bursts, framing/timing patterns (e.g.
    keyfobs, trunked radio control channels), and occupied bandwidth.

    This is an offline file-processing tool and does not access the HackRF
    device.

    Args:
        iq_file_path: Path to the raw IQ capture file (e.g. from
            `hackrf_transfer` with `direction="rx"`).
        sample_rate_hz: Sample rate the capture was recorded at, in Hz.
        output_image_path: Path to write the spectrogram PNG to.
        center_freq_hz: Center frequency the capture was recorded at, in Hz.
            Used to label the frequency axis in absolute terms.
        fft_size: FFT size (frequency resolution) for the spectrogram.
        max_duration_seconds: Only process up to this many seconds of the
            input capture (from the start), to bound memory/runtime for
            large files. Set to None to process the whole file (use with
            caution on large captures).
    """
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


def main() -> None:
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
    args = parser.parse_args()

    set_bin_dir(args.bin_dir)
    mcp.run()


if __name__ == "__main__":
    main()
