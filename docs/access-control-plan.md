# Access control for agent-relay-mcp: plan

Status: final plan for the laptop, decided 2026-11-09. Built so far (branch
access-control): steps 2 to 6 of section 9; the lid behaviour of step 4 is
still to be checked at the laptop.

Today, whoever can use the Claude account the connector is added to (or knows
the connector URL) can make Claude Code work in any project. This plan adds
approvals: projects are locked by default, and nothing runs without an
approval given by the owner in a way that the Claude account cannot reach.

Scope: the laptop. The server instance on the V-server is deferred and kept
in [server-instance-plan.md](server-instance-plan.md); the core is built so
that it can run there later without GNOME.

## 1. Decisions taken

1. **Laptop: approvals only locally**, through the GNOME menu or
   `agent-relay-ctl` on the laptop (including approvals given in advance,
   and keeping the laptop awake). The laptop
   never accepts an approval that comes through the V-server or a web app the
   V-server serves: a compromised V-server must not be able to approve
   anything on the laptop.
2. **Phone approval for the laptop comes later**, through a channel the
   V-server cannot forge. The options are compared in
   [phone-approval-for-the-laptop.md](phone-approval-for-the-laptop.md).
3. **The relay core runs without GNOME**, so the same program can later run
   on the V-server as a second instance. Everything that enforces is in the
   core; GNOME and D-Bus are optional parts on top.
4. **Host projects stay allowed on the laptop** (projects without a dev
   container, `allow_host_projects = true`). They are locked by default and
   approved the same way as other projects, but every place that shows or
   approves one warns clearly that it runs without a dev container, with full
   access to the computer (section 4, "Host projects").
5. **Revoking lets running requests finish.** "Revoke" (the default) does not
   stop a request that is already running; "Revoke and stop" is offered in
   addition (section 4, "Revoke").
6. **Defaults**: idle limit 30 min, request expiry 30 min, upper limit 12 h
   for one project and 4 h for all projects.
7. **Laptop first; the server instance is deferred** (including phone
   approval, passkey, subdomain and push). Built now: the access core in the
   relay, the D-Bus interface, `agent-relay-ctl`, and the GNOME extension
   with menu, notifications and keep-awake.
8. **A project with a dev container can deliberately run on the host**, but
   only through a grant of its own kind, "on the host", given at the laptop
   with the same warning as for host projects. Claude cannot choose it; it can
   only ask for it (decision 13). Only one kind is active per project, never
   both, and the kind can only change while the other one has nothing to do
   (no running request, nothing queued); otherwise the switch is refused or
   waits, with a clear message (section 4, "On the host").
9. **D-Bus** connects the relay with the GNOME extension and
   `agent-relay-ctl` on the laptop (section 3).
10. **Defaults for host grants and keep-awake**: grants for host projects and
    of the kind "on the host" last at most 2 h and end when the screen locks
    (this can be switched off per grant); keep-awake is on while a request
    runs, without the lid inhibitor (the lid checkbox is off).
11. **The menu and the notifications are in English**, like the rest of the
    relay.
12. **The chat may end approvals**: the MCP tool `release_access` ends the
    grant for a project, or all grants, when the user says they are done. It
    only takes access away, so it is acceptable as a tool; it is refused,
    with a clear message, while a request runs or messages wait there.
    Giving approvals stays at the laptop only.
13. **The chat may ask for the host** (added 2026-11-10):
    `send_message(..., run_on_host=true)` for a project with a dev container
    does not run anything. Unless the project is already approved on the
    host, the message becomes a request of the kind "on the host" (in place
    of running, even under a grant for the dev container). The GNOME menu,
    the notification and `agent-relay-ctl` show it as "asks to run ON THE
    HOST" with the full-access warning, and approving it needs the
    full-access confirmation; it can also be approved for the dev container
    instead. A grant for the dev container never turns into one for the host
    this way, and the switch rules apply (only while the other side is idle).
    One waiting request per project, so a request for the host and an
    ordinary one do not mix.

## 2. Threat model

| Who | What they can do today | What must hold afterwards |
|---|---|---|
| Someone with access to the Claude account | Call every tool of every connector: send messages to any project | Can only create requests; cannot approve one, on any instance |
| Claude Code in a dev container (or text it reads: prompt injection) | Change the project's files, run commands in the container, upload with whatever credentials the container holds | Cannot approve; cannot reach the control interface |
| Claude Code in a project on the laptop host (no container) | Everything the user can do | Cannot be contained (same user). Runs only under an explicit grant, given with a clear warning (section 4, "Host projects") |
| A compromised V-server | Read and send MCP traffic to the laptop relay (TLS ends on the server, the tunnel starts there) | Can only create requests on the laptop, like the Claude account; cannot approve |

