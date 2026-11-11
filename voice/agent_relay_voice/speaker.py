"""Speech output: a worker thread that synthesizes sentence by sentence and
plays them through one pacat stream per run of sentences.

Engines slower than real time (Pocket TTS 24 layers: RTF about 1.3 on the
i7-10750H) would stutter if every chunk were played as it arrives. So each
sentence holds back just enough audio that playback does not run dry before
synthesis ends: (RTF - 1) x the expected length of the sentence, minus what is
still queued from the sentence before. The RTF is a running estimate per
engine, starting from the engine's expected_rtf.

Every run has a generation number. stop() moves to a new generation and kills
the playing stream, so whatever is still queued or being synthesized for the
old generation is dropped (barge-in, "stop speaking").
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from pathlib import Path
from typing import Callable

from .audio import Playback, to_int16

log = logging.getLogger("voice.speaker")

SECONDS_PER_CHAR = 0.07       # German speech, roughly; only for the pre-buffer estimate


def prebuffer_seconds(rtf: float, text: str, queued_ahead: float) -> float:
    """Audio to hold back before playing a sentence, so it plays without gaps."""
    if rtf <= 0.9:
        return 0.0
    expected = max(0.5, len(text) * SECONDS_PER_CHAR)
    return max(0.0, (rtf - 1.0) * expected * 1.15 + 0.15 - queued_ahead)


class Speaker:
    def __init__(self, on_busy: Callable[[bool], None] = lambda b: None,
                 # called when a run of sentences starts to play: (generation,
                 # seconds from the start of its first synthesis)
                 on_first_audio: Callable[[int, float], None] = lambda g, t: None,
                 out_file: Path | None = None) -> None:
        self.engine = None                    # tts.Engine, set when loaded
        self.on_busy = on_busy
        self.on_first_audio = on_first_audio
        self.out_file = out_file              # write a WAV instead of playing (tests)
        self.generation = 0
        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._playback: Playback | None = None
        self._pending = 0                     # queued items of the current generation
        self._busy = False
        self._rtf: dict[int, float] = {}       # id(engine) -> running real-time factor
        self._ends_at = 0.0                   # when the audio written so far has played
        self._thread = threading.Thread(target=self._run, name="speaker", daemon=True)
        self._thread.start()

    @property
    def busy(self) -> bool:
        return self._busy

    def _set_busy(self, busy: bool) -> None:
        if busy != self._busy:
            self._busy = busy
            self.on_busy(busy)

    def say(self, text: str, generation: int | None = None) -> None:
        with self._lock:
            gen = self.generation if generation is None else generation
            if gen != self.generation:
                return
            self._pending += 1
            self._set_busy(True)
        self._queue.put(("say", gen, text))

    def end(self, generation: int | None = None) -> None:
        """No more sentences in this run: let the stream play out."""
        with self._lock:
            gen = self.generation if generation is None else generation
            if gen != self.generation:
                return
            self._pending += 1
            self._set_busy(True)
        self._queue.put(("end", gen, ""))

    def stop(self) -> int:
        """Drops everything queued and stops playback at once; returns the new generation."""
        with self._lock:
            self.generation += 1
            self._pending = 0
            playback, self._playback = self._playback, None
            self._set_busy(False)
        if playback:
            playback.abort()
        return self.generation

    def close(self) -> None:
        self.stop()
        self._queue.put(None)

    def _done_one(self, gen: int) -> None:
        with self._lock:
            if gen == self.generation:
                self._pending = max(0, self._pending - 1)
                if self._pending == 0 and self._playback is None:
                    self._set_busy(False)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            kind, gen, text = item
            try:
                if gen != self.generation:
                    continue
                if kind == "say":
                    self._speak(gen, text)
                else:
                    with self._lock:
                        playback, self._playback = self._playback, None
                    if playback:
                        playback.finish()
            except Exception:  # noqa: BLE001 - a TTS failure must not end the thread
                log.exception("speech output failed")
                with self._lock:
                    playback, self._playback = self._playback, None
                if playback:
                    playback.abort()
            finally:
                self._done_one(gen)

    def _speak(self, gen: int, text: str) -> None:
        engine = self.engine
        if engine is None:
            log.warning("no speech output engine loaded; not saying %r", text[:80])
            return
        t0 = time.perf_counter()
        rtf = self._rtf.get(id(engine), getattr(engine, "expected_rtf", 0.5))
        need = prebuffer_seconds(rtf, text, max(0.0, self._ends_at - time.monotonic()))
        prebuffer = need
        held: list = []
        held_s = 0.0
        audio_s = 0.0
        for chunk in engine.synthesize(text):
            if gen != self.generation:
                return
            held.append(chunk)
            held_s += len(chunk) / engine.sample_rate
            audio_s += len(chunk) / engine.sample_rate
            if held_s >= need:
                if not self._write(gen, engine, held, t0):
                    return
                held, held_s, need = [], 0.0, 0.0
        if held and not self._write(gen, engine, held, t0):
            return
        synth_s = time.perf_counter() - t0
        if audio_s > 0.3:
            self._rtf[id(engine)] = 0.6 * rtf + 0.4 * (synth_s / audio_s)
        log.info("said %.1f s of audio, synthesis %.2f s (RTF %.2f, pre-buffer %.1f s): %s",
                 audio_s, synth_s, synth_s / max(audio_s, 1e-6), prebuffer, text[:100])

    def _write(self, gen: int, engine, chunks: list, t0: float) -> bool:
        with self._lock:
            if gen != self.generation:
                return False
            playback = self._playback
            new_run = playback is None
            if playback is None or playback.rate != engine.sample_rate:
                if playback is not None:
                    playback.finish()
                playback = self._playback = Playback(engine.sample_rate, self.out_file)
                self._ends_at = 0.0
        if new_run:
            self.on_first_audio(gen, time.perf_counter() - t0)
        for chunk in chunks:
            duration = len(chunk) / engine.sample_rate
            now = time.monotonic()
            self._ends_at = max(now, self._ends_at) + duration
            if not playback.write(to_int16(chunk, engine.gain_db)):
                return False
        return True
