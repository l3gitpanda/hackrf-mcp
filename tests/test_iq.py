import numpy as np
import pytest
from scipy.io import wavfile

from hackrf_mcp import iq, server


def _write_iq_file(path, samples: np.ndarray) -> None:
    i = np.clip(samples.real * 127, -127, 127).astype(np.int8)
    q = np.clip(samples.imag * 127, -127, 127).astype(np.int8)
    interleaved = np.empty(2 * samples.size, dtype=np.int8)
    interleaved[0::2] = i
    interleaved[1::2] = q
    interleaved.tofile(path)


def _nfm_capture(sample_rate_hz, duration_s, tone_offset_hz, audio_freq_hz=1000, deviation_hz=3000):
    n = int(sample_rate_hz * duration_s)
    t = np.arange(n) / sample_rate_hz
    phase = 2 * np.pi * tone_offset_hz * t + (deviation_hz / audio_freq_hz) * np.sin(2 * np.pi * audio_freq_hz * t)
    return 0.5 * np.exp(1j * phase)


def test_read_iq_int8_roundtrip(tmp_path):
    samples = np.array([0.5 + 0.25j, -0.5 - 0.25j, 0.0 + 0j], dtype=np.complex64)
    path = tmp_path / "samples.cs8"
    _write_iq_file(path, samples)

    out = iq.read_iq_int8(path)

    assert out.size == 3
    np.testing.assert_allclose(out, samples, atol=1 / 64)


def test_read_iq_int8_respects_max_samples(tmp_path):
    samples = np.ones(100, dtype=np.complex64)
    path = tmp_path / "samples.cs8"
    _write_iq_file(path, samples)

    out = iq.read_iq_int8(path, max_samples=10)

    assert out.size == 10


def test_mix_zero_shift_is_identity():
    samples = np.array([1 + 1j, -1 - 1j], dtype=np.complex64)
    out = iq.mix(samples, sample_rate_hz=1000, shift_hz=0)
    np.testing.assert_array_equal(out, samples)


def test_demodulate_nfm_recovers_tone():
    sample_rate_hz = 240_000
    audio_freq_hz = 1000
    samples = _nfm_capture(sample_rate_hz, duration_s=0.5, tone_offset_hz=0, audio_freq_hz=audio_freq_hz)

    audio = iq.demodulate(samples, sample_rate_hz, "nfm")

    fft = np.abs(np.fft.rfft(audio))
    freqs = np.fft.rfftfreq(audio.size, 1 / sample_rate_hz)
    peak_freq = freqs[np.argmax(fft)]

    assert abs(peak_freq - audio_freq_hz) < 10


def test_demodulate_am_recovers_envelope():
    sample_rate_hz = 240_000
    n = int(sample_rate_hz * 0.5)
    t = np.arange(n) / sample_rate_hz
    audio_freq_hz = 1000
    envelope = 0.5 + 0.5 * np.sin(2 * np.pi * audio_freq_hz * t)
    samples = envelope.astype(np.complex64)

    audio = iq.demodulate(samples, sample_rate_hz, "am")

    fft = np.abs(np.fft.rfft(audio))
    freqs = np.fft.rfftfreq(audio.size, 1 / sample_rate_hz)
    peak_freq = freqs[np.argmax(fft)]

    assert abs(peak_freq - audio_freq_hz) < 10


def test_resample_to_rate_changes_length():
    audio = np.zeros(1000)
    out = iq.resample_to_rate(audio, in_rate_hz=240_000, out_rate_hz=48_000)
    assert out.size == pytest.approx(200, abs=1)


def test_resample_to_rate_noop_when_equal():
    audio = np.arange(10, dtype=float)
    out = iq.resample_to_rate(audio, in_rate_hz=48_000, out_rate_hz=48_000)
    np.testing.assert_array_equal(out, audio)


def test_write_wav_writes_int16_pcm(tmp_path):
    audio = np.sin(np.linspace(0, 2 * np.pi, 1000))
    path = tmp_path / "out.wav"

    iq.write_wav(path, audio, 48_000)

    rate, data = wavfile.read(path)
    assert rate == 48_000
    assert data.dtype == np.int16
    assert data.size == 1000


def test_compute_spectrogram_finds_tone_frequency():
    sample_rate_hz = 240_000
    tone_offset_hz = 30_000
    n = sample_rate_hz  # 1 second
    t = np.arange(n) / sample_rate_hz
    samples = (0.5 * np.exp(2j * np.pi * tone_offset_hz * t)).astype(np.complex64)

    freqs_hz, times_s, power_db = iq.compute_spectrogram(samples, sample_rate_hz, fft_size=1024)

    peak_freq = freqs_hz[np.argmax(power_db.mean(axis=1))]
    assert abs(peak_freq - tone_offset_hz) < (sample_rate_hz / 1024) * 2
    assert times_s.size > 0