## 3. Architecture

```
            Claude (account) ── MCP ──┐
                                      ▼
 ┌───────────────────────── relay core ──────────────────────────┐
 │ MCP endpoint · runs · queue · access (grants, requests)        │
 │ keep-awake · control layer (commands + events)                 │
 └──────────────────────────────▲─────────────────────────────────┘
                                │ D-Bus adapter (session bus)
                   ┌────────────┴─────────────┐
           GNOME extension             agent-relay-ctl
      (menu, notifications)          (terminal, scripts)
```

- **Relay core** (Python): everything that enforces. It is the only place
  where a message can be executed, so the check is there. It imports nothing
  from GNOME or D-Bus.
- **Control layer** (in the core, transport-neutral): commands (state,
  grant, revoke, approve, deny, keep-awake settings) and an event stream
  (request added, grant expiring or ended, run started or finished, state
  changed). Transports are thin adapters on top.
- **D-Bus adapter** (laptop, now): the relay claims a name on the session
  bus, `io.github.glashoff.AgentRelay1`, with one object. Methods take and
  return small JSON strings, which keeps the interface easy to extend;
  events are D-Bus signals, so the extension never polls. GJS speaks D-Bus
  natively; on the Python side `dbus-fast` (asyncio), installed as an
  optional extra (`agent-relay-mcp[desktop]`), so the core runs without it.
  The adapter also listens to `org.gnome.ScreenSaver` for screen locks
  (section 7).
- **Unix socket adapter** (later, for the server instance): the same control
  layer as JSON lines on a socket, which also works without a desktop and
  over SSH. Not built now.
- **`agent-relay-ctl`**: command line for state, grants, revoke, deny and
  keep-awake, over D-Bus.
- **Nothing that grants is reachable over HTTP or as an MCP tool.** The MCP
  endpoint has no way to grant or approve; its only access tool,
  `release_access`, ends grants (decision 12).

## 4. Core: grants and requests

- **Grant**: target (one project, or all container projects), expiry time,
  idle limit (ends after this long without a message; default 30 min),
  "ends when the screen locks", last use. Expiry uses wall-clock time, so it
  also ends during suspend; it is checked on every call, timers only drive
  notifications.
- **Upper limits** (decision 6): 12 h for one project, 4 h for all projects.
  A chosen duration beyond the limit is cut to it (e.g. "until midnight" for
  all projects at 18:00 ends at 22:00), and the menu says so.
- **Request**: created when `send_message` reaches a locked project:
  canonical project name, the message (and any further messages to the same
  project, appended), time, a two-digit matching number, expiry after 30 min.
  At most one open request per project and about ten in total.
- **Tool behaviour**:
  - `send_message` to a locked project: "Waiting for approval (number 47)";
    once approved, the messages go through the existing queue (new request,
    or passed into a running one). Denied or expired requests are dropped.
  - Passing a message into a running request needs an active grant.
  - `get_updates`: "waiting for approval" or "denied"; the reply of a request
    that started under a grant is still delivered after the grant ended.
  - `list_projects`: locked, or approved with time left.
  - `stop`: always allowed.
- **Revoke** (decision 5):
  - **"Revoke"**, the default: the grant ends at once, but a request that is
    already running finishes, and its reply is delivered as usual. Nothing
    new starts: no new request, no message passed into the running one.
    Messages that were queued and not yet sent are not sent; they become an
    open request that needs a new approval, so nothing is lost and nothing
    runs without one.
  - **"Revoke and stop"**, offered in addition: ends the grant, stops running
    requests of the projects it covered and drops their queues.
  - A grant that ends by itself (expiry, idle limit, screen lock) behaves
    like "Revoke".
- **State**: `~/.local/state/agent-relay-mcp/access.json`, mode 600, written
  atomically. Missing or unreadable means locked (fail closed).
- **Log**: every grant, approval, denial and expiry goes to the journal.

### Host projects

Projects without a dev container stay allowed (decision 4). They are locked
by default and approved like any other project, but always with a warning.

- **What the warning says**, in short: "No dev container: Claude Code runs
  directly on this computer, as you, with full access to all files,
  credentials and programs." Longer where there is room: while such a grant
  is active, Claude Code (or text it reads, by prompt injection) can also
  change the approval system itself (`access.json`, the relay's code, the
  GNOME extension) and so give itself further grants that the menu would not
  show reliably any more.
