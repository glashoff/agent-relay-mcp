# agent-relay-mcp

An MCP server that lets an assistant on your computer hand tasks to **Claude
Code**, in the dev container of one of your projects, and report back.

It is made for the local voice service (`voice/`): you talk to it at the
laptop, it passes your request on to Claude Code working in your project, and
later tells you in a few words what came out. The relay is reached only
through Unix sockets on this machine; it has no network port.

```
voice service (container) ── socat ──> $XDG_RUNTIME_DIR/agent-relay/mcp.sock
                                                  │
                                                  ▼
                                   agent-relay-mcp on your computer
                                                  │  docker exec
                                                  ▼
                                   claude -p … in the project's dev container
```

## What the client gets

Five tools:

| Tool | What it does |
|---|---|
| `list_projects` | The folders in your projects directory, with whether their container runs, Claude Code is working, a reply is waiting, and whether they are approved |
| `send_message(project, message, new_conversation=false, wait_seconds=0, run_on_host=false)` | Passes a request to Claude Code in that project, continuing the project's conversation; starts the dev container if needed. Claude Code stays open between messages, so the message is passed straight in, also while it works; only while it is being started is it queued for a moment. Returns at once, unless `wait_seconds` is set. `run_on_host=true` only asks to run a project with a dev container on the host instead: it waits for approval with the full-access warning |
| `get_updates(project, wait_seconds=0)` | The state of the latest request: still working (with its recent steps, and the reply so far if Claude Code answered and works on in the background), or the final reply |
| `stop(project)` | Ends the current request (or Claude Code waiting for the next message); the conversation is kept |
| `release_access(project="", all_projects=false)` | Ends your approval for a project (or all) when you say you are done, so it is locked again; refused while Claude Code still works there. It can only take access away |

Requests to a coding agent take minutes, but an assistant should not hang on
a tool call that long. So nothing here waits by default: the assistant says
"Claude Code is working on it", and you ask later (the voice service also
announces finished requests from the event socket). The optional
`wait_seconds` (at most 15 by default) is for questions that are obviously
quick.

## Approvals

Every project is locked. A message to a locked project does not run; it
becomes a request with a two-digit number, which the assistant tells you. You
approve it at the computer, and the message then runs by itself. Nothing the
assistant or Claude Code can call gives approvals: they come only through the
control interface on the D-Bus session bus, used by the GNOME extension
(below) and by `agent-relay-ctl`:

```sh
agent-relay-ctl                        # grants, waiting requests, projects
agent-relay-ctl approve 47 --for 2h    # approve request 47, granting its project
agent-relay-ctl deny 47
agent-relay-ctl grant web --for 1h     # approve a project in advance (or --all)
agent-relay-ctl revoke web             # running requests finish; --stop ends them
agent-relay-ctl watch                  # events as they come
```

- **Grants** cover one project or all projects with a dev container, and end
  after their time, after 30 minutes without a message or a running request,
  and, only if ticked for the grant, when the screen locks (off by default). Upper limits: 12 h for a project, 4 h for all
  projects, 2 h for full access.
- **Full access**: projects without a dev container, and a project with one
  that you choose to run on the host instead (`--on-host`, with a
  conversation of its own there), run as you with access to everything. Their
  grants need you to type "full access". A project changes between its dev
  container and the host only while the other side is idle (`--when-idle`
  waits for that).
- **Asking for the host**: the assistant can ask to run a project with a dev
  container on the host (`send_message(..., run_on_host=true)`). That only
  makes a request shown as "asks to run ON THE HOST"; nothing runs on the
  host until you approve it with "full access" (or approve it for the dev
  container instead), and a grant for the dev container never becomes one
  for the host this way.
- **Revoking** lets a running request finish (background tasks included) and
  then ends Claude Code there; messages queued for it wait for approval
  again. `--stop` ends it at once.
