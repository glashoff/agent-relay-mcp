# Server instance on the V-server: plan (deferred)

Status: deferred. Decided on 2026-11-09 to start with the laptop only; this
part was split off from [access-control-plan.md](access-control-plan.md) and
is kept for later. The relay core is built so that it can run here without
GNOME (see the main plan, section 3).

What the server instance needs from the core, beyond the laptop work: a Unix
socket as a second transport of the control layer (D-Bus needs a desktop
session), `agent-relay-ctl --host vserver` over SSH, a `push` notifier, the
approval web app, and device management (`agent-relay-ctl device …`).

Laptop rules that stay in force: the laptop never accepts an approval that
comes through the V-server or a web app it serves. A "Server" section in the
GNOME menu may show and give grants for the server instance, under these
rules, because the server may be compromised:

- Data from the server is untrusted: plain text only, length limits.
- Laptop and server sections share no buttons; nothing the server sends can
  approve or change anything on the laptop.
- The SSH connection forwards only the server's socket to the laptop
  (`-L`), never anything back (`-R`), no agent forwarding, a key of its
  own.

Open questions for this part: Podman or rootless Docker; how much RAM the
V-server has; host name for the approval page (`freigabe.fkirchhoff.com`?);
push through `kalender-push` or a key of the relay's own; passkey on the
phone or a hardware key.

Order, once taken up: (1) instance with user `relay`, rootless containers,
network and resource limits, Caddy entry, second connector, approvals in
advance from the laptop (CLI and menu section "Server"); (2) phone approval:
approval page and service worker served by the relay, device setup by QR
code, push, passkey check bound to request and duration, confirmation push,
tests including attacks (replayed signature, other duration, used nonce,
wrong origin).

## Isolation

- **Own user** `relay`, no sudo, no access to `/srv/www`, `/srv/kalender-push`
  or certificates.
- **Rootless containers** (Podman, or rootless Docker): rootful Docker is as
  good as root, and the server also runs the websites. The dev container CLI
  works with Podman (`--docker-path podman`). Escaping a container gives at
  most the user `relay`.
- **No host projects**: `allow_host_projects` is off and cannot be turned on
  for this instance; the grant kind "on the host" does not exist there
  either.
