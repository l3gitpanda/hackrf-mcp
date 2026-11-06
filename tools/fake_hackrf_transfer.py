"""A stand-in for `hackrf_transfer -r` for the demo and tests: noise and a
few carriers at the requested rate, written in real time to stdout (-r -)
or a file, with hackrf_transfer's once-a-second power line on stderr. Stops
after -n samples if given, else on SIGINT, as the real one does.

At rates up to 2.4 MS/s there is also something to listen to: an AM call
AM_OFFSET_HZ from the centre (AM_TONE_HZ at 80%, on air 2 s of every 5),
and a steady NFM tone NFM_OFFSET_HZ from it (NFM_TONE_HZ, 3 kHz deviation).
Every carrier and tone makes whole cycles in a quarter second, so the
quarter-second blocks join without a seam.

    fake_hackrf_transfer.py -r - -f 98000000 -s 10000000 -l 32 -g 40 -a 0
    fake_hackrf_transfer.py -r capture.cs8 -n 4000000 -f 152000000 -s 2000000
"""

import signal
import sys
import time

import numpy as np


AM_OFFSET_HZ = -25_000
AM_TONE_HZ = 1_000
NFM_OFFSET_HZ = 300_000
NFM_TONE_HZ = 1_500
TONES_UP_TO_HZ = 2_400_000
_blocks: dict = {}


def _int8(x: np.ndarray) -> bytes:
    out = np.empty(2 * x.size, dtype=np.int8)
    out[0::2] = np.clip(np.rint(x.real * 127), -127, 127)
    out[1::2] = np.clip(np.rint(x.imag * 127), -127, 127)
    return out.tobytes()


def _quarter(rate: int, center_hz: int) -> tuple[bytes, bytes]:
    """A quarter second of the air, without and with the AM call."""
    key = (rate, center_hz)
    if key not in _blocks:
        rng = np.random.default_rng(center_hz % 2**32)
        n = rate // 4
        t = np.arange(n, dtype=np.float64) / rate
        x = (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64) * 0.02
        for off, amp in ((-0.31, 0.05), (0.12, 0.08), (0.27, 0.04)):
            x += (amp * np.exp(2j * np.pi * off * rate * t)).astype(np.complex64)
        for _ in range(3):  # bursts
            a = int(rng.integers(0, n - rate // 40))
            x[a : a + rate // 200] += (0.3 * np.exp(2j * np.pi * -0.05 * rate * t[a : a + rate // 200])).astype(np.complex64)
        on = x
        if rate <= TONES_UP_TO_HZ:
            x = x + (0.03 * np.exp(1j * (2 * np.pi * NFM_OFFSET_HZ * t + 2.0 * np.sin(2 * np.pi * NFM_TONE_HZ * t)))).astype(np.complex64)
            am = 0.05 * (1 + 0.8 * np.sin(2 * np.pi * AM_TONE_HZ * t)) * np.exp(2j * np.pi * AM_OFFSET_HZ * t)
            on = x + am.astype(np.complex64)
        _blocks[key] = (_int8(x), _int8(on))
    return _blocks[key]


def block(rate: int, center_hz: int, start: int, n: int) -> bytes:
    """n samples of the air from sample `start` on, as int8 I/Q bytes."""
    quiet, call = _quarter(rate, center_hz)
    q = rate // 4
    out = []
    while n > 0:
        k, at = divmod(start, q)
        take = min(n, q - at)
        on = 1.0 <= (k * 0.25) % 5.0 < 3.0
        out.append((call if on else quiet)[2 * at : 2 * (at + take)])
        start += take
        n -= take
    return b"".join(out)


def main(argv: list[str]) -> int:
    signal.signal(signal.SIGINT, lambda *a: sys.exit(0))
    args = dict(zip(argv[::2], argv[1::2]))
    rate = int(float(args.get("-s", "2000000")))
    center = int(float(args.get("-f", "0")))
    n = rate // 4  # a quarter second at a time
    left = 2 * int(float(args["-n"])) if "-n" in args else None
    target = args.get("-r", "-")
    sink = sys.stdout.buffer if target == "-" else open(target, "wb")
    start = time.monotonic()
    sent = 0
    last = start
    while left is None or left > 0:
        chunk = block(rate, center, sent * n, n)
        chunk = chunk if left is None else chunk[:left]
        sink.write(chunk)
        sink.flush()
        if left is not None:
            left -= len(chunk)
        sent += 1
        now = time.monotonic()
        if now - last >= 1:
            last = now
            sys.stderr.write(f"{2 * rate / 2**20:.1f} MiB / 1.000 sec = {2 * rate / 2**20:.1f} MiB/second, average power -30.0 dBfs, 0 bytes free in buffer, 0 underruns\n")
            sys.stderr.flush()
        ahead = start + sent * 0.25 - now
        if ahead > 0 and (left is None or left > 0):
            time.sleep(ahead)
    if sink is not sys.stdout.buffer:
        sink.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
