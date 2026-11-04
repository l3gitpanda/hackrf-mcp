"""The LoRa receiver in hackrf_mcp.lora_phy: its coding against fixed
values and three frames received from real radios, and the demodulator
against an independent transmitter (tests/lora_tx.py) with noise, frequency
and timing offsets and the clock offset that comes with them."""

import numpy as np
import pytest
from lora_tx import encode, modulate

from hackrf_mcp import iq
from hackrf_mcp import lora_phy as L

# --- Coding ----------------------------------------------------------------


def test_whitening_is_the_lfsr_sequence():
    assert list(L.whitening(10)) == [0xFF, 0xFE, 0xFC, 0xF8, 0xF0, 0xE1, 0xC2, 0x85, 0x0B, 0x17]


def test_crc16_is_ccitt_with_zero_initial_value():
    assert L.crc16(b"123456789") == 0x31C3  # the standard check value of CRC-16/XMODEM
    # LoRa's payload CRC leaves the last two bytes out and XORs them in.
    assert L.payload_crc(b"\x01\x02\x03\x04") == L.crc16(b"\x01\x02") ^ 0x0304


@pytest.mark.parametrize("cr", [1, 2, 3, 4])
def test_hamming_round_trip_and_single_errors(cr):
    for v in range(16):
        word = L.hamming_encode(v, cr)
        assert L.hamming_decode(word, cr) == (v, 0)
        for bit in range(4 + cr):
            got, err = L.hamming_decode(word ^ (1 << bit), cr)
            if cr >= 3:
                assert (got, err) == (v, 1)  # corrected
            else:
                assert err == 2  # detected only


def test_sync_words_from_symbols():
    # A nibble of 8-15 is a signed offset on the air: 0x2B at SF11 is
    # (16, -40), as the station heard Meshtastic radios send it; at SF7 it is
    # the same symbol as the unsigned 88.
    assert L.sync_symbols(0x2B, 2048) == (16, 2008)
    assert L.sync_symbols(0x2B, 128) == (16, 88)
    assert L.sync_symbols(0x34, 2048) == (24, 32)
    assert L.sync_word((16, 2008), 2048) == 0x2B
    assert L.sync_word((16, 2009), 2048) == 0x2B
    assert L.sync_word((16, 88), 2048) == 0x2B
    assert L.sync_word((15, 89), 2048) == 0x2B  # a bin either way
    assert L.sync_word((8, 16), 1024) == 0x12
    assert L.sync_word((24, 2047), 2048) == 0x30
    # What an artefact that merely repeats reads as.
    assert L.sync_word((0, 0), 1024) is None
    assert L.sync_word((1023, 1), 1024) is None
    assert L.sync_word((44, 88), 2048) is None


# Frames received from commercial LoRa transmitters, as this receiver
# demodulated them, with the payloads the dataset gives. From the EPFL
# LoRaIQ dataset (J. Tapparel et al., Zenodo record 17708397, CC BY 4.0):
# the files named (transmission_idx 15582, 1354 and 11200).
REAL_FRAMES = [
    # sigmfs/25_01_15-10_17/rrh1/10: SF7, 125 kHz, CR 4/5, 7 bytes
    (
        7,
        bytes.fromhex("30303030303132"),
        [
            109, 9, 125, 13, 25, 97, 1, 5, 2, 125, 75, 86, 57, 97, 64, 39, 109, 2, 60, 69, 83, 6, 67
        ],
    ),
    # sigmfs/25_09_17-09_30/rrh2/3: SF10, 250 kHz, CR 4/5, 19 bytes
    (
        10,
        bytes.fromhex("1300e68f136da1424740c5af4127d0421a404b"),
        [
            41, 309, 189, 93, 905, 877, 737, 81, 983, 554, 476, 304, 538, 957, 866, 619, 991, 222, 214, 828,
            763, 703, 245, 787, 225, 679, 681, 849
        ],
    ),
    # sigmfs/25_09_18-14_48/rrh1/148: SF10, 250 kHz, CR 4/5, 128 bytes
    (
        10,
        bytes.fromhex(
            "d603331421bc504247408a558330b7431a4000d603331421bc504247408a5583"
            "30b7431a4000d603331421bc504247408a558330b7431a4000d603331421bc50"
            "4247408a558330b7431a4000d603331421bc504247408a558330b7431a4000d6"
            "03331421bc504247408a558330b7431a4000d603331421bc504247408a558330"
        ),
        [
            801, 241, 225, 157, 1001, 821, 229, 181, 323, 527, 603, 750, 54, 835, 287, 982, 834, 195, 5,
            227, 792, 447, 1018, 798, 345, 610, 751, 139, 376, 973, 632, 776, 656, 445, 159, 327, 10, 606,
            184, 342, 609, 742, 716, 593, 633, 827, 895, 229, 538, 379, 656, 988, 576, 127, 517, 233, 975,
            27, 998, 827, 57, 634, 277, 881, 425, 734, 200, 727, 181, 197, 234, 926, 793, 566, 299, 148,
            224, 974, 237, 998, 218, 114, 900, 999, 482, 736, 223, 603, 689, 779, 590, 823, 446, 205, 567,
            292, 775, 386, 997, 584, 54, 988, 362, 746, 679, 246, 180, 866, 424, 213, 578, 407, 421, 650,
            520, 222, 300, 843, 600, 974, 181, 325, 45, 531, 264, 31, 299, 532, 254, 484, 958, 769, 548,
            876, 942, 226, 188, 551
        ],
    ),
]


