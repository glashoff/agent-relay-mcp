# Voice service

Talk to the relay by voice on the laptop: speech recognition, a Claude Code
instance as the conversation partner, and speech output, all running locally
in a Docker container. Design and decisions: `../docs/voice-plan.md`.

```
headset ─ PipeWire ─ pulse socket ─> capture ─> mute detection ─> VAD ─> Whisper (CPU)
                                                                           │
relay ─ mcp.sock ─> socat :8765 <── Claude Code "voice" (relay MCP, WebSearch, WebFetch)
      ─ events.sock ─> notices                                             │
GNOME extension ─ control.sock ─> state, settings                          ▼
headset <─ PipeWire <─ pulse socket <── playback <── TTS (Pocket TTS / Piper), sentence by sentence
```

## Install and start

Needs Docker (the user in the `docker` group), PipeWire with its pulse
socket, and `claude` on the host (only to create the token).

```sh
cd voice
./agent-relay-voice build          # builds agent-relay-voice:latest (~2.8 GB, a few minutes)
./agent-relay-voice token          # claude setup-token, paste the token; stored with mode 600
./install.sh                       # installs the systemd user unit (does not enable it)
systemctl --user start agent-relay-voice
./agent-relay-voice status         # state through the control socket
journalctl --user -u agent-relay-voice -f
```

The first start downloads the models into the Docker volume
`agent-relay-voice-models` (large-v3-turbo ~1.6 GB, Pocket TTS 24l ~0.5 GB;
others on first use). The state is `starting` with `loading` saying what is
loading until speech recognition is ready.

To start voice at login: `systemctl --user enable agent-relay-voice`. The
GNOME extension switches it on and off by starting and stopping the unit.

The relay must serve its Unix sockets (config keys `mcp_socket` and
`events_socket`, by default in `$XDG_RUNTIME_DIR/agent-relay/`). Without them
the service runs anyway: Claude Code starts without the relay tools, notices
are retried, and the log says so. When the MCP socket appears before the first
turn, Claude Code is restarted with it; otherwise start a new conversation.

## Commands (host)

| Command | What it does |
|---|---|
| `agent-relay-voice build [--no-cache]` | build the image |
| `agent-relay-voice run` | run the service in the foreground (what the unit runs) |
| `agent-relay-voice stop` | stop the container |
| `agent-relay-voice token` | create the Claude Code token with `claude setup-token` |
| `agent-relay-voice status [--options]` | the state (and choices) from the control socket |
| `agent-relay-voice probe` | live microphone levels: RMS, share of exact zeros, mute state, VAD |
| `agent-relay-voice listen` | audio loop only: prints mute changes and transcripts |
| `agent-relay-voice cli say "Hallo"` | the container's own command line (`say`, `transcribe`, ...) |

`run`, `probe`, `listen` and `cli` take `--sink NAME` / `--source NAME` to use
another pulse sink or source than the default (sets `PULSE_SINK` /
`PULSE_SOURCE` in the container).

## Checking a headset: probe

Mute detection reads the microphone signal itself. Measured with a Jabra Link
380 (16-bit units): boom up = a constant pattern, RMS about 0.5 with 75 %
exact zeros; room noise with the boom down RMS 5-30 with fewer than 10 %
zeros; speech 400-4300; headset off = only zeros. Rule: **RMS < 1.5 and more
than 50 % zeros for 300 ms = muted**; anything else unmutes at once.

```sh
agent-relay-voice probe
#   time     rms  zeros  state   vad
#    3.1     0.5    75%  MUTED  0.01
#    5.4    14.2     2%  open   0.03
#    6.0  1830.4     0%  open   0.98 #######################################
```

Fold the boom up and down and speak; if another headset's muted signal looks
different, adjust `mute_rms`, `mute_zero_fraction`, `mute_hold_s` in the
settings.

## Settings