- **Where it appears**:
  - GNOME menu: host projects are marked in the project list, in open
    requests and in active grants (a warning icon and the label "full
    access").
  - Giving or allowing a grant for a host project needs one more step: a
    confirmation that repeats the warning ("Allow with full access").
  - Notifications for a host project (request, request started, grant ends)
    carry the warning in the title.
  - `agent-relay-ctl grant` for a host project asks for "full access" to be
    typed on the terminal; there is no option to skip it.
  - The tool texts say it too: `list_projects` shows "no dev container,
    full access", and the reply "waiting for approval" says that the approval
    at the laptop will warn about it, so Claude can mention it to the user.
- **"All projects"** covers container projects only. Host projects are added
  to it only by a separate checkbox "including host projects (full access)",
  off by default, with the same confirmation step.
- **Shorter limits** (decision 10): for host projects the upper limit is
  2 h instead of 12 h, and the grant ends when the screen locks by default.
- **This project itself** (`agent-relay-mcp`) is a host project. A grant for
  it lets Claude Code change the relay's source. With the fixed installation
  (section 8) that only takes effect when you install a new version, which is
  one more reason for it.

### On the host: a container project without its container

A project that has a dev container can be run directly on the host on
purpose (decision 8). This is a property of the grant, never of the request.

- **A grant kind of its own**: every grant for a project is either "in the
  dev container" (the normal kind) or "on the host". Only one kind is ever
  active per project, never both (see "Switching" below).
- **Only at the laptop**: given in the GNOME menu (in "give a grant", and as
  a choice when allowing an open request: "in the dev container" is
  preselected, "on the host" needs the extra confirmation) or with
  `agent-relay-ctl grant --on-host` (with the typed confirmation).
- **Never chosen by Claude**: nothing in the text of a message changes it,
  and the only parameter, `send_message(run_on_host=true)`, merely asks
  (decision 13). The person approving decides the kind. "All projects"
  never includes the host kind.
- **The warning** is that of host projects, plus what is lost: "Runs on this
  computer instead of its dev container: full access as you, and the
  container's protections (read-only files, missing credentials, network
  limits) do not apply." It appears in the same places: menu (marked in
  grants and requests), the extra confirmation step, notification titles,
  `agent-relay-ctl`, and the tool texts (`list_projects`: "on the host, full
  access"; the reply to `send_message` says the request runs on the host).
- **Limits**: those of host projects (decision 10: 2 h, ends when the
  screen locks by default).
- **Where it runs**: in the project folder on the host, as the user, with the
  host's Claude Code, like a host project. The container is neither needed nor
  stopped.
- **The conversation**: Claude Code keeps its sessions per machine and folder,
  so a session from the container cannot be resumed on the host or the other
  way round. The relay keeps one conversation per project and kind, and the
  first message after a switch says that a separate conversation is used
  (and the other one continues once the kind is switched back).
- **Switching** (from one kind to the other, by giving a grant of the other
  kind):
  - **Only while the other kind is idle**: no request running there and no
    message in its queue. Then the switch happens at once: the old grant
    ends, the new one starts.
  - **Otherwise it is refused or waits**, with a clear message naming what
    is still busy, e.g. "Switching webgame to the host: the dev container
    is still working on a request (12 min so far) and has 2 queued
    messages."
    - In the GNOME menu the message comes with two buttons: "Switch when
      idle" and "Cancel".
    - `agent-relay-ctl grant --on-host` refuses with that message and a
      non-zero exit status, unless `--when-idle` is given.
  - **While a switch waits**: the old kind finishes what it has (the running
    request and the messages already queued for it), but takes nothing new:
    no new request and no message passed in. Messages that arrive meanwhile
    are held for the new kind and start there once the switch is done; the
    reply to `send_message` says so ("A switch to the host is pending; the
    message runs there once the dev container has finished."). The menu shows
    the pending switch with what it is waiting for.
  - **The new grant's duration** starts when the switch happens, not when it
    was asked for.
  - **A pending switch can be cancelled** in the menu or with
    `agent-relay-ctl`; held messages then become an open request, because
    they were sent for the other kind. It also ends if the old kind stays busy
    longer than the request expiry (30 min), with a notification; "Revoke and
    stop" on the old kind makes it idle at once.
  - "Busy" means activity, not a grant: a request that is still finishing
    after its grant was revoked (decision 5) keeps the switch waiting.
- **When it ends**: the project goes back to locked, not to a container
  grant.
- **Configuration**: `allow_host_projects = false` turns off both, host
  projects and running container projects on the host.

## 5. Keeping the computer awake

Part of the core, so it works without GNOME (and does nothing where there
is no logind session to inhibit, as on a server). The relay holds a systemd-logind inhibitor by running
`systemd-inhibit --what=… --mode=block sleep infinity` and ending it to
release; no library needed.

- **Modes** (GNOME menu or `agent-relay-ctl`): off; while a request runs
  (default, decision 10); while a grant is active. A few minutes of grace after a run.
- **Level 1, idle**: blocks idle suspend (`idle:sleep`). The screen still
  locks and blanks. Today the laptop never idle-suspends on AC and suspends
  after 15 min on battery.
- **Level 2, lid closed** (explicit checkbox, off by default): also blocks
  `handle-lid-switch`, so closing the lid only turns off the display; Wi-Fi
  and the tunnel stay up. GNOME normally suspends on lid close itself (unless
  an external monitor is connected); that the inhibitor stops it is the first
  thing to verify in that phase.
- **Battery guard**: on battery below a threshold (default 20 %) the relay
  drops the inhibitor and notifies. GNOME's own critical-battery action stays
  in force.
- **Heat**: a laptop running with the lid closed in a bag gets hot. Lid-closed
  operation is for a desk, not for the road.

## 6. GNOME extension (laptop)

GNOME Shell 48.7 (Debian 13, Wayland). Consequences:

- ES modules (since GNOME 45): `import … from 'resource:///org/gnome/shell/…'`
  and `export default class … extends Extension`; older examples do not
  work.
- Quick settings: a `QuickMenuToggle` in a `SystemIndicator`, added with
  `Main.panel.statusArea.quickSettings.addExternalIndicator()`; title and
  subtitle ("Relay: 2 grants, 1 request").
- Notifications: the API since GNOME 46 (`MessageTray.Source`,
  `MessageTray.Notification`, `addAction()`).
- `metadata.json` declares shell version 48; GNOME 49 means testing and
  probably small changes.
- Wayland cannot reload the shell: develop in a nested shell
  (`dbus-run-session gnome-shell --nested --wayland`); enabling it in the real
  session takes one log out and in.
- The extension declares only the normal session mode, not the lock screen:
  approving needs an unlocked laptop.

Contents of the menu (section "Laptop"):

- Service state with start, stop, restart (systemd user manager over D-Bus;
  works while the relay is down).
- Open requests: project, the message as plain text, age, matching number;
  "Allow (duration …)" and "Deny"; for a project with a dev container also
  "Allow on the host" (with the extra confirmation). They stay until decided
  or expired.
- Active grants with time and idle time left; "Revoke" and "Revoke and stop".
- Give a grant: a project or all container projects (host projects only by
  their own checkbox); 15 min, 1 h, 4 h, until midnight; "ends when locked".
  For a project with a dev container, the kind: "in the dev container"
  (default) or "on the host". Host projects and the kind "on the host" are
  marked and need the extra confirmation (section 4).
- Keep awake: mode and the lid checkbox.

Notifications: new request (allow 1 h, deny, open menu), grant ends in 5 min
(extend), grant ended, request started (stop, stop and lock). Foreign text is
escaped and shortened. For a host project, or a project running on the host,
the title carries the warning, and "allow" in the notification opens the
confirmation instead of allowing directly. Notifications never offer "allow
on the host"; that is only in the menu.

While the screen is locked the extension is not running (it declares only
the normal session mode), so it shows nothing then. When it starts again
after unlocking, it reads the state and shows one summary notification for
what happened meanwhile ("2 open requests, 1 grant ended"); open requests
are in the menu in any case.

Two clicks open the relay part of quick settings; a small panel icon with
the lock state and the number of open requests is optional (to be decided).
The extension lives in this repository and is installed by copying, not by
a symlink into the repository.

## 7. Screen lock

Grants do not all end when the screen locks: the main use is giving a grant
and then walking away, and the screen locks a few minutes later. Instead:

- Each grant has "ends when the screen locks"; default off for one project,
  on for all projects.
- Upper limits and the idle limit always apply.
- The relay notices the lock itself, through the D-Bus adapter
  (`org.gnome.ScreenSaver`, signal `ActiveChanged`), not through the
  extension, which is switched off while the screen is locked.

## 8. Security measures

- **Host projects and the kind "on the host" stay allowed, with warnings**
  (decisions 4 and 8, section 4). This is the one place where the approval
  system cannot protect itself: under such a grant, Claude Code runs as the
  user and can
  change the approval system. The warnings, the extra confirmation, the
  shorter limits and the journal make that a deliberate choice each time;
  revoking (and stopping) always works and ends further runs, but cannot undo
  what already happened during the grant.
- **Caller check** (an extra hurdle, no real protection against the same
  user): grant and approve over D-Bus are only accepted from gnome-shell (the
  caller's PID from the bus, `GetConnectionCredentials`, and its program
  path). `agent-relay-ctl` grants only after a confirmation typed on the
  terminal (read from `/dev/tty`), so it fails in a process without one, such
  as Claude Code started by the relay. Revoke, deny and stop are accepted
  from any caller.
- **Fixed installation**: the relay and the extension are installed from a
  reviewed git state (`pipx install` of a tag, the extension copied), not
  editable from the project folder Claude works in.
- **The connector URL stays secret** as before; approvals add to it, they do
  not replace it.
- **Containers** reach neither the session bus nor the relay's port (checked on the laptop for fkirchhoff.com: no bus or Docker
  socket mounted).
- **Requests**: rate limits, one open request per project, foreign text shown
  as a quote, the canonical project name from the relay.

## 9. Order of implementation

1. **Preconditions** (by you, before any of this code runs): install the
   relay from a fixed state instead of editable, so that work in this
   repository does not reach the running relay before it is ready.
2. **Core: access**: grants, requests through the queue, revoke behaviour,
   expiry, idle limit, upper limits, host projects and the kind "on the host"
   (with one conversation per kind) with their warnings and limits, tool
   texts, `instance_name`, the transport-neutral control layer, tests,
   including that no tool input can select the host. Not live yet.
3. **D-Bus adapter and `agent-relay-ctl`**, with the caller check and the
   screen-lock listener; tests against a private bus (`dbus-run-session`).
   2 and 3 go live together: from then on everything is locked, and grants
   are given with `agent-relay-ctl` until the menu exists.
4. **Keep-awake** in the core, starting with a test of how GNOME treats the
   lid inhibitor (needs you at the laptop to close the lid).
5. **GNOME extension: menu**: service state, requests, grants, giving
   grants, keep-awake; developed in a nested shell, installed by copying;
   activating it needs one log out and in.
6. **GNOME extension: notifications**, including the summary after
   unlocking.
7. **Later**: the server instance ([server-instance-plan.md](server-instance-plan.md))
   and phone approval for the laptop
   ([phone-approval-for-the-laptop.md](phone-approval-for-the-laptop.md)).

## 10. Open questions

1. A panel icon (lock state, number of open requests) in addition to quick
   settings? Needed only for step 5.

## 11. Security review (2026-11-09)

A separate review of the branch, focused on ways through MCP and out of a dev
container. Accepted and not counted as findings: a granted host project runs
as the user and can change the approval system.

Fixed, each with a test:

- **Critical: a container grant could run on the host.** Claude Code in a
  container could delete devcontainer.json; the next message under the
  container grant then ran on the host. Grants now record whether the project
  had a dev container and stop covering it when that changes; where a
  conversation runs follows the grant's kind, never what is stored; nothing
  falls back to the host without a grant for it.
- **High: a container could change what runs on the host when it is
  started.** devcontainer.json runs `initializeCommand`, mounts and runArgs on
  this computer. The relay no longer starts a stopped dev container whose
  earlier container could write `.devcontainer` (not mounted read-only); it
  says so, and the container is started at the computer instead. On this
  laptop all containers mount it read-only except webgame's
  (`start_writable_devcontainers` overrides).
- **Medium: unbounded output** from a run could exhaust the relay's memory:
  reading is limited per poll, overlong lines are skipped, kept steps and
  results are capped, malformed output is skipped.
- **Medium: grants stayed valid without a control interface** (no dbus-fast,
  no bus, name taken): then nothing could revoke them, so none counts now.
- **Medium: a folder named `*`** looked like the grant for all projects:
  such names are not projects.
- **Low:** the run's pid file (writable in the container) is no longer trusted
  (pid seen at launch; never 0 or 1 for kill); session ids from the output
  must be UUIDs; a request can no longer be approved after it expired or after
  messages were added to it since it was shown; at most 20 messages per
  request; an access.json that is valid JSON but malformed locks everything
  instead of crashing; state.json is private (0600); grants that end on lock
  end at start if the screen is locked; scripts use private temp files, warn
  when installing from a worktree, and rollback.sh warns that versions
  without access control leave every project open.

Remaining, by design or for later:

- The D-Bus caller check is a hurdle, not a wall: any process of the user can
  pass it (for example with `script` for a terminal). Containers and MCP
  cannot reach it; host processes are the accepted risk.
- The relay is installed editable from the checkout (decided), so changes in
  the checkout take effect at the next restart.
- Another process of the user could own the bus name before the relay; the
  relay then has no control interface and every project stays locked, but the
  extension would show the other process's state.