- `agent-relay-ctl` asks for a confirmation typed on the terminal, and the
  relay accepts grants only from GNOME Shell or a process with a terminal, so
  Claude Code started by the relay cannot approve anything. Grants are kept in
  `~/.local/state/agent-relay-mcp/access.json`; an unreadable file locks
  everything.

### The GNOME extension

`gnome-extension/` holds an extension for GNOME Shell 48: an icon in the top
bar (a padlock; open when something is approved, a warning sign for full
access; with the number of waiting requests) and a tile in quick settings,
both with the same menu: the relay service with start, stop and restart,
requests waiting for approval, active grants with revoke, giving grants, and
keeping the computer awake, and what is working now (with stop). It notifies
only when something needs you: new requests (with allow and deny) and grants
about to end; "Notify about activity too" in the menu (off by default) adds
requests that start, approvals that end and the like. It does not run on the
lock screen; after unlocking it says what is waiting.

A "Voice" submenu controls the local voice service (`voice/`, see
`docs/voice-plan.md`): a switch starts and stops `agent-relay-voice.service`
(greyed out with a hint while the unit is not installed), and while the
service runs it shows what it is doing (listening, paused, microphone
muted, hearing you, transcribing, thinking, speaking, errors and models
loading), a "Listening" switch to pause without stopping the service, "Stop
speaking", "New conversation", the choice of speech recognition, voice and
Claude model, and the last exchange (in full in a dialog). A small
microphone next to the panel icon shows that voice is on: highlighted while
it hears you, crossed out while muted or paused. The extension talks to the
service over `$XDG_RUNTIME_DIR/agent-relay-voice/control.sock` (JSON lines,
not D-Bus) and reconnects when the service restarts; nothing in the voice
part can grant or approve access.

`scripts/install.sh` (below) copies it into
`~/.local/share/gnome-shell/extensions` and enables it; it loads at the next
login.

`gnome-extension/test-in-headless-shell.sh` runs it in a headless GNOME Shell
with a private session bus, its own folders, a throwaway relay and a fake
voice service (`gnome-extension/fake-voice-service.py`, a test helper that
implements the control socket; `--demo 10` plays a conversation in a loop),
and leaves screenshots; it touches nothing of the running session.

### Keeping the computer awake

While a request runs, background tasks included (and five minutes after), the
relay keeps the computer from going to sleep by idling (Claude Code waiting
for the next message does not count), through a systemd-logind inhibitor. Other modes:
off, or while anything is approved; and optionally also with the lid closed
(it gets warm in a bag). It lets go on battery below 20 %.

### Following along

The relay writes a readable log of each project's conversation into the
project folder, `.relay/log.md`: what the assistant sent (and messages passed in or
queued, requests waiting for approval), Claude Code's steps, what it says in
between, its thinking as short summaries, and the replies. It reads the
output every two seconds while a request runs, so the log is current without
anyone asking. Each message that starts a request gets a "Request" header of
its own, also when it goes into a Claude Code that is still open. Open the file in VS Code and use *Open Preview to the Side*
(Ctrl+K V): the preview follows the file as it grows.

- `.relay/.gitignore` (containing `*`) keeps it out of git, without changes to
  the project's own `.gitignore`.
- At the start of a request, a log over 1 MB moves to `.relay/log.1.md`
  (replacing the one before); long texts are shortened.
- It works the same for projects with a dev container (the folder is
  mounted there, the relay writes it from outside) and for host projects.
  The relay never follows a symlink in `.relay` and only appends to a regular
  file, so a container cannot redirect it; HTML and images in Claude Code's
  text are defused, so the preview loads nothing.
- `project_log = false` turns it off; `thinking_display` controls whether
  thinking summaries are there (`"summarized"`, the default).

## How it works

- **One conversation per project, kept open.** A conversation is one
  `claude -p` run with `--input-format stream-json --output-format
  stream-json` that stays open between messages; a new run continues the same
  session with `--resume`. Details: `docs/conversation-plan.md`.