@pytest.mark.parametrize("sf,payload,symbols", REAL_FRAMES)
def test_real_frames_decode(sf, payload, symbols):
    got = L.decode_symbols(symbols, sf, ldro=False)
    assert got is not None and got.header.checksum_ok
    assert (got.header.length, got.header.cr, got.header.has_crc) == (len(payload), 1, True)
    assert got.payload == payload
    assert got.crc_ok is True
    assert got.errors == 0


def test_a_corrupted_real_frame_fails_its_crc():
    sf, payload, symbols = REAL_FRAMES[1]
    bad = list(symbols)
    bad[20] = (bad[20] + 7) % (1 << sf)
    got = L.decode_symbols(bad, sf, ldro=False)
    assert got is not None and got.crc_ok is False


def test_encoder_and_frame_length_agree():
    for sf, cr, ldro in [(7, 1, False), (11, 1, False), (12, 4, True), (11, 4, True), (9, 2, False)]:
        for n in (1, 2, 16, 255):
            syms = encode(bytes(range(n)), sf, cr, ldro=ldro)
            assert len(syms) == L.frame_symbols(n, sf, cr, True, ldro)
            got = L.decode_symbols(syms, sf, ldro)
            assert got.payload == bytes(range(n)) and got.crc_ok


# --- Demodulation ----------------------------------------------------------

CASES = [
    # sf, bw, cr, length, cfo (Hz), SNR (dB per sample), timing (chips)
    (7, 125e3, 1, 16, 3300.0, -3.0, 2000.37),
    (9, 250e3, 1, 60, -7700.0, -8.0, 1500.81),
    (11, 250e3, 1, 237, 18000.0, -12.0, 3000.5),  # LongFast, long, with a clock offset
    (11, 500e3, 4, 60, -15000.0, -12.0, 2500.12),  # LongTurbo
    (12, 125e3, 4, 40, 4100.0, -16.0, 5000.66),  # LongSlow, LDRO
    (11, 125e3, 4, 40, -6000.0, -14.0, 4000.25),  # LongMod, LDRO
    (8, 250e3, 2, 2, 14070.0, -6.0, 1983.03),
    (10, 125e3, 3, 100, 11153.0, -10.0, 3832.02),
]


@pytest.mark.parametrize("sf,bw,cr,length,cfo,snr,lead", CASES)
def test_demodulator_finds_and_decodes(sf, bw, cr, length, cfo, snr, lead):
    rng = np.random.default_rng(sf * 100 + length)
    payload = bytes(rng.integers(0, 256, size=length, dtype=np.uint8))
    ldro = L.ldro_default(sf, bw)
    x = modulate(encode(payload, sf, cr, ldro=ldro), sf, cfo_hz=cfo, bw_hz=bw, snr_db=snr, lead=lead, seed=length)
    frames = L.Demodulator(sf, bw, 906.875e6).find(x)
    assert len(frames) == 1
    f = frames[0]
    assert f.payload == payload and f.crc_ok and f.errors == 0
    assert f.sync_word == 0x2B
    assert abs(f.cfo_hz - cfo) < 0.2 * bw / (1 << sf)
    assert abs(f.sample - (lead + 20.25 * (1 << sf))) < 2


def test_the_clock_offset_matters_for_long_frames():
    """At 20 ppm a LongFast frame of 255 bytes drifts by about 4 chips; the
    receiver follows it only when it knows the carrier frequency."""
    sf, bw, cfo = 11, 250e3, 18000.0
    payload = bytes(range(255))
    x = modulate(encode(payload, sf, 1), sf, cfo_hz=cfo, bw_hz=bw, snr_db=10.0, lead=2000.5)
    assert L.Demodulator(sf, bw, 906.875e6).find(x)[0].crc_ok
    assert not L.Demodulator(sf, bw, 0).find(x)[0].crc_ok  # 0: no clock offset assumed


