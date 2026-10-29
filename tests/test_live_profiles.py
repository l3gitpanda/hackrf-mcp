import json
import sys
import time

import numpy as np
import pytest

from hackrf_mcp import iq, live_profiles as lp, settings
from hackrf_mcp.live_profiles import CONTENT_NOT_LOGGED, MultimonParser, Spec, SpecError


def _live(**kw):
    return settings.LiveSettings(**kw)


def _found(profile):
    return f"/usr/bin/{profile.programs[0]}"


# --- Tuning ------------------------------------------------------------------


def test_choose_center_covers_channels_and_avoids_dc():
    c = lp.choose_center((931_750_000,), 2_205_000)
    assert abs(931_750_000 - c) >= 20_000
    assert abs(931_750_000 - c) <= 0.4 * 2_205_000
    chans = tuple(162_400_000 + 25_000 * k for k in range(7))
    c = lp.choose_center(chans, 2_205_000)
    assert all(abs(f - c) >= 20_000 for f in chans)
    assert c % 25 == 0
    with pytest.raises(SpecError, match="span"):
        lp.choose_center((929_000_000, 932_000_000), 2_205_000)


def test_resolve_profiles_and_refusals():
    t = lp.resolve(Spec("adsb"), _live(), _found)
    assert (t.center_hz, t.sample_rate_hz) == (1_090_000_000, 2_400_000)
    t = lp.resolve(Spec("flex", (931_750_000,)), _live(), _found)
    assert t.sample_rate_hz == 2_205_000 and t.freqs_hz == (931_750_000,)
    with pytest.raises(SpecError, match="frequencies_hz"):
        lp.resolve(Spec("flex"), _live(), _found)
    with pytest.raises(SpecError, match="unknown profile"):
        lp.resolve(Spec("nope"), _live(), _found)
    with pytest.raises(SpecError, match="not installed"):
        lp.resolve(Spec("ais"), _live(), lambda p: None)
    with pytest.raises(SpecError, match="LNA"):
        lp.resolve(Spec("ais", lna_gain_db=7), _live(), _found)
    with pytest.raises(SpecError, match="outside"):
        lp.resolve(Spec("acars", (136_000_000,)), _live(), _found)
    with pytest.raises(SpecError, match="fixed"):
        lp.resolve(Spec("adsb", (1_091_000_000,)), _live(), _found)


def test_gains_come_from_the_call_then_the_environment_then_the_profile():
    assert lp.resolve(Spec("eas"), _live(), _found).lna_gain_db == lp.PROFILES["eas"].lna_gain_db
    assert lp.resolve(Spec("eas"), _live(lna_gain_db=16), _found).lna_gain_db == 16
    assert lp.resolve(Spec("eas", lna_gain_db=8), _live(lna_gain_db=16), _found).lna_gain_db == 8


def test_spec_round_trips_through_json():
    s = Spec("flex", (931_750_000,), 32, 40, True)
    assert Spec.from_json(json.loads(json.dumps(s.to_json()))) == s


# --- FFT channelizer ---------------------------------------------------------


