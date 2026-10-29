#!/bin/bash
# Build the hackrf_decode programs Ubuntu 24.04 doesn't package:
#   redsea      (rds)  https://github.com/windytan/redsea
#   dump978-fa  (uat)  https://github.com/flightaware/dump978
# Run as root on the server: sudo deploy/build-decoders.sh [build dir]
# The other decoders (dump1090-mutability, AIS-catcher, acarsdec, dumpvdl2,
# multimon-ng, rtl_433) come from apt or their projects' releases.
set -euo pipefail

REDSEA_TAG=v1.3.1
DUMP978_TAG=v11.1
BUILD_DIR=${1:-/opt/hackrf-build}

export DEBIAN_FRONTEND=noninteractive
apt-get install -y --no-install-recommends \
  build-essential pkg-config git meson ninja-build \
  libliquid-dev nlohmann-json3-dev libsndfile1-dev \
  libboost-system-dev libboost-program-options-dev libboost-regex-dev \
  libboost-filesystem-dev libsoapysdr-dev

mkdir -p "$BUILD_DIR"
cd "$BUILD_DIR"

rm -rf redsea
git clone --depth 1 --branch "$REDSEA_TAG" https://github.com/windytan/redsea.git
(cd redsea && meson setup build --buildtype=release && meson compile -C build && meson install -C build)

rm -rf dump978
git clone --depth 1 --branch "$DUMP978_TAG" https://github.com/flightaware/dump978.git
(cd dump978 && make dump978-fa && install -m 0755 dump978-fa /usr/local/bin/dump978-fa)

ldconfig
redsea --version
dump978-fa --version 2>&1 | head -1 || true
echo "build-decoders: done"
