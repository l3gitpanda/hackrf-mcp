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


@dataclass(frozen=True)
class LiveSettings:
    """Settings of the live decoder service (live_service.py), which the
    MCP server's live tools also read to find its socket and log. Both
    processes take them from the same environment (the service unit's
    EnvironmentFile), so they agree. No tool can change them."""

    # Holds the log (live.db), the control socket and the saved profile.
    live_dir: Path = field(default_factory=lambda: _default_state_dir() / "live")
    # Rows older than this, or beyond the size limit (oldest first), are deleted.
    max_age_days: float = 7.0
    max_db_mb: float = 256.0
    # Nothing is written while free disk space is below this.
    min_free_mb: float = 2048.0
    # Pager (FLEX/POCSAG) message text is logged by the live service, and
    # shown by hackrf_decode, only if the operator turns this on
    # (HACKRF_MCP_PAGER_CONTENT); otherwise only time, channel, capcode and
    # message type are.
    pager_content: bool = False
    # Receiver position (lat, lon), which lets dump1090 resolve more positions.
    receiver: tuple[float, float] | None = None
    # A hackrf-agent to take samples from instead of a HackRF on this
    # machine (HACKRF_LIVE_AGENT=host:port), and the file holding its token.
    agent: tuple[str, int] | None = None
    agent_token_file: Path | None = None
    # Default receive gains for profiles that don't set their own.
    lna_gain_db: int | None = None
    vga_gain_db: int | None = None
    # Spectrum history (live_spectrum.py): one row per second and one per
    # minute of mean and peak power per bin, in spectrum.db beside the log,
    # with limits of its own. Second rows go after spectrum_max_age_days,
    # minute rows after spectrum_minute_days, and the oldest second rows
    # first when the file passes spectrum_max_mb.
    spectrum: bool = True
    spectrum_max_mb: float = 1024.0
    spectrum_max_age_days: float = 7.0
    spectrum_minute_days: float = 90.0
    # The share of the samples the spectrum is computed from (0-1]; 1 is
    # every sample, so the peak catches every burst.
    spectrum_duty: float = 1.0
    # Spectrum occupancy (occupancy.py): every occupancy_interval_min
    # minutes, on the UTC clock, the live service lends the HackRF to
    # hackrf_sweep for a few seconds and keeps the mean and peak of each bin
    # in occupancy.db beside the log. Off unless HACKRF_LIVE_OCCUPANCY is
    # set: each slot stops decoding for the length of the sweep.
    occupancy: bool = False
    occupancy_interval_min: int = 15
    occupancy_range_mhz: tuple[int, int] = (24, 1700)
    occupancy_bin_hz: int = 100_000
    occupancy_passes: int = 20
    # Fixed gains, so slots compare over time; the amplifier stays off.
    occupancy_lna_gain_db: int = 24
    occupancy_vga_gain_db: int = 40
    # A slot that finds the HackRF lent to a device tool waits this long for
    # it, then is skipped; one within occupancy_alert_quiet_min of an alert
    # hit (web/alerts.py) is skipped, so a track that set off an alert isn't
    # broken (0 turns that off). A watch always skips a slot.
    occupancy_late_min: float = 10.0
    occupancy_alert_quiet_min: float = 10.0
    # Slots are kept occupancy_max_age_days, daily roll-ups
    # occupancy_daily_days, and the oldest slots go first when the file
    # passes occupancy_max_mb.
    occupancy_max_mb: float = 2048.0
    occupancy_max_age_days: float = 365.0
    occupancy_daily_days: float = 1826.0
    # Captures from the web UI (captures.py): recorded by this service into
    # the capture directory's web/ (HACKRF_MCP_DATA_DIR), each at most
    # capture_max_s long (and within the MCP server's own limits), kept
    # capture_keep_days or until they pass capture_keep_mb, oldest first. A
    # capture waits up to capture_wait_s for a device tool or sweep that has
    # the receiver. Each one stops decoding for its length, and is started
    # only by someone holding the web UI's token.
    captures: bool = True
    capture_max_s: float = 60.0
    capture_keep_days: float = 7.0
    capture_keep_mb: float = 4096.0
    capture_wait_s: float = 30.0

    def __post_init__(self) -> None:
        if not 0 < self.spectrum_duty <= 1:
            raise ValueError(f"HACKRF_LIVE_SPECTRUM_DUTY must be above 0 and at most 1, not {self.spectrum_duty:g}")
        for name in (
            "spectrum_max_mb",
            "spectrum_max_age_days",
            "spectrum_minute_days",
            "occupancy_max_mb",
            "occupancy_max_age_days",
            "occupancy_daily_days",
            "capture_max_s",
            "capture_keep_days",
            "capture_keep_mb",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be above 0, not {getattr(self, name):g}")
        m = self.occupancy_interval_min
        if not (1 <= m <= 1440 and 1440 % m == 0):
            raise ValueError(f"HACKRF_LIVE_OCCUPANCY_INTERVAL_MIN must divide a day (5, 10, 15, 30, 60, ...), not {m}")
        lo, hi = self.occupancy_range_mhz
        if not 1 <= lo < hi <= 6000:
            raise ValueError(f"HACKRF_LIVE_OCCUPANCY_RANGE_MHZ must be within 1-6000, low to high, not {lo}-{hi}")
        if not 2445 <= self.occupancy_bin_hz <= 5_000_000:
            raise ValueError(f"HACKRF_LIVE_OCCUPANCY_BIN_HZ must be 2445-5000000, not {self.occupancy_bin_hz}")
        if not 1 <= self.occupancy_passes <= 200:
            raise ValueError(f"HACKRF_LIVE_OCCUPANCY_PASSES must be 1-200, not {self.occupancy_passes}")
        if not (0 <= self.occupancy_lna_gain_db <= 40 and self.occupancy_lna_gain_db % 8 == 0):
            raise ValueError(f"HACKRF_LIVE_OCCUPANCY_LNA_GAIN must be 0-40 in steps of 8, not {self.occupancy_lna_gain_db}")
        if not (0 <= self.occupancy_vga_gain_db <= 62 and self.occupancy_vga_gain_db % 2 == 0):
            raise ValueError(f"HACKRF_LIVE_OCCUPANCY_VGA_GAIN must be 0-62 in steps of 2, not {self.occupancy_vga_gain_db}")
        if not 0 <= self.occupancy_late_min < m:
            raise ValueError(f"HACKRF_LIVE_OCCUPANCY_LATE_MIN must be 0 or more and under the interval ({m}), not {self.occupancy_late_min:g}")
        if not 0 <= self.occupancy_alert_quiet_min <= 1440:
            raise ValueError(f"HACKRF_LIVE_OCCUPANCY_ALERT_QUIET_MIN must be 0-1440, not {self.occupancy_alert_quiet_min:g}")
        if not 0 <= self.capture_wait_s <= 600:
            raise ValueError(f"HACKRF_LIVE_CAPTURE_WAIT_S must be 0-600, not {self.capture_wait_s:g}")

    @property
    def db_path(self) -> Path:
        return self.live_dir / "live.db"

    @property
    def feed_socket_path(self) -> Path:
        return self.live_dir / "feed.sock"

    @property
    def spectrum_db_path(self) -> Path:
        return self.live_dir / "spectrum.db"

    @property
    def occupancy_db_path(self) -> Path:
        return self.live_dir / "occupancy.db"

    @property
    def socket_path(self) -> Path:
        return self.live_dir / "control.sock"

    @property
    def state_path(self) -> Path:
        return self.live_dir / "state.json"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> LiveSettings:
        env = os.environ if env is None else env
        kwargs: dict = {}
        if v := env.get("HACKRF_LIVE_DIR"):
            kwargs["live_dir"] = Path(v).expanduser()
        for name, key in (
            ("max_age_days", "HACKRF_LIVE_MAX_AGE_DAYS"),
            ("max_db_mb", "HACKRF_LIVE_MAX_DB_MB"),
            ("min_free_mb", "HACKRF_MCP_MIN_FREE_MB"),
        ):
            if v := env.get(key):
                kwargs[name] = float(v)
        # HACKRF_LIVE_PAGER_CONTENT is the name it had when only the live
        # log used it.
        if v := env.get("HACKRF_MCP_PAGER_CONTENT") or env.get("HACKRF_LIVE_PAGER_CONTENT"):
            kwargs["pager_content"] = parse_bool(v)
        if v := env.get("HACKRF_LIVE_RECEIVER"):
            lat, sep, lon = v.partition(",")
            if not sep:
                raise ValueError(f"HACKRF_LIVE_RECEIVER {v!r} must look like LAT,LON")
            kwargs["receiver"] = (float(lat), float(lon))
        if v := env.get("HACKRF_LIVE_AGENT"):
            host, sep, port = v.strip().rpartition(":")
            if not sep or not host or not port.isdigit():
                raise ValueError(f"HACKRF_LIVE_AGENT {v!r} must look like HOST:PORT")
            kwargs["agent"] = (host.strip("[]"), int(port))
            token = env.get("HACKRF_LIVE_AGENT_TOKEN_FILE")
            if not token:
                raise ValueError("HACKRF_LIVE_AGENT needs HACKRF_LIVE_AGENT_TOKEN_FILE, the file holding the agent's token")
            kwargs["agent_token_file"] = Path(token).expanduser()
        for name, key in (("lna_gain_db", "HACKRF_LIVE_LNA_GAIN"), ("vga_gain_db", "HACKRF_LIVE_VGA_GAIN")):
            if v := env.get(key):
                kwargs[name] = int(v)
        if v := env.get("HACKRF_LIVE_SPECTRUM"):
            kwargs["spectrum"] = parse_bool(v)
        for name, key in (
            ("spectrum_max_mb", "HACKRF_LIVE_SPECTRUM_MAX_MB"),
            ("spectrum_max_age_days", "HACKRF_LIVE_SPECTRUM_MAX_AGE_DAYS"),
            ("spectrum_minute_days", "HACKRF_LIVE_SPECTRUM_MINUTE_DAYS"),
            ("spectrum_duty", "HACKRF_LIVE_SPECTRUM_DUTY"),
            ("occupancy_late_min", "HACKRF_LIVE_OCCUPANCY_LATE_MIN"),
            ("occupancy_alert_quiet_min", "HACKRF_LIVE_OCCUPANCY_ALERT_QUIET_MIN"),
            ("occupancy_max_mb", "HACKRF_LIVE_OCCUPANCY_MAX_MB"),
            ("occupancy_max_age_days", "HACKRF_LIVE_OCCUPANCY_MAX_AGE_DAYS"),
            ("occupancy_daily_days", "HACKRF_LIVE_OCCUPANCY_DAILY_DAYS"),
            ("capture_max_s", "HACKRF_LIVE_CAPTURE_MAX_S"),
            ("capture_keep_days", "HACKRF_LIVE_CAPTURE_KEEP_DAYS"),
            ("capture_keep_mb", "HACKRF_LIVE_CAPTURE_KEEP_MB"),
            ("capture_wait_s", "HACKRF_LIVE_CAPTURE_WAIT_S"),
        ):
            if v := env.get(key):
                kwargs[name] = float(v)
        if v := env.get("HACKRF_LIVE_CAPTURES"):
            kwargs["captures"] = parse_bool(v)
        if v := env.get("HACKRF_LIVE_OCCUPANCY"):
            kwargs["occupancy"] = parse_bool(v)
        for name, key in (
            ("occupancy_interval_min", "HACKRF_LIVE_OCCUPANCY_INTERVAL_MIN"),
            ("occupancy_bin_hz", "HACKRF_LIVE_OCCUPANCY_BIN_HZ"),
            ("occupancy_passes", "HACKRF_LIVE_OCCUPANCY_PASSES"),
            ("occupancy_lna_gain_db", "HACKRF_LIVE_OCCUPANCY_LNA_GAIN"),
            ("occupancy_vga_gain_db", "HACKRF_LIVE_OCCUPANCY_VGA_GAIN"),
        ):
            if v := env.get(key):
                kwargs[name] = int(v)
        if v := env.get("HACKRF_LIVE_OCCUPANCY_RANGE_MHZ"):
            lo, sep, hi = v.partition("-")
            if not sep:
                raise ValueError(f"HACKRF_LIVE_OCCUPANCY_RANGE_MHZ {v!r} must look like LOW-HIGH in MHz, e.g. 24-1700")
            kwargs["occupancy_range_mhz"] = (int(lo), int(hi))
        return cls(**kwargs)


_live = LiveSettings()


def get_live() -> LiveSettings:
    return _live


def configure_live(settings: LiveSettings) -> None:
    global _live
    _live = settings


@dataclass(frozen=True)
class WebSettings:
    """Settings of the web UI's backend (hackrf_mcp.web, the hackrf-web
    service), from HACKRF_WEB_* in the same environment file. It reads the
    live service's log and spectrum history and talks to its sockets, so it
    takes LiveSettings too."""

    host: str = "127.0.0.1"
    port: int = 8766
    # The bearer tokens it accepts: a file holding one (HACKRF_WEB_TOKEN_FILE)
    # and/or SHA-256 hex digests (HACKRF_WEB_TOKEN_SHA256, comma-separated).
    token_file: Path | None = None
    token_sha256: tuple[str, ...] = ()
    # A browser signs in once with a token and gets a session cookie this
    # long. The key that signs it is kept in state_dir.
    session_days: float = 30.0
    state_dir: Path | None = None
    # Origins (scheme://host:port) besides the server's own that may send
    # state-changing requests and open websockets; normally none.
    extra_origins: tuple[str, ...] = ()
    # Alerts (web/alerts.py) can post their hits to a webhook. The URL is a
    # secret, so it is read from a file (HACKRF_WEB_ALERT_WEBHOOK_FILE) that
    # only this service needs to see; the body is JSON, a Discord message or
    # ntfy text (HACKRF_WEB_ALERT_WEBHOOK_FORMAT).
    alert_webhook_file: Path | None = None
    alert_webhook_format: str = "json"
    # The UI's address as browsers reach it (HACKRF_WEB_URL), for the links
    # in webhook messages; none without it.
    public_url: str | None = None
    # The aircraft database (web/aircraftdb.py): "weekly" downloads the FAA
    # registry and plane-alert-db and rebuilds it every 7 days; "off" (the
    # default) makes no outbound connection, and lookups use rules alone.
    aircraft_db: str = "off"

    def __post_init__(self) -> None:
        if not 0 < self.port < 65536:
            raise ValueError(f"HACKRF_WEB_PORT must be a port number, not {self.port}")
        if self.session_days <= 0:
            raise ValueError("HACKRF_WEB_SESSION_DAYS must be above 0")
        if self.alert_webhook_format not in ("json", "discord", "ntfy"):
            raise ValueError("HACKRF_WEB_ALERT_WEBHOOK_FORMAT must be json, discord or ntfy")
        if self.aircraft_db not in ("off", "weekly"):
            raise ValueError("HACKRF_WEB_AIRCRAFT_DB must be off or weekly")

    def alert_webhook_url(self) -> str | None:
        """The webhook's URL from its file, or None when none is set up."""
        if self.alert_webhook_file is None:
            return None
        url = self.alert_webhook_file.read_text().strip()
        return url or None

    def state_path(self, live: LiveSettings) -> Path:
        return self.state_dir or live.live_dir.parent / "web"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> WebSettings:
        env = os.environ if env is None else env
        kwargs: dict = {}
        if v := env.get("HACKRF_WEB_HOST"):
            kwargs["host"] = v.strip()
        if v := env.get("HACKRF_WEB_PORT"):
            kwargs["port"] = int(v)
        if v := env.get("HACKRF_WEB_TOKEN_FILE"):
            kwargs["token_file"] = Path(v).expanduser()
        if v := env.get("HACKRF_WEB_TOKEN_SHA256"):
            kwargs["token_sha256"] = tuple(x.strip() for x in v.split(",") if x.strip())
        if v := env.get("HACKRF_WEB_SESSION_DAYS"):
            kwargs["session_days"] = float(v)
        if v := env.get("HACKRF_WEB_STATE_DIR"):
            kwargs["state_dir"] = Path(v).expanduser()
        if v := env.get("HACKRF_WEB_EXTRA_ORIGINS"):
            kwargs["extra_origins"] = tuple(x.strip().rstrip("/") for x in v.split(",") if x.strip())
        if v := env.get("HACKRF_WEB_ALERT_WEBHOOK_FILE"):
            kwargs["alert_webhook_file"] = Path(v).expanduser()
        if v := env.get("HACKRF_WEB_ALERT_WEBHOOK_FORMAT"):
            kwargs["alert_webhook_format"] = v.strip().lower()
        if v := env.get("HACKRF_WEB_URL"):
            kwargs["public_url"] = v.strip().rstrip("/")
        if v := env.get("HACKRF_WEB_AIRCRAFT_DB"):
            kwargs["aircraft_db"] = v.strip().lower()
        return cls(**kwargs)