def test_fft_channelizer_extracts_tones_continuously():
    rate = 2_000_000
    t = np.arange(rate // 2) / rate
    x = (0.3 * np.exp(2j * np.pi * (750_000 + 100) * t) + 0.3 * np.exp(2j * np.pi * (-400_000 - 250) * t)).astype(
        np.complex64
    )
    ch = iq.FFTChannelizer(rate, [750_000, -400_000, 100_000], 160, 10_000)
    parts = [[], [], []]
    for i in range(0, x.size, 77_777):  # chunk size unrelated to the block size
        for k, y in enumerate(ch(x[i : i + 77_777])):
            parts[k].append(y)
    y = [np.concatenate(p) for p in parts]
    assert y[0].size == y[1].size > 0
    settled = slice(100, None)  # past the filter's start-up
    for k, offset in ((0, 100), (1, -250)):
        a = y[k][settled]
        assert abs(np.mean(np.abs(a)) - 0.3) < 0.01
        f, _ = iq.fm_discriminate(a)
        hz = f[1:] * rate / 160 / (2 * np.pi)
        # Constant frequency all the way through: no jumps where blocks join.
        assert np.max(np.abs(hz - offset)) < 5
    assert np.mean(np.abs(y[2][settled])) < 1e-3  # an empty channel stays empty
    assert ch.power_db[0] > ch.power_db[2] + 60


def test_fft_channelizer_refuses_rates_it_cannot_split():
    with pytest.raises(ValueError):
        iq.FFTChannelizer(2_000_001, [0], 160, 10_000)
    with pytest.raises(ValueError):
        iq.FFTChannelizer(2_000_000, [0], 7, 10_000)
    with pytest.raises(ValueError):
        iq.FFTChannelizer(2_000_000, [0], 160, 20_000)


def test_byte_converters():
    raw = np.array([-128, -1, 0, 1, 127, 5], dtype=np.int8).tobytes()
    assert list(iq.cs8_to_cu8(raw)) == [0, 127, 128, 129, 255, 133]
    c = iq.cs8_to_complex(raw)
    assert c.dtype == np.complex64 and c.size == 3
    assert c[0] == np.complex64(-1 - 1 / 128 * 1j)


# --- ADS-B and AIS aggregation ----------------------------------------------


def test_adsb_aggregator_summarises_each_aircraft_per_minute():
    rows = []
    agg = lp.AdsbAggregator(rows.append, poll_s=5)
    base = 60 * 1000.0
    snap = lambda total, aircraft: {"messages": total, "aircraft": aircraft}
    a1 = {"hex": "a1b2c3", "flight": "UAL123  ", "squawk": "1200", "altitude": 10000, "messages": 10, "seen": 0.5,
          "lat": 33.5, "lon": -118.1, "seen_pos": 1.0, "rssi": -20.0, "speed": 300, "track": 90}
    assert agg.update(base + 5, snap(10, [a1])) == 10
    a1b = dict(a1, altitude=11000, messages=25, rssi=-12.0, seen=0.2)
    gone = {"hex": "ffffff", "messages": 3, "seen": 50}  # not heard in this poll
    assert agg.update(base + 10, snap(28, [a1b, gone])) == 18
    assert rows == []
    agg.update(base + 65, snap(30, [dict(a1b, messages=27, seen=1)]))  # next minute
    assert len(rows) == 1
    r = rows[0]
    assert (r.subject, r.label, r.kind, r.profile) == ("A1B2C3", "UAL123", "aircraft", "adsb")
    assert r.fields["altitude_min"] == 10000 and r.fields["altitude_max"] == 11000
    assert r.fields["messages"] == 25 and r.fields["positions"] == 2
    assert r.level_db == -12.0
    assert base <= r.ts <= r.ts_end < base + 60
    agg.flush()
    assert len(rows) == 2 and rows[1].fields["messages"] == 2


def _ais(mmsi, mtype, **kw):
    return {"class": "AIS", "mmsi": mmsi, "type": mtype, "channel": "A", "signalpower": -20.0, "nmea": ["!AIVDM,x"], **kw}


def test_ais_aggregator_summarises_positions_and_keeps_other_messages():
    rows = []
    agg = lp.AisAggregator(rows.append)
    base = 60 * 2000.0
    agg.message(base + 1, _ais(367000001, 5, shipname="KILLEEN@@", callsign="WDA1234"))
    assert len(rows) == 1 and rows[0].kind == "ais" and rows[0].label == "KILLEEN"
    agg.message(base + 2, _ais(367000001, 1, lat=33.5, lon=-118.0, speed=3.71, course=267.0001))
    agg.message(base + 12, _ais(367000001, 3, lat=33.6, lon=-118.1, speed=102.3))  # 102.3 = not available
    agg.message(base + 13, _ais(993692036, 21, name="LB", lat=33.69, lon=-118.18))
    agg.message(base + 14, {"class": "AIS"})  # no MMSI: ignored
    agg.flush()
    ship = next(r for r in rows if r.subject == "367000001" and r.kind == "vessel")
    assert ship.label == "KILLEEN" and ship.fields["callsign"] == "WDA1234"
    assert ship.fields["lat"] == 33.6 and ship.fields["speed"] == 3.7 and ship.fields["messages"] == 2
    assert ship.fields["types"] == [1, 3] and ship.freq_hz == 161_975_000
    aton = next(r for r in rows if r.subject == "993692036")
    assert aton.kind == "aid-to-navigation" and aton.label == "LB"


# --- Line parsers ------------------------------------------------------------

ACARS_LINE = (
    '{"timestamp":1793313855.8,"station_id":"rx1","channel":0,"freq":0.000,"level":-10.5,"error":0,'
    '"mode":"2","label":"16","block_id":"2","ack":false,"tail":".N215NV","flight":"G40651","msgno":"M24A",'
    '"text":"215500,12753","app":{"name":"acarsdec","ver":"3.7"}}'
)
VDL2_LINE = (
    '{"vdl2":{"app":{"name":"dumpvdl2","ver":"2.6.0"},"t":{"sec":1793314584,"usec":43789},"freq":136975000,'
    '"sig_level":-24.1,"noise_level":-45.5,"avlc":{"src":{"addr":"A3C2A9","type":"Aircraft","status":"Airborne"},'
    '"dst":{"addr":"29B838","type":"Ground station"},"frame_type":"I","acars":{"reg":".N341SV","label":"_d",'
    '"flight":"AA2465","msg_text":""}}}}'
)


def test_parse_acars_and_vdl2():
    r = lp.parse_acars(ACARS_LINE, 131_550_000, 5.0)
    assert (r.subject, r.label, r.freq_hz, r.level_db) == ("N215NV", "G40651", 131_550_000, -10.5)
    assert "station_id" not in r.fields and r.fields["text"] == "215500,12753"
    assert lp.parse_acars("exiting ...", 1, 0) is None
    v = lp.parse_vdl2(VDL2_LINE, 5.0)
    assert (v.subject, v.label, v.freq_hz, v.level_db) == ("A3C2A9", "AA2465", 136_975_000, -24.1)
    assert v.fields["acars"]["reg"] == ".N341SV" and v.fields["dst_type"] == "Ground station"


SECRET = "MEET AT 5 BRING THE KEYS"
FLEX_NEW = f"FLEX|2026-10-29 22:56:00|1600/4/F/A|14.102|000499030|ALN|{SECRET}"
FLEX_OLD = f"FLEX: 2026-10-29 22:56:00 1600/4/A 14.102 [000385750] UNK {SECRET}"
POCSAG_ALPHA = f"POCSAG1200: Address: 1234567  Function: 3  Alpha:   {SECRET}"
POCSAG_NUM = "POCSAG512: Address:  223344  Function: 0  Numeric: 5551234"
POCSAG_TONE = "POCSAG1200: Address:   99999  Function: 1 "


def _no_secret(rows):
    for r in rows:
        assert SECRET not in json.dumps(r.fields)
        assert SECRET not in (r.raw or "")
        assert "KEYS" not in (r.raw or "") and "5551234" not in (r.raw or "")


def test_pager_text_is_dropped_by_default():
    p = MultimonParser("flex", 931_750_000, pager_content=False)
    rows = []
    for line in (FLEX_NEW, "a continuation line of the message", FLEX_OLD, "  more text"):
        rows += p.feed(line, 100.0)
    rows += p.flush()
    assert len(rows) == 2
    _no_secret(rows)
    assert rows[0].subject == "499030" and rows[0].label == "ALN"
    assert rows[0].fields == {"protocol": "FLEX", "mode": "1600/4/F/A", "frame": "14.102", "capcodes": ["499030"], "type": "ALN"}
    assert rows[0].raw.endswith(CONTENT_NOT_LOGGED)
    assert rows[1].subject == "385750" and rows[1].label == "UNK" and rows[1].raw.endswith(CONTENT_NOT_LOGGED)

    p = MultimonParser("pocsag", 152_000_000, pager_content=False)
    rows = [r for line in (POCSAG_ALPHA, POCSAG_NUM, POCSAG_TONE) for r in p.feed(line, 1.0)]
    _no_secret(rows)
    assert [(r.subject, r.label) for r in rows] == [("1234567", "Alpha"), ("223344", "Numeric"), ("99999", "Tone")]
    assert rows[0].fields["function"] == 3 and "message" not in rows[0].fields
    # A pager line in a shape the parser doesn't know leaves no row at all.
    assert p.feed(f"FLEX_NEXT|weird|{SECRET}", 2.0) == []


def test_pager_text_is_kept_when_the_operator_allows_it():
    p = MultimonParser("flex", 931_750_000, pager_content=True)
    assert p.feed(FLEX_NEW, 100.0) == []  # held for continuation lines
    assert p.feed("SECOND LINE", 100.5) == []
    rows = p.tick(103.0)
    assert len(rows) == 1
    assert rows[0].fields["message"] == SECRET + "\nSECOND LINE"
    assert SECRET in rows[0].raw
    rows = p.feed(POCSAG_ALPHA, 104.0)
    assert rows == [] and p.flush()[0].fields["message"] == SECRET


def test_aprs_and_eas_lines():
    p = MultimonParser("aprs", 144_390_000, pager_content=False)
    (r,) = p.feed("APRS: N0CALL-9>APRS,WIDE1-1,WIDE2-1:!3400.00N/11800.00W>hello", 1.0)
    assert (r.kind, r.subject) == ("aprs", "N0CALL-9")
    assert r.fields["path"] == "WIDE1-1,WIDE2-1" and r.fields["payload"].startswith("!3400")
    p = MultimonParser("eas", 162_475_000, pager_content=False)
    (r,) = p.feed("EAS: ZCZC-WXR-RWT-006037-006059+0030-3021600-KEAX/NWS-", 1.0)
    assert (r.kind, r.subject, r.label) == ("eas", "KEAX/NWS", "RWT")
    assert r.fields["originator"] == "WXR" and r.fields["locations"] == ["006037", "006059"]
    assert p.feed("EAS (part): ZCZC-WXR", 2.0) == []


def test_wav_header_is_a_streaming_mono_16_bit_header():
    h = lp.wav_header(12_500)
    assert h[:4] == b"RIFF" and h[8:16] == b"WAVEfmt " and len(h) == 44
    assert int.from_bytes(h[24:28], "little") == 12_500


# --- A whole pipeline, with a stand-in decoder program -----------------------


def _write_cs8(path, x):
    out = np.empty(2 * x.size, dtype=np.int8)
    out[0::2] = np.clip(np.rint(x.real * 127), -127, 127)
    out[1::2] = np.clip(np.rint(x.imag * 127), -127, 127)
    out.tofile(path)


def test_multimon_pipeline_end_to_end_with_a_fake_decoder(tmp_path):
    # A stand-in for multimon-ng: reads the audio it is sent, notes how many
    # bytes that was, then prints a FLEX page with text and a continuation.
    fake = tmp_path / "fake-multimon"
    got_file = tmp_path / "bytes"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "n = len(sys.stdin.buffer.read())\n"
        f"open({str(got_file)!r}, 'w').write(str(n))\n"
        f"print({FLEX_NEW!r}, flush=True)\n"
        "print('continued text', flush=True)\n"
    )
    fake.chmod(0o755)
    rate = lp.MULTIMON_INPUT_RATE
    capture = tmp_path / "c.cs8"
    t = np.arange(rate // 4) / rate
    _write_cs8(capture, (0.2 * np.exp(2j * np.pi * -25_000 * t)).astype(np.complex64))
    rows = []
    live = _live(live_dir=tmp_path / "live")
    tuning = lp.resolve(Spec("flex", (931_750_000,)), live, lambda p: str(fake))
    ctx = lp.Context(
        settings=live,
        emit=rows.append,
        count=lambda n: None,
        run_dir=tmp_path / "run",
        make_source=lambda tu: lp.FileSource(capture, rate, realtime=False),
    )
    pipe = lp.make_pipeline(tuning, ctx)
    pipe.start()
    deadline = time.monotonic() + 10
    while not pipe.finished() and time.monotonic() < deadline:
        time.sleep(0.05)
    for p in pipe.procs:  # end of input: the fake prints and exits
        p.proc.stdin.close()
        p.proc.wait(timeout=10)
    pipe.stop()
    pages = [r for r in rows if r.kind == "page"]
    assert len(pages) == 1
    _no_secret(pages)
    assert pages[0].freq_hz == 931_750_000 and pages[0].level_db is not None
    assert not any("continued" in (r.raw or "") for r in rows)
    # The decoder got 16-bit audio at 22,050 S/s for the quarter second
    # (less the channelizer's last part-block).
    got = int(got_file.read_text())
    assert 0.6 * 2 * 22_050 / 4 < got <= 2 * 22_050 / 4