- **Network**: containers reach the internet only; not the server's local
  services (the laptop relay's tunnel port, the push service, admin ports).
  Checked and blocked explicitly.
- **Limits**: CPU, memory and disk per container, so a build does not slow
  down the websites. The server's RAM needs checking; Claude Code with builds
  wants 2-4 GB.
- **Code**: the server clones repositories read-only. Results are fetched on
  the laptop with `git fetch vserver` (SSH remote on the server clone); the
  server never gets push rights on GitHub.
- **Claude login** in the containers with a `CLAUDE_CODE_OAUTH_TOKEN` from
  `claude setup-token`, as on the laptop.

## Two connectors in Claude

- The server instance listens on its own port (e.g. `127.0.0.1:8766`); Caddy
  publishes it under its own host name and secret path.
- New setting `instance_name` ("Laptop", "Server"): goes into the server
  name, the instructions ("This connector works on the V-server. Use it when
  the user says 'on the server' or the laptop is off."), the tool
  descriptions and `list_projects` ("Projects on the server").
- In Claude: two connectors with their own names ("Relay Laptop", "Relay
  Server"); their tools are named apart, and either can be switched off per
  chat.

## Approvals

1. **In advance from the laptop**: the "Server" section of the GNOME menu, or
   `agent-relay-ctl --host vserver grant …` (SSH with the laptop's key). For
   trips: approve before leaving; nothing to confirm while driving.
2. **By phone: push message plus approval web app** (built with the server
   instance).

### Is the web app acceptable on the server?

The claim: a compromised server controls its own instance anyway, so a web
app on it adds no risk. **For a full compromise (root on the V-server) that
is right**: the attacker can change grants, the relay code and the containers
directly; forging an approval gains nothing. It holds only if three further
points are kept, because there are smaller compromises than root, and the
web app is new surface:

1. **Partial compromises must not approve.** Some things on the server can be
   changed from a dev container, so effectively by Claude:
   - The push service `kalender-push`: its code is uploaded from the
     fkirchhoff.com dev container (`upload.py`, `upload-paths.txt`) and the
     container runs it with `node --watch`. Whoever drives Claude Code in that
     project controls the push service, its VAPID key and its subscriptions.
   - Everything on the origin `https://fkirchhoff.com` under the uploaded
     paths (JavaScript of the family calendar).

   Therefore:
   - **The approval page is served by the relay itself**, on its own host
     name (e.g. `freigabe.fkirchhoff.com`), from files no container can
     write.
   - **The passkey's relying party ID is exactly that host name.** Pages on
     `fkirchhoff.com` cannot use it: a page may only ask for passkeys of its
     own host name or a parent of it.
   - **The push service is an untrusted transport.** It may delay, drop,
     read or fake notifications; none of that can approve.
2. **New surface.** The page is reachable from the internet and runs as user
   `relay`. A bug there could become a way into the server instance, and with
   it the Claude token in its containers. Keep it minimal: few routes, strict
   input checks, rate limits, a strict Content Security Policy, no third-party
   scripts.
3. **Nothing of it may count on the laptop**, now or later. The laptop never
   accepts passkeys of `freigabe.fkirchhoff.com`, push messages from the
   server or approvals relayed by it (decision 1). The phone approval for the
   laptop will use keys the server never sees.

### How a phone approval works (server instance)

- **Setting up a phone** (once): `agent-relay-ctl --host vserver device add
  "Pixel"` prints a QR code with a one-time setup link (valid 10 min). The
  phone opens it, creates a passkey (fingerprint) and subscribes to push. The
  server stores the public key, the credential ID and the subscription. New
  devices only come this way, over SSH, never over MCP.
- **Request**: Claude sends a message to a locked project. Claude's reply says
  "waiting for approval on your phone, number 47". A push message arrives:
  "Server: approve 'webgame'? No. 47" with the start of the text, and two
  buttons:
  - "Deny": sends a one-time token from the push payload, no passkey; denying
    only makes things safer.
  - "Review": opens the approval page.
- **Approval page** (served by the relay): instance, project, matching number,
  the full message as plain text, its time, a duration (15 min, 1 h, 4 h
  within the limits); "Allow", "Deny", "Deny and lock everything".
- **Allow**: a passkey assertion with user verification. The WebAuthn
  challenge is a hash over request ID, project, a hash of the message text,
  the chosen duration and a nonce valid for 5 min. The relay checks
  signature, origin, relying party ID, user verification, the challenge
  binding, that the nonce is unused, and the signature counter where the
  device keeps one. Only then does the grant exist.
- **Confirmation push**: "Approved: webgame until 15:30", also shown in the
  GNOME menu, so an approval you did not give stands out.
- **Why an attacker with the Claude account cannot approve**: no MCP tool,
  container or connector can produce a WebAuthn signature. The key is in the
  phone's secure hardware and needs a fingerprint. A fake push, for example a
  calendar reminder with a misleading title, opens the calendar, not the
  approval page. A signature fits exactly one request, duration and moment.
  Devices are added over SSH only. What remains is deception (a request of
  theirs at the moment you expect your own); the matching number, the full
  text, one open request per project, and treating any unexpected request as
  an alarm counter it.
- **Remaining risks**: Android passkeys sync through the Google password
  manager (a hardware key over NFC as the only device is stricter, and less
  convenient). If a phone is lost, remove it from the laptop with
  `device remove`. While driving, approve in advance instead.

### Using the existing push service of fkirchhoff.com

`kalender-push` (Node, in a container on the Docker network `caddy_web`)
already holds a VAPID key, sends Web Push, and cleans up subscriptions that
answer 410. Reusing it as the **sender only**:

- **Subscription on the approval origin.** The phone subscribes in the
  service worker of `freigabe.fkirchhoff.com` (served by the relay), with
  the VAPID public key of `kalender-push`. The subscription and its keys stay
  with the relay; they are not added to the calendar's person and device
  list, so calendar reminders never go there and the kitchen tablet never
  gets approval messages.
- **Internal send interface.** `kalender-push` gets one more endpoint, "send
  this payload to this subscription", on a Unix socket in a directory shared
  with the user `relay` (for example `/run/kalender-push/send.sock`, group
  `relay`), plus a token. It is not on a TCP port and not routed by Caddy;
  the rootless containers of the relay cannot reach it.
- **What goes through it**: project, matching number, the first 200
  characters, the request ID and the one-time deny token. Nothing that can
  approve. `kalender-push` encrypts the payload, so it sees it in plain text.
  As said above, its code is under the control of the fkirchhoff.com
  container, so it is an untrusted transport: at worst it drops or delays
  messages (then approve in advance or from the laptop) or shows fake ones,
  which cannot approve anything.
- **What it saves**: one push stack and one VAPID key to look after. **The
  alternative**, the relay sending Web Push itself with a key of its own
  (`pywebpush`, about 30 lines), avoids the dependency on code that the
  fkirchhoff.com container can change. Both are acceptable, because the push
  carries no trust; reuse is the default, as asked.
- **Changes in fkirchhoff.com**: the send endpoint and the socket in
  `kalender-push`, a socket directory and group in `deploy.py`, a Caddy site
  for `freigabe.fkirchhoff.com` that forwards to the relay (in the protected
  Caddy file, applied with `deploy.py` from the host).
