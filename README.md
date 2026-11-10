# agent-relay-mcp

An MCP server that lets a Claude chat hand tasks to **Claude Code on your own
computer**, in the dev container of one of your projects, and report back.

It is made for the Claude mobile app's hands-free voice mode: you talk to
Claude on your phone, it passes your request on to Claude Code working in
your project at home, and later reads you a short summary of what came out.
It works the same from any Claude chat that supports custom connectors.

```
phone (Claude app) ── Anthropic ──HTTPS──> your reverse proxy /<secret>/mcp
                                                  │  (tunnel, if the computer is behind NAT)
                                                  ▼
                                   agent-relay-mcp on your computer (127.0.0.1:8765)
                                                  │  docker exec
                                                  ▼
                                   claude -p … in the project's dev container
```

## What the chat gets

Five tools:

| Tool | What it does |
|---|---|
| `list_projects` | The folders in your projects directory, with whether their container runs, Claude Code is working, a reply is waiting, and whether they are approved |
| `send_message(project, message, new_conversation=false, wait_seconds=0, run_on_host=false)` | Passes a request to Claude Code in that project, continuing the project's conversation; starts the dev container if needed. While Claude Code works, the message is passed into the running request, or queued as the next one. Returns at once, unless `wait_seconds` is set. `run_on_host=true` only asks to run a project with a dev container on the host instead: it waits for approval with the full-access warning |
| `get_updates(project, wait_seconds=0)` | The state of the latest request: still working (with its recent steps), or the final reply |
| `stop(project)` | Ends the current request; the conversation is kept |
| `release_access(project="", all_projects=false)` | Ends your approval for a project (or all) when you say you are done, so it is locked again; refused while Claude Code still works there. It can only take access away |

Requests to a coding agent take minutes, but Claude's apps give up on a tool
call after somewhere between 20 and 60 seconds. So nothing here waits by
default: the chat says "Claude Code is working on it", and you ask later. The
optional `wait_seconds` (at most 15 by default) is for questions that are
obviously quick.

## Approvals

Every project is locked. A message to a locked project does not run; it
becomes a request with a two-digit number, which the chat tells you. You
approve it at the computer, and the message then runs by itself. Nothing the
chat or Claude Code can call gives approvals: they come only through the
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
- **Asking for the host**: the chat can ask to run a project with a dev
  container on the host (`send_message(..., run_on_host=true)`). That only
  makes a request shown as "asks to run ON THE HOST"; nothing runs on the
  host until you approve it with "full access" (or approve it for the dev
  container instead), and a grant for the dev container never becomes one
  for the host this way.
- **Revoking** lets a running request finish; messages queued for it wait for
  approval again. `--stop` ends it.
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

`scripts/install.sh` (below) copies it into
`~/.local/share/gnome-shell/extensions` and enables it; it loads at the next
login.

`gnome-extension/test-in-headless-shell.sh` runs it in a headless GNOME Shell
with a private session bus, its own folders and a throwaway relay, and leaves
screenshots; it touches nothing of the running session.

### Keeping the computer awake

While a request runs (and five minutes after), the relay keeps the computer
from going to sleep by idling, through a systemd-logind inhibitor. Other modes:
off, or while anything is approved; and optionally also with the lid closed
(it gets warm in a bag). It lets go on battery below 20 %.

### Following along

The relay writes a readable log of each project's conversation into the
project folder, `.relay/log.md`: what the chat sent (and messages passed in or
queued, requests waiting for approval), Claude Code's steps, what it says in
between, its thinking as short summaries, and the replies. It reads the
output every two seconds while a request runs, so the log is current without
anyone asking. Open the file in VS Code and use *Open Preview to the Side*
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

- **One conversation per project.** Each request is one `claude -p` run with
  `--input-format stream-json --output-format stream-json`; follow-ups
  continue the same session with `--resume`.
- **Messages while it works.** A run reads its input from a FIFO in its run
  directory, so a message sent while Claude Code works is passed into the
  running request, and Claude Code takes it into account at its next step, as
  when typing ahead in the IDE. A small shell loop next to the run closes the
  FIFO once a result has come after the last message passed in, which ends
  the run. A message that cannot be passed in (the container is still
  starting, or the run is already ending) is queued and sent as the next
  request in the same conversation as soon as the run has ended; a reply
  nobody picked up in between is reported along with the next one. `stop`
  drops the queue. Runs need `flock`, `timeout` and `mkfifo` in the container
  (util-linux and coreutils).
- **Runs are detached.** A run is started with `setsid` inside the container
  and writes to files under `~/.cache/agent-relay-mcp/<run>/` there. Restarting
  this server, a dropped tunnel or a phone that never asks again does not stop
  it; the server keeps only the ids (in `~/.local/state/agent-relay-mcp/`) and
  reads the output again when asked. Run directories older than 7 days are
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
- A Claude plan with custom connectors, and a public HTTPS endpoint for this
  server (next section).

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
$EDITOR ~/.config/agent-relay-mcp/config.toml               # public_hosts at least

cp contrib/agent-relay-mcp.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now agent-relay-mcp
```

Without a long-lived login session, a user service stops at logout; run
`loginctl enable-linger $USER` to keep it running.

## Exposing it

Claude's servers, not your phone, call the connector, so the endpoint has to
be reachable from the internet over HTTPS. The server itself only listens on
loopback and speaks plain HTTP; put something in front of it:

1. **A reverse proxy on a machine with a public address** that forwards one
   secret path and nothing else. With Caddy:

   ```caddy
   example.org {
       handle_path /<long random string>/* {
           reverse_proxy 127.0.0.1:8765
       }
   }
   ```

   Generate the secret with `python3 -c 'import secrets; print(secrets.token_urlsafe(24))'`.
   `handle_path` strips it, so the server sees `/mcp`. Keep this path out of
   the proxy's access log.

2. **A tunnel** from your computer to that machine, when the computer is
   behind NAT. `contrib/agent-relay-tunnel.service` is an SSH reverse tunnel
   that restarts itself and starts and stops with the server; its comments
   show how to restrict the key on the server side. Tailscale Funnel or a Cloudflare Tunnel do the same job.

3. Set `public_hosts` to the proxy's host name. The server refuses requests
   with any other `Host` header.

Then add a custom connector in Claude (Settings → Connectors) with the URL
`https://example.org/<secret>/mcp` and no authentication. Adding it reports
that it "couldn't determine how this server signs in"; that is expected,
choose *Continue anyway*.

## Security

Read this before exposing it.

- **The URL lets anyone ask.** There is no sign-in: whoever knows the URL can
  send requests, which then wait for your approval (section "Approvals"), and
  can use whatever you have approved while the grant lasts. Treat the URL
  like a password, keep it out of logs, and change it (in the proxy and the
  connector) if it may have leaked. OAuth would add a real sign-in at the cost
  of running an authorization server; it is not implemented.
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
- **Usage.** Every request is a Claude Code run on your plan, on top of the
  chat that sent it.

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