- **Messages go straight in.** A run reads its input from a FIFO in its run
  directory, which a small shell loop next to it keeps open, so every message
  is passed into the open run: while Claude Code works, it takes it into
  account at its next step, as when typing ahead in the IDE; while it waits,
  it starts on it. Only while the run is being started (or is just ending) is
  a message queued, and sent as soon as possible; a reply nobody picked up in
  between is reported along with the next one. `stop` drops the queue.
- **Done is not the end.** A request is done once a result has come after the
  last message and no background task of Claude Code's (a subagent or a
  command run in the background) is open; when one ends, Claude Code takes up
  its result in a pass of its own, which belongs to the same request.
  Background tasks keep the run open and count as work (for the idle time of
  approvals and for keeping the computer awake); a run that only waits for
  the next message does not. `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0` is set,
  so Claude Code never kills background tasks after ten minutes when its
  input ends.
- **When a run ends.** The relay closes the run's input, Claude Code finishes
  what it has and exits, when nothing was sent for `conversation_idle_minutes`
  (15 by default, with no background task open), when no approval covers it
  any more (after revoking: once it is done), on `stop`, `release_access`, a
  switch between dev container and host, or a new conversation. While a run
  is open, a `claude` process runs in the dev container, so an idle-stop of
  the container that counts Claude Code as activity keeps it up until then.
  Runs need `flock`, `timeout` and `mkfifo` in the container (util-linux and
  coreutils).
- **Runs are detached.** A run is started with `setsid` inside the container
  and writes to files under `~/.cache/agent-relay-mcp/<run>/` there. Restarting
  this server or a client that never asks again does not stop it, and it
  stays open: the restarted server finds it again from the ids it
  keeps (in `~/.local/state/agent-relay-mcp/`), passes the next message into
  it and reads the output again. Run directories untouched for 7 days are
  removed. A run on this machine (a project without a dev container, or one
  approved to run on the host) is started in a systemd scope of its own
  (`systemd-run --user --scope`) when the relay runs as a systemd service;
  otherwise it would belong to the service, and restarting the relay would
  end it (`host_run_scope`).
- **Containers are found, not configured.** A project's container is the one
  with the label `devcontainer.local_folder=<project path>`, which the dev
  container CLI sets. Commands run as the container's `remoteUser` (from its
  metadata label), in the folder the project is mounted at.
- **Unattended.** Claude Code runs with `--permission-mode auto` by default:
  nobody is at the computer to answer a permission prompt.

## Requirements

