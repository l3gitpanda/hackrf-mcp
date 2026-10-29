import json

import numpy as np
import pytest

from hackrf_mcp import decoders, server

DUMP1090_SAMPLE = """\
*8dac1b449915532a50681919b0d7;
CRC: 000000
RSSI: -8.7 dBFS
Score: 1400
Time: 9290.50us
DF:17 AA:AC1B44 CA:5 ME:9915532A506819
 Extended Squitter Airborne velocity over ground, subsonic (19/1)
  ICAO Address:  AC1B44 (Mode S / ADS-B)
  Air/Ground:    airborne
  Heading:       316
  Speed:         477 kt groundspeed

*8dac1b44587fc5e61dd8c2000000;
CRC: 000000
RSSI: -12.9 dBFS
Score: 1800
Time: 556543.58us
DF:17 AA:AC1B44 CA:5 ME:587FC5E61DD8C2
 Extended Squitter Airborne position (barometric altitude) (11)
  ICAO Address:  AC1B44 (Mode S / ADS-B)
  Air/Ground:    airborne
  Altitude:      24500 ft barometric
  CPR type:      Airborne
  CPR latitude:  33.40505 (62222)
  CPR longitude: -118.11413 (121026)
  CPR decoding:  global

*8da2b12d2004d4f1c20820000000;
CRC: 000000
RSSI: -6.9 dBFS
  ICAO Address:  A2B12D (Mode S / ADS-B)
  Ident:         UAL817  

*020583b4244d7c;
CRC: a2b12d
DF:0 addr:A2B12D VS:0 CC:1 SL:0 RI:11 AC:948
 Short Air-Air Surveillance
  ICAO Address:  A2B12D (Mode S / ADS-B)
  Altitude:      5100 ft barometric

*8da2b12d99000000000000000000;
CRC: 000000
  ICAO Address:  A2B12D (Mode S / ADS-B)
  CPR latitude:  (74369)
  CPR longitude: (78038)
  CPR decoding:  none
"""


def _write_iq_file(path, samples: np.ndarray) -> None:
    interleaved = np.empty(2 * samples.size, dtype=np.int8)
    interleaved[0::2] = np.clip(np.rint(samples.real * 127), -127, 127).astype(np.int8)
    interleaved[1::2] = np.clip(np.rint(samples.imag * 127), -127, 127).astype(np.int8)
    interleaved.tofile(path)


@pytest.fixture
def fake_programs(monkeypatch):
    """Pretend every decoder program is installed, and record each run."""
    calls = []
    outputs = {}

    monkeypatch.setattr(decoders, "find_program", lambda d: f"/usr/bin/{d.programs[0]}")

    def fake_run(args, stdin_chunks, timeout, workdir):
        data = b"".join(stdin_chunks) if stdin_chunks is not None else None
        calls.append({"args": args, "stdin": data, "files": sorted(p.name for p in workdir.iterdir())})
        prog = args[0].rsplit("/", 1)[-1]
        return decoders.RunResult(outputs.get(prog, ""), 0, False, 0.1)

    monkeypatch.setattr(decoders, "run_program", fake_run)
    return calls, outputs


def test_parse_dump1090_aggregates_per_aircraft():
    aircraft, good = decoders.parse_dump1090(DUMP1090_SAMPLE)
    # The DF0 block's "CRC" is its address, not an error; it still counts.
    assert good == 5
    assert set(aircraft) == {"AC1B44", "A2B12D"}
    a = aircraft["AC1B44"]
    assert a["messages"] == 2
    assert a["positions"] == 1
    assert (a["lat"], a["lon"]) == (33.40505, -118.11413)
    assert a["altitude"] == "24500 ft barometric"
    assert a["rssi"] == -8.7
    assert aircraft["A2B12D"]["callsign"] == "UAL817"
    assert aircraft["A2B12D"]["altitude"] == "5100 ft barometric"
    assert aircraft["A2B12D"]["positions"] == 0


def test_adsb_native_rate_streams_cu8(tmp_path, fake_programs):
    calls, outputs = fake_programs
    outputs["dump1090-mutability"] = DUMP1090_SAMPLE
    path = tmp_path / "a.cs8"
    np.array([-128, 0, 127, -1], dtype=np.int8).tofile(path)
    result = server.hackrf_decode("adsb", str(path), 2_400_000, 1_090_000_000)
    assert "2 aircraft" in result
    assert "UAL817" in result
    assert "pos=33.4051,-118.1141" in result
    assert calls[0]["args"][1:] == ["--ifile", "-", "--iformat", "UC8", "--stats", "--oversample"]
    assert list(calls[0]["stdin"]) == [0, 128, 255, 127]


