"""Persistent settings of the voice service: one JSON file in the mounted
config directory (default /config/settings.json in the container, which is
~/.config/agent-relay-voice/settings/ on the host). Missing keys take their
defaults, unknown keys are kept out, values of the wrong type are replaced by
the default with a warning, so a hand-edited file cannot stop the service."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path

from .textproc import DEFAULT_DICTIONARY

log = logging.getLogger("voice.settings")

# What helped Whisper most in test/: an initial prompt with the terms in
# context, plus hotwords. Project names from the relay are added at runtime.
DEFAULT_PROMPT = (
    "Ich arbeite mit Claude Code im Dev-Container. Mach einen Commit auf dem "
    "Branch und öffne einen Pull Request. Der Caddy Reverse Proxy läuft, "
    "nach dem Deploy teste ich den MCP-Server."
)
DEFAULT_HOTWORDS = [
    "Claude Code", "Dev-Container", "Commit", "Pull Request", "Branch", "Merge",
    "Rebase", "Caddy", "Reverse Proxy", "Deploy", "MCP-Server", "GitHub", "Docker",
    "Webhooks", "Linter", "Relay",
]

DEFAULTS: dict = {
    # engines (ids from `options`)
    "stt": "large-v3-turbo",
    "tts": "pocket-german-24l",
    "claude_model": "",               # "": Claude Code's own default
    # listening switched on or off inside a running service
    "listening": True,
    # turn-taking
    "vad_threshold": 0.5,             # Silero speech probability
    "vad_silence_s": 0.8,             # silence that ends an utterance
    "min_utterance_s": 0.3,           # shorter noises are ignored
    "max_utterance_s": 60.0,          # an utterance is cut here
    # mute detection (Jabra Link 380: boom up = RMS ~0.5 with 75 % zeros)
    "mute_rms": 1.5,
    "mute_zero_fraction": 0.5,
    "mute_hold_s": 0.3,
    # recognition
    "stt_language": "de",
    "stt_beam_size": 5,
    "stt_prompt": DEFAULT_PROMPT,
    "hotwords": DEFAULT_HOTWORDS,
    "hotwords_from_projects": True,
    # speech output
    "pronunciation": DEFAULT_DICTIONARY,   # Piper only
    "pocket_gain_db": 12.0,                # Pocket TTS is ~12 dB quieter
    "pocket_voice": "juergen",
    # relay events that become notices for the instance
    "notice_events": ["run_ended", "request_added"],
}


def _type_ok(key: str, value) -> bool:
    default = DEFAULTS[key]
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, float):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if isinstance(default, int):
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(default, list):
        return isinstance(value, list) and all(isinstance(v, str) for v in value)
    return isinstance(value, type(default))


class Settings:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.values = dict(DEFAULTS)
        if path and path.is_file():
            try:
                raw = json.loads(path.read_text())
            except (OSError, ValueError) as error:
                log.warning("%s unreadable (%s); using defaults", path, error)
                raw = {}
            if not isinstance(raw, dict):
                log.warning("%s is not a JSON object; using defaults", path)
                raw = {}
            for key, value in raw.items():
                if key not in DEFAULTS:
                    log.warning("%s: unknown key %r ignored", path, key)
                elif not _type_ok(key, value):
                    log.warning("%s: %s has the wrong type, using the default", path, key)
                else:
                    self.values[key] = float(value) if isinstance(DEFAULTS[key], float) else value

    def __getitem__(self, key: str):
        return self.values[key]

    def update(self, **changes) -> None:
        for key, value in changes.items():
            if key not in DEFAULTS:
                raise KeyError(key)
            if not _type_ok(key, value):
                raise TypeError(f"{key} has the wrong type")
            self.values[key] = float(value) if isinstance(DEFAULTS[key], float) else value
        self.save()

    def save(self) -> None:
        if not self.path:
            return
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".settings-")
                with os.fdopen(fd, "w") as f:
                    json.dump(self.values, f, indent=2, ensure_ascii=False)
                    f.write("\n")
                os.chmod(tmp, 0o600)
                os.replace(tmp, self.path)
            except OSError as error:
                log.warning("could not save settings to %s: %s", self.path, error)
