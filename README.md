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

Four tools:

| Tool | What it does |
|---|---|
| `list_projects` | The folders in your projects directory, with whether their container runs, Claude Code is working, or a reply is waiting |
| `send_message(project, message, new_conversation=false, wait_seconds=0)` | Passes a request to Claude Code in that project, continuing the project's conversation; starts the dev container if needed. Returns at once, unless `wait_seconds` is set |
| `get_updates(project, wait_seconds=0)` | The state of the latest request: still working (with its recent steps), or the final reply |
| `stop(project)` | Ends the current request; the conversation is kept |

Requests to a coding agent take minutes, but Claude's apps give up on a tool
call after somewhere between 20 and 60 seconds. So nothing here waits by
default: the chat says "Claude Code is working on it", and you ask later. The
optional `wait_seconds` (at most 15 by default) is for questions that are
obviously quick.

## How it works

- **One conversation per project.** Each message is one `claude -p` run with
  `--output-format stream-json`; follow-ups continue the same session with
  `--resume`.
- **Runs are detached.** A run is started with `setsid` inside the container
  and writes to files under `~/.cache/agent-relay-mcp/<run>/` there. Restarting
  this server, a dropped tunnel or a phone that never asks again does not stop
  it; the server keeps only the ids (in `~/.local/state/agent-relay-mcp/`) and
  reads the output again when asked. Run directories older than 7 days are
  removed.
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

```sh
pipx install git+https://github.com/glashoff/agent-relay-mcp   # or: pipx install .
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

- **The URL is the only credential.** There is no sign-in: whoever knows the
  URL can make Claude Code work in any of your projects. Treat it like a
  password, keep it out of logs, and change it (in the proxy and the
  connector) if it may have leaked. OAuth would add a real sign-in at the cost
  of running an authorization server; it is not implemented.
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
without containers, so they need neither Docker nor an account.

## License

GPL-3.0-or-later, see [LICENSE](LICENSE).
