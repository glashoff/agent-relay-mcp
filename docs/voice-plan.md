# Voice on the laptop: plan

Status: steps 1-8 built 2026-11-11 (not yet installed or tested with a real token and headset); section 10 planned.

Talk to the relay by voice on the laptop itself: speech recognition, a
Claude Code instance as the conversation partner, and speech output all run
locally, in a Docker container. The only remote parties are the model behind
Claude Code and whatever the instance looks up on the web. No claude.ai chat,
no connector, no V-server, no tunnel.

```
 host                                 │ container "agent-relay-voice"
                                      │
 headset ── PipeWire ── pulse socket ─┼─> capture ─> mute detection ─> VAD ─> Whisper (CPU)
                                      │                                          │
                                      │                                          ▼
 relay ── unix socket: MCP ───────────┼─> (local forwarder) <── Claude Code "voice" instance
       ── unix socket: events ────────┼─> spoken notices          (relay MCP, web search/fetch;
                                      │                             nothing else)
 GNOME extension ── unix socket ──────┼─> control (state, settings)      │
                                      │                                  ▼
 headset <── PipeWire <── pulse ──────┼── playback <── TTS (Piper / Pocket TTS)
                                      │
                       internet ──────┼── allowed; local and private networks rejected
```

## 1. Goals and non-goals

- Hands-free: the microphone listens all the time; a pause ends an utterance.
  Folding up the headset boom mutes it; the app then transcribes nothing
  (section 5).
- German with many English technical terms, in both directions.
- Choice of engines, as measured in `test/` and `test/tts/`:
  - Speech recognition: everything that runs on the CPU: faster-whisper
    large-v3-turbo (default), its German fine-tune, small; whisper.cpp CPU
    builds (large-v3-turbo f16/q8_0). No GPU backends (NVK was 2.5-3x slower
    than the CPU, see `test/README.md`).
  - Speech output: Piper "thorsten-high", Pocket TTS German "juergen" with the
    24-layer model, and the faster 6-layer model.
- The instance answers questions itself, may research on the web, and hands
  work in projects to the relay. Its model is selectable.
- Controlled from GNOME: the existing relay extension gets a voice section.
- Not a goal: running on the phone (that is claude-code-voice), the V-server
  instance, wake words.

## 2. Why a container

The voice service needs a large stack of third-party packages (PyTorch for
Pocket TTS, faster-whisper/CTranslate2, onnxruntime, piper-tts, Hugging Face
libraries). On the host every one of them would run with the user's full
rights: SSH keys, projects, the session bus and with it the relay's control
interface. In the container a compromised package sees only what is passed in
on purpose (section 4).

Docker, like the dev containers (base-devcontainer) and the V-server.

## 3. Components

1. **Voice service** (`voice/` in this repository: Python package, Dockerfile,
   start script, systemd user unit that runs the container): audio capture,
   mute detection, VAD, speech recognition, the Claude Code instance, speech
   output, playback.
2. **Relay additions** (small, no new dependencies):
   - the MCP endpoint also on a Unix socket, so the container needs no TCP
     path to the host;
   - the event stream (run started/ended, request added …) on a second Unix
     socket as JSON lines, read-only. This is the "Unix socket adapter"
     planned in the access-control plan, section 3, restricted to events: no
     commands, nothing that grants.
3. **GNOME extension**: a "Voice" submenu in the existing relay menu: on/off,
   state (off / listening / muted / transcribing / thinking / speaking),
   speech engine and output voice, Claude model, "stop speaking", the last
   exchange as text. The panel icon shows when voice is listening. It talks to
   the voice service over a Unix socket (JSON lines; Gio can do that), not
   over D-Bus, because the session bus is not passed into the container.
4. **Claude Code "voice" instance**, started and owned by the voice service
   inside the container.

## 4. The container

| Passed in | How | Why |
|---|---|---|
| Microphone and speakers | the PipeWire pulse socket (`$XDG_RUNTIME_DIR/pulse/native`) mounted, `PULSE_SERVER` set; the same approach as base-devcontainer's audio option | the purpose of the service |
| Relay MCP | Unix socket from the relay, mounted; inside, a forwarder (socat) offers it as `http://localhost:8765/mcp`, which the relay's Host check accepts | Claude Code's MCP client needs a URL |
| Relay events | Unix socket, mounted | spoken notices |
| Control | a Unix socket the service creates in a mounted directory under `$XDG_RUNTIME_DIR/agent-relay-voice/` | GNOME extension |
| Claude token | file mounted read-only (mode 600 on the host) | inference only, see section 6 |
| Models | a named volume (Hugging Face cache, Piper voices, whisper.cpp models), filled on first start | survive image rebuilds |
| Internet | allowed | web research, the Anthropic API, model downloads |

