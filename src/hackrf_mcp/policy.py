"""Transmit policy: decide whether a transmission is allowed under the
server's tx_mode and band configuration.

The band check is a frequency check only. It confirms that the carrier and
the measured occupied bandwidth of the signal sit inside one allowed band. It
does not check transmit power, modulation, out-of-band emissions or
harmonics, or which bands a given license class may use; those remain the
operator's responsibility.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from hackrf_mcp.settings import Settings

# Band edges in MHz, as (low, high, label). US allocations: the ISM bands that
# FCC Parts 15/18 devices use, and the Part 97 amateur bands inside the
# HackRF's range (the channelized 60 m band is left out). Override or extend
# these with a JSON bands file:
#   {"sets": {"name": [[low_mhz, high_mhz, "label"], ...]}}
BUILTIN_BAND_SETS: dict[str, list[tuple[float, float, str]]] = {
    "ism": [
        (13.553, 13.567, "13.56 MHz ISM"),
        (26.957, 27.283, "27 MHz ISM"),
        (40.66, 40.70, "40.68 MHz ISM"),
        (902.0, 928.0, "915 MHz ISM"),
        (2400.0, 2483.5, "2.4 GHz ISM"),
        (5725.0, 5875.0, "5.8 GHz ISM"),
    ],
    "amateur": [
        (1.8, 2.0, "160 m"),
        (3.5, 4.0, "80 m"),
        (7.0, 7.3, "40 m"),
        (10.1, 10.15, "30 m"),
        (14.0, 14.35, "20 m"),
        (18.068, 18.168, "17 m"),
        (21.0, 21.45, "15 m"),
        (24.89, 24.99, "12 m"),
        (28.0, 29.7, "10 m"),
        (50.0, 54.0, "6 m"),
        (144.0, 148.0, "2 m"),
        (222.0, 225.0, "1.25 m"),
        (420.0, 450.0, "70 cm"),
        (902.0, 928.0, "33 cm"),
        (1240.0, 1300.0, "23 cm"),
        (2300.0, 2310.0, "13 cm"),
        (2390.0, 2450.0, "13 cm"),
        (5650.0, 5925.0, "5 cm"),
    ],
}


@dataclass(frozen=True)
class Band:
    low_hz: float
    high_hz: float
    label: str
    set_name: str

    def contains(self, low_hz: float, high_hz: float) -> bool:
        return self.low_hz <= low_hz and high_hz <= self.high_hz

    def describe(self) -> str:
        return f"{self.label} ({self.low_hz / 1e6:g}-{self.high_hz / 1e6:g} MHz, {self.set_name})"


@dataclass(frozen=True)
class TxDecision:
    allowed: bool
    reason: str
    band: Band | None = None
    span_hz: tuple[float, float] | None = None


def load_band_sets(bands_file: Path | None = None) -> dict[str, list[tuple[float, float, str]]]:
    sets = {name: list(bands) for name, bands in BUILTIN_BAND_SETS.items()}
    if bands_file is not None:
        data = json.loads(Path(bands_file).read_text())
        for name, bands in data.get("sets", {}).items():
            sets[name.lower()] = [(float(lo), float(hi), str(label)) for lo, hi, label in bands]
    return sets


def allowed_bands(settings: Settings) -> list[Band]:
    """The bands "allowed-bands" mode permits: the selected sets plus any
    extra ranges. Raises ValueError for an unknown set name."""
    sets = load_band_sets(settings.bands_file)
    bands: list[Band] = []
    for name in settings.tx_band_sets:
        if name not in sets:
            raise ValueError(f"unknown band set {name!r}; known sets: {', '.join(sorted(sets))}")
        bands += [Band(lo * 1e6, hi * 1e6, label, name) for lo, hi, label in sets[name]]
    bands += [Band(lo, hi, "extra range", "extra") for lo, hi in settings.tx_extra_ranges]
    return bands


def occupied_span(
    center_hz: float,
    sample_rate_hz: float,
    occupied_offsets_hz: tuple[float, float] | None,
    margin_hz: float = 0.0,
) -> tuple[float, float]:
    """Absolute frequency span a transmission covers. With no measurement,
    assume the whole sample-rate bandwidth is occupied."""
    if occupied_offsets_hz is None:
        low, high = -sample_rate_hz / 2, sample_rate_hz / 2
    else:
        low, high = occupied_offsets_hz
    # The carrier (LO leakage at the center frequency) is always emitted.
    low, high = min(low, 0.0) - margin_hz, max(high, 0.0) + margin_hz
    return center_hz + low, center_hz + high


def check_tx(
    settings: Settings,
    center_hz: float,
    sample_rate_hz: float,
    occupied_offsets_hz: tuple[float, float] | None = None,
    margin_hz: float = 0.0,
) -> TxDecision:
    if settings.tx_mode == "off":
        return TxDecision(False, "transmit is disabled on this server (tx_mode=off)")

    span = occupied_span(center_hz, sample_rate_hz, occupied_offsets_hz, margin_hz)
    if settings.tx_mode == "unrestricted":
        return TxDecision(True, "tx_mode=unrestricted: no band check", span_hz=span)

    for band in allowed_bands(settings):
        if band.contains(*span):
            return TxDecision(True, f"fits inside {band.describe()}", band=band, span_hz=span)

    measured = "measured" if occupied_offsets_hz is not None else "assumed full sample-rate"
    return TxDecision(
        False,
        f"{measured} span {span[0] / 1e6:.6f}-{span[1] / 1e6:.6f} MHz is not inside any "
        f"allowed band (tx_mode=allowed-bands, sets: {', '.join(settings.tx_band_sets) or 'none'})",
        span_hz=span,
    )
