# Approving laptop requests from the phone: the options

Status: comparison for a later decision; nothing of this is built. Written
2026-11-09. Background: [access-control-plan.md](access-control-plan.md).

## What this is about

At first, requests to the laptop are approved only at the laptop, in the GNOME
menu. Later it should also work from the phone, for example while you are in
another room or away. One rule stays: **a compromised V-server must not be able
to approve anything on the laptop.** That rules out the simplest solution, a web
page on the V-server, and is the reason for this comparison.

## Three questions behind every option

Every way to approve from the phone has three parts. For each one, ask who
could abuse it.

1. **The way there**: how the request reaches the phone and the answer gets
   back to the laptop (internet, V-server, a VPN, a push service). Whoever
   controls the way can always delay or drop messages. The question is
   whether they can also **read or change** them.
2. **The display**: the program that shows you "Project X wants to do Y" and
   has the "Allow" button. **Whoever supplies this program decides what you
   see.** It could show you something harmless while you actually approve
   something else.
3. **The proof**: what tells the laptop that you approved and not someone
   else. In practice that is a key on the phone, unlocked by fingerprint or
   PIN, whose public half the laptop knows.

A good solution keeps parts 2 and 3 away from anyone who might be
compromised: the V-server, a company, the network. Part 1 may run through
untrusted hands, as long as they can only delay.

## Why a web app from the V-server is not enough for the laptop

A web app is a page the browser loads from a server. Even when installed as
an app on the home screen (a PWA), **the browser keeps fetching its code from
the server**: the page itself on every start, and the service worker (the
background part) is checked for updates at least every 24 hours. So the
server supplies the display (part 2) again and again. A compromised V-server
could:

- show you a harmless request while you approve another one that someone
  sent through your Claude account, and the fingerprint prompt looks the same
  either way;
- during setup, hand the laptop a key the server holds instead of the one on
  your phone; Android passkeys usually come without a manufacturer
  certificate, so the laptop cannot tell the difference;
- since the passkey belongs to the server's domain, use it later for anything
  on that domain.

On the server instance this does not matter: a compromised server controls
its own instance anyway. For the laptop it would make the V-server the
weakest point, so the web app stays limited to the server instance.

## The options

### A. Tailscale with Tailnet Lock and ACLs

**What it is**: Tailscale builds a private network ("tailnet") between your
devices, wherever they are, over encrypted WireGuard connections. The phone
reaches the laptop directly, even on mobile data and behind any router.
Tailscale, the company, runs a coordination server that tells the devices
about each other.

