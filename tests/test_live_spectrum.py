import time
from types import SimpleNamespace

import numpy as np
import pytest

from hackrf_mcp import live_service, live_spectrum, server, settings
from hackrf_mcp.live_log import LiveLogWriter
from hackrf_mcp.live_profiles import FileSource, HackrfSource
from hackrf_mcp.live_service import LiveService, LogPump
from hackrf_mcp.live_spectrum import (
    BINS,
    TIER_MINUTE,
    TIER_SECOND,
    SpectrumRecorder,
    SpectrumRow,
    SpectrumTap,
    SpectrumWriter,
)

RATE = 2_400_000
BIN_HZ = RATE / BINS  # 4687.5 Hz
BLOCK = 131072  # samples per block, as live_profiles.READ_BYTES gives
# Enough samples for one row: rows come out up to one batch (0.25 s) late.
ONE_ROW = int(1.3 * RATE)


def cs8(x: np.ndarray) -> bytes:
    """Complex samples in [-1, 1) as hackrf_transfer's int8 I/Q."""
    out = np.empty(2 * x.size, dtype=np.float64)
    out[0::2], out[1::2] = x.real, x.imag
    return np.clip(np.rint(out * 128), -128, 127).astype(np.int8).tobytes()


def tone(n: int, offset_hz: float, amplitude: float, rate: int = RATE, start: int = 0) -> np.ndarray:
    t = np.arange(start, start + n) / rate
    return amplitude * np.exp(2j * np.pi * offset_hz * t)


