"""Listening (listen.py): channels, demodulation, squelch, clips, the audio
socket, the live service's listen command, and the web routes."""

import hashlib
import struct
import sys
import time
from pathlib import Path

import numpy as np
import pytest
from starlette.testclient import TestClient

from hackrf_mcp import listen, live_client, live_profiles, settings
from hackrf_mcp.listen import Channel, ListenError
from hackrf_mcp.live_service import LiveService
from hackrf_mcp.web import app as web_app

from test_live_service import Clock, FakePipeline

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
import fake_hackrf_transfer  # noqa: E402

RATE = listen.WINDOW_RATE_HZ
T0 = 1_793_923_200.0  # 2026-11-06 00:00:00 UTC


def cs8(x: np.ndarray) -> bytes:
    out = np.empty(2 * x.size, np.int8)
    out[0::2] = np.clip(np.rint(x.real * 127), -127, 127)
    out[1::2] = np.clip(np.rint(x.imag * 127), -127, 127)
    return out.tobytes()


def air(seconds: float, *, am=None, nfm=None, wfm=None, rate: int = RATE, seed: int = 1) -> bytes:
    """Noise, plus carriers at offsets from the centre: am = (offset, tone,
    [(on, off), ...]) keyed AM at 80% depth; nfm/wfm = (offset, tone,
    deviation), continuous."""
    n = int(rate * seconds)
    t = np.arange(n) / rate
    rng = np.random.default_rng(seed)
    x = (rng.standard_normal(n) + 1j * rng.standard_normal(n)) * 0.02
    if am:
        off, tone, spans = am
        key = np.zeros(n, bool)
        for a, b in spans:
            key |= (t >= a) & (t < b)
        x = x + 0.05 * (1 + 0.8 * np.sin(2 * np.pi * tone * t)) * np.exp(2j * np.pi * off * t) * key
    for fm in (nfm, wfm):
        if fm:
            off, tone, dev = fm
            x = x + 0.03 * np.exp(1j * (2 * np.pi * off * t + (dev / tone) * np.sin(2 * np.pi * tone * t)))
    return cs8(x)


def feed(tap, data: bytes, clock=None, block: int = 1 << 18, start: float = 0.0) -> None:
    for i in range(0, len(data), block):
        if clock is not None:
            clock.t = start + (i + block) / 2 / tap.rate
        tap(data[i : i + block])


def tone_of(audio: np.ndarray, rate: float) -> tuple[float, float]:
    seg = audio[: int(rate)] if audio.size >= rate else audio
    spec = np.abs(np.fft.rfft(seg * np.hanning(seg.size)))
    k = int(np.argmax(spec[5:])) + 5
    return k * rate / seg.size, float(np.sqrt(np.mean(seg**2)))


def frames_of(sent: list[bytes]) -> list[dict]:
    return [listen.parse_frame(f) for f in sent]


def audio_of(frames: list[dict], channel: int) -> np.ndarray:
    parts = [listen.ulaw_decode(f["payload"]) for f in frames if f["type"] == listen.AUDIO and f["channel"] == channel]
    return np.concatenate(parts) if parts else np.zeros(0, np.float32)


# --- mu-law and WAV --------------------------------------------------------------------


def test_mu_law_matches_g711():
    assert listen.ulaw_encode(np.array([0.0])) == b"\xff"
    assert listen.ulaw_encode(np.array([1.0])) == b"\x80"
    assert listen.ulaw_encode(np.array([-1.0])) == b"\x00"
    assert listen.ULAW_TABLE[0xFF] == 0.0 and listen.ULAW_TABLE[0x80] == pytest.approx(32124 / 32768)
    x = np.linspace(-0.9, 0.9, 1001)
    y = listen.ulaw_decode(listen.ulaw_encode(x))
    big = np.abs(x) > 0.01
    assert np.max(np.abs(y[big] - x[big]) / np.abs(x[big])) < 0.07
    assert np.max(np.abs(y - x)) < 0.02


