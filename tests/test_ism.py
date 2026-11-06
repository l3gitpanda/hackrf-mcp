"""ISM-band devices through rtl_433 (protocols/ism.py): rows, repeats, the
live profile's tuning and pipeline, and the capture decoder."""

import json
import sys
import time

import numpy as np

from hackrf_mcp import decoders, settings
from hackrf_mcp import live_profiles as lp
from hackrf_mcp.decoders import Capture
from hackrf_mcp.live_profiles import Spec
from hackrf_mcp.protocols import ism

WEATHER = {
    "time": "@0.512s", "model": "Acurite-Tower", "id": 12345, "channel": "A", "battery_ok": 1,
    "temperature_C": 18.3, "humidity": 52, "mic": "CHECKSUM", "mod": "ASK", "freq": 433.94012,
    "rssi": -7.2, "snr": 21.4, "noise": -28.6, "protocol": 40,
}  # fmt: skip
TYRE = {"model": "Toyota", "type": "TPMS", "id": "f1e2d3c4", "pressure_PSI": 33.5, "temperature_C": 21.0, "mic": "CRC", "freq": 315.02, "rssi": -15.0}


def test_a_message_is_a_row_with_every_field():
    r = ism.row_of(WEATHER, 433_920_000, 1000.0)
    assert (r.profile, r.kind, r.subject, r.label) == ("ism", "ism", "Acurite-Tower 12345 chA", "Acurite-Tower")
    assert r.freq_hz == 433_940_120 and r.level_db == -7.2 and r.ts == 1000.0
    assert "time" not in r.fields and r.fields["temperature_C"] == 18.3 and r.fields["protocol"] == 40
    assert json.loads(r.raw)["humidity"] == 52
    t = ism.row_of(TYRE, 315_000_000, 1.0)
    assert t.subject == "Toyota f1e2d3c4" and t.fields["type"] == "TPMS"
    assert ism.row_of({"model": "Generic-Remote"}, 315_000_000, 1.0).freq_hz == 315_000_000
    assert ism.parse('{"model": "X", "id": 1}') == {"model": "X", "id": 1}
    for line in ("rtl_433 version 23.11", "{not json", "[1, 2]", '{"time": "@1s"}', ""):
        assert ism.parse(line) is None


def test_repeats_within_three_seconds_are_one_row():
    d = ism.Dedupe()
    assert d.fresh(WEATHER, 100.0)
    assert not d.fresh({**WEATHER, "rssi": -9.9, "snr": 3.0, "freq": 433.95}, 100.4)  # the same, heard again
    assert d.fresh({**WEATHER, "temperature_C": 18.4}, 101.0)  # a new reading
    assert d.fresh({**WEATHER, "temperature_C": 18.4}, 104.5)  # the next transmission
    assert d.fresh(TYRE, 104.6)


def test_each_band_tunes_clear_of_the_dc_spike():
    live = settings.LiveSettings()
    for freqs, rate in (((), 250_000), ((315_000_000,), 250_000), ((915_000_000,), 1_000_000)):
        t = lp.resolve(Spec("ism", freqs), live, lambda p: "/usr/bin/rtl_433")
        f = t.freqs_hz[0]
        assert f == (freqs[0] if freqs else 433_920_000)
        assert t.sample_rate_hz == 2_000_000
        pipe = ism.IsmPipeline(t, None)
        assert pipe.out_rate == rate and pipe.bandwidth_hz < rate
        # The channel's whole filter lies inside the usable band, and clear
        # of the DC spike at the centre.
        off, half = abs(f - t.center_hz), pipe.bandwidth_hz / 2
        assert off + half <= 0.4 * t.sample_rate_hz and off - half >= 20_000
        assert pipe.decoder_args(f) == ["/usr/bin/rtl_433", "-r", "cs16:-", "-s", str(rate), "-f", str(f), "-F", "json", "-M", "level", "-M", "protocol"]


def _write_cs8(path, x):
    out = np.empty(2 * x.size, dtype=np.int8)
    out[0::2] = np.clip(np.rint(x.real * 127), -127, 127)
    out[1::2] = np.clip(np.rint(x.imag * 127), -127, 127)
    out.tofile(path)