def test_adsb_multiple_of_rate_is_decimated(tmp_path, fake_programs):
    calls, _ = fake_programs
    path = tmp_path / "a.cs8"
    _write_iq_file(path, np.full(9600, 0.1 + 0.1j, np.complex64))
    result = server.hackrf_decode("adsb", str(path), 9_600_000, 1_090_000_000)
    assert "nothing decoded" in result
    assert "Converted 9.6 MS/s to 2.4 MS/s" in result
    assert "SC16" in calls[0]["args"]
    assert len(calls[0]["stdin"]) == 4 * (9600 // 4)


def test_adsb_refuses_other_rates(tmp_path, fake_programs):
    path = tmp_path / "a.cs8"
    _write_iq_file(path, np.zeros(100, np.complex64))
    result = server.hackrf_decode("adsb", str(path), 2_500_000, 1_090_000_000)
    assert result.startswith("Cannot decode:")
    assert "2,400,000" in result
    # 2.0 MS/s, direct or decimated from 8 MS/s, decoded nothing on the QNAP.
    for rate in (2_000_000, 8_000_000):
        result = server.hackrf_decode("adsb", str(path), rate, 1_090_000_000)
        assert result.startswith("Cannot decode:") and "2.0 MS/s decodes nothing" in result


def test_missing_program_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(decoders, "find_program", lambda d: None)
    path = tmp_path / "a.cs8"
    _write_iq_file(path, np.zeros(100, np.complex64))
    result = server.hackrf_decode("uat", str(path), 2_083_333, 978_000_000)
    assert "dump978-fa" in result and "not installed" in result


def test_ais_reads_the_file_directly_when_centered(tmp_path, fake_programs):
    calls, outputs = fake_programs
    msgs = [
        {"class": "AIS", "mmsi": 367435980, "type": 1, "lat": 33.5164, "lon": -117.893, "speed": 3.9, "status_text": "Under way using engine", "signalpower": -30.7},
        {"class": "AIS", "mmsi": 367435980, "type": 3, "lat": 33.5165, "lon": -117.8934, "speed": 3.8},
        {"class": "AIS", "mmsi": 993692211, "type": 21, "name": "LONG BEACH LT", "aid_type_text": "Light, without sectors", "lat": 33.7232, "lon": -118.1867},
    ]
    outputs["AIS-catcher"] = "AIS-catcher banner\n" + "\n".join(json.dumps(m) for m in msgs) + "\n[AIS engine] received: 3 msgs\n"
    path = tmp_path / "ais.cs8"
    _write_iq_file(path, np.zeros(1000, np.complex64))
    result = server.hackrf_decode("ais", str(path), 2_000_000, 162_000_000)
    assert "2 stations, 3 messages" in result
    assert "MMSI 367435980" in result and "msgs=2" in result
    assert "'LONG BEACH LT'" in result
    assert calls[0]["args"][-3:] == ["-r", "CS8", str(path)]
    assert calls[0]["stdin"] is None


def test_ais_off_center_is_mixed_and_piped(tmp_path, fake_programs):
    calls, _ = fake_programs
    path = tmp_path / "ais.cs8"
    _write_iq_file(path, np.zeros(1000, np.complex64))
    server.hackrf_decode("ais", str(path), 2_000_000, 162_100_000)
    assert calls[0]["args"][-3:] == ["-r", "CS8", "."]
    assert len(calls[0]["stdin"]) == 2000


def test_ais_refuses_capture_without_the_channels(tmp_path, fake_programs):
    path = tmp_path / "ais.cs8"
    _write_iq_file(path, np.zeros(1000, np.complex64))
    result = server.hackrf_decode("ais", str(path), 2_000_000, 163_000_000)
    assert "outside this capture" in result


def test_vdl2_needs_a_multiple_of_105k(tmp_path, fake_programs):
    path = tmp_path / "v.cs8"
    _write_iq_file(path, np.zeros(1000, np.complex64))
    assert "2,100,000" in server.hackrf_decode("vdl2", str(path), 2_000_000, 136_800_000)


def test_vdl2_decimates_and_passes_channels(tmp_path, fake_programs):
    calls, outputs = fake_programs
    outputs["dumpvdl2"] = json.dumps(
        {"vdl2": {"freq": 136975000, "avlc": {"src": {"addr": "A1B2C3", "type": "Aircraft"}, "acars": {"flight": "UA1", "reg": ".N1", "label": "H1", "msg_text": "HELLO"}}}}
    )
    path = tmp_path / "v.cs8"
    _write_iq_file(path, np.zeros(21_000, np.complex64))
    result = server.hackrf_decode("vdl2", str(path), 2_100_000, 136_800_000)
    args = calls[0]["args"]
    assert args[args.index("--oversample") + 1] == "10"
    assert args[args.index("--centerfreq") + 1] == "136812500"
    assert "136975000" in args
    assert "vdl2: 1 frames" in result and "HELLO" in result


def test_acars_writes_one_wav_per_channel(tmp_path, fake_programs):
    calls, outputs = fake_programs
    outputs["acarsdec"] = json.dumps({"flight": "AAL1", "tail": "N123AA", "label": "Q0", "mode": "2", "text": "PING"})
    path = tmp_path / "a.cs8"
    _write_iq_file(path, (0.01 * np.ones(200_000)).astype(np.complex64))
    result = server.hackrf_decode("acars", str(path), 2_000_000, 130_800_000)
    # 129.125 is 1.675 MHz off center, outside the usable +-0.8 MHz.
    assert len(calls) == 5
    assert all(c["args"][1:3] == ["-o", "4"] for c in calls)
    assert all(any(f.endswith(".wav") for f in c["files"]) for c in calls)
    assert "acars: 5 messages" in result and "flight=AAL1" in result


def test_multimon_demodulates_fm_and_passes_modes(tmp_path, fake_programs):
    calls, outputs = fake_programs
    outputs["multimon-ng"] = "POCSAG1200: Address:  123456  Function: 0  Alpha:   TEST\n"
    rate = 2_000_000
    t = np.arange(rate // 4) / rate
    phase = 2 * np.pi * 40_000 * t + 3.0 * np.sin(2 * np.pi * 1_000 * t)
    path = tmp_path / "p.cs8"
    _write_iq_file(path, (0.5 * np.exp(1j * phase)).astype(np.complex64))
    result = server.hackrf_decode("pocsag", str(path), rate, 929_000_000, frequencies_hz=[929_040_000])
    args = calls[0]["args"]
    assert ["-a", "POCSAG512"] == args[args.index("POCSAG512") - 1 : args.index("POCSAG512") + 1]
    assert args[-1] == "-"
    pcm = np.frombuffer(calls[0]["stdin"], "<i2").astype(np.float32)
    assert abs(pcm.size - 22_050 / 4) < 50
    spec = np.abs(np.fft.rfft(pcm[2000:]))
    freqs = np.fft.rfftfreq(pcm[2000:].size, 1 / 22_050)
    assert abs(freqs[np.argmax(spec)] - 1_000) < 30
    assert "TEST" in result


def test_multimon_needs_a_frequency(tmp_path, fake_programs):
    path = tmp_path / "p.cs8"
    _write_iq_file(path, np.zeros(100, np.complex64))
    assert "needs frequencies_hz" in server.hackrf_decode("flex", str(path), 2_000_000, 929_000_000)


def test_decode_saves_full_output_inside_capture_dir(tmp_path, fake_programs):
    _, outputs = fake_programs
    outputs["dump1090-mutability"] = DUMP1090_SAMPLE
    path = tmp_path / "a.cs8"
    _write_iq_file(path, np.zeros(100, np.complex64))
    result = server.hackrf_decode("adsb", str(path), 2_400_000, 1_090_000_000, output_path="adsb.txt")
    assert (tmp_path / "adsb.txt").read_text() == DUMP1090_SAMPLE
    assert "saved to adsb.txt" in result
    refused = server.hackrf_decode("adsb", str(path), 2_400_000, 1_090_000_000, output_path="../x.txt")
    assert refused.startswith("REFUSED")


def test_run_program_feeds_stdin_and_times_out(tmp_path):
    ok = decoders.run_program(["cat"], [b"abc", b"def"], 10, tmp_path)
    assert ok.output == "abcdef" and ok.returncode == 0 and not ok.timed_out
    slow = decoders.run_program(["sleep", "5"], None, 0.3, tmp_path)
    assert slow.timed_out