**How approval would work**: the relay serves a small approval page **from
the laptop itself**, reachable only inside the tailnet. The page has a proper
HTTPS certificate (`tailscale cert`), which passkeys require. The phone opens
it, you check the request and confirm with a passkey. A push message ("there
is a request, open the page") may come through any untrusted service; it
carries no trust.

- **Tailnet Lock**: new devices are only accepted into your network if one
  of your own devices signs them. Without it, Tailscale, or someone who takes
  over your Tailscale login, could quietly add a device. With it, they
  cannot.
- **ACLs** (access rules): the phone may reach only the laptop's approval
  port, nothing else; the laptop needs no access to the phone; other devices
  get nothing.

**Advantages**
- Little work: apps for Android and Linux exist, setup takes an hour.
- Works from anywhere, no router or port configuration.
- The display comes from the laptop, so the V-server is not involved at all.
- Standard browser and passkey, no app of our own.

**Disadvantages**
- You depend on a company and on the account you sign in to Tailscale with
  (Google, GitHub, …). Tailnet Lock removes the main risk, adding devices;
  you still trust Tailscale's software on both devices and the service
  staying available.
- The HTTPS name is issued through Tailscale's DNS. In theory Tailscale could
  get a certificate for it, but with Tailnet Lock it cannot steer your
  traffic to itself.
- Android allows only one VPN at a time, so Tailscale takes that slot.
- Running the coordination server yourself (Headscale) on the V-server would
  bring the V-server back into the trust chain, so that is not an option.

### B. Plain WireGuard

**What it is**: the same encryption as Tailscale, set up by hand, without a
company in between.

**The catch**: laptop and phone are both behind routers (the phone usually
behind the mobile operator's), so they cannot reach each other directly.
They need a meeting point with a public address: the V-server. For the
server to stay harmless, it may only **forward encrypted packets**, not take
part in the encryption itself. That means routing rules on the server and on
the laptop. The approval page also needs HTTPS for passkeys; without a public
name that means a certificate authority of your own, installed on the phone.

**Advantages**
- No third party; small and well-reviewed technology.
- The server only forwards and can block, nothing else.

**Disadvantages**
- Setup and troubleshooting need real network knowledge: keys, addresses,
  routing, firewall, your own certificates.
- A mistake in the forwarding easily makes the server a full participant
  again, without anything noticeable going wrong.
- Same VPN-slot limitation on Android.
- Little gain over Tailscale with Tailnet Lock, for considerably more work.

### C. Reverse tunnel over the V-server with TLS passthrough

**What it is**: like today's connector, the laptop keeps a tunnel open to the
V-server, and the server passes connections to a name of its own, for example
`laptop-approval.fkirchhoff.com`, on to the laptop. Unlike today, the server
does **not** decrypt them (TLS passthrough). The HTTPS connection ends on the
laptop, which holds the certificate.

**What has to be right**:
- The V-server's front end must forward by name without decrypting. Caddy
  needs an extra module for that, or another program in front of all
  websites. This touches every site on the server.
- **The certificate**: normally whoever controls the server for a name can
  get a certificate for it, which means a compromised V-server could again
  pretend to be the laptop. Prevented by:
  - the laptop getting its certificate through a DNS check (`dns-01`), with
    its own access to the DNS provider;
  - a CAA record in DNS that allows certificates for this name only from
    this one account and only by DNS check;
  - optionally, watching the public certificate logs for strangers.
- If the DNS of fkirchhoff.com were managed on the V-server itself, this does
  not work.

**Advantages**
- No company in between, no VPN on the phone, a normal browser and passkey.
- The display comes from the laptop; the server only passes encrypted data
  through.

**Disadvantages**
- Changes the web front of the V-server for all sites.
- Its safety depends entirely on the certificate setup (DNS access, CAA);
  that is easy to get subtly wrong.
- A compromised server can still block, and see when you approve.

### D. Native Android app with signed approvals

**What it is**: a small app of our own on the phone. It is installed once and
**only changes when you install a new version**. No server delivers its code.

**How it works**:
1. **Pairing via QR code**: the GNOME menu shows a QR code with the laptop's
   public key and a one-time secret. The app scans it and creates a key of
   its own in the phone's secure hardware (Android Keystore), usable only
   after fingerprint or PIN. It sends its public key back, protected by the
   one-time secret. That secret travelled only optically from screen to
   camera, so the V-server cannot slip in a key of its own.
2. **Request**: the laptop **signs** the request (project, text, number) and
   puts it in a "mailbox" on the V-server. A push message wakes the app.
   The app checks the laptop's signature, so the server cannot invent or
   change requests.
3. **Approval**: you read the request in the app, choose a duration and
   confirm with your fingerprint. The phone signs exactly this request and
   duration, and the answer goes back through the mailbox.
4. **Laptop**: checks the signature with the phone's stored public key, and
   only then grants.

**Push for waking the app**:
- **UnifiedPush with ntfy**: no Google. ntfy can run on the V-server (or use
  ntfy.sh); the phone needs the ntfy app as a distributor. Delivery depends
  on Android letting ntfy keep its connection (battery settings).
- **FCM (Firebase Cloud Messaging)**: Google's push, the most reliable and
  battery-friendly on Android; needs a Firebase project. Since the push only
  says "something new", Google learns nothing important.

Either way the push and the mailbox are untrusted: they can delay, never
forge.

**Advantages**
- The strongest option. Neither the V-server, nor a company, nor the network
  has to be trusted for anything but delivery.
- No VPN, works anywhere, nothing changes on the server's web front.
- The same app can later approve for the server instance as well and replace
  its web app.

**Disadvantages**
- The most work: an app of our own (Kotlin), with QR scanner, Keystore,
  fingerprint prompt, push integration and a little UI. It also needs looking
  after when Android changes.
- Distribution: installing your own APK outside the Play Store. Google is
  tightening this for certified Android devices (developer verification,
  rolling out from 2026); the current rules need checking before starting.
- Updates are deliberate. That is a feature, but also a chore.

### E. Web app from the V-server (for comparison)

Covered above: the server supplies the display every time it is opened, so a
compromised server can make you approve something other than what you see.
**Fine for the server instance, not for the laptop.**

## Overview

| | A. Tailscale + Lock + ACLs | B. Plain WireGuard | C. Tunnel + TLS passthrough | D. Native app, signed | E. Web app from V-server |
|---|---|---|---|---|---|
| Can a compromised V-server approve? | No | No (if forwarding only) | No (if certificate setup is right) | No | **Yes** |
| Who supplies the display | Laptop | Laptop | Laptop | App on the phone | V-server |
| Third party to trust | Tailscale (reduced by Lock) | None | DNS provider, certificate authority | Push service, for delivery only | V-server |
| Works on mobile data | Yes | Yes, through the server | Yes | Yes | Yes |
| VPN on the phone | Yes | Yes | No | No | No |
| Setup work | Low | High | High | Highest (build an app) | Low |
| Ongoing care | Low | Medium | Medium | Medium | Low |
| Risk of subtle setup mistakes | Low | High | High | Medium | n/a |

## Recommendation

1. **Goal: D, the native Android app with signed approvals.** It is the only
   option in which nothing outside your two devices has to be trusted, and it
   can later serve the server instance too. Push through FCM for reliability,
   or UnifiedPush/ntfy if Google should stay out; the mailbox can be a small
   endpoint of the server instance.
2. **If phone approval for the laptop is wanted before the app exists: A,
   Tailscale with Tailnet Lock and ACLs**, with the approval page served by
   the laptop and confirmed by passkey. It is quick to set up and safe enough,
   at the price of depending on a company. Later, D can replace it.
3. **Not recommended**: B (much effort, little gain over A), C (changes the
   whole web front and hinges on a fragile certificate setup), E for the
   laptop (by design).

Until then, the GNOME menu (including approvals in advance and keeping the
laptop awake) covers the laptop.