def test_the_live_pipeline_feeds_rtl_433_and_logs_its_messages(tmp_path):
    # A stand-in for rtl_433: notes its arguments and how many bytes it was
    # sent, then prints a banner, one reading three times (as devices send
    # them) and a tyre sensor.
    fake = tmp_path / "fake-rtl_433"
    got = tmp_path / "got.json"
    lines = [json.dumps(WEATHER), json.dumps({**WEATHER, "rssi": -8.0}), json.dumps(WEATHER), json.dumps(TYRE)]
    fake.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "n = len(sys.stdin.buffer.read())\n"
        f"open({str(got)!r}, 'w').write(json.dumps({{'args': sys.argv[1:], 'bytes': n}}))\n"
        "print('rtl_433 version 23.11 branch  at ...', flush=True)\n"
        f"for line in {lines!r}: print(line, flush=True)\n"
    )
    fake.chmod(0o755)
    live = settings.LiveSettings(live_dir=tmp_path / "live")
    tuning = lp.resolve(Spec("ism"), live, lambda p: str(fake))
    rate = tuning.sample_rate_hz
    capture = tmp_path / "c.cs8"
    t = np.arange(rate // 2) / rate
    _write_cs8(capture, (0.2 * np.exp(2j * np.pi * (433_920_000 - tuning.center_hz) * t)).astype(np.complex64))
    rows, counts = [], []
    ctx = lp.Context(
        settings=live,
        emit=rows.append,
        count=counts.append,
        run_dir=tmp_path / "run",
        make_source=lambda tu: lp.FileSource(capture, rate, realtime=False),
    )
    pipe = lp.make_pipeline(tuning, ctx)
    pipe.start()
    deadline = time.monotonic() + 10
    while not pipe.finished() and time.monotonic() < deadline:
        time.sleep(0.05)
    for p in pipe.procs:
        p.proc.stdin.close()
        p.proc.wait(timeout=10)
    deadline = time.monotonic() + 5
    while len(rows) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    pipe.stop()
    assert [r.subject for r in rows] == ["Acurite-Tower 12345 chA", "Toyota f1e2d3c4"]
    assert sum(counts) == 4  # every message decoded counts, repeats too
    seen = json.loads(got.read_text())
    assert seen["args"][:6] == ["-r", "cs16:-", "-s", "250000", "-f", "433920000"]
    # Half a second at 250 kS/s, 4 bytes a sample (less the channelizer's
    # last part-block).
    assert 0.6 * 4 * 250_000 / 2 < seen["bytes"] <= 4 * 250_000 / 2
    assert pipe.health()["channel_levels_dbfs"]["433.9200"] is not None


def test_the_capture_decoder_runs_rtl_433_over_the_capture(tmp_path, monkeypatch):
    path = tmp_path / "c.cs8"
    path.write_bytes(b"\0" * 4000)
    calls = []

    def fake_run(args, stdin_chunks, timeout, workdir):
        calls.append((args, sum(len(c) for c in stdin_chunks)))
        out = "\n".join(["banner", json.dumps(WEATHER), json.dumps({**WEATHER, "temperature_C": 18.6}), json.dumps(TYRE)])
        return decoders.RunResult(out, 0, False, 0.1)

    monkeypatch.setattr(decoders, "run_program", fake_run)
    cap = Capture(path, 2_000_000, 433_620_000, 1500)
    res = ism.decode_ism(cap, "rtl_433", 60, tmp_path, [])
    assert calls[0][0][:6] == ["rtl_433", "-r", "cs8:-", "-s", "2000000", "-f"] and calls[0][1] == 3000
    assert res.headline == "ism: 3 messages from 2 device(s)."
    assert res.table[0].startswith("  Acurite-Tower 12345 chA: 2 message(s); last battery_ok=1 temperature_C=18.6 humidity=52")
    assert "pressure_PSI=33.5" in res.table[1]
    d = decoders.DECODERS["ism"]
    assert d.rate_ok(2_000_000) and not d.rate_ok(20_000_000) and 433_920_000 in d.default_freqs_hz
    monkeypatch.setattr(decoders, "run_program", lambda *a: decoders.RunResult("nothing here", 0, False, 0.1))
    assert ism.decode_ism(cap, "rtl_433", 60, tmp_path, []).headline.startswith("ism: nothing")
