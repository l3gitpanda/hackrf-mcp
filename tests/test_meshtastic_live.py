"""The meshtastic live profile: its tuning, a replay through the real
pipeline (rows, labels, positions, what is and isn't logged), and how it
keeps up with a live source."""

import time

import numpy as np
from lora_tx import encode, modulate
from test_meshtastic import NODEINFO, POSITION, SENDER, TEXT, packet

from hackrf_mcp import iq, live_profiles as lp, lora_phy, settings
from hackrf_mcp.live_profiles import Spec
from hackrf_mcp.protocols import meshtastic as M

LONG = M.BY_RADIO[(250_000, 11)]


def test_tuning_keeps_the_channel_off_the_dc_spike():
    t = lp.resolve(Spec("meshtastic"), settings.LiveSettings())
    assert t.program == lp.BUILT_IN and lp.find_program(t.profile) == lp.BUILT_IN
    assert t.freqs_hz == (906_875_000,) and t.sample_rate_hz == 2_000_000
    # The whole 250 kHz channel (and a 500 kHz one) clear of the centre.
    assert abs(t.center_hz - 906_875_000) >= 250_000 + 20_000
    assert lp.available()["meshtastic"] == lp.BUILT_IN
    assert [c.sfs for c in M.live_channels(t.freqs_hz)] == [(11,)]
    # Any other frequency is searched with every preset.
    assert {c.bw_hz for c in M.live_channels((907_000_000,))} == {125e3, 250e3, 500e3}
    # Its own colour, not the palette's next one (a lighter adsb blue).
    assert t.profile.color == "#ffd2b4"


def _replay(tmp_path, frames, seconds, center):
    fs = 2_000_000
    total = int(seconds * fs)
    x = np.zeros(total, np.complex64)
    for freq, preset, raw, start, cfo, *rest in frames:
        y = modulate(encode(raw, preset.sf, preset.cr), preset.sf, bw_hz=preset.bw_hz, cfo_hz=cfo,
                     os=fs // preset.bw_hz, lead=start * preset.bw_hz + 0.3, tail=0, amplitude=0.05,
                     sync_word=rest[0] if rest else 0x2B, fc_hz=freq)[:total]
        x[: y.size] += y * np.exp(2j * np.pi * (freq - center) / fs * np.arange(y.size)).astype(np.complex64)
    rng = np.random.default_rng(2)
    x += (0.01 * (rng.standard_normal(total) + 1j * rng.standard_normal(total))).astype(np.complex64)
    path = tmp_path / "live.cs8"
    path.write_bytes(iq.complex_to_cs8(x, 127 / 0.15))
    return path


def test_a_replay_logs_one_row_per_packet(tmp_path):
    live = settings.LiveSettings(live_dir=tmp_path / "live")
    tuning = lp.resolve(Spec("meshtastic"), live)
    slot = 906_875_000
    frames = [
        (slot, LONG, packet(SENDER, 1, NODEINFO), 0.05, 6000),
        (slot, LONG, packet(SENDER, 2, POSITION), 0.80, 6000),
        (slot, LONG, packet(SENDER, 3, TEXT), 1.55, 6000),
        (slot, LONG, packet(0x0BADF00D, 4, TEXT, to=SENDER), 2.30, -2000),
        (slot, LONG, b"\x40" + bytes(range(12)), 3.05, 0, 0x34),
    ]
    path = _replay(tmp_path, frames, 3.6, tuning.center_hz)
    rows, positions, counted = [], [], []
    ctx = lp.Context(
        settings=live,
        emit=rows.append,
        count=counted.append,
        run_dir=tmp_path / "run",
        make_source=lambda tu: lp.FileSource(path, tu.sample_rate_hz, realtime=False),
        emit_position=positions.append,
    )
    pipe = lp.make_pipeline(tuning, ctx)
    pipe.start()
    deadline = time.monotonic() + 30
    while not pipe.finished() and time.monotonic() < deadline:
        time.sleep(0.05)
    pipe.stop()
    assert pipe.failure() in (None, "the sample stream ended")
    assert [r.kind for r in rows] == ["nodeinfo", "position", "text", "text"]
    assert all(r.profile == "meshtastic" and r.freq_hz == slot for r in rows)
    info, pos, text, dm = rows
    assert info.subject == "!a1b2c3d4" and info.fields["long_name"] == "Test Node" and info.fields["hw_model"] == "HELTEC_V3"
    # The name heard in node info labels the node's later rows.
    assert pos.label == text.label == "Test Node" and pos.fields["name"] == "Test Node"
    assert abs(pos.fields["lat"] - 37.1234567) < 1e-6 and pos.fields["to"] == "all"
    # Every text it can read is logged, a direct message's too.
    assert text.fields["text"] == "hello mesh" and "hello mesh" in text.raw
    assert dm.subject == "!0badf00d" and dm.fields["to"] == "!a1b2c3d4" and dm.fields["text"] == "hello mesh"
    assert "hello mesh" in dm.raw
    assert [(p.subject, round(p.lat, 6), round(p.lon, 6)) for p in positions] == [("!a1b2c3d4", 37.123457, -122.123457)]
    assert sum(counted) == 4
    h = pipe.health()
    assert (h["packets"], h["other_lora"], h["crc_failed"], h["dropped_blocks"]) == (4, 1, 0, 0)
    # A replay's rows are dated by where they are in the file.
    gaps = np.diff([r.ts for r in rows])
    assert np.allclose(gaps, 0.75, atol=0.01)


