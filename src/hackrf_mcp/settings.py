"""Server settings, read once at startup from HACKRF_MCP_* environment
variables (and CLI flags in main()).

Nothing here can be changed through an MCP tool: the transmit mode in
particular is the operator's decision, not the model's.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

# "off": never transmit. "allowed-bands": transmit only when the signal fits
# inside one of the configured bands (see policy.py). "unrestricted": transmit
# on any frequency the hardware accepts.
TX_MODES = ("off", "allowed-bands", "unrestricted")


def _default_state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    return (Path(base) if base else Path.home() / ".local" / "state") / "hackrf-mcp"


@dataclass(frozen=True)
class Settings:
    # Every file path a tool accepts is resolved inside this directory.
    data_dir: Path = field(default_factory=lambda: Path.home() / "hackrf-captures")
    tx_mode: str = "off"
    # Named band sets from policy.BUILTIN_BAND_SETS (or bands_file) that
    # "allowed-bands" mode permits.
    tx_band_sets: tuple[str, ...] = ("ism",)
    # Extra (low_hz, high_hz) ranges added to the selected band sets.
    tx_extra_ranges: tuple[tuple[float, float], ...] = ()
    bands_file: Path | None = None
    tx_log_path: Path = field(default_factory=lambda: _default_state_dir() / "tx-audit.jsonl")
    max_rx_seconds: float = 60.0
    max_capture_mb: float = 2048.0
    # Refuse a capture that would leave less than this much free disk.
    min_free_mb: float = 2048.0
    # Upper bound on any transmission, including repeat_tx.
    max_tx_seconds: float = 60.0
    # Bias-tee DC power on the antenna port (hackrf_biast, antenna_enable)
    # stays locked unless the operator allows it: it can damage an antenna,
    # LNA or filter that isn't built for it.
    allow_bias_tee: bool = False

    def __post_init__(self) -> None:
        if self.tx_mode not in TX_MODES:
            raise ValueError(f"tx_mode must be one of {', '.join(TX_MODES)}, not {self.tx_mode!r}")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        kwargs: dict = {}
        if v := env.get("HACKRF_MCP_DATA_DIR"):
            kwargs["data_dir"] = Path(v).expanduser()
        if v := env.get("HACKRF_MCP_TX_MODE"):
            kwargs["tx_mode"] = v.strip().lower()
        if v := env.get("HACKRF_MCP_TX_BANDS"):
            kwargs["tx_band_sets"] = parse_band_set_names(v)
        if v := env.get("HACKRF_MCP_TX_EXTRA_RANGES"):
            kwargs["tx_extra_ranges"] = parse_mhz_ranges(v)
        if v := env.get("HACKRF_MCP_BANDS_FILE"):
            kwargs["bands_file"] = Path(v).expanduser()
        if v := env.get("HACKRF_MCP_TX_LOG"):
            kwargs["tx_log_path"] = Path(v).expanduser()
        for name, key in (
            ("max_rx_seconds", "HACKRF_MCP_MAX_RX_SECONDS"),
            ("max_capture_mb", "HACKRF_MCP_MAX_CAPTURE_MB"),
            ("min_free_mb", "HACKRF_MCP_MIN_FREE_MB"),
            ("max_tx_seconds", "HACKRF_MCP_MAX_TX_SECONDS"),
        ):
            if v := env.get(key):
                kwargs[name] = float(v)
        if v := env.get("HACKRF_MCP_ALLOW_BIAS_TEE"):
            kwargs["allow_bias_tee"] = parse_bool(v)
        return cls(**kwargs)


def parse_bool(value: str) -> bool:
    v = value.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"expected true or false, not {value!r}")


def parse_band_set_names(value: str) -> tuple[str, ...]:
    """Parse "ism,amateur" into ("ism", "amateur"); "none" means no sets."""
    names = tuple(n.strip().lower() for n in value.split(",") if n.strip())
    return () if names == ("none",) else names


def parse_mhz_ranges(value: str) -> tuple[tuple[float, float], ...]:
    """Parse "144-148, 420.5-421" (MHz) into ((144e6, 148e6), (420.5e6, 421e6))."""
    ranges = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        low, sep, high = part.partition("-")
        if not sep:
            raise ValueError(f"range {part!r} must look like LOW-HIGH in MHz")
        low_hz, high_hz = float(low) * 1e6, float(high) * 1e6
        if low_hz >= high_hz:
            raise ValueError(f"range {part!r} has its low edge above its high edge")
        ranges.append((low_hz, high_hz))
    return tuple(ranges)


_current = Settings()


def get() -> Settings:
    return _current


def configure(settings: Settings) -> None:
    global _current
    _current = settings