def test_the_clip_file_is_a_mu_law_wav():
    data = listen.ulaw_encode(np.zeros(801))
    wav = listen.ulaw_wav(data, 8000)
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE" and struct.unpack_from("<I", wav, 4)[0] == len(wav) - 8
    fmt, channels, rate, byte_rate, align, bits = struct.unpack_from("<HHIIHH", wav, 20)
    assert (fmt, channels, rate, byte_rate, align, bits) == (7, 1, 8000, 8000, 1, 8)
    i = wav.index(b"data")
    assert struct.unpack_from("<I", wav, i + 4)[0] == 801 and wav[i + 8 : i + 8 + 801] == data
    assert len(wav) % 2 == 0


# --- channels -------------------------------------------------------------------------


def test_channels_are_checked_and_take_the_bands_mode():
    chs = listen.parse_channels(
        [{"freq_hz": 121_500_000}, {"freq_hz": 162_550_000}, {"freq_hz": 98.1e6, "squelch_db": 99, "label": "  x" * 30}]
    )
    assert [c.mode for c in chs] == ["am", "nfm", "wfm"]
    assert chs[0].clips and not chs[1].clips and not chs[2].clips
    assert chs[2].squelch_db == listen.SQUELCH_RANGE_DB[1] and len(chs[2].label) == 40
    assert not Channel(121_500_000, "nfm").clips and not Channel(108_100_000, "am").clips
    with pytest.raises(ListenError, match="at most 4"):
        listen.parse_channels([{"freq_hz": 121e6 + i * 25_000} for i in range(5)])
    with pytest.raises(ListenError, match="given twice"):
        listen.parse_channels([{"freq_hz": 121_500_000}, {"freq_hz": 121_500_500}])
    with pytest.raises(ListenError, match="mode"):
        listen.parse_channels([{"freq_hz": 121_500_000, "mode": "cw"}])
    with pytest.raises(ListenError, match="outside what the HackRF"):
        listen.parse_channels([{"freq_hz": 7e9}])
    with pytest.raises(ListenError, match="at least one"):
        listen.parse_channels([])
    assert listen.mhz(121_500_000) == "121.500 MHz" and listen.mhz(123_458_333) == "123.4583 MHz"


def test_a_window_holds_the_channels_off_its_centre():
    chs = listen.parse_channels([{"freq_hz": 121_500_000}])
    c = listen.window_center(chs)
    assert abs(c - 121_500_000) >= 20_000 and listen.fits(c, RATE, chs)
    two = listen.parse_channels([{"freq_hz": 121_500_000}, {"freq_hz": 122_200_000}])
    assert listen.fits(listen.window_center(two), RATE, two)
    with pytest.raises(ListenError, match="don't fit one 2 MHz window"):
        listen.window_center(listen.parse_channels([{"freq_hz": 121_000_000}, {"freq_hz": 123_000_000}]))
    # Inside a running band, or not; broadcast FM needs the room for its width.
    assert listen.fits(1_090_000_000, 2_400_000, listen.parse_channels([{"freq_hz": 1_090_500_000}]))
    assert not listen.fits(162_000_000, 2_000_000, listen.parse_channels([{"freq_hz": 162_790_000, "mode": "wfm"}]))


# --- cutting, demodulating, squelch -------------------------------------------------------


