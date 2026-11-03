# syntax=docker/dockerfile:1
# The split station's worker (TODO.md, milestone 13): the live service, the
# web UI and the MCP server, and the decoders they run. The HackRF is on
# another machine behind hackrf-agent (HACKRF_LIVE_AGENT); nothing in this
# image opens a radio.
#
# The decoders are the versions the single-box station ran: dump1090-mutability
# and multimon-ng from Ubuntu 24.04, and from source AIS-catcher 0.66,
# acarsdec 3.7 and dumpvdl2 2.6.0 with libacars 2.2. Their flags and output
# formats are what the protocol modules parse, so change versions with care.
#
#   docker build -t hackrf-mcp .
#   (deploy/worker/compose.yaml runs it as three services)

ARG UBUNTU=24.04

# --- decoders built from source ---------------------------------------------
FROM ubuntu:${UBUNTU} AS decoders
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates git build-essential cmake pkg-config \
      libglib2.0-dev libxml2-dev zlib1g-dev libjansson-dev libsndfile1-dev \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
ARG LIBACARS=v2.2.1
ARG DUMPVDL2=v2.6.0
ARG ACARSDEC=acarsdec-3.7
ARG AISCATCHER=v0.66
RUN git clone --depth 1 --branch "${LIBACARS}" https://github.com/szpajder/libacars.git \
 && cmake -S libacars -B libacars/build -DCMAKE_BUILD_TYPE=Release \
 && cmake --build libacars/build -j"$(nproc)" \
 && cmake --install libacars/build \
 && ldconfig
# No SDR libraries are installed, so dumpvdl2, acarsdec and AIS-catcher are
# built for file and stdin input only, which is how the live service feeds them.
RUN git clone --depth 1 --branch "${DUMPVDL2}" https://github.com/szpajder/dumpvdl2.git \
 && cmake -S dumpvdl2 -B dumpvdl2/build -DCMAKE_BUILD_TYPE=Release \
 && cmake --build dumpvdl2/build -j"$(nproc)" \
 && install -m 0755 dumpvdl2/build/src/dumpvdl2 /usr/local/bin/dumpvdl2
RUN git clone --depth 1 --branch "${ACARSDEC}" https://github.com/TLeconte/acarsdec.git \
 && cmake -S acarsdec -B acarsdec/build -DCMAKE_BUILD_TYPE=Release \
 && cmake --build acarsdec/build -j"$(nproc)" \
 && install -m 0755 acarsdec/build/acarsdec /usr/local/bin/acarsdec
RUN git clone --depth 1 --branch "${AISCATCHER}" https://github.com/jvde-github/AIS-catcher.git \
 && cmake -S AIS-catcher -B AIS-catcher/build -DCMAKE_BUILD_TYPE=Release \
 && cmake --build AIS-catcher/build -j"$(nproc)" \
 && install -m 0755 AIS-catcher/build/AIS-catcher /usr/local/bin/AIS-catcher

# --- the worker ----------------------------------------------------------------
FROM ubuntu:${UBUNTU}
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates tini python3 \
      dump1090-mutability multimon-ng rtl-433 \
      libglib2.0-0t64 libxml2 zlib1g libjansson4 libsndfile1 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=decoders /usr/local/lib/ /usr/local/lib/
COPY --from=decoders /usr/local/bin/dumpvdl2 /usr/local/bin/acarsdec /usr/local/bin/AIS-catcher /usr/local/bin/
RUN ldconfig

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv
WORKDIR /opt/hackrf-mcp
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN UV_PYTHON_DOWNLOADS=never UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy \
    uv sync --frozen --no-dev --no-cache
ENV PATH=/opt/hackrf-mcp/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    FASTMCP_CHECK_FOR_UPDATES=off \
    FASTMCP_SHOW_SERVER_BANNER=false \
    MPLCONFIGDIR=/tmp/matplotlib

# The image's own user (uid 1000 is Ubuntu's "ubuntu"; give it to the worker).
RUN userdel -r ubuntu 2>/dev/null; useradd -u 1000 -m -s /usr/sbin/nologin hackrf \
 && mkdir -p /data && chown hackrf:hackrf /data
USER hackrf
VOLUME /data
ENV HACKRF_LIVE_DIR=/data/live \
    HACKRF_MCP_DATA_DIR=/data/captures \
    HACKRF_MCP_TX_LOG=/data/tx-audit.jsonl \
    HACKRF_MCP_TX_MODE=off

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "hackrf_mcp.live_service"]