Not passed in: the session bus, the home directory, `~/.claude`, SSH keys,
the Docker socket, any device besides audio through PipeWire.

**Network**: full internet, nothing local. At start a firewall script (as
root, then dropping to the service user) rejects every private and local
range, IPv4 and IPv6 (10/8, 172.16/12, 192.168/16, 169.254/16, 100.64/10,
fc00::/7, fe80::/10, the Docker bridge and the host itself …), following
base-devcontainer's `init-firewall.py`; if a rule fails, the container does
not start. The relay is reached only through its socket, so the host needs no
open port toward the container. Public DNS (1.1.1.1, 9.9.9.9) instead of the
LAN router, as in the dev containers.

**Hardening**: runs as an unprivileged user, `no-new-privileges`, all
capabilities dropped except the ones the firewall step needs (`NET_ADMIN`,
dropped again before the service starts), read-only root file system with
tmpfs for `/tmp`.

## 5. Audio, mute and turn-taking

- **Capture**: 16 kHz mono from the default source through the pulse socket,
  read in 100 ms blocks.
- **Mute detection** (measured with the Jabra Link 380, 2026-11-11): with the
  boom up, the headset sends a constant pattern instead of the microphone
  (RMS about 0.5, 75 % exact zeros); room noise with the boom down has an RMS
  of 5-30 and almost no zeros; a switched-off headset sends only zeros. The
  rule "RMS < 1.5 and more than 50 % zeros for 300 ms" is muted, anything
  else is not. Configurable thresholds; other headsets can be checked with a
  built-in probe (`agent-relay-voice probe`).
- **When muted**: nothing is transcribed; an utterance in progress is
  dropped. Folding the boom down starts listening again. Speech output is not
  affected by muting; to stop it, speak into it or click "stop".
- **VAD**: Silero VAD (comes with faster-whisper) on the live stream. An
  utterance ends after 0.8 s of silence (configurable); very short noises
  (< 0.3 s) are ignored.
- **Barge-in**: speaking while output plays stops playback (the headset does
  not pick up the speakers). The interrupted rest is dropped; the new
  utterance goes to Claude as usual.