def test_compute_spectrum_finds_tone_frequency():
    sample_rate_hz = 240_000
    tone_offset_hz = 30_000
    n = sample_rate_hz
    t = np.arange(n) / sample_rate_hz
    samples = (0.5 * np.exp(2j * np.pi * tone_offset_hz * t)).astype(np.complex64)

    freqs_hz, power_db = iq.compute_spectrum(samples, sample_rate_hz, fft_size=1024)

    peak_freq = freqs_hz[np.argmax(power_db)]
    assert abs(peak_freq - tone_offset_hz) < (sample_rate_hz / 1024) * 2


def test_occupied_bandwidth_finds_tone_span():
    sample_rate_hz = 240_000
    tone_offset_hz = 30_000
    n = sample_rate_hz
    t = np.arange(n) / sample_rate_hz
    samples = (0.5 * np.exp(2j * np.pi * tone_offset_hz * t)).astype(np.complex64)

    freqs_hz, power_db = iq.compute_spectrum(samples, sample_rate_hz, fft_size=1024)
    low_hz, high_hz, bw_hz = iq.occupied_bandwidth(freqs_hz, power_db, threshold_db=6.0)

    assert low_hz <= tone_offset_hz <= high_hz
    assert bw_hz >= 0


def test_parse_sweep_csv():
    data = (
        "2024-01-01, 00:00:00, 0, 2000000, 1000000, 1, -50.0, -40.0\n"
        "2024-01-01, 00:00:00, 2000000, 4000000, 1000000, 1, -30.0, -20.0\n"
    )

    freqs_hz, power_db = iq.parse_sweep_csv(data)

    assert freqs_hz.size == 4
    assert np.all(np.diff(freqs_hz) > 0)
    np.testing.assert_allclose(power_db, [-50.0, -40.0, -30.0, -20.0])


def test_parse_sweep_csv_ignores_malformed_lines():
    data = "not,a,valid,row\n2024-01-01, 00:00:00, 0, 1000000, 1000000, 1, -10.0\n"

    freqs_hz, power_db = iq.parse_sweep_csv(data)

    assert freqs_hz.size == 1
    np.testing.assert_allclose(power_db, [-10.0])


def test_render_sweep_png(tmp_path):
    freqs_hz = np.array([1e6, 2e6, 3e6])
    power_db = np.array([-50.0, -10.0, -50.0])
    path = tmp_path / "sweep.png"

    iq.render_sweep_png(path, freqs_hz, power_db, title="test")

    assert path.exists()
    assert path.stat().st_size > 0


def test_detect_bursts_finds_pulse():
    sample_rate_hz = 100_000
    n = sample_rate_hz  # 1 second
    samples = (0.01 * (np.random.randn(n) + 1j * np.random.randn(n))).astype(np.complex64)

    # Inject a strong burst from 0.5s to 0.6s
    burst_start = int(0.5 * sample_rate_hz)
    burst_end = int(0.6 * sample_rate_hz)
    samples[burst_start:burst_end] += 1.0

    bursts = iq.detect_bursts(samples, sample_rate_hz, threshold_db=10.0)

    assert len(bursts) == 1
    burst = bursts[0]
    assert abs(burst["start_s"] - 0.5) < 0.01
    assert abs(burst["end_s"] - 0.6) < 0.01
    assert burst["duration_s"] > 0


def test_detect_bursts_empty_input():
    bursts = iq.detect_bursts(np.array([], dtype=np.complex64), 100_000)
    assert bursts == []


def test_detect_bursts_no_bursts_in_flat_signal():
    sample_rate_hz = 100_000
    n = sample_rate_hz
    samples = (0.1 * np.ones(n)).astype(np.complex64)

    bursts = iq.detect_bursts(samples, sample_rate_hz, threshold_db=10.0)

    assert bursts == []


def test_hackrf_iq_to_wav_tool(tmp_path):
    sample_rate_hz = 240_000
    samples = _nfm_capture(sample_rate_hz, duration_s=0.5, tone_offset_hz=20_000)
    iq_path = tmp_path / "capture.cs8"
    wav_path = tmp_path / "out.wav"
    _write_iq_file(iq_path, samples)

    result = server.hackrf_iq_to_wav(
        iq_file_path=str(iq_path),
        sample_rate_hz=sample_rate_hz,
        mode="nfm",
        output_wav_path=str(wav_path),
        center_freq_hz=100_000_000,
        target_freq_hz=100_020_000,
    )

    assert "Wrote" in result
    assert wav_path.exists()
    rate, data = wavfile.read(wav_path)
    assert rate == 48_000
    assert data.size > 0


