import numpy as np
import pytest

from hackrf_mcp import iq, server


def _write_iq_file(path, samples: np.ndarray) -> None:
    interleaved = np.empty(2 * samples.size, dtype=np.int8)
    interleaved[0::2] = np.clip(np.rint(samples.real * 127), -127, 127).astype(np.int8)
    interleaved[1::2] = np.clip(np.rint(samples.imag * 127), -127, 127).astype(np.int8)
    interleaved.tofile(path)


def _noise(n, level=0.01, seed=0):
    rng = np.random.default_rng(seed)
    return (level * (rng.standard_normal(n) + 1j * rng.standard_normal(n))).astype(np.complex64)


def test_iter_iq_chunks_covers_file_and_respects_cap(tmp_path):
    path = tmp_path / "c.cs8"
    _write_iq_file(path, _noise(10_000, 0.3))
    chunks = list(iq.iter_iq_chunks(path, chunk_samples=3000))
    assert [c.size for c in chunks] == [3000, 3000, 3000, 1000]
    np.testing.assert_allclose(np.concatenate(chunks), iq.read_iq_int8(path), atol=1e-6)
    assert sum(c.size for c in iq.iter_iq_chunks(path, 3000, max_samples=4500)) == 4500


def test_iter_cu8_chunks_flips_the_sign_bit(tmp_path):
    path = tmp_path / "c.cs8"
    np.array([-128, -1, 0, 1, 127, 5], dtype=np.int8).tofile(path)
    out = b"".join(iq.iter_cu8_chunks(path, chunk_samples=2))
    assert list(out) == [0, 127, 128, 129, 255, 133]


def test_channelize_in_chunks_matches_one_pass(tmp_path):
    rate = 200_000
    path = tmp_path / "c.cs8"
    t = np.arange(100_000) / rate
    x = 0.4 * np.exp(2j * np.pi * 30_000 * t) + _noise(t.size, 0.05)
    _write_iq_file(path, x.astype(np.complex64))
    whole = np.concatenate(list(iq.channelize(path, rate, 30_000, 20, chunk_samples=1 << 20)))
    pieces = np.concatenate(list(iq.channelize(path, rate, 30_000, 20, chunk_samples=777)))
    assert whole.size == pieces.size == 100_000 // 20
    np.testing.assert_allclose(pieces, whole, atol=1e-4)
    # The tone was mixed to 0 Hz: after the filter settles it's a constant.
    settled = whole[200:]
    assert np.std(np.angle(settled)) < 0.1
    assert abs(np.mean(np.abs(settled)) - 0.4) < 0.05


def test_decimation_stages_multiply_back():
    for factor in (1, 2, 10, 40, 160, 7 * 13):
        stages = iq._decimation_stages(factor)
        assert int(np.prod(stages)) == factor if stages else factor == 1


def test_pick_decimation():
    assert iq.pick_decimation(2_000_000, 44_100) == 40
    assert iq.pick_decimation(2_000_000, 171_000) == 10
    assert iq.pick_decimation(2_000_000, 3_000_000) == 1


def test_fm_discriminate_is_continuous_across_chunks():
    rate = 48_000
    t = np.arange(4800) / rate
    x = np.exp(2j * np.pi * 1_000 * t).astype(np.complex64)
    a, prev = iq.fm_discriminate(x[:1000])
    b, _ = iq.fm_discriminate(x[1000:], prev)
    joined = np.concatenate([a[1:], b])
    np.testing.assert_allclose(joined, 2 * np.pi * 1_000 / rate, atol=1e-3)


def test_stream_spectrogram_bounds_columns_and_finds_tone(tmp_path):
    rate = 240_000
    path = tmp_path / "c.cs8"
    t = np.arange(rate * 2) / rate
    _write_iq_file(path, (0.5 * np.exp(2j * np.pi * 20_000 * t)).astype(np.complex64))
    freqs, times, power = iq.stream_spectrogram(path, rate, fft_size=256, max_time_bins=50)
    assert power.shape == (256, 50)
    assert times.size == 50
    assert abs(freqs[np.argmax(power.max(axis=1))] - 20_000) < rate / 256


def test_stream_spectrogram_max_keeps_a_short_burst(tmp_path):
    rate = 100_000
    path = tmp_path / "c.cs8"
    x = _noise(rate * 10)
    x[500_000:500_200] += 0.8 * np.exp(2j * np.pi * 10_000 * np.arange(200) / rate)
    _write_iq_file(path, x)
    _, _, pmax = iq.stream_spectrogram(path, rate, fft_size=64, max_time_bins=20, reduce="max")
    _, _, pmean = iq.stream_spectrogram(path, rate, fft_size=64, max_time_bins=20, reduce="mean")
    assert pmax.max() - np.median(pmax) > pmean.max() - np.median(pmean) + 10