- **Recognition**: the utterance is transcribed with the chosen model, with
  the prompt and hotwords from `test/` (editable list of terms; project names
  from the relay's `list_projects` are added automatically).
- **Playback**: per sentence. Pocket TTS gets +12 dB gain (it is quieter than
  the others, see `test/tts/README.md`).

## 6. The Claude Code instance

One long-lived process in stream-json mode, so the conversation keeps its
context:

```
claude -p --input-format stream-json --output-format stream-json \
  --include-partial-messages --verbose \
  --tools "WebSearch,WebFetch" --strict-mcp-config --mcp-config <relay-only.json> \
  --setting-sources "" --append-system-prompt <voice prompt> \
  --allowedTools "mcp__relay,WebSearch,WebFetch" [--model <model>]
```

- **Tools**: web search and fetch, and the relay. No Bash, no file access.
  `--strict-mcp-config` with a config that names only the relay loads no
  other MCP server; no settings files are read, so no hooks or plugins. The
  working directory is an empty directory inside the container.
- **Authentication**: a token from `claude setup-token`, entered once with a
  command like base-devcontainer's `--claude-token` and stored with mode 600.
  It can only make model requests, so the instance cannot load claude.ai
  connectors either; `ENABLE_CLAUDEAI_MCP_SERVERS=false` is set in addition.
  (`--bare` would be stricter still but accepts only an API key.)
- **System prompt**: German voice conversation, short spoken answers, no
  markdown, paths or code read aloud; it may answer questions itself and look
  things up on the web, and uses the relay for work in projects; approvals
  happen at the laptop. Based on the prompt in claude-code-voice
  (`ClaudeRemote.kt`).
- **Model**: selectable (`--model`, e.g. haiku, sonnet, opus); changing it
  starts a new conversation. Default: Claude Code's own default.
- **Streaming**: text deltas are cut into sentences; each sentence goes to
  TTS as soon as it is complete, so speech starts after the first sentence.
- **Relay events**: when a run ends (`run_ended`) or a request waits for
  approval (`request_added`), the service feeds a short notice into the
  conversation ("[Ereignis: der Auftrag in Projekt X ist fertig]"); the
  instance then calls `get_updates` and tells the user. Notices that arrive
  while the user speaks wait until the turn is over.
- **New conversation**: from the menu, or by voice ("neues Gespräch"), which
  restarts the process.
- **Web content is untrusted**: a fetched page can contain instructions
  (prompt injection). The worst the instance can do with them is send
  messages through the relay, which, like any other relay client, cannot
  approve anything; approvals stay at the laptop.

## 7. Engines

Copied from `test/` and `test/tts/` into `voice/` (not imported from there;
`test/` stays a playground):

| | Engines | Notes |
|---|---|---|
| Speech recognition | faster-whisper large-v3-turbo (default), German turbo, small; whisper.cpp CPU f16/q8_0 (built in the image) | turbo about 5-8 s for a short sentence on this CPU; small about 2 s |
| Speech output | Pocket TTS German 24l "juergen", Pocket 6-layer, Piper thorsten-high | Pocket 24l: first chunk about 0.4 s, real-time factor about 1 on this CPU, i.e. borderline; 6-layer: RTF 0.3 |
| Pronunciation dictionary | for Piper only (it made Pocket worse) | editable list |

Models load once at start (or on switching) and stay in memory. Expected
memory: turbo about 1.5 GB, Pocket 24l about 1 GB. Image size: several GB
(PyTorch CPU build); the disk is at 95 %, so the test environments in
`test/` should be removed once the service works.

## 8. Interfaces

Fixed before building, so the relay, the voice service and the extension can
be built in parallel.

**Relay sockets** (new config keys, both optional, off when empty):

- `mcp_socket`, default for the voice setup
  `$XDG_RUNTIME_DIR/agent-relay/mcp.sock`: the same MCP app as the TCP
  endpoint (same Host and Origin checks; the forwarder in the container sends
  `Host: localhost:8765`).
- `events_socket`, default `$XDG_RUNTIME_DIR/agent-relay/events.sock`: every
  client gets each control event as one JSON object per line, exactly the
  JSON the D-Bus `Event` signal carries (`{"type": "run_ended", ...}`). The
  relay reads nothing from it; a client that does not keep up is dropped.
- The directory `$XDG_RUNTIME_DIR/agent-relay/` is created with mode 700, the
  sockets with mode 600; a stale socket file is replaced at start.

**Voice control socket**: `$XDG_RUNTIME_DIR/agent-relay-voice/control.sock`,
created by the service (the directory is mounted into the container). JSON
lines in both directions.

- Request: `{"id": 1, "cmd": "<command>", ...arguments}`; reply:
  `{"id": 1, "ok": true, "result": ...}` or
  `{"id": 1, "ok": false, "error": "<text>"}`.
- Commands:
  - `state` → the state object below.
  - `options` → `{"stt": [{"id", "label"}], "tts": [...], "claude_models":
    [...]}`.
  - `set` with any of `stt`, `tts`, `claude_model` (ids from `options`),
    `listening` (bool: pause or resume listening without stopping the
    service).
  - `stop_speaking`, `new_conversation`.
- Pushed without a request, to every client: `{"event": "state", "state":
  {...}}` whenever the state changes, and `{"event": "exchange", "user":
  "...", "assistant": "..."}` when a turn is complete.
- State object: `{"phase": "starting" | "listening" | "paused" | "muted" |
  "hearing" | "transcribing" | "thinking" | "speaking" | "error",
  "listening": true | false, "error":
  null | "<text>", "loading": null | "<what is loading>", "settings": {"stt",
  "tts", "claude_model"}, "last_user": "...", "last_assistant": "..."}`.

**On and off** from the menu means starting and stopping the systemd user
unit `agent-relay-voice.service` (which runs the container); "paused" is
listening switched off inside a running service.

## 9. Steps

1. Relay: MCP and event stream on Unix sockets, with tests.
2. `voice/` skeleton: package, Dockerfile, firewall script, start script,
   systemd user unit, token command. Engines moved over from `test/`.
3. Audio loop: capture, mute detection, VAD, transcription; a command-line
   mode that prints transcripts and the mute state (testable on its own).
4. Speech output with sentence streaming and barge-in.
5. Claude Code instance: start, stream-json in and out, the locked-down
   flags; relay events as notices.
6. Control socket of the service.
7. GNOME extension: voice submenu, state in the panel, settings.
8. Tests: mute detection on recorded samples, sentence splitting, the
   stream-json handling with a fake `claude`, the flags actually passed, the
   firewall (a private address is unreachable, a public one is reachable).

## 10. Next: voice menu and approvals by voice (not built)

**Voice menu**, handled by the voice service itself, never passed to Claude:
"Menü" as a whole utterance enters it (a short tone confirms); inside, short
commands matched fuzzily against a fixed vocabulary: "Modell Sonnet/Opus/…",
"Kompaktieren" (`/compact` for the voice instance; check that it works in
stream-json mode, otherwise restart with a summary), "Neues Gespräch",
"Erkennung klein/turbo/…", "Stimme Thorsten/Jürgen/…", "Zuhören aus",
"Hilfe" (reads the commands), "Zurück"/"Ende"; it closes by itself after a
few seconds of silence. Every change is confirmed aloud.

**Approvals by voice**, with the trust on the host:

1. In the menu, "Freigabe webgame" (optionally with a duration) makes the
   voice service send an ordinary request to the relay; it cannot grant.
2. The relay on the host reads out what the waiting request actually says,
   after a short tone, with espeak-ng in English, e.g. "Approve webgame, in
   its dev container, for two hours? Press play to confirm." (English, as
   the extension is; espeak's English is also clearer than its German).
3. A press of the headset's Play key within about 20 s approves exactly that
   one request. Not with more than one request waiting; a press outside the
   window does nothing.
4. Host grants work the same way; the announcement says plainly "full access
   to this laptop". The key code of Play is to be checked with the real
   headset (likely KEY_PLAYPAUSE or KEY_PLAYCD).

Why it is safe even though speech recognition runs in the container:

- The container decides only what is *requested*. What is *approved* is what
  the host announces, read from the relay's own state, and the key press
  happens on a device the container has no access to (it is not passed in).
- A compromised container could request something other than what was said
  and announce it falsely in its own voice. Hence the host's announcement:
  in espeak-ng's clearly different voice, after a tone, and while the host
  mutes the container's audio stream (`wpctl` / `pactl` on its sink input),
  so the container can neither talk over it nor imitate it at that moment.
- Claude never takes part: the menu commands are recognised by the voice
  service, and the relay offers no tool that grants.
- espeak-ng is a Debian package (C, no Python dependencies, no models), so
  the host side brings no new third-party stack.

Access to the Play key:

- The Jabra Link 380 dongle is also an input device (`/dev/input/event7`
  now; USB 0b0e:24ca, "Jabra Link 380"), readable only by root and the group
  `input`. The user is not in `input`, and that group would allow reading
  every keyboard, so a udev rule for this dongle only, e.g.
  `/etc/udev/rules.d/70-agent-relay-jabra.rules`:
  `SUBSYSTEM=="input", KERNEL=="event*", ATTRS{idVendor}=="0b0e",
  ATTRS{idProduct}=="24ca", TAG+="uaccess"` (access for the user logged in at
  the seat; a fixed `OWNER=` would be the alternative). Installed by the
  install script with sudo, after asking. The relay finds the device by name
  under `/dev/input/by-id/`, not by the event number, which can change.
- The relay grabs the device (`EVIOCGRAB`) only during the confirmation
  window, so GNOME does not also see the key as play/pause then; otherwise
  the key keeps working as usual.
- Reading the key needs no new Python dependency (the event format is a
  fixed C struct; the ioctl comes from `fcntl`).

## 11. Decisions (2026-11-11)

1. Speech output: Pocket TTS 24l, plus the 6-layer model as a faster option,
   plus Piper thorsten-high.
2. Speech recognition: large-v3-turbo and small are both offered, with the
   other CPU models.
3. The instance may answer questions itself and research on the web; its
   model is selectable.
4. Authentication with a token from `claude setup-token`.
5. The service runs in a Docker container: full internet, no local or private
   networks, no session bus.
