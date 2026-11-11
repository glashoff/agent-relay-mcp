# Voice service for agent-relay-mcp. Built and run by ./agent-relay-voice on
# the host (see README.md). CPU only: no CUDA, CPU builds of PyTorch and
# whisper.cpp.

ARG PYTHON_IMAGE=python:3.13-slim-trixie

# --- whisper.cpp, CPU build, pinned ------------------------------------------
FROM ${PYTHON_IMAGE} AS whispercpp
ARG WHISPERCPP_TAG=v1.9.5
RUN apt-get update \
 && apt-get install -y --no-install-recommends git cmake g++ make ca-certificates \
 && rm -rf /var/lib/apt/lists/*
# Static binary, tuned for the CPU it is built on (GGML_NATIVE): the image is
# built on the machine it runs on.
RUN git clone --depth 1 --branch "${WHISPERCPP_TAG}" https://github.com/ggml-org/whisper.cpp /src \
 && cmake -S /src -B /build -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF \
      -DGGML_VULKAN=OFF -DWHISPER_BUILD_TESTS=OFF -DWHISPER_SDL2=OFF -DWHISPER_BUILD_EXAMPLES=ON \
 && cmake --build /build -j "$(nproc)" --target whisper-server \
 && install -m 755 /build/bin/whisper-server /whisper-server

# --- Claude Code, pinned, with the official installer --------------------------
FROM ${PYTHON_IMAGE} AS claude
ARG CLAUDE_CODE_VERSION=2.1.289
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://claude.ai/install.sh -o /tmp/install.sh \
 && HOME=/opt/cc bash /tmp/install.sh "${CLAUDE_CODE_VERSION}" \
 && cp -L /opt/cc/.local/bin/claude /claude \
 && /claude --version | grep -q "^${CLAUDE_CODE_VERSION} "

# --- the service -----------------------------------------------------------------
FROM ${PYTHON_IMAGE}
# parec/pacat/pactl for the pulse socket, socat for the relay forwarder,
# iptables + setpriv (util-linux) for the entrypoint.
RUN apt-get update \
 && apt-get install -y --no-install-recommends pulseaudio-utils socat iptables ca-certificates libgomp1 \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /opt/voice/requirements.txt
RUN pip install --no-cache-dir -r /opt/voice/requirements.txt \
 && python3 -c "import faster_whisper, piper, pocket_tts, onnxruntime, torch; print('torch', torch.__version__)"

COPY --from=whispercpp /whisper-server /usr/local/bin/whisper-server
COPY --from=claude /claude /usr/local/bin/claude

# The service user ("relay-voice"; Debian already has a group "voice") has
# the host user's ids, so the mounted sockets (mode 600, owned by the host
# user) work. Its home is a tmpfs at run time.
ARG VOICE_UID=1000
ARG VOICE_GID=1000
RUN groupadd -g "${VOICE_GID}" relay-voice \
 && useradd -u "${VOICE_UID}" -g relay-voice -d /home/voice -M -s /usr/sbin/nologin relay-voice \
 && mkdir -p /data /config /home/voice \
 && chown relay-voice:relay-voice /data /config /home/voice

COPY agent_relay_voice /opt/voice/agent_relay_voice
COPY tests /opt/voice/tests

ENV PYTHONPATH=/opt/voice \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/voice \
    HF_HOME=/data/huggingface \
    XDG_CACHE_HOME=/data/cache \
    HF_HUB_DISABLE_TELEMETRY=1 \
    VOICE_CONFIG=/config \
    VOICE_UID=${VOICE_UID} \
    VOICE_GID=${VOICE_GID} \
    PULSE_SERVER=unix:/run/pulse/native \
    DISABLE_AUTOUPDATER=1
WORKDIR /opt/voice
# Firewall as root, then setpriv to the voice user without capabilities.
ENTRYPOINT ["python3", "-m", "agent_relay_voice.firewall"]
CMD ["python3", "-m", "agent_relay_voice", "serve"]