def test_hackrf_iq_to_wav_empty_file(tmp_path):
    iq_path = tmp_path / "empty.cs8"
    iq_path.write_bytes(b"")

    result = server.hackrf_iq_to_wav(
        iq_file_path=str(iq_path),
        sample_rate_hz=240_000,
        mode="nfm",
        output_wav_path=str(tmp_path / "out.wav"),
    )

    assert "no IQ samples" in result


def test_hackrf_iq_spectrogram_tool(tmp_path):
    sample_rate_hz = 240_000
    n = sample_rate_hz
    t = np.arange(n) / sample_rate_hz
    samples = (0.5 * np.exp(2j * np.pi * 20_000 * t)).astype(np.complex64)
    iq_path = tmp_path / "capture.cs8"
    img_path = tmp_path / "spectrogram.png"
    _write_iq_file(iq_path, samples)

    result = server.hackrf_iq_spectrogram(
        iq_file_path=str(iq_path),
        sample_rate_hz=sample_rate_hz,
        output_image_path=str(img_path),
        center_freq_hz=100_000_000,
    )

    assert len(result) == 2
    assert "Wrote" in result[0]
    assert "100.0" in result[0]
    assert img_path.exists()
    assert img_path.stat().st_size > 0


def test_hackrf_iq_spectrogram_empty_file(tmp_path):
    iq_path = tmp_path / "empty.cs8"
    iq_path.write_bytes(b"")

    result = server.hackrf_iq_spectrogram(
        iq_file_path=str(iq_path),
        sample_rate_hz=240_000,
        output_image_path=str(tmp_path / "out.png"),
    )

    assert "no IQ samples" in result[0]


def test_hackrf_iq_to_wav_return_audio(tmp_path):
    from fastmcp.utilities.types import Audio

    sample_rate_hz = 240_000
    samples = _nfm_capture(sample_rate_hz, duration_s=0.5, tone_offset_hz=20_000)
    iq_path = tmp_path / "capture.cs8"
    wav_path = tmp_path / "out.wav"
    _write_iq_file(iq_path, samples)

    result = server.hackrf_iq_to_wav(
        iq_file_path=str(iq_path),
        sample_rate_hz=sample_rate_hz,
        mode="nfm",
        output_wav_path=str(wav_path),
        center_freq_hz=100_000_000,
        target_freq_hz=100_020_000,
        return_audio=True,
    )

    assert len(result) == 2
    assert "Wrote" in result[0]
    assert isinstance(result[1], Audio)


def test_hackrf_iq_info(tmp_path):
    sample_rate_hz = 240_000
    n = sample_rate_hz
    t = np.arange(n) / sample_rate_hz
    samples = (0.5 * np.exp(2j * np.pi * 30_000 * t)).astype(np.complex64)
    iq_path = tmp_path / "capture.cs8"
    _write_iq_file(iq_path, samples)

    result = server.hackrf_iq_info(
        iq_file_path=str(iq_path),
        sample_rate_hz=sample_rate_hz,
        center_freq_hz=100_000_000,
    )

    assert "100.0" in result
    assert "Occupied bandwidth" in result


def test_hackrf_iq_info_empty_file(tmp_path):
    iq_path = tmp_path / "empty.cs8"
    iq_path.write_bytes(b"")

    result = server.hackrf_iq_info(iq_file_path=str(iq_path), sample_rate_hz=240_000)

    assert "no IQ samples" in result


def test_hackrf_iq_detect_bursts(tmp_path):
    sample_rate_hz = 100_000
    n = sample_rate_hz
    samples = (0.01 * (np.random.randn(n) + 1j * np.random.randn(n))).astype(np.complex64)
    samples[int(0.5 * sample_rate_hz) : int(0.6 * sample_rate_hz)] += 1.0

    iq_path = tmp_path / "capture.cs8"
    _write_iq_file(iq_path, samples)

    result = server.hackrf_iq_detect_bursts(
        iq_file_path=str(iq_path),
        sample_rate_hz=sample_rate_hz,
        center_freq_hz=100_000_000,
        threshold_db=10.0,
    )

    assert "Found 1 burst" in result
    assert "MHz" in result


def test_hackrf_iq_detect_bursts_none_found(tmp_path):
    sample_rate_hz = 100_000
    n = sample_rate_hz
    samples = (0.1 * np.ones(n)).astype(np.complex64)

    iq_path = tmp_path / "capture.cs8"
    _write_iq_file(iq_path, samples)

    result = server.hackrf_iq_detect_bursts(
        iq_file_path=str(iq_path),
        sample_rate_hz=sample_rate_hz,
    )

    assert "No bursts found" in result


def test_hackrf_iq_detect_bursts_empty_file(tmp_path):
    iq_path = tmp_path / "empty.cs8"
    iq_path.write_bytes(b"")

    result = server.hackrf_iq_detect_bursts(iq_file_path=str(iq_path), sample_rate_hz=100_000)

    assert "no IQ samples" in result
