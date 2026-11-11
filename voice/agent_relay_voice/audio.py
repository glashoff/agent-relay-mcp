"""Audio in and out through the host's PipeWire pulse socket.

Uses parec/pacat (pulseaudio-utils) as subprocesses: they speak the pulse
protocol, follow the host's default source and sink (or PULSE_SOURCE /
PULSE_SINK), and a stuck or dead stream is just a process to restart or kill.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
import wave
from pathlib import Path
from typing import Callable

import numpy as np

log = logging.getLogger("voice.audio")

CAPTURE_RATE = 16000
BLOCK_SAMPLES = 1600          # 100 ms
CLIENT_NAME = "agent-relay-voice"


class Capture:
    """Reads 16 kHz mono int16 blocks of 100 ms from the default source and
    calls on_block(block) in its own thread. Restarts parec when it dies;
    on_error(text or None) reports whether capture works."""

    def __init__(self, on_block: Callable[[np.ndarray], None],
                 on_error: Callable[[str | None], None] = lambda e: None,
                 command: list[str] | None = None) -> None:
        self.on_block = on_block
        self.on_error = on_error
        self.command = command or [
            "parec", "--raw", "--format=s16le", f"--rate={CAPTURE_RATE}", "--channels=1",
            "--latency-msec=50", f"--client-name={CLIENT_NAME}", "--stream-name=microphone",
        ]
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._thread = threading.Thread(target=self._run, name="capture", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()
        self._thread.join(timeout=3)

    def _run(self) -> None:
        delay = 1.0
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self._proc = subprocess.Popen(self.command, stdout=subprocess.PIPE,
                                              stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
            except OSError as error:
                self.on_error(f"Cannot start audio capture: {error}")
                self._stop.wait(5)
                continue
            got_audio = False
            nbytes = BLOCK_SAMPLES * 2
            stdout = self._proc.stdout
            while not self._stop.is_set():
                data = stdout.read(nbytes)
                if not data or len(data) < nbytes:
                    break
                if not got_audio:
                    got_audio = True
                    delay = 1.0
                    self.on_error(None)
                self.on_block(np.frombuffer(data, dtype="<i2").copy())
            if self._stop.is_set():
                break
            self._proc.kill()
            err = (self._proc.stderr.read() or b"").decode(errors="replace").strip()
            self._proc.wait()
            message = err.splitlines()[-1] if err else f"parec exited with {self._proc.returncode}"
            log.warning("capture stopped: %s", message)
            self.on_error(f"Microphone not available: {message}")
            if time.monotonic() - started > 30:
                delay = 1.0
            self._stop.wait(delay)
            delay = min(delay * 2, 15.0)


def to_int16(audio: np.ndarray, gain_db: float = 0.0) -> bytes:
    if gain_db:
        audio = audio * (10 ** (gain_db / 20))
    return (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes()


class Playback:
    """One output stream (pacat) for a run of sentences at one sample rate.
    write() blocks while the server's buffer is full, which paces synthesis
    against playback. abort() stops at once (barge-in), finish() lets the
    queued audio play out."""

    def __init__(self, rate: int, out_file: Path | None = None) -> None:
        self.rate = rate
        self._lock = threading.Lock()
        self._aborted = False
        self._wav = None
        self._proc = None
        if out_file is not None:
            self._wav = wave.open(str(out_file), "wb")
            self._wav.setnchannels(1)
            self._wav.setsampwidth(2)
            self._wav.setframerate(rate)
        else:
            self._proc = subprocess.Popen(
                ["pacat", "--playback", "--raw", "--format=s16le", f"--rate={rate}",
                 "--channels=1", "--latency-msec=150", f"--client-name={CLIENT_NAME}",
                 "--stream-name=speech"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    def write(self, pcm: bytes) -> bool:
        """False once aborted or when the player died."""
        if self._aborted:
            return False
        if self._wav is not None:
            self._wav.writeframes(pcm)
            return True
        try:
            self._proc.stdin.write(pcm)
            self._proc.stdin.flush()
            return True
        except (BrokenPipeError, ValueError, OSError):
            if not self._aborted:
                err = (self._proc.stderr.read() or b"").decode(errors="replace").strip()
                log.warning("playback failed: %s", err or "pacat exited")
            return False

    def finish(self, timeout: float = 120.0) -> None:
        if self._wav is not None:
            self._wav.close()
            return
        try:
            self._proc.stdin.close()
        except OSError:
            pass
        try:
            self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._proc.kill()

    def abort(self) -> None:
        with self._lock:
            self._aborted = True
            if self._wav is not None:
                try:
                    self._wav.close()
                except Exception:  # noqa: BLE001 - closing twice is harmless
                    pass
                return
            if self._proc.poll() is None:
                self._proc.kill()