`~/.config/agent-relay-voice/settings/settings.json` (mounted as `/config`),
written by the service (mode 600) whenever something is changed through the
control socket. Edit it while the service is stopped; unknown keys and values
of the wrong type are ignored with a warning in the log.

| Key | Default | |
|---|---|---|
| `stt` | `large-v3-turbo` | also `large-v3-turbo-german`, `small`, `wcpp-large-v3-turbo`, `wcpp-large-v3-turbo-q8_0` |
| `tts` | `pocket-german-24l` | also `pocket-german` (6 layers), `piper-thorsten-high` |
| `claude_model` | `""` | Claude Code's default; `fable`, `opus`, `sonnet`, `haiku` |
| `listening` | `true` | paused when false |
| `vad_threshold` | 0.5 | Silero speech probability |
| `vad_silence_s` | 0.8 | silence that ends an utterance |
| `min_utterance_s` | 0.3 | shorter noises are ignored |
| `max_utterance_s` | 60 | an utterance is cut here |
| `mute_rms`, `mute_zero_fraction`, `mute_hold_s` | 1.5, 0.5, 0.3 | mute rule |
| `stt_language`, `stt_beam_size` | `de`, 5 | |
| `stt_prompt` | sentence with the usual terms | Whisper initial prompt |
| `hotwords` | Claude Code, Dev-Container, Pull Request, ... | faster-whisper hotwords (whisper.cpp has none) |
| `hotwords_from_projects` | `true` | adds the project names from the relay's `list_projects` |
| `pronunciation` | `Term=Replacement` lines | Piper only |
| `pocket_gain_db` | 12 | Pocket TTS is about 12 dB quieter |
| `pocket_voice` | `juergen` | |
| `notice_events` | `run_ended`, `request_added` | relay events that become notices (also possible: `request_expired`, `run_started`) |

## How a turn works

1. parec captures 16 kHz mono in 100 ms blocks. Mute detection runs on every
   block; when muted (or paused), an utterance in progress is dropped.
2. Silero VAD (the model shipped with faster-whisper, run window by window
   with its state kept) cuts utterances: 0.3 s preroll, ends after 0.8 s of
   silence, ignored below 0.3 s of speech.
3. Whisper transcribes the utterance with the prompt and hotwords. Known
   Whisper inventions on noise ("Untertitel der Amara.org-Community", ...)
   are dropped. "Neues Gespräch" restarts Claude Code instead.
4. The text goes to the Claude Code process as a stream-json user message.
   Text deltas are cut into sentences; each complete sentence goes to TTS and
   is played at once, so speech starts after the first sentence. Markdown,
   code blocks and URLs are stripped before speaking.
5. Barge-in: once the user has spoken for 0.3 s while output plays, playback
   stops, the rest of the answer is dropped and Claude's turn is interrupted;
   the new utterance is the next turn. Something said while Claude is still
   thinking (nothing spoken yet) waits and is sent when the turn ends.
6. Relay events (`run_ended`, `request_added`) become notices such as
   `[Ereignis: Der Auftrag in Projekt X ist fertig.]`, sent to the instance
   between turns, never while the user speaks or a turn runs; the instance
   fetches details with `get_updates` and tells the user. Without an instance
   (no token) the notice is spoken directly.

## Measured (i7-10750H, in the container, 2026-11-11)

Through the whole service (audio played into a PipeWire null sink, a fake
`claude` that answers at once), Piper sample of 6.8 s German speech with
English terms:

| | |
|---|---|
| Start with cached models | 9-13 s until listening (STT and TTS load in parallel) |
| large-v3-turbo, 7.2 s utterance | 5.0-6.9 s |
| small | 2.2-2.6 s |
| whisper.cpp large-v3-turbo q8_0 | 15.3 s |
| Pocket TTS 24l | RTF 1.25-1.4 (slower than real time; the speaker pre-buffers), first chunk 0.5-0.9 s |
| Pocket TTS 6l | RTF ~0.4, first audio ~0.2 s |
| Piper thorsten-high | RTF ~0.2, first audio ~0.5-0.7 s (a whole sentence) |
| First audio after the end of speech | 0.8 s (VAD) + STT + Claude + ~0.9 s (Pocket 24l); with turbo about 8 s plus Claude, with small about 4 s plus Claude |
| Memory | about 4.2 GB (turbo + Pocket 24l) |
| Image / models volume | 2.75 GB / 4.0 GB (turbo, small, whisper.cpp q8_0, Pocket 24l and 6l, Piper) |

