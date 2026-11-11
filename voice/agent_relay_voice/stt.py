"""Speech recognition engines, all on the CPU (see test/README.md for the
measurements behind the choices): faster-whisper with int8 and half the
logical CPUs as threads (6 on the i7-10750H was fastest), and whisper.cpp's
CPU build as a whisper-server subprocess that keeps its model in memory.

Models are downloaded on first use into the Hugging Face cache in the models
volume (HF_HOME).
"""

from __future__ import annotations

import io
import json
import logging
import os
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
import wave
from pathlib import Path

import numpy as np

log = logging.getLogger("voice.stt")

# id -> description. "repo" for faster-whisper is what WhisperModel() gets:
# a faster-whisper alias or a Hugging Face repo in CTranslate2 format.
MODELS: dict[str, dict] = {
    "large-v3-turbo": {
        "backend": "faster-whisper", "repo": "large-v3-turbo",
        "label": "Whisper large-v3-turbo (faster-whisper)",
    },
    "large-v3-turbo-german": {
        "backend": "faster-whisper", "repo": "jimmymeister/whisper-large-v3-turbo-german-ct2",
        "label": "Whisper large-v3-turbo German fine-tune (faster-whisper)",
    },
    "small": {
        "backend": "faster-whisper", "repo": "small",
        "label": "Whisper small (faster-whisper, fast)",
    },
    "wcpp-large-v3-turbo": {
        "backend": "whisper.cpp", "file": "ggml-large-v3-turbo.bin",
        "label": "Whisper large-v3-turbo f16 (whisper.cpp)",
    },
    "wcpp-large-v3-turbo-q8_0": {
        "backend": "whisper.cpp", "file": "ggml-large-v3-turbo-q8_0.bin",
        "label": "Whisper large-v3-turbo q8_0 (whisper.cpp)",
    },
}
WHISPERCPP_REPO = "ggerganov/whisper.cpp"
WHISPER_SERVER = os.environ.get("WHISPER_SERVER", "/usr/local/bin/whisper-server")
CPU_THREADS = int(os.environ.get("STT_CPU_THREADS", max(1, (os.cpu_count() or 2) // 2)))

# Whisper's well-known inventions on silence and noise; a transcript that is
# only one of these is treated as empty.
HALLUCINATIONS = {
    "untertitel der amara.org-community", "untertitel im auftrag des zdf für funk, 2017",
    "untertitel von stephanie geiges", "vielen dank.", "vielen dank fürs zuschauen.",
    "danke fürs zuschauen.", "tschüss.", "bis zum nächsten mal.", "copyright wdr 2021",
    "swr 2021", "untertitelung des zdf, 2020", "thank you.", "thanks for watching!",
}


def is_hallucination(text: str) -> bool:
    t = " ".join(text.lower().split())
    return t in HALLUCINATIONS or t.startswith("untertitel")


class Engine:
    def transcribe(self, audio: np.ndarray, *, language: str | None, prompt: str | None,
                   hotwords: str | None, beam_size: int) -> str:
        raise NotImplementedError

    def close(self) -> None:
        pass


class FasterWhisperEngine(Engine):
    def __init__(self, repo: str) -> None:
        from faster_whisper import WhisperModel

        self.model = WhisperModel(repo, device="cpu", compute_type="int8", cpu_threads=CPU_THREADS)

    def transcribe(self, audio, *, language, prompt, hotwords, beam_size):
        segments, _info = self.model.transcribe(
            audio, language=language, beam_size=beam_size,
            vad_filter=False,               # the utterance is already cut by our VAD
            initial_prompt=prompt or None, hotwords=hotwords or None,
            condition_on_previous_text=False)
        return " ".join(s.text.strip() for s in segments).strip()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _multipart(fields: dict[str, str], data: bytes) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    out = io.BytesIO()
    for k, v in fields.items():
        out.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    out.write(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.wav"\r\n'
              f"Content-Type: audio/wav\r\n\r\n".encode())
    out.write(data)
    out.write(f"\r\n--{boundary}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={boundary}"


class WhisperCppEngine(Engine):
    """whisper-server (CPU build, pinned v1.9.5) on 127.0.0.1 and a free port.
    Has no hotwords; the initial prompt is passed."""

    START_TIMEOUT = 300

    def __init__(self, filename: str) -> None:
        from huggingface_hub import hf_hub_download

        model = hf_hub_download(WHISPERCPP_REPO, filename)
        self.port = _free_port()
        cmd = [WHISPER_SERVER, "-m", model, "--host", "127.0.0.1", "--port", str(self.port),
               "-t", str(CPU_THREADS), "-fa"]
        log.info("starting %s", " ".join(cmd))
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                     stdin=subprocess.DEVNULL, start_new_session=True)
        self._stderr_tail: list[str] = []
        threading.Thread(target=self._drain, daemon=True).start()
        deadline = time.monotonic() + self.START_TIMEOUT
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"whisper-server exited: {' '.join(self._stderr_tail[-3:])}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=2):
                    return
            except (urllib.error.URLError, OSError):
                time.sleep(0.3)
        self.close()
        raise RuntimeError("whisper-server did not come up")

    def _drain(self) -> None:
        for line in self.proc.stderr:
            self._stderr_tail = (self._stderr_tail + [line.decode(errors="replace").strip()])[-20:]

    def transcribe(self, audio, *, language, prompt, hotwords, beam_size):
        wav = io.BytesIO()
        with wave.open(wav, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
        body, ctype = _multipart({
            "response_format": "json", "language": language or "auto", "prompt": prompt or "",
            "beam_size": str(beam_size), "temperature": "0.0", "no_language_probabilities": "true",
        }, wav.getvalue())
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/inference", data=body,
                                     headers={"Content-Type": ctype}, method="POST")
        timeout = max(120.0, len(audio) / 16000 * 6)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        if "error" in data:
            raise RuntimeError(f"whisper-server: {data['error']}")
        return str(data.get("text", "")).strip()

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(timeout=10)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


def load(model_id: str) -> Engine:
    spec = MODELS[model_id]
    if spec["backend"] == "whisper.cpp":
        if not Path(WHISPER_SERVER).is_file():
            raise RuntimeError("whisper.cpp is not built into this image")
        return WhisperCppEngine(spec["file"])
    return FasterWhisperEngine(spec["repo"])


def options() -> list[dict]:
    return [{"id": k, "label": v["label"]} for k, v in MODELS.items()]


def warmup(engine: Engine) -> None:
    """One short run, so the first real utterance is not slowed by one-time costs."""
    engine.transcribe(np.zeros(16000, dtype=np.float32), language="de", prompt=None,
                      hotwords=None, beam_size=1)
