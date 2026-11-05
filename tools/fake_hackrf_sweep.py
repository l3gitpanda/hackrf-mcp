"""A stand-in for `hackrf_sweep` for the demo and tests: its CSV on stdout
for -f LOW:HIGH (MHz), -w BIN_WIDTH, -1 or -N PASSES, with its line order
(two tunings per 20 MHz step, two 5 MHz lines each) and its stderr. The
"air" is a noise floor with steady carriers, a few that come and go from
pass to pass, and one that is on only from 13:00 to 22:00 UTC, so an
occupancy heat map built from it has something to show.

    fake_hackrf_sweep.py -f 24:1700 -w 100000 -l 24 -g 40 -N 20
"""

import sys
import time

import numpy as np

RATE = 20_000_000
STEP = 20_000_000
LINE = 5_000_000

# (centre MHz, width kHz, level dB): always on.
STEADY = [(88.5 + 0.8 * i, 180, -20 - 3 * (i % 5)) for i in range(24)] + [
    (162.4, 25, -45),
    (162.55, 25, -52),
    (472.0, 6000, -48),
    (881.5, 10000, -40),
    (1090.0, 1500, -70),
]
# (centre MHz, width kHz, level dB, chance per pass): on in some passes.
BURSTY = [(152.0, 25, -38, 0.3), (931.9, 25, -30, 0.2), (1090.0, 1500, -35, 0.5), (433.92, 200, -50, 0.1)]
# On from 13:00 to 22:00 UTC.
DAYTIME = [(460.1, 12.5, -42)]


def fft_size(width: int) -> int:
    n = max(4, RATE // width)
    while (n + 4) % 8:
        n += 1
    return n


def line_db(lo_hz: int, n: int, rng, hour: int) -> np.ndarray:
    per = n // 4
    bw = RATE / n
    f = lo_hz + (np.arange(per) + 0.5) * bw
    noise = -78 + 6 * (f / 6e9) + rng.normal(0, 2.5, per)
    p = np.power(10, noise / 10)
    on = [(c, w, lv) for c, w, lv in STEADY]
    on += [(c, w, lv) for c, w, lv, chance in BURSTY if rng.random() < chance]
    if 13 <= hour < 22:
        on += DAYTIME
    for c, w, lv in on:
        half = max(w * 500.0, bw / 2)
        hit = np.abs(f - c * 1e6) <= half
        p[hit] += np.power(10, (lv + rng.normal(0, 1.0, hit.sum())) / 10)
    return 10 * np.log10(p)


def main(argv: list[str]) -> int:
    args = dict(zip(argv[::2], argv[1::2]))
    lo_mhz, hi_mhz = (int(x) for x in args.get("-f", "0:6000").split(":"))
    n = fft_size(int(args.get("-w", "1000000")))
    passes = 1 if "-1" in argv else int(args.get("-N", "0") or 0) or 1
    steps = 1 + (hi_mhz - lo_mhz - 1) // 20
    lo = lo_mhz * 1_000_000
    sys.stderr.write(f"call hackrf_sample_rate_set(20.000 MHz)\nSweeping from {lo_mhz} MHz to {lo_mhz + steps * 20} MHz\n")
    rng = np.random.default_rng()
    t0 = time.monotonic()
    out = sys.stdout
    for _ in range(passes):
        now = time.gmtime()
        stamp = time.strftime("%Y-%m-%d, %H:%M:%S", now) + ".000000"
        for s in range(steps):
            f = lo + s * STEP
            for line_lo in (f, f + 2 * LINE, f + LINE, f + 3 * LINE):
                db = line_db(line_lo, n, rng, now.tm_hour)
                out.write(f"{stamp}, {line_lo}, {line_lo + LINE}, {RATE / n:.2f}, {n}, " + ", ".join(f"{v:.2f}" for v in db) + "\n")
        time.sleep(0.02)
    out.flush()
    took = time.monotonic() - t0
    sys.stderr.write(f"Exiting...\nTotal sweeps: {passes} in {took:.5f} seconds ({passes / max(took, 1e-9):.2f} sweeps/second)\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