Transcript (turbo and small): "Ich arbeite mit Claude Code im Dev-Container.
Schick dem Projekt Agent Relay bitte die Nachricht, dass der Pull Request
gemerkt werden soll."

Because Pocket 24l runs slower than real time here, each sentence holds back
(RTF - 1) x its expected length before playing (`speaker.py`), from a running
RTF estimate; so it plays without gaps but starts later. The 6-layer model has
no such delay.

## The Claude Code instance

One long-lived process, started with exactly these flags
(`agent_relay_voice/claude.py`, `build_command`, tested):

```
claude -p --input-format stream-json --output-format stream-json
  --include-partial-messages --verbose
  --tools WebSearch,WebFetch
  --strict-mcp-config --mcp-config /tmp/agent-relay-voice/mcp.json   # only {"relay": http://localhost:8765/mcp}
  --setting-sources ""                                               # no user/project/local settings
  --allowedTools mcp__relay,WebSearch,WebFetch
  --permission-mode dontAsk                                          # anything else is denied, never asked
  --append-system-prompt <voice prompt>
  [--model <model>]
```

- Environment: only `PATH`, `HOME` (a tmpfs), `LANG`,
  `CLAUDE_CODE_OAUTH_TOKEN` (read from the mounted token file),
  `ENABLE_CLAUDEAI_MCP_SERVERS=false`, `DISABLE_AUTOUPDATER=1`,
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`. Working directory: an empty
  directory in the tmpfs home.
- Claude Code 2.1.289 (pinned, official installer) accepts the empty
  `--setting-sources ""` (and refuses unknown values).
- The relay's MCP endpoint is reached through `socat` on 127.0.0.1:8765 in the
  container, forwarding to the mounted `mcp.sock`; it sends
  `Host: localhost:8765`, which the relay accepts.
- Claude Code's own error messages (e.g. "Not logged in", a 401 for a revoked
  token) are not read aloud; the service says in German that the login failed
  and shows the error in the state.
- The system prompt (`agent_relay_voice/prompt.py`) is based on
  claude-code-voice's voice prompt: short spoken German answers, no markdown,
  paths or code; may answer itself and research on the web; project work goes
  through the relay; approvals only at the laptop; web content is untrusted.

## The container

`agent-relay-voice run` starts it with exactly:

| Passed in | Where | Mode |
|---|---|---|
| `$XDG_RUNTIME_DIR/pulse/native` | `/run/pulse/native` (`PULSE_SERVER`) | |
| `$XDG_RUNTIME_DIR/agent-relay/` (mcp.sock, events.sock) | `/run/agent-relay` | read-only |
| `$XDG_RUNTIME_DIR/agent-relay-voice/` | `/run/agent-relay-voice` (control.sock) | |
| `~/.config/agent-relay-voice/claude-token` | `/run/secrets/claude-token` | read-only, only if it exists |
| `~/.config/agent-relay-voice/settings/` | `/config` | |
| volume `agent-relay-voice-models` | `/data` (Hugging Face cache, Piper, whisper.cpp models) | |

and `--read-only` root, tmpfs for `/tmp`, `/run` and the home directory,
`--cap-drop ALL` plus `NET_ADMIN`, `SETUID`, `SETGID`, `SETPCAP` for the
entrypoint and `KILL` for docker-init to forward SIGTERM (the service process
itself has none), `--security-opt no-new-privileges`, `--dns 1.1.1.1 --dns
9.9.9.9`, `--init`. The directories on the host are created with mode 700
before the start, so Docker does not create them as root.

The entrypoint (`agent_relay_voice/firewall.py`, as root) rejects every
private and local range for IPv4 (10/8, 172.16/12 with the Docker bridge and
the host on it, 192.168/16, 169.254/16, 100.64/10, 127/8 outside `lo`, ...)
and IPv6 (`::1` outside `lo`, `fc00::/7`, `fe80::/10`, IPv4-mapped, multicast,
...), plus the host's own global addresses (passed in as `VOICE_BLOCK_EXTRA`)
and the default gateway; inbound only replies. If a rule fails, the container
does not start. Then it execs the service through `setpriv` as the host
user's uid/gid with an empty bounding set and no-new-privs: the service has no
capabilities.

Not passed in: the session bus, the home directory, `~/.claude`, SSH keys,
the Docker socket, devices.

## Control socket

`$XDG_RUNTIME_DIR/agent-relay-voice/control.sock`, mode 600, JSON lines
(contract: `docs/voice-plan.md`, section 8).

- `{"id": 1, "cmd": "state"}` → the state object
- `{"id": 2, "cmd": "options"}` → `{"stt": [{"id", "label"}], "tts": [...], "claude_models": [...]}`
- `{"id": 3, "cmd": "set", "stt": "small", "listening": false}` → the new state
- `{"id": 4, "cmd": "stop_speaking"}`, `{"id": 5, "cmd": "new_conversation"}` → the state
- errors: `{"id": 3, "ok": false, "error": "unknown stt 'x'"}`
- pushed: `{"event": "state", "state": {...}}` on every change,
  `{"event": "exchange", "user": "...", "assistant": "..."}` after each turn

State: `{"phase", "error", "loading", "settings": {"stt", "tts",
"claude_model"}, "listening", "last_user", "last_assistant"}`; `listening` is
the setting, `phase` one of starting, listening, paused, muted, hearing,
transcribing, thinking, speaking, error.

```sh
printf '{"id":1,"cmd":"state"}\n' | socat - UNIX-CONNECT:$XDG_RUNTIME_DIR/agent-relay-voice/control.sock
```

## Tests

```sh
docker run --rm --network none -u "$(id -u):$(id -g)" --entrypoint python3 \
  agent-relay-voice:latest -m pytest -q -p no:cacheprovider /opt/voice/tests