def test_the_floor_is_the_noise_a_quiet_channel_sees():
    rng = np.random.default_rng(2)
    n = RATE * 2
    x = ((rng.standard_normal(n) + 1j * rng.standard_normal(n)) * 0.05).astype(np.complex64)
    x += (0.3 * np.exp(2j * np.pi * 400_000 * np.arange(n) / RATE)).astype(np.complex64)  # one busy channel
    ch = listen.Channelizer(RATE, [-300_000, 400_000], [125, 50], [10_000, 16_000])
    blocks = ch(x)
    assert len(blocks) == int((n - ch.step) // ch.step) + 1 or len(blocks) > 40
    quiet = np.mean([listen.db(p[0]) for _, p, _ in blocks[2:]])
    floor = np.mean([listen.db(f[0]) for _, _, f in blocks[2:]])
    busy = np.mean([listen.db(p[1]) for _, p, _ in blocks[2:]])
    assert abs(quiet - floor) < 1.0
    assert busy > floor + 20
    assert [y.size for y in blocks[3][0]] == [ch.step // 125, ch.step // 50]


@pytest.mark.parametrize(
    "mode,kw,tone",
    [("am", {"am": (-25_000, 1000, [(0, 9)])}, 1000), ("nfm", {"nfm": (300_000, 1500, 3000)}, 1500), ("wfm", {"wfm": (-500_000, 2000, 50_000)}, 2000)],
)
def test_each_mode_brings_back_its_tone(mode, kw, tone):
    off = next(iter(kw.values()))[0]
    center = 100_000_000
    chs = (Channel(center + off, mode),)
    sent = []
    tap = listen.AudioTap(center, RATE, chs, send=sent.append, threaded=False)
    feed(tap, air(2.0, **kw))
    a = audio_of(frames_of(sent), 0)
    rate = tap.demods[0].audio_rate_hz
    assert rate == {"am": 8000, "nfm": 8000, "wfm": pytest.approx(16_666.7, abs=0.1)}[mode]
    got, rms = tone_of(a[int(0.5 * rate) :], rate)
    assert got == pytest.approx(tone, abs=3)
    assert 0.2 < rms < 0.7


def test_the_squelch_records_one_clip_per_transmission():
    center = 121_525_000
    chs = listen.parse_channels([{"freq_hz": 121_500_000}, {"freq_hz": center + 300_000, "mode": "nfm"}])
    sent, clips = [], []
    clock = Clock(0.0)
    tap = listen.AudioTap(center, RATE, chs, send=sent.append, on_clip=lambda *a: clips.append(a), clock=clock, threaded=False)
    # Two calls 3 s apart (two clips), and a 0.2 s blip (none).
    feed(tap, air(9.0, am=(-25_000, 1000, [(1.0, 2.5), (5.5, 6.5), (8.0, 8.2)]), nfm=(300_000, 1500, 3000)), clock)
    tap.close()
    assert len(clips) == 2
    (i, ts, te, data, rate, peak, cont), second = clips[0], clips[1]
    assert i == 0 and rate == 8000 and not cont
    assert ts == pytest.approx(1.0 - listen.PRE_ROLL_S, abs=0.1)
    assert te == pytest.approx(2.5 + listen.TAIL_S, abs=0.12)
    assert len(data) == pytest.approx((te - ts) * 8000, abs=2)
    assert peak > -40
    assert second[1] == pytest.approx(5.5 - listen.PRE_ROLL_S, abs=0.1)
    got, _ = tone_of(listen.ulaw_decode(data)[2000:], 8000)
    assert got == pytest.approx(1000, abs=3)
    frames = frames_of(sent)
    # The weather-like channel never goes quiet: all of it is heard, and it
    # never records.
    nfm = audio_of(frames, 1)
    assert nfm.size / 8000 > 8.5
    assert all(not f["flags"] & listen.FLAG_RECORDING for f in frames if f["channel"] == 1)
    # The AM channel is heard only while its squelch is open.
    heard = audio_of(frames, 0).size / 8000
    assert 3.5 < heard < 7.5
    meters = [f for f in frames if f["type"] == listen.METER and f["channel"] == 0]
    assert len(meters) == pytest.approx(9 / listen.METER_S, abs=4)
    assert all(m["threshold_db"] == pytest.approx(m["floor_db"] + listen.AUTO_MARGIN_DB, abs=0.01) for m in meters)


def test_a_long_transmission_is_cut_and_carries_on():
    center = 121_525_000
    chs = listen.parse_channels([{"freq_hz": 121_500_000}])
    clips = []
    clock = Clock(0.0)
    tap = listen.AudioTap(center, RATE, chs, on_clip=lambda *a: clips.append(a), clip_max_s=2.0, clock=clock, threaded=False)
    feed(tap, air(5.5, am=(-25_000, 1000, [(0.5, 5.0)])), clock)
    tap.close()
    assert [round(c[2] - c[1]) for c in clips] == [2, 2, 1]
    assert [c[6] for c in clips] == [False, True, True]


def test_the_slider_moves_the_threshold_and_its_bottom_keeps_it_open():
    center = 121_525_000
    chs = (Channel(121_500_000, "am", squelch_db=listen.SQUELCH_RANGE_DB[0]),)
    sent = []
    tap = listen.AudioTap(center, RATE, chs, send=sent.append, threaded=False)
    feed(tap, air(2.0))  # nothing on the air
    assert audio_of(frames_of(sent), 0).size / 8000 > 1.5
    tap.set_squelch([10.0])
    assert tap.status()[0]["squelch_db"] == 10.0
    sent.clear()
    feed(tap, air(2.0, am=(-25_000, 1000, [(0, 2)]), seed=5))
    f = frames_of(sent)[-1]
    assert f["threshold_db"] == pytest.approx(f["floor_db"] + listen.AUTO_MARGIN_DB + 10, abs=0.01)


def test_nobody_listening_means_no_audio_frames_but_clips_go_on():
    center = 121_525_000
    chs = listen.parse_channels([{"freq_hz": 121_500_000}])
    sent, clips = [], []
    tap = listen.AudioTap(center, RATE, chs, send=sent.append, has_listeners=lambda: False, on_clip=lambda *a: clips.append(a), threaded=False)
    feed(tap, air(3.0, am=(-25_000, 1000, [(0.5, 1.5)])))
    tap.close()
    assert clips and not [f for f in frames_of(sent) if f["type"] == listen.AUDIO]
    assert [f for f in frames_of(sent) if f["type"] == listen.METER]


def test_the_threaded_tap_drops_rather_than_waits():
    chs = listen.parse_channels([{"freq_hz": 121_500_000}])
    tap = listen.AudioTap(121_525_000, RATE, chs)
    data = air(1.0)
    for _ in range(listen.QUEUE_BLOCKS * 3):
        tap(data[: 1 << 16])
    tap.close()
    tap._thread.join(timeout=10)
    assert not tap._thread.is_alive()
    assert tap.health()["blocks"] > 0
    with pytest.raises(ListenError, match="not inside the band"):
        listen.AudioTap(162_000_000, RATE, chs)


def test_the_fake_transfer_puts_tones_where_a_listen_finds_them():
    """tools/fake_hackrf_transfer.py: an AM call on and off 25 kHz below the
    centre, and a steady NFM tone 300 kHz above it."""
    rate = RATE
    blocks = [fake_hackrf_transfer.block(rate, 162_000_000, i * rate // 4, rate // 4) for i in range(24)]
    data = b"".join(blocks)
    center = 121_525_000
    chs = (Channel(center - 25_000, "am"), Channel(center + 300_000, "nfm"))
    sent, clips = [], []
    tap = listen.AudioTap(center, rate, chs, send=sent.append, on_clip=lambda *a: clips.append(a), threaded=False)
    feed(tap, data)
    tap.close()
    frames = frames_of(sent)
    got, _ = tone_of(audio_of(frames, 1)[4000:], 8000)
    assert got == pytest.approx(fake_hackrf_transfer.NFM_TONE_HZ, abs=3)
    assert clips and tone_of(listen.ulaw_decode(clips[0][3])[2000:], 8000)[0] == pytest.approx(fake_hackrf_transfer.AM_TONE_HZ, abs=3)


# --- the clip store and presets ------------------------------------------------------------


def test_clips_are_kept_by_age_and_size(tmp_path):
    clock = Clock(T0)
    store = listen.ClipStore(tmp_path / "clips", keep_days=1, keep_mb=1 / 1024, clock=clock)
    ids = []
    for i in range(3):
        cid = store.new_id(T0 + i)
        store.write(cid, b"x" * 400)
        ids.append(cid)
    assert store.status()["count"] == 3 and store.status()["bytes"] == 1200
    assert store.prune() == [ids[0]]  # 1 KB at most
    assert store.prune(need_bytes=500) == [ids[1]]
    clock.t = T0 + 2 * 86400
    assert store.prune() == [ids[2]]
    assert store.delete(ids[2]) == 0
    for bad in ("../x", "20261106T000000Z-abcd.wav", "20261106T000000Z-ABCD"):
        with pytest.raises(ValueError):
            store.path(bad)


def test_presets_come_from_the_station_not_the_code(tmp_path):
    assert [p["label"] for p in listen.builtin_presets()][0] == "NOAA WX2"
    assert listen.parse_presets("121.5 am Guard one; 122.75 # air to air\n bad line\n 162.55 wfm\n7000 x") == [
        {"freq_hz": 121_500_000, "mode": "am", "label": "Guard one"},
        {"freq_hz": 122_750_000, "mode": "am", "label": None},
        {"freq_hz": 162_550_000, "mode": "wfm", "label": None},
    ]
    live = settings.LiveSettings(live_dir=tmp_path, listen_presets="123.45 Chat")
    assert len(live.listen_preset_list()) == 8
    (tmp_path / "listen-presets.txt").write_text("122.75 am Air to air\n")
    assert [p["label"] for p in live.listen_preset_list()[7:]] == ["Chat", "Air to air"]
    env = settings.LiveSettings.from_env({"HACKRF_LIVE_LISTEN": "off", "HACKRF_LIVE_CLIP_KEEP_MB": "10", "HACKRF_LIVE_LISTEN_PRESETS_FILE": str(tmp_path / "p")})
    assert not env.listen and env.clip_keep_mb == 10 and env.listen_presets_file == tmp_path / "p"
    with pytest.raises(ValueError, match="CLIP_MAX_S"):
        settings.LiveSettings(clip_max_s=0.5)


# --- the live service -----------------------------------------------------------------------


class FakeSource(live_profiles.Source):
    def __init__(self, tuning):
        super().__init__()
        self.sample_rate_hz = tuning.sample_rate_hz
        self.tuning = tuning

    # Always moving, so the supervisor never sees the source stall.
    @property
    def bytes_read(self):
        self._n = getattr(self, "_n", 0) + 1
        return self._n

    @bytes_read.setter
    def bytes_read(self, v):
        pass

    def push(self, data: bytes) -> None:
        self._deliver(lambda b: None, data)


class SourcePipeline(FakePipeline):
    def start(self):
        self.source = self.ctx.make_source(self.tuning)
        self.running = True


@pytest.fixture
def lsvc(tmp_path):
    pipes, rows = [], []

    def make(tuning, ctx):
        p = SourcePipeline(tuning, ctx)
        pipes.append(p)
        return p

    clock = Clock(T0)
    live = settings.LiveSettings(live_dir=tmp_path / "live", min_free_mb=0)
    s = LiveService(live, None, make_pipeline=make, make_source=FakeSource, program_finder=lambda p: "/bin/true", clock=clock)
    s.audio_threaded = False
    s.clips = listen.ClipStore(live.clips_dir, clock=clock)
    s.add_sink(rows.append)
    s.pipes, s.logged, s.test_clock = pipes, rows, clock
    events = []
    real = s._event
    s._event = lambda e, p=None, d=None: (events.append((e, p, d)), real(e, p, d))
    s.events = events
    s.handle({"cmd": "start", "profile": "adsb"})
    return s


AIRBAND = [{"freq_hz": 121_500_000}]


def test_a_listen_elsewhere_asks_first_then_lends_itself_the_receiver(lsvc):
    plan = lsvc.handle({"cmd": "listen-plan", "channels": AIRBAND})
    assert plan["ok"] and plan["retune"] and plan["running"] == "adsb" and plan["center_hz"] == 121_525_000
    r = lsvc.handle({"cmd": "listen", "channels": AIRBAND})
    assert not r["ok"] and r["retune_needed"] and "stops adsb" in r["error"]
    assert lsvc.pipes[-1].tuning.profile.name == "adsb"
    r = lsvc.handle({"cmd": "listen", "channels": AIRBAND, "retune": True})
    assert r["ok"] and r["profile"] == "listen" and r["watch"]["profile"] == "listen"
    act = r["listen"]["active"]
    assert act["owns"] and act["state"] == "playing" and act["left_s"] == 15 * 60
    assert act["channels"][0]["freq_hz"] == 121_500_000 and act["channels"][0]["clips"]
    src = lsvc.pipes[-1].source
    assert src.taps["audio"] is lsvc.audio and lsvc.pipes[-2].stopped
    (said,) = [d for e, p, d in lsvc.events if e == "listen"]
    assert said == "121.5000 MHz AM, a 2 MHz window at 121.5250 MHz, 15 min"
    assert [e for e, p, d in lsvc.events][-3:] == ["listen", "stop", "start"]
    # A call on the channel: a clip, its file, and its row.
    clock = lsvc.test_clock
    data = air(4.5, am=(-25_000, 1000, [(0.5, 1.5)]))
    for i in range(0, len(data), 1 << 18):
        clock.t = T0 + (i + (1 << 18)) / 2 / RATE
        src.push(data[i : i + (1 << 18)])
    (row,) = [r for r in lsvc.logged if r.kind == "clip"]
    assert row.profile == "listen" and row.subject == "121.500 MHz" and row.freq_hz == 121_500_000
    assert row.fields["seconds"] == pytest.approx(1.55, abs=0.15) and row.fields["mode"] == "am"
    path = lsvc.clips.path(row.fields["clip"])
    assert path.read_bytes()[:4] == b"RIFF" and row.ts_end - row.ts == pytest.approx(row.fields["seconds"], abs=0.06)
    # Stopping hands the receiver back.
    r = lsvc.handle({"cmd": "listen-stop"})
    assert r["listen"]["active"] is None and r["watch"] is None and r["profile"] == "adsb"
    assert ("listen-end", "listen", "stopped on request") in lsvc.events
    assert ("watch-end", "listen", "stopped on request") in lsvc.events
    assert "audio" not in src.taps
    r = lsvc.handle({"cmd": "clip-delete", "id": row.fields["clip"]})
    assert r["freed_bytes"] > 0 and not path.exists()


def test_a_listen_ends_by_itself(lsvc):
    lsvc.handle({"cmd": "listen", "channels": AIRBAND, "retune": True, "minutes": 2})
    lsvc.test_clock.t += 121
    lsvc.supervise()
    st = lsvc.status()
    assert st["listen"]["active"] is None and st["profile"] == "adsb" and st["listen"]["last"]["why"] == "time limit reached"
    assert lsvc.handle({"cmd": "listen", "channels": AIRBAND, "retune": True, "minutes": 61})["error"].startswith("a listen lasts 1-60")


def test_inside_a_watch_it_rides_it(lsvc):
    lsvc.handle({"cmd": "watch", "center_hz": 120_000_000, "sample_rate_hz": 10_000_000, "minutes": 5})
    plan = lsvc.handle({"cmd": "listen-plan", "channels": AIRBAND})
    assert not plan["retune"] and plan["riding"] == "watch" and plan["center_hz"] == 120_000_000
    r = lsvc.handle({"cmd": "listen", "channels": AIRBAND})
    assert r["ok"] and not r["listen"]["active"]["owns"] and r["listen"]["active"]["left_s"] == 300
    assert lsvc.pipes[-1].source.taps["audio"] is lsvc.audio and lsvc.audio.rate == 10_000_000
    # The same channels again only move the squelch.
    tap = lsvc.audio
    n = len(lsvc.events)
    lsvc.handle({"cmd": "listen", "channels": [{"freq_hz": 121_500_000, "squelch_db": 6}]})
    assert lsvc.audio is tap and tap.squelches[0].offset_db == 6 and len(lsvc.events) == n
    # The watch ending moves the receiver: the listen goes with it.
    r = lsvc.handle({"cmd": "watch-stop"})
    assert r["listen"]["active"] is None and r["profile"] == "adsb"
    assert ("listen-end", "listen", "the receiver moved to adsb") in lsvc.events


def test_while_listening_a_sweep_skips_and_a_capture_borrows(lsvc):
    lsvc.handle({"cmd": "listen", "channels": AIRBAND, "retune": True})
    assert lsvc.occupancy_take(30) == {"state": "listen"}
    took = lsvc.capture_take(40)
    assert took["state"] == "ok" and took["paused_profile"] == "listen"
    st = lsvc.status()
    assert st["listen"]["active"]["state"] == "waiting" and lsvc.audio is None
    assert lsvc.capture_give(took["token"])
    lsvc.supervise()
    assert lsvc.status()["listen"]["active"]["state"] == "playing"
    assert lsvc.pipes[-1].source.taps["audio"] is lsvc.audio
    assert lsvc.capture_context()["listen"] and lsvc.capture_context()["running"] is None


def test_starting_a_profile_or_stopping_ends_the_listen(lsvc):
    lsvc.handle({"cmd": "listen", "channels": AIRBAND, "retune": True})
    r = lsvc.handle({"cmd": "start", "profile": "ais"})
    assert r["listen"]["active"] is None and r["profile"] == "ais"
    # AIS's band holds the weather channels: no retune, no air time.
    plan = lsvc.handle({"cmd": "listen-plan", "channels": [{"freq_hz": 162_550_000}]})
    assert not plan["retune"] and plan["riding"] == "ais"
    r = lsvc.handle({"cmd": "listen", "channels": [{"freq_hz": 162_550_000}]})
    assert r["listen"]["active"]["channels"][0]["mode"] == "nfm" and r["profile"] == "ais"
    assert lsvc.handle({"cmd": "stop"})["listen"]["active"] is None
    assert lsvc.handle({"cmd": "start", "profile": "listen"})["error"].startswith("listen is started with its own command")


def test_listening_can_be_off(tmp_path):
    live = settings.LiveSettings(live_dir=tmp_path, listen=False)
    s = LiveService(live, None, make_pipeline=FakePipeline, program_finder=lambda p: "/bin/true", clock=Clock(T0))
    r = s.handle({"cmd": "listen", "channels": AIRBAND})
    assert not r["ok"] and "off on this station" in r["error"]
    assert s.handle({"cmd": "clip-delete", "id": "20261106T000000Z-abcd"})["error"].startswith("clips are off")
    assert s.status()["listen"]["enabled"] is False


# --- the audio socket and the web side ------------------------------------------------------


def test_the_audio_socket_frames_reach_a_client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    srv = listen.AudioServer(Path("a.sock")).start()
    try:
        frames = listen.read_frames(Path("a.sock"))
        deadline = time.time() + 5
        while not srv.has_clients() and time.time() < deadline:
            time.sleep(0.01)
        srv.send(listen.frame(listen.AUDIO, 2, listen.FLAG_OPEN, 8000, 7, 1.5, -30, -40, -48, b"\x01\x02"))
        f = next(frames)
        assert (f["type"], f["channel"], f["rate_hz"], f["seq"], f["payload"]) == (listen.AUDIO, 2, 8000, 7, b"\x01\x02")
        assert f["level_db"] == pytest.approx(-30)
    finally:
        srv.close()


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = []

    def control(req, timeout=20.0):
        calls.append(req)
        if req["cmd"] == "clip-delete" and req["id"] == "20261106T000000Z-dead":
            raise live_client.LiveError("no such clip")
        return {"ok": True, "echo": req}

    live = settings.LiveSettings(live_dir=Path("live"), min_free_mb=0)
    live.live_dir.mkdir()
    token = "t" * 32
    app = web_app.create_app(
        live, settings.WebSettings(state_dir=tmp_path / "state"), [hashlib.sha256(token.encode()).digest()], b"k" * 32,
        control=control, feed=False, own_stats=lambda: None,
    )  # fmt: skip
    client = TestClient(app)
    client.headers["Authorization"] = "Bearer " + token
    return client, calls, live


def test_the_listen_api_asks_the_live_service(web):
    client, calls, live = web
    body = {"channels": [{"freq_hz": 121_500_000, "mode": "am", "squelch_db": 3, "evil": 1}], "minutes": 10, "retune": True, "x": 1}
    assert client.post("/api/listen/plan", json=body).status_code == 200
    assert calls[-1] == {"cmd": "listen-plan", "channels": [{"freq_hz": 121_500_000, "mode": "am", "squelch_db": 3}], "minutes": 10.0, "retune": True}
    client.post("/api/listen", json={"channels": [{"freq_hz": 1.62e8}]})
    assert calls[-1] == {"cmd": "listen", "channels": [{"freq_hz": 1.62e8}]}
    client.post("/api/listen/stop")
    assert calls[-1] == {"cmd": "listen-stop"}
    assert client.post("/api/listen", json={"channels": 5}).status_code == 400
    reg = client.get("/api/registry").json()["listen"]
    assert reg["max_channels"] == 4 and reg["modes"] == ["am", "nfm", "wfm"] and reg["clip_keep_mb"] == 1024


def test_a_clip_is_served_and_deleted_through_the_live_service(web):
    client, calls, live = web
    cid = "20261106T000000Z-abcd"
    live.clips_dir.mkdir()
    (live.clips_dir / f"{cid}.wav").write_bytes(listen.ulaw_wav(b"\xff" * 10, 8000))
    r = client.get(f"/api/clips/{cid}")
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav" and r.content[:4] == b"RIFF"
    assert "content-disposition" not in r.headers
    r = client.get(f"/api/clips/{cid}?download=1")
    assert r.headers["content-disposition"] == f'attachment; filename="clip-{cid}.wav"'
    assert client.get("/api/clips/20261106T000000Z-ffff").status_code == 404
    assert client.get("/api/clips/x").status_code == 400
    assert client.post(f"/api/clips/{cid}/delete").status_code == 200 and calls[-1] == {"cmd": "clip-delete", "id": cid}
    assert client.post("/api/clips/20261106T000000Z-dead/delete").status_code == 400


def test_the_audio_websocket_relays_frames(web):
    client, calls, live = web
    srv = listen.AudioServer(live.audio_socket_path).start()
    try:
        with client.websocket_connect("/ws/audio", headers={"Origin": "http://testserver"}) as ws:
            deadline = time.time() + 5
            while not srv.has_clients() and time.time() < deadline:
                time.sleep(0.02)
            data = listen.frame(listen.METER, 1, 0, 8000, 3, 2.0, -50, -40, -48)
            srv.send(data)
            assert ws.receive_bytes() == data
        deadline = time.time() + 5
        while srv.has_clients() and time.time() < deadline:
            time.sleep(0.02)
            srv.send(data)  # a send notices the hub has gone
        assert not srv.has_clients()
    finally:
        srv.close()
