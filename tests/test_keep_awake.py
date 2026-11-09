"""Keeping the computer awake, with a stand-in for systemd-inhibit that only
records how it was called, and a made-up battery."""

import sys
import time

import pytest

from agent_relay_mcp.keep_awake import KeepAwake


@pytest.fixture
def fake(tmp_path):
    log = tmp_path / "inhibit.log"
    script = tmp_path / "fake-inhibit"
    script.write_text(f"#!/bin/sh\necho \"$@\" >> {log}\nexec sleep 60\n")
    script.chmod(0o755)
    power = tmp_path / "power"
    (power / "BAT0").mkdir(parents=True)
    battery = power / "BAT0"
    (battery / "status").write_text("Charging\n")
    (battery / "capacity").write_text("80\n")

    def make(**kwargs):
        return KeepAwake(tmp_path / "keep_awake.json", command=str(script),
                         power_dir=power, **kwargs)
    return make, log, battery


def calls(log):
    for _ in range(50):
        if log.exists():
            break
        time.sleep(0.02)
    return log.read_text().splitlines() if log.exists() else []


def test_held_while_running_and_for_the_grace_time(fake):
    make, log, _ = fake
    k = make(grace=300)
    k.update(running=True, granted=False, now=1000)
    assert k.active
    assert "--what=idle:sleep" in calls(log)[0] and "--mode=block" in calls(log)[0]
    k.update(running=False, granted=False, now=1200)          # within the grace time
    assert k.active
    k.update(running=False, granted=False, now=1400)
    assert not k.active
    k.close()


def test_modes_and_the_lid(fake):
    make, log, _ = fake
    k = make()
    k.configure("off", False)
    k.update(running=True, granted=True, now=0)
    assert not k.active
    k.configure("granted", True)
    k.update(running=False, granted=True, now=0)
    assert k.active and "handle-lid-switch" in calls(log)[-1]
    k.update(running=False, granted=False, now=0)
    assert not k.active
    with pytest.raises(ValueError):
        k.configure("always", False)
    # The setting is kept.
    assert make().mode == "granted" and make().lid


def test_a_low_battery_lets_it_sleep(fake):
    make, _, battery = fake
    k = make()
    k.update(running=True, granted=False, now=0)
    assert k.active
    (battery / "status").write_text("Discharging\n")
    (battery / "capacity").write_text("15\n")
    note = k.update(running=True, granted=False, now=1)
    assert not k.active and "15 %" in note and "battery" in k.state()["reason"]
    (battery / "status").write_text("Charging\n")
    k.update(running=True, granted=False, now=2)
    assert k.active
    k.close()


def test_without_an_inhibit_command_nothing_happens(tmp_path):
    k = KeepAwake(None, command="")
    k.update(running=True, granted=True, now=0)
    assert not k.active and "cannot inhibit" in k.state()["reason"]


def test_the_inhibitor_ends_with_the_relay(fake, tmp_path):
    import subprocess
    make, log, _ = fake
    code = (f"import sys; sys.path.insert(0, {str(tmp_path)!r});"
            "from agent_relay_mcp.keep_awake import KeepAwake;"
            "from pathlib import Path;"
            f"k = KeepAwake(None, command={str(tmp_path / 'fake-inhibit')!r},"
            f" power_dir=Path({str(tmp_path / 'power')!r}));"
            "k.update(running=True, granted=False, now=0); print(k.process.pid, flush=True);"
            "import os; os._exit(0)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    pid = int(out.stdout.split()[0])
    for _ in range(100):
        try:
            with open(f"/proc/{pid}/stat") as f:
                if f.read().split(") ")[1][0] == "Z":
                    break
        except FileNotFoundError:
            break
        time.sleep(0.05)
    else:
        raise AssertionError("the inhibitor outlived the relay")