```

Unit tests: mute detection on synthetic signals, the VAD segmenter, sentence
splitting and streaming, settings persistence, relay notices and the event
socket with reconnect, the MCP client over a Unix socket, the exact Claude
flags and environment, stream-json handling with a fake `claude`
(`tests/fake_claude.py`), and the service end to end over the control socket
(turns, barge-in, queued messages, notices, missing token, crashes, auth
errors).

## Files

```
agent-relay-voice          host launcher (stdlib): build, run, stop, token, status, probe, listen, cli
agent-relay-voice.service  systemd user unit (template; install.sh fills in the path)
install.sh                 installs the unit, links the launcher into ~/.local/bin
Dockerfile, requirements.txt
agent_relay_voice/
  __main__.py   command line: serve, probe, listen, say, transcribe
  firewall.py   entrypoint: firewall as root, then setpriv to the service user
  service.py    the service: state, turns, barge-in, notices, control commands
  audio.py      parec capture, pacat playback
  mute.py       mute detection
  vad.py        streaming Silero VAD and the utterance segmenter
  stt.py        faster-whisper and whisper.cpp (whisper-server) engines
  tts.py        Pocket TTS and Piper engines
  speaker.py    sentence-wise synthesis and playback with barge-in
  textproc.py   sentence splitting while streaming, markup removal, pronunciation dictionary
  claude.py     the Claude Code flags, stream-json parsing, the process
  prompt.py     the voice system prompt and the service's own German sentences
  relay.py      relay events (with reconnect), notices, minimal MCP client
  control.py    the control socket
  settings.py   persistent settings
tests/
```