class _Live(lp.Source):
    """A source that delivers only what the test hands it."""

    sample_rate_hz = 2_000_000

    def start(self, consumer):
        self.consumer = consumer

    def stop(self):
        self.finished.set()


def test_a_live_source_is_never_held_up(tmp_path, monkeypatch):
    monkeypatch.setattr(M.MeshtasticPipeline, "QUEUE_SECONDS", 0.1)  # 400 kB
    src = _Live()
    ctx = lp.Context(settings=settings.LiveSettings(), emit=lambda r: None, count=lambda n: None,
                     run_dir=tmp_path, make_source=lambda tu: src)
    pipe = lp.make_pipeline(lp.resolve(Spec("meshtastic"), settings.LiveSettings()), ctx)
    # The worker can't run: everything stays queued until it is full.
    monkeypatch.setattr(pipe, "_thread", lambda target, name: None)
    pipe.start()
    block = bytes(1 << 17)
    t0 = time.monotonic()
    for _ in range(6):
        src.consumer(block)
    assert time.monotonic() - t0 < 0.5  # dropped, not waited for
    assert pipe.health()["dropped_blocks"] == 3 and pipe.health()["queue_s"] == 0.1
    # Live, a frame is dated by when its samples arrived.
    pipe.arrivals.extend([(131_072, 100.0), (262_144, 100.07)])
    assert abs(pipe._ts(0.05) - (100.0 - (131_072 - 100_000) / 2e6)) < 1e-9
    assert abs(pipe._ts(0.1) - (100.07 - (262_144 - 200_000) / 2e6)) < 1e-9
    pipe.stop()


def test_the_live_scanner_reports_frames_as_they_are_found():
    got = []
    sc = lora_phy.Scanner(2_000_000, 913_125_000 - 300_000, [lora_phy.Channel(913_125_000, 250e3, (9,))], got.append)
    preset = M.BY_RADIO[(250_000, 9)]
    y = modulate(encode(b"pushed in pieces", 9, 1), 9, bw_hz=250e3, os=8, lead=1000.5, amplitude=0.05)
    y = y * np.exp(2j * np.pi * 300_000 / 2_000_000 * np.arange(y.size)).astype(np.complex64)
    for i in range(0, y.size, 50_000):
        sc.feed(y[i : i + 50_000])
    sc.finish()
    assert [f.frame.payload for f in got] == [b"pushed in pieces"] and preset.sf == got[0].frame.sf


def test_the_status_says_where_frames_were_turned_away(tmp_path):
    """A frame whose header fails and one with another sync word, through
    the pipeline: the summary hackrf_live_status prints counts both."""
    live = settings.LiveSettings(live_dir=tmp_path / "live")
    tuning = lp.resolve(Spec("meshtastic"), live)
    slot = 906_875_000
    raw = packet(SENDER, 9, TEXT)
    syms = encode(raw, LONG.sf, LONG.cr)
    for k in range(1, 6):  # the header block, past what its code corrects
        syms[k] = (syms[k] + 4 * 37 * k) % (1 << LONG.sf)
    fs = 2_000_000
    y = modulate(syms, LONG.sf, bw_hz=250e3, os=8, lead=2000.3, amplitude=0.05, cfo_hz=3000, fc_hz=slot)
    y2 = modulate(encode(b"\x40" + bytes(10), LONG.sf, 1), LONG.sf, bw_hz=250e3, os=8, lead=1000.3,
                  amplitude=0.05, sync_word=0x34, fc_hz=slot)
    x = np.concatenate([y, y2]).astype(np.complex64)
    x = x * np.exp(2j * np.pi * (slot - tuning.center_hz) / fs * np.arange(x.size)).astype(np.complex64)
    path = tmp_path / "bad.cs8"
    path.write_bytes(iq.complex_to_cs8(x, 127 / 0.15))
    ctx = lp.Context(settings=live, emit=lambda r: None, count=lambda n: None, run_dir=tmp_path / "run",
                     make_source=lambda tu: lp.FileSource(path, tu.sample_rate_hz, realtime=False))
    pipe = lp.make_pipeline(tuning, ctx)
    pipe.start()
    deadline = time.monotonic() + 30
    while not pipe.finished() and time.monotonic() < deadline:
        time.sleep(0.05)
    pipe.stop()
    text = "\n".join(pipe.health()["decoder_summary"])
    # Both sample phases find the bad header's preamble, so it counts twice.
    assert "header checksum failed 2" in text and "another sync word 1" in text and "packets logged 0" in text
    assert "turned away (header failed): SF11 250 kHz at 906.8750 MHz, sync symbols 16,2008" in text
