"""Keeping the computer from going to sleep while Claude Code works.

Holds a systemd-logind inhibitor by running `systemd-inhibit … sleep infinity`
and ends it to let go, so no library is needed. Where there is no logind
session to inhibit (a server), starting it fails and nothing happens.

Modes: "off"; "running" (while a request runs, and a few minutes after);
"granted" (while any grant is active). "lid" also blocks the lid switch, so
closing the lid only turns the display off. On battery below a threshold the
inhibitor is dropped, so a forgotten setting cannot drain the battery.
"""

from __future__ import annotations

import ctypes
import json
import logging
import signal
import subprocess
from pathlib import Path

log = logging.getLogger("agent_relay_mcp")

MODES = ("off", "running", "granted")
GRACE_SECONDS = 5 * 60
BATTERY_THRESHOLD = 20      # percent
COMMAND = "systemd-inhibit"     # replaced in the tests


def _die_with_parent() -> None:
    """In the child: get SIGTERM when the relay dies, so a crash cannot leave
    the computer unable to sleep."""
    try:
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except OSError:
        pass


def battery(power_dir: Path) -> tuple[bool, int | None]:
    """(on battery, lowest charge in percent) from /sys/class/power_supply."""
    discharging, lowest = False, None
    for supply in power_dir.glob("BAT*"):
        try:
            status = (supply / "status").read_text().strip()
            capacity = int((supply / "capacity").read_text().strip())
        except (OSError, ValueError):
            continue
        discharging = discharging or status == "Discharging"
        lowest = capacity if lowest is None else min(lowest, capacity)
    return discharging, lowest


class KeepAwake:
    def __init__(self, path: Path | None, *, command: str | None = None,
                 power_dir: Path = Path("/sys/class/power_supply"),
                 threshold: int = BATTERY_THRESHOLD, grace: int = GRACE_SECONDS):
        self.path = path
        self.command = command if command is not None else COMMAND
        self.power_dir = power_dir
        self.threshold = threshold
        self.grace = grace
        self.mode = "running"
        self.lid = False
        self.process: subprocess.Popen | None = None
        self.held_what = ""
        self.until = 0.0            # end of the grace time after a run
        self.reason = ""            # why it is not held although wanted
        self._load()

    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
            if raw.get("mode") in MODES:
                self.mode = raw["mode"]
            self.lid = bool(raw.get("lid", False))
        except (OSError, ValueError, AttributeError):
            log.warning("ignoring unreadable %s", self.path)

    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"mode": self.mode, "lid": self.lid}))
        tmp.replace(self.path)

    def configure(self, mode: str, lid: bool) -> None:
        if mode not in MODES:
            raise ValueError(f"Unknown mode {mode!r}: use one of {', '.join(MODES)}.")
        self.mode, self.lid = mode, lid
        self._save()

    @property
    def active(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def update(self, *, running: bool, granted: bool, now: float) -> str:
        """Holds or lets go as needed; returns a note when the battery made it
        let go just now, else ""."""
        if running:
            self.until = now + self.grace
        want = (self.mode == "running" and (running or now < self.until)) or \
            (self.mode == "granted" and granted)
        note = ""
        if want:
            discharging, charge = battery(self.power_dir)
            if discharging and charge is not None and charge < self.threshold:
                if self.active:
                    note = (f"The battery is at {charge} %: the computer may sleep again "
                            "to save it.")
                self.reason = f"battery at {charge} %"
                want = False
            else:
                self.reason = ""
        else:
            self.reason = ""
        what = "idle:sleep" + (":handle-lid-switch" if self.lid else "")
        if want and (not self.active or self.held_what != what):
            self._release()
            self._hold(what)
        elif not want:
            self._release()
        return note

    def _hold(self, what: str) -> None:
        try:
            if not self.command:
                raise OSError("no inhibit command")
            self.process = subprocess.Popen(
                [self.command, f"--what={what}", "--mode=block", "--who=Agent Relay",
                 "--why=Claude Code is working for you", "sleep", "infinity"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, preexec_fn=_die_with_parent)
            self.held_what = what
            log.info("keeping the computer awake (%s)", what)
        except OSError as error:
            self.process = None
            self.reason = f"cannot inhibit sleep: {error}"
            log.warning("cannot keep the computer awake: %s", error)

    def _release(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
            log.info("the computer may sleep again")
        self.process = None
        self.held_what = ""

    def close(self) -> None:
        self._release()

    def state(self) -> dict:
        return {"mode": self.mode, "lid": self.lid, "active": self.active,
                "reason": self.reason, "modes": list(MODES)}

