"""A stand-in for `hackrf_transfer -r -` for the demo and tests: noise and a
few carriers at the requested rate, written to stdout in real time, with
hackrf_transfer's once-a-second power line on stderr. Exits on SIGINT, as
the real one does.

    fake_hackrf_transfer.py -r - -f 98000000 -s 10000000 -l 32 -g 40 -a 0
"""

import signal
import sys
import time

import numpy as np


def main(argv: list[str]) -> int:
    signal.signal(signal.SIGINT, lambda *a: sys.exit(0))
    args = dict(zip(argv[::2], argv[1::2]))
    rate = int(float(args.get("-s", "2000000")))
    rng = np.random.default_rng(int(float(args.get("-f", "0"))) % 2**32)
    n = rate // 4  # a quarter second, looped
    t = np.arange(n, dtype=np.float32) / rate
    x = (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64) * 0.02
    for off, amp in ((-0.31, 0.05), (0.12, 0.08), (0.27, 0.04)):
        x += (amp * np.exp(2j * np.pi * off * rate * t)).astype(np.complex64)
    for _ in range(3):  # bursts
        a = int(rng.integers(0, n - rate // 40))
        x[a : a + rate // 200] += (0.3 * np.exp(2j * np.pi * -0.05 * rate * t[a : a + rate // 200])).astype(np.complex64)
    out = np.empty(2 * n, dtype=np.int8)
    out[0::2] = np.clip(np.rint(x.real * 127), -127, 127)
    out[1::2] = np.clip(np.rint(x.imag * 127), -127, 127)
    block = out.tobytes()
    start = time.monotonic()
    sent = 0
    last = start
    while True:
        sys.stdout.buffer.write(block)
        sys.stdout.buffer.flush()
        sent += 1
        now = time.monotonic()
        if now - last >= 1:
            last = now
            sys.stderr.write(f"{2 * rate / 2**20:.1f} MiB / 1.000 sec = {2 * rate / 2**20:.1f} MiB/second, average power -30.0 dBfs, 0 bytes free in buffer, 0 underruns\n")
            sys.stderr.flush()
        ahead = start + sent * 0.25 - now
        if ahead > 0:
            time.sleep(ahead)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