- Linux with Python 3.11+ and Docker (macOS is untested).
- Projects with a `.devcontainer/devcontainer.json`, and a way to start their
  containers from the command line: the [dev container
  CLI](https://github.com/devcontainers/cli) by default, or your own script
  (`start_command`).
- Claude Code installed **in the containers** and signed in there. A token
  from `claude setup-token` in the `CLAUDE_CODE_OAUTH_TOKEN` environment
  variable is enough and is the smaller credential: it can make model requests
  and nothing else.
- A client on this machine that speaks MCP over the Unix socket (next
  section), such as the voice service.

## Install

On a laptop with this checkout, one script installs or updates everything,
and can be run again after every update (it keeps the configuration, the
relay's state and a changed systemd unit, and does no git operations):

```sh
scripts/install.sh               # relay (pipx, editable, [desktop]), service, GNOME extension
scripts/install.sh --dry-run     # what it would do
scripts/rollback.sh <commit>     # emergency: run an older version, leaving the checkout alone
```

By hand, or on a machine without this checkout:

```sh
pipx install 'agent-relay-mcp[desktop] @ git+https://github.com/glashoff/agent-relay-mcp'
# or, from a checkout: pipx install '.[desktop]'
# [desktop] adds dbus-fast, needed for approvals; agent-relay-ctl comes with it
mkdir -p ~/.config/agent-relay-mcp
cp config.example.toml ~/.config/agent-relay-mcp/config.toml
$EDITOR ~/.config/agent-relay-mcp/config.toml               # projects_dir at least

cp contrib/agent-relay-mcp.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now agent-relay-mcp
```

Without a long-lived login session, a user service stops at logout; run
`loginctl enable-linger $USER` to keep it running.

## Reaching it

The relay listens only on Unix sockets, by default:

```toml
mcp_socket = "$XDG_RUNTIME_DIR/agent-relay/mcp.sock"
events_socket = "$XDG_RUNTIME_DIR/agent-relay/events.sock"
```

- `mcp_socket` is the MCP endpoint (Streamable HTTP at `/mcp`); it is
  required. Clients send a local `Host` header (`localhost` or `127.0.0.1`,
  with or without a port; the voice container's forwarder sends
  `localhost:8765`); any other `Host`, or a foreign `Origin`, is refused. A
  client that only speaks HTTP over TCP needs a forwarder, e.g.
  `socat TCP-LISTEN:8765,bind=127.0.0.1,fork UNIX-CONNECT:<mcp_socket>`, as
  the voice container runs one.
- `events_socket` sends every event of the control interface (request
  started or ended, request added, grant ended …) as one JSON object per
  line, exactly what the D-Bus signal `Event` carries. It reads nothing: no
  commands, so nothing can be approved through it. A client that does not
  keep up is dropped. It does not need D-Bus. Empty turns it off.
- The directory is created with mode 700, the sockets with mode 600, so only
  your user can connect; mount the directory into a container to pass them
  on. A socket left over from a crash is replaced; one another relay is
  listening on stops the start.
- There is no TCP listener any more (until 2026-11-11 there was one, for a
  reverse proxy on a server, so that the Claude app could reach the relay
  from the internet). `listen_host`, `listen_port` and `public_hosts` in an
  old configuration are ignored with a warning.

### Voice on the laptop (optional)

`voice/` holds a voice service: speech recognition (faster-whisper or
whisper.cpp, on the CPU), a locked-down Claude Code instance (web search and
fetch plus this relay's MCP tools, nothing else) and German speech output
(Pocket TTS or Piper). It runs in a Docker container: internet only, no local
networks, no session bus, read-only root, no capabilities for the service. It
needs the relay's Unix sockets above. Setup: `voice/agent-relay-voice build`,
`voice/agent-relay-voice token`, `voice/install.sh`, then
`systemctl --user start agent-relay-voice`, or switch it on from the GNOME
extension's Voice menu. Details in `voice/README.md`, the design in
`docs/voice-plan.md`.

## Security

- **Who can ask.** Only processes of your user on this machine can connect
  to the sockets (mode 600 in a directory with mode 700), and whatever you
  mount the directory into (the voice container). There is no sign-in beyond
  that: a client can send requests, which then wait for your approval
  (section "Approvals"), and can use whatever you have approved while the
  grant lasts. Nothing reachable through the sockets can approve.
- **Approvals are as strong as this user account.** Claude Code in a project
  without a dev container (or one approved to run on the host) runs as you
  and could change the relay, its grants or its code. Grant full access only
  for as long as needed, and install the relay from a fixed version rather
  than editable from a folder Claude Code works in.
- **The container is the boundary.** Claude Code in auto mode runs commands and
  changes files without asking. What a request can do is what the project's
  container can do: its mounts, its network, the credentials inside it. Keep
  account logins, SSH agent sockets and push rights out of the containers.
  `allow_host_projects` removes that boundary for projects without a
  container; leave it off unless you mean it.
- **Prompt injection.** Text Claude Code reads while working (files, web
  pages, command output) can try to steer it. The same container limits apply.
- **This server can run Docker**, which is as good as root on most machines.
  It runs only `docker ps/inspect/exec` and your `start_command`, and it never
  runs a command it received; requests reach Claude Code only as its prompt.
- **Usage.** Every request is Claude Code working on your plan, on top of the
  assistant that sent it.

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/pytest
```

The tests replace Claude Code with `tests/fake_claude.py` and use projects
without containers, so they need neither Docker nor an account. The D-Bus
tests start a private `dbus-daemon`, never touching the session bus.

## License

GPL-3.0-or-later, see [LICENSE](LICENSE).
