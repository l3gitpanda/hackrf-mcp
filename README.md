# hackrf-mcp

An MCP server that wraps the [hackrf_tools](https://github.com/greatscottgadgets/hackrf)
command-line utilities, letting Claude inspect and control a connected
[HackRF](https://greatscottgadgets.com/hackrf/) software-defined radio.

## Requirements

- A HackRF device connected via USB.
- `hackrf_tools` (`hackrf_info`, `hackrf_sweep`, `hackrf_transfer`,
  `hackrf_clock`, `hackrf_operacake`, `hackrf_biast`), either on your `PATH`
  or in a folder you point the server at with `--bin-dir`/`HACKRF_TOOLS_DIR`
  (see below).
- Python 3.10+.
- `numpy`, `scipy`, and `matplotlib` (installed via `uv sync`) for the
  `hackrf_iq_to_wav` and `hackrf_iq_spectrogram` analysis tools.

## Install

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

## Tests

```bash
uv run pytest
```

Tests mock out `hackrf_mcp.cli.run`/`subprocess`, so they don't require a
HackRF device or `hackrf_tools` to be installed.

## Run

```bash
uv run hackrf-mcp
```

This starts the server over stdio.

### Locating hackrf_tools binaries

If `hackrf_info`, `hackrf_sweep`, etc. aren't on your `PATH`, point the server
at the folder containing them with `--bin-dir` or the `HACKRF_TOOLS_DIR`
environment variable:

```bash
uv run hackrf-mcp --bin-dir "C:/Users/me/AppData/Local/hackrf-tools"
# or
HACKRF_TOOLS_DIR="C:/Users/me/AppData/Local/hackrf-tools" uv run hackrf-mcp
```

`--bin-dir` is checked first, then PATH.

### Claude Code

```bash
claude mcp add hackrf -- uv run --directory D:/hackrf-mcp hackrf-mcp --bin-dir "C:/Users/me/AppData/Local/hackrf-tools"
```

### Claude Desktop

Add it to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "hackrf": {
      "command": "uv",
      "args": [
        "run", "--directory", "D:/hackrf-mcp", "hackrf-mcp",
        "--bin-dir", "C:/Users/me/AppData/Local/hackrf-tools"
      ]
    }
  }
}
```

## Tools

| Tool | Purpose |
|---|---|
| `hackrf_info` | Show connected device info (board ID, firmware, serial). |
| `hackrf_sweep` | Sweep a frequency range and report power levels per bin. |
| `hackrf_transfer` | Receive IQ samples to a file, or transmit from a file. |
| `hackrf_clock` | Read/configure CLKIN/CLKOUT and Pro P1/P2 connectors. |
| `hackrf_operacake` | List/configure Opera Cake antenna switch boards. |
| `hackrf_biast` | Enable/disable/configure bias-tee antenna power. |
| `hackrf_iq_to_wav` | Demodulate a raw IQ capture (NFM/WFM/AM/USB/LSB) to a WAV file. |
| `hackrf_iq_spectrogram` | Render a spectrogram PNG from a raw IQ capture. |

`hackrf_iq_to_wav` and `hackrf_iq_spectrogram` are offline analysis tools (numpy/scipy/matplotlib) that operate on capture files written by `hackrf_transfer` — they don't touch the device, so they can be run anytime, including while the HackRF is busy.

The lower-level/destructive tools (`hackrf_debug`, `hackrf_cpldjtag`,
`hackrf_spiflash` — register access, CPLD JTAG programming, and SPI flash
firmware read/write) are implemented but currently disabled (commented out in
`server.py`), since misuse can leave the device in a bad state or require
re-flashing. Uncomment them in `src/hackrf_mcp/server.py` if you need them.

## Safety notes

- **Transmitting (`hackrf_transfer` with `direction="tx"`)** puts the HackRF
  into TX mode and radiates RF. Transmitting is subject to regulations in
  your jurisdiction (e.g. FCC Part 97/15 in the US) — only transmit on
  frequencies and at power levels you are authorized to use, and prefer a
  dummy load or shielded enclosure when testing.
- **Gain settings**: start conservative (RF amp off, LNA ~16dB, VGA ~16dB)
  and increase gradually. Excessive gain doesn't damage the hardware but can
  cause distortion/spurious signals; too little gain buries the signal in
  noise. See [setting_gain](https://hackrf.readthedocs.io/en/latest/setting_gain.html).
- **`hackrf_spiflash` writes firmware** to SPI flash. Only write `.bin` images
  from a [trusted HackRF release](https://github.com/greatscottgadgets/hackrf/releases),
  and don't disconnect the device mid-write. If the firmware is damaged, the
  device can usually be recovered by booting into DFU mode (hold the DFU
  button while powering on) and reflashing via `dfu-util` — see
  [updating_firmware](https://hackrf.readthedocs.io/en/latest/updating_firmware.html).
- **`hackrf_cpldjtag` and `hackrf_debug` register writes** can leave the
  device in a non-working state until reset or re-flashed; use only with
  files/values from trusted sources.