def test_stream_stats_reports_power_and_peak(tmp_path):
    rate = 240_000
    path = tmp_path / "c.cs8"
    t = np.arange(rate) / rate
    _write_iq_file(path, (0.5 * np.exp(2j * np.pi * -30_000 * t)).astype(np.complex64))
    n, mean_db, freqs, power = iq.stream_stats(path, rate, fft_size=1024)
    assert n == rate
    assert abs(mean_db - 10 * np.log10(0.25)) < 0.5
    assert abs(freqs[np.argmax(power)] + 30_000) < rate / 1024


def test_detect_bursts_file_across_chunk_boundaries(tmp_path, monkeypatch):
    monkeypatch.setattr(iq, "CHUNK_SAMPLES", 4096)
    rate = 100_000
    path = tmp_path / "c.cs8"
    x = _noise(rate)
    x[50_000:60_000] += 0.8
    x[80_000:80_100] += 0.8  # 1 ms
    _write_iq_file(path, x)
    bursts, n, used_rate = iq.detect_bursts_file(path, rate, threshold_db=10, min_duration_s=0.0005)
    assert n == rate and used_rate == rate
    assert len(bursts) == 2
    assert abs(bursts[0]["start_s"] - 0.5) < 0.001
    assert abs(bursts[0]["end_s"] - 0.6) < 0.001
    assert abs(bursts[1]["duration_s"] - 0.001) < 0.0005


def test_detect_bursts_file_reports_short_pulses_at_their_length(tmp_path):
    rate = 2_400_000
    path = tmp_path / "c.cs8"
    x = _noise(rate // 10)
    x[100_000 : 100_000 + 288] += 0.8  # 120 us, a long Mode S reply
    _write_iq_file(path, x)
    bursts, _, _ = iq.detect_bursts_file(path, rate, threshold_db=10, min_duration_s=0.00005)
    assert len(bursts) == 1
    assert abs(bursts[0]["duration_s"] - 120e-6) < 30e-6


def test_detect_bursts_file_channel_finds_narrow_burst(tmp_path):
    rate = 2_000_000
    path = tmp_path / "c.cs8"
    x = _noise(rate, level=0.05)
    t = np.arange(54_000) / rate
    # A 27 ms burst at +25 kHz, far below the noise of the full 2 MHz band.
    x[600_000:654_000] += (0.02 * np.exp(2j * np.pi * 25_000 * t)).astype(np.complex64)
    _write_iq_file(path, x)
    wide, _, _ = iq.detect_bursts_file(path, rate, threshold_db=6, min_duration_s=0.005)
    narrow, _, used_rate = iq.detect_bursts_file(
        path, rate, threshold_db=6, min_duration_s=0.005, channel_offset_hz=25_000, channel_bandwidth_hz=25_000
    )
    assert wide == []
    assert used_rate < rate
    assert len(narrow) == 1
    assert abs(narrow[0]["start_s"] - 0.3) < 0.002
    assert abs(narrow[0]["peak_freq_offset_hz"] - 25_000) < 2_000


def test_spectrogram_tool_reads_whole_file_by_default(tmp_path):
    rate = 100_000
    path = tmp_path / "c.cs8"
    _write_iq_file(path, _noise(rate * 30))
    result = server.hackrf_iq_spectrogram(
        iq_file_path=str(path), sample_rate_hz=rate, output_image_path=str(tmp_path / "s.png"), fft_size=256
    )
    assert "30.00s of input" in result[0]
    assert "NOTE" not in result[0]


def test_tools_say_when_they_stop_early(tmp_path):
    rate = 100_000
    path = tmp_path / "c.cs8"
    _write_iq_file(path, _noise(rate * 3))
    result = server.hackrf_iq_info(iq_file_path=str(path), sample_rate_hz=rate, max_duration_seconds=1)
    assert "only the first 1.00s of 3.00s" in result
    bursts = server.hackrf_iq_detect_bursts(iq_file_path=str(path), sample_rate_hz=rate, max_duration_seconds=1)
    assert "only the first 1.00s of 3.00s" in bursts


def test_detect_bursts_tool_channel_needs_both_arguments(tmp_path):
    path = tmp_path / "c.cs8"
    _write_iq_file(path, _noise(1000))
    result = server.hackrf_iq_detect_bursts(iq_file_path=str(path), sample_rate_hz=100_000, channel_freq_hz=5)
    assert "must be given together" in result


@pytest.mark.parametrize("mode", ["nfm", "wfm", "am"])
def test_iq_to_wav_decimates_before_demodulating(tmp_path, mode):
    rate = 2_000_000
    path = tmp_path / "c.cs8"
    t = np.arange(rate // 2) / rate
    phase = 2 * np.pi * 50_000 * t + 3.0 * np.sin(2 * np.pi * 1_000 * t)
    _write_iq_file(path, (0.5 * np.exp(1j * phase)).astype(np.complex64))
    result = server.hackrf_iq_to_wav(
        iq_file_path=str(path),
        sample_rate_hz=rate,
        mode=mode,
        output_wav_path=str(tmp_path / "o.wav"),
        center_freq_hz=100_000_000,
        target_freq_hz=100_050_000,
    )
    assert "Wrote" in result
    assert (tmp_path / "o.wav").stat().st_size > 1000