def noise(n: int, sigma: float = 0.02, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return sigma * (rng.standard_normal(n) + 1j * rng.standard_normal(n)) / np.sqrt(2)


def feed(tap, samples: np.ndarray, block: int = BLOCK) -> None:
    data = cs8(samples)
    for i in range(0, len(data), 2 * block):
        tap(data[i : i + 2 * block])


class Ticker:
    """A clock that moves on a fixed step each time it is read."""

    def __init__(self, t: float, step: float):
        self.t, self.step = t, step

    def __call__(self) -> float:
        now = self.t
        self.t += self.step
        return now


def tuning(profile="adsb", center=1_090_000_000, rate=RATE):
    return SimpleNamespace(
        profile=SimpleNamespace(name=profile),
        center_hz=center,
        sample_rate_hz=rate,
        lna_gain_db=32,
        vga_gain_db=40,
        amp=False,
    )


def writer(tmp_path, **kw) -> SpectrumWriter:
    args = dict(max_age_s=7 * 86400, minute_age_s=90 * 86400, max_bytes=1 << 40, min_free_bytes=0)
    args.update(kw)
    return SpectrumWriter(tmp_path / "spectrum.db", **args)


# --- The tap -------------------------------------------------------------------


def test_a_tone_lands_in_its_bin_at_its_level():
    rows = []
    tap = SpectrumTap(RATE, rows.append)
    # 64 bins above the centre, on a bin centre, at half of full scale.
    feed(tap, tone(ONE_ROW, 64 * BIN_HZ, 0.5) + noise(ONE_ROW))
    assert len(rows) == 1
    row = rows[0]
    assert row.mean_db.shape == (BINS,) and row.mean_db.dtype == np.float32
    assert int(np.argmax(row.mean_db)) == BINS // 2 + 64
    assert row.mean_db[BINS // 2 + 64] == pytest.approx(20 * np.log10(0.5), abs=0.5)
    assert np.median(row.mean_db) < -40
    freqs = live_spectrum.bin_freqs(1_090_000_000, RATE)
    assert freqs[BINS // 2 + 64] == 1_090_000_000 + 300_000
    assert row.seconds == pytest.approx(1.0, abs=0.06)


def test_rows_are_cut_at_exact_sample_counts_however_blocks_fall():
    rows = []
    fed = [0]
    # The clock tells the true time of each block, as a real stream would.
    tap = SpectrumTap(RATE, rows.append, clock=lambda: 1000.0 + fed[0] / RATE)
    data = cs8(noise(int(3.3 * RATE)))
    # Blocks of 100,000 samples divide into neither frames nor seconds.
    for i in range(0, len(data), 200_000):
        tap(data[i : i + 200_000])
        fed[0] += len(data[i : i + 200_000]) // 2
    assert len(rows) == 3
    # Stamped by sample position, a second apart, each exactly a second long.
    assert [round(r.ts, 3) for r in rows] == [1000.0, 1001.0, 1002.0]
    assert all(r.seconds == 1.0 for r in rows)
    # No samples are lost between blocks: every frame starting before 3 s is in.
    assert sum(r.frames for r in rows) == -(-3 * RATE // BINS)


def test_a_change_mid_batch_lands_in_the_right_row():
    rows = []
    tap = SpectrumTap(RATE, rows.append)
    x = noise(int(2.3 * RATE))
    x[:RATE] += tone(RATE, 64 * BIN_HZ, 0.5)
    x[RATE:] += tone(x.size - RATE, -64 * BIN_HZ, 0.5, start=RATE)
    feed(tap, x)
    first, second = rows[0].mean_db, rows[1].mean_db
    a, b = BINS // 2 + 64, BINS // 2 - 64
    assert first[a] == pytest.approx(-6.0, abs=0.5) and first[b] < -35
    assert second[b] == pytest.approx(-6.0, abs=0.5) and second[a] < -40


def test_the_peak_catches_a_burst_the_mean_hides():
    rows = []
    tap = SpectrumTap(RATE, rows.append)
    x = noise(ONE_ROW)
    # A 1 ms burst (2,400 samples) 100 bins below the centre.
    x[500_000:502_400] += tone(2_400, -100 * BIN_HZ, 0.5, start=500_000)
    feed(tap, x)
    row = rows[0]
    b = BINS // 2 - 100
    assert np.all(row.peak_db >= row.mean_db - 0.01)
    assert row.peak_db[b] > row.mean_db[b] + 20
    assert row.peak_db[b] == pytest.approx(20 * np.log10(0.5), abs=3)


def test_duty_transforms_a_share_of_each_block():
    full, part = [], []
    x = noise(ONE_ROW)
    feed(SpectrumTap(RATE, full.append), x)
    feed(SpectrumTap(RATE, part.append, duty=0.25), x)
    assert part[0].frames == pytest.approx(full[0].frames / 4, rel=0.05)
    with pytest.raises(ValueError):
        SpectrumTap(RATE, part.append, duty=0)


def test_quantise_round_trips_within_half_a_step_and_clips():
    db = np.array([-200.0, -130.0, -60.3, -2.5, 0.0, np.nan, -np.inf], dtype=np.float32)
    back = live_spectrum.dequantise(live_spectrum.quantise(db))
    assert back[0] == -130.0 and back[1] == -130.0
    assert back[2] == pytest.approx(-60.3, abs=0.25)
    assert back[3] == -2.5 and back[4] == -2.5
    assert back[5] == -130.0 and back[6] == -130.0


# --- The recorder and the database -----------------------------------------------


def flat_row(ts: float, mean: float, peak: float, frames: int = 100) -> SpectrumRow:
    return SpectrumRow(
        ts, 1.0, frames, np.full(BINS, mean, dtype=np.float32), np.full(BINS, peak, dtype=np.float32)
    )


def test_recorder_writes_seconds_and_rolls_minutes_up(tmp_path):
    items = []
    rec = SpectrumRecorder(items.append, clock=lambda: 3597.0)
    rec.tap_for(tuning())
    token = rec.current
    rec._row(token, flat_row(3598.0, -60.0, -40.0))
    rec._row(token, flat_row(3599.0, -50.0, -30.0))
    rec._row(token, flat_row(3600.0, -70.0, -45.0))  # the next minute
    rec.end(3601.0)
    kinds = [(i[0], i[2] if i[0] == "row" else None) for i in items]
    assert kinds == [
        ("segment", None),
        ("row", TIER_SECOND),
        ("row", TIER_SECOND),
        ("row", TIER_MINUTE),
        ("row", TIER_SECOND),
        ("row", TIER_MINUTE),
        ("end", None),
    ]
    w = writer(tmp_path)
    assert w.apply(items) == 5
    conn = live_spectrum.open_reader(w.path)
    [seg] = live_spectrum.segments(conn)
    assert seg["profile"] == "adsb" and seg["center_hz"] == 1_090_000_000 and seg["ts_end"] == 3601.0
    assert seg["sample_rate_hz"] == RATE and seg["bins"] == BINS and seg["db_step"] == 0.5
    seconds = live_spectrum.read(conn, tier=TIER_SECOND)
    assert [r["ts"] for r in seconds] == [3598.0, 3599.0, 3600.0]
    assert np.allclose(seconds[1]["mean"], -50.0) and np.allclose(seconds[1]["peak"], -30.0)
    minutes = live_spectrum.read(conn, tier=TIER_MINUTE)
    assert [r["ts"] for r in minutes] == [3598.0, 3600.0]
    # The minute's mean is the mean power, not the mean of the dB values.
    expected = 10 * np.log10((10 ** -6 + 10 ** -5) / 2)
    assert minutes[0]["mean"][0] == pytest.approx(expected, abs=0.25)
    assert minutes[0]["peak"][0] == -30.0 and minutes[0]["frames"] == 200
    # Rows that arrive after their segment ended are ignored.
    rec._row(token, flat_row(3602.0, -60.0, -40.0))
    assert items[-1][0] == "end"


def test_a_new_tap_ends_the_previous_segment():
    items = []
    rec = SpectrumRecorder(items.append, clock=lambda: 10.0)
    rec.tap_for(tuning("adsb"))
    rec.tap_for(tuning("vdl2", 136_812_500, 2_100_000))
    assert [i[0] for i in items] == ["segment", "end", "segment"]
    assert rec.status()["segment"]["profile"] == "vdl2"
    assert rec.tap.sample_rate_hz == 2_100_000


def test_live_sinks_get_about_four_rows_a_second():
    got = []
    rec = SpectrumRecorder(lambda item: None)
    rec.add_live_sink(lambda seg, row: got.append((seg["profile"], row.frames)))
    tap = rec.tap_for(tuning())
    feed(tap, noise(ONE_ROW))
    assert 3 <= len(got) <= 5 and got[0][0] == "adsb"


def test_writer_prunes_by_age_per_tier_then_by_size(tmp_path):
    w = writer(tmp_path, max_age_s=100, minute_age_s=500)
    mean = peak = bytes(BINS)
    items = [("segment", 1, {**_meta(), "ts_start": 0.0})]
    items += [("row", 1, TIER_SECOND, float(t), 1, mean, peak) for t in range(700, 1000, 10)]
    items += [("row", 1, TIER_MINUTE, float(t), 1, mean, peak) for t in range(0, 1000, 60)]
    items += [("end", 1, 1000.0), ("segment", 2, {**_meta(), "ts_start": 1000.0})]
    w.apply(items)
    r = w.prune(now=1000.0)
    conn = live_spectrum.open_reader(w.path)
    seconds = [x["ts"] for x in live_spectrum.read(conn, tier=TIER_SECOND)]
    minutes = [x["ts"] for x in live_spectrum.read(conn, tier=TIER_MINUTE)]
    # Second rows are kept 100 s, minute rows 500 s.
    assert seconds == [float(t) for t in range(900, 1000, 10)]
    assert minutes == [float(t) for t in range(540, 1000, 60)]
    assert r.deleted_age == 20 + 9
    # Segment 2 is still open: kept although it has no rows.
    assert len(live_spectrum.segments(conn)) == 2
    # Over the size limit, second rows go first, oldest first.
    w2 = writer(tmp_path / "b", max_bytes=1)
    items = [("segment", 1, {**_meta(), "ts_start": 0.0})]
    items += [("row", 1, TIER_SECOND, float(t), 1, mean, peak) for t in range(100)]
    items += [("row", 1, TIER_MINUTE, 50.0, 1, mean, peak), ("end", 1, 100.0)]
    w2.apply(items)
    r2 = w2.prune(now=100.0)
    conn2 = live_spectrum.open_reader(w2.path)
    assert r2.deleted_size > 0
    assert conn2.execute("SELECT count(*) FROM spectra WHERE tier = 1").fetchone()[0] == 0
    # With the seconds gone, minute rows follow, and then the empty segment.
    assert conn2.execute("SELECT count(*) FROM spectra").fetchone()[0] == 0
    assert live_spectrum.segments(conn2) == []


def _meta() -> dict:
    return {
        "profile": "adsb",
        "center_hz": 1_090_000_000,
        "sample_rate_hz": RATE,
        "bins": BINS,
        "db_min": -130.0,
        "db_step": 0.5,
        "lna_gain_db": 32,
        "vga_gain_db": 40,
        "amp": False,
        "duty": 1.0,
    }


def test_rows_are_dropped_while_disk_is_short(tmp_path, monkeypatch):
    w = writer(tmp_path, min_free_bytes=1 << 40)
    monkeypatch.setattr(live_spectrum.shutil, "disk_usage", lambda p: SimpleNamespace(free=1 << 20))
    n = w.apply(
        [
            ("segment", 1, {**_meta(), "ts_start": 0.0}),
            ("row", 1, TIER_SECOND, 1.0, 1, bytes(BINS), bytes(BINS)),
        ]
    )
    assert n == 0 and w.stats.rows_dropped == 1 and w.stats.low_disk
    assert len(live_spectrum.segments(live_spectrum.open_reader(w.path))) == 1


def test_segments_left_open_end_at_their_last_row(tmp_path):
    w = writer(tmp_path)
    w.apply(
        [
            ("segment", 1, {**_meta(), "ts_start": 5.0}),
            ("row", 1, TIER_SECOND, 9.0, 1, bytes(BINS), bytes(BINS)),
        ]
    )
    w.close()
    writer(tmp_path).close()
    [seg] = live_spectrum.segments(live_spectrum.open_reader(tmp_path / "spectrum.db"))
    assert seg["ts_end"] == 9.0


# --- The service -----------------------------------------------------------------


def test_a_failing_tap_does_not_stop_decoding(tmp_path):
    path = tmp_path / "x.cs8"
    path.write_bytes(bytes(4 * BLOCK))
    src = FileSource(path, RATE, realtime=False)

    def broken(buf):
        raise RuntimeError("tap bug")

    src.tap = broken
    got = []
    src.start(lambda buf: got.append(len(buf)))
    assert src.finished.wait(5)
    assert sum(got) == 4 * BLOCK and src.tap_errors == 2
    assert src.health()["spectrum_tap_errors"] == 2


def test_the_hackrf_source_feeds_its_tap_before_the_decoder(tmp_path):
    fake = tmp_path / "hackrf_transfer"
    fake.write_text("#!/bin/sh\nhead -c 524288 /dev/zero\n")
    fake.chmod(0o755)
    src = HackrfSource(str(fake), 1_090_000_000, RATE, 32, 40, False)
    order = []
    src.tap = lambda buf: order.append(("tap", len(buf)))
    src.start(lambda buf: order.append(("decoder", len(buf))))
    assert src.finished.wait(5)
    src.stop()
    assert sum(n for who, n in order if who == "tap") == 524288
    assert order[0][0] == "tap" and [who for who, _ in order[:2]] == ["tap", "decoder"]


class SourcePipeline:
    """A pipeline that reads its source and decodes nothing."""

    def __init__(self, tuning, ctx):
        self.tuning, self.ctx, self.source = tuning, ctx, None

    def start(self):
        self.source = self.ctx.make_source(self.tuning)
        self.source.start(lambda buf: None)

    def stop(self):
        self.source.stop()

    def failure(self):
        return None

    def health(self):
        return self.source.health()

    def stderr_tail(self):
        return []


def test_the_service_records_one_segment_per_run(tmp_path):
    path = tmp_path / "band.cs8"
    path.write_bytes(cs8(tone(int(2.2 * RATE), 64 * BIN_HZ, 0.5) + noise(int(2.2 * RATE))))
    live = settings.LiveSettings(live_dir=tmp_path / "live", min_free_mb=0)
    w = SpectrumWriter(live.spectrum_db_path, 86400, 86400, 1 << 30, 0)
    pump = LogPump(LiveLogWriter(live.db_path, 86400, 1 << 30, 0), spectrum_writer=w)
    rec = SpectrumRecorder(pump.spectrum)
    svc = LiveService(
        live,
        pump,
        make_pipeline=SourcePipeline,
        make_source=lambda t: FileSource(path, t.sample_rate_hz, realtime=False),
        program_finder=lambda p: "/bin/true",
        spectrum=rec,
    )
    svc.handle({"cmd": "start", "profile": "adsb"})
    assert svc.pipeline.source.finished.wait(10)
    svc.handle({"cmd": "start", "profile": "vdl2"})
    assert svc.pipeline.source.finished.wait(10)
    st = svc.status()["spectrum"]
    assert st["enabled"] and st["segment"]["profile"] == "vdl2" and st["rows_recorded"] >= 4
    assert st["tap_cpu_s"] > 0 and 0 < st["tap_cpu_share"]
    svc.handle({"cmd": "stop"})
    pump.drain()
    conn = live_spectrum.open_reader(live.spectrum_db_path)
    segs = live_spectrum.segments(conn)
    assert [(s["profile"], s["center_hz"], s["sample_rate_hz"]) for s in segs] == [
        ("adsb", 1_090_000_000, 2_400_000),
        ("vdl2", 136_812_500, 2_100_000),
    ]
    assert all(s["ts_end"] is not None for s in segs)
    first = live_spectrum.read(conn, segment=segs[0]["id"])
    assert len(first) == 2
    assert int(np.argmax(first[0]["mean"])) == BINS // 2 + 64
    assert svc.status()["spectrum"]["file_bytes"] > 0


def test_replay_prints_what_the_spectrum_holds(tmp_path, capsys):
    w = writer(tmp_path)
    mean = np.full(BINS, -80.0, dtype=np.float32)
    mean[BINS // 2 + 64] = -20.0
    w.apply(
        [
            ("segment", 1, {**_meta(), "ts_start": 0.0}),
            ("row", 1, TIER_SECOND, 1.0, 10, live_spectrum.quantise(mean), live_spectrum.quantise(mean + 5)),
            ("end", 1, 2.0),
        ]
    )
    live_service._print_spectrum(w.path)
    out = capsys.readouterr().out
    assert "1 second rows" in out and "1090.3000 MHz mean -20.0 peak -15.0" in out


# --- Settings and status -----------------------------------------------------------


def test_spectrum_settings_from_the_environment():
    s = settings.LiveSettings.from_env(
        {
            "HACKRF_LIVE_DIR": "/x",
            "HACKRF_LIVE_SPECTRUM": "off",
            "HACKRF_LIVE_SPECTRUM_MAX_MB": "512",
            "HACKRF_LIVE_SPECTRUM_MAX_AGE_DAYS": "3",
            "HACKRF_LIVE_SPECTRUM_MINUTE_DAYS": "30",
            "HACKRF_LIVE_SPECTRUM_DUTY": "0.5",
        }
    )
    assert (s.spectrum, s.spectrum_max_mb, s.spectrum_max_age_days, s.spectrum_minute_days, s.spectrum_duty) == (
        False,
        512.0,
        3.0,
        30.0,
        0.5,
    )
    assert str(s.spectrum_db_path) == "/x/spectrum.db"
    defaults = settings.LiveSettings()
    assert defaults.spectrum and defaults.spectrum_max_mb == 1024 and defaults.spectrum_duty == 1.0
    for bad in ("0", "1.5"):
        with pytest.raises(ValueError):
            settings.LiveSettings.from_env({"HACKRF_LIVE_SPECTRUM_DUTY": bad})


def _status(**spectrum) -> dict:
    now = time.time()
    return {
        "state": "running",
        "profile": "adsb",
        "tuning": "adsb: tuned 1090.0000 MHz",
        "running_since": now - 60,
        "desired": None,
        "decoded_1m": 1,
        "decoded_10m": 1,
        "decoded_total": 1,
        "rows_10m": 1,
        "rows_total": 1,
        "health": {},
        "now": now,
        "spectrum": spectrum,
    }


def test_live_status_reports_the_spectrum_history():
    out = server._format_live_status(
        _status(
            enabled=True,
            path="/var/lib/hackrf-mcp/live/spectrum.db",
            file_bytes=3 * 1024 * 1024,
            max_mb=1024,
            max_age_days=7,
            minute_days=90,
            rows_recorded=42,
            last_row_at=time.time() - 1,
            segment={"profile": "adsb", "center_hz": 1_090_000_000, "sample_rate_hz": RATE, "ts_start": 0},
            tap_cpu_s=12.5,
            tap_cpu_share=0.0452,
        )
    )
    assert "its FFTs using 4.5% of a core" in out
    assert "Spectrum history: /var/lib/hackrf-mcp/live/spectrum.db, 3.0 MiB of 1024 MiB" in out
    assert "42 seconds recorded" in out and "recording adsb at 1090.0000 MHz, 2.4 MS/s wide" in out
    out = server._format_live_status(_status(enabled=True, max_mb=1024, max_age_days=7, minute_days=90, segment=None))
    assert "adsb's decoder opens the HackRF itself" in out
    assert "Spectrum history: off" in server._format_live_status(_status(enabled=False))