def test_noise_and_a_repeating_artefact_make_no_frames():
    rng = np.random.default_rng(3)
    noise = (rng.standard_normal(2_000_000) + 1j * rng.standard_normal(2_000_000)).astype(np.complex64)
    for sf in (7, 9, 11):
        assert L.Demodulator(sf, 250e3, 906.875e6).find(noise) == []
    # Upchirps that just repeat (no sync word, no downchirps) look like a
    # preamble that never ends.
    n = 1 << 9
    chirps = np.tile(np.repeat(L.upchirp(9), 4), 60) + 0.1 * noise[: 60 * n * 4]
    assert L.Demodulator(9, 250e3, 906.875e6).find(chirps.astype(np.complex64)) == []


def test_two_frames_in_a_row_and_another_sync_word():
    sf, bw = 9, 250e3
    a, b = b"first frame", b"second, LoRaWAN-like"
    x1 = modulate(encode(a, sf, 1), sf, bw_hz=bw, cfo_hz=2000, snr_db=0, lead=1000.3, tail=2000, seed=1)
    x2 = modulate(encode(b, sf, 1), sf, bw_hz=bw, cfo_hz=2000, snr_db=0, lead=500.6, sync_word=0x34, seed=2)
    frames = L.Demodulator(sf, bw, 906.875e6).find(np.concatenate([x1, x2]))
    assert [(f.payload, f.sync_word) for f in frames] == [(a, 0x2B), (b, 0x34)]


# --- Scanning a capture ----------------------------------------------------


def _capture(tmp_path, fs, center, frames, seconds, noise=0.01):
    """An 8-bit capture holding (freq, sf, bw, cr, payload, start s, cfo)."""
    total = int(seconds * fs)
    x = np.zeros(total, np.complex64)
    rng = np.random.default_rng(7)
    for freq, sf, bw, cr, payload, start, cfo in frames:
        os = int(fs // bw)
        y = modulate(encode(payload, sf, cr, ldro=L.ldro_default(sf, bw)), sf, bw_hz=bw, cfo_hz=cfo, os=os,
                     lead=start * bw + 0.37, tail=0, amplitude=0.05, fc_hz=freq)
        y = y[:total]
        k = np.arange(y.size)
        x[: y.size] += y * np.exp(2j * np.pi * (freq - center) / fs * k).astype(np.complex64)
    x += (noise * (rng.standard_normal(total) + 1j * rng.standard_normal(total))).astype(np.complex64)
    path = tmp_path / "c.iq"
    path.write_bytes(iq.complex_to_cs8(x, 127 / 0.15))
    return path


def test_scan_finds_frames_across_segments_and_channels(tmp_path, monkeypatch):
    # Small segments, so frames land on and across their boundaries.
    monkeypatch.setattr(L._Track, "min_segment", 1 << 15)
    fs, center = 2_000_000, 906_500_000
    frames = [
        (906_875_000, 9, 250e3, 1, b"one", 0.05, 3000),
        (906_875_000, 9, 250e3, 1, b"two, a little longer", 0.21, 3000),
        (906_875_000, 7, 250e3, 1, b"three", 0.30, 3000),
        (906_125_000, 8, 250e3, 2, b"four on another channel", 0.12, -5000),
    ]
    path = _capture(tmp_path, fs, center, frames, 0.45)
    chans = [L.Channel(906_875_000, 250e3, (7, 8, 9)), L.Channel(906_125_000, 250e3, (7, 8, 9))]
    found = L.scan(iq.iter_iq_chunks(path, chunk_samples=50_000), fs, center, chans)
    got = [(f.channel.freq_hz, f.frame.sf, f.frame.payload) for f in found if f.frame.crc_ok]
    assert sorted(got) == sorted((float(fr[0]), fr[1], fr[4]) for fr in frames)
    starts = {f.frame.payload: f.seconds for f in found}
    assert abs(starts[b"one"] - (0.05 + 20.25 * 512 / 250e3)) < 0.002


def test_rate_fits():
    assert L.rate_fits(2_000_000, 250e3) and L.rate_fits(2_000_000, 500e3) and L.rate_fits(8_000_000, 125e3)
    assert not L.rate_fits(2_400_000, 250e3)
