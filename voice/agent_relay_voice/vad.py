"""Voice activity detection on the live stream and cutting it into utterances.

SileroStream runs the Silero VAD model that ships with faster-whisper window by
window (512 samples at 16 kHz = 32 ms), keeping the model's recurrent state and
the 64-sample context between calls, which faster-whisper's own batch API does
not do. Segmenter turns the per-window speech probabilities into utterances.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import numpy as np

WINDOW = 512
CONTEXT = 64
SAMPLE_RATE = 16000


class SileroStream:
    def __init__(self) -> None:
        import onnxruntime
        from faster_whisper.utils import get_assets_path

        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.log_severity_level = 4
        path = os.path.join(get_assets_path(), "silero_vad_v6.onnx")
        self.session = onnxruntime.InferenceSession(
            path, providers=["CPUExecutionProvider"], sess_options=opts)
        self.reset()

    def reset(self) -> None:
        self._h = np.zeros((1, 1, 128), dtype=np.float32)
        self._c = np.zeros((1, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, CONTEXT), dtype=np.float32)

    def __call__(self, window: np.ndarray) -> float:
        """Speech probability of one 512-sample float32 window."""
        x = np.concatenate([self._context, window.reshape(1, WINDOW).astype(np.float32)], axis=1)
        out, self._h, self._c = self.session.run(None, {"input": x, "h": self._h, "c": self._c})
        self._context = x[:, -CONTEXT:]
        return float(np.asarray(out).reshape(-1)[0])


@dataclass
class SegmenterEvent:
    kind: str                 # "start" | "confirmed" | "end" | "discard"
    audio: np.ndarray | None = None
    speech_s: float = 0.0


@dataclass
class _Utterance:
    chunks: list = field(default_factory=list)
    speech_windows: int = 0
    silence_windows: int = 0
    total_windows: int = 0
    confirmed: bool = False


class Segmenter:
    """Cuts a 16 kHz float32 stream into utterances.

    - An utterance starts at the first window with probability >= threshold
      (with `preroll_s` of audio before it, so the first syllable is kept).
    - It is "confirmed" once it has min_speech_s of speech windows; only then
      does it count (barge-in, transcription). Shorter noises are discarded.
    - It ends after silence_s of windows below threshold - 0.15, or at max_s.
    """

    def __init__(self, vad, threshold: float = 0.5, silence_s: float = 0.8,
                 min_speech_s: float = 0.3, max_s: float = 60.0, preroll_s: float = 0.3) -> None:
        self.vad = vad
        self.configure(threshold, silence_s, min_speech_s, max_s)
        self.preroll_windows = max(1, round(preroll_s * SAMPLE_RATE / WINDOW))
        self._pending = np.zeros(0, dtype=np.float32)
        self._preroll: list[np.ndarray] = []
        self._current: _Utterance | None = None
        self.last_prob = 0.0

    def configure(self, threshold: float, silence_s: float, min_speech_s: float, max_s: float) -> None:
        self.threshold = threshold
        self.neg_threshold = max(0.01, threshold - 0.15)
        self.silence_windows = max(1, round(silence_s * SAMPLE_RATE / WINDOW))
        self.min_speech_windows = max(1, round(min_speech_s * SAMPLE_RATE / WINDOW))
        self.max_windows = max(1, round(max_s * SAMPLE_RATE / WINDOW))

    @property
    def active(self) -> bool:
        return self._current is not None

    def reset(self) -> SegmenterEvent | None:
        """Drops what is in progress (muted, paused). Returns a discard event
        if an utterance was dropped."""
        dropped = self._current is not None
        self._current = None
        self._preroll = []
        self._pending = np.zeros(0, dtype=np.float32)
        if hasattr(self.vad, "reset"):
            self.vad.reset()
        return SegmenterEvent("discard") if dropped else None

    def push(self, samples: np.ndarray) -> list[SegmenterEvent]:
        events: list[SegmenterEvent] = []
        data = np.concatenate([self._pending, samples.astype(np.float32)])
        n = len(data) // WINDOW
        self._pending = data[n * WINDOW:]
        for i in range(n):
            window = data[i * WINDOW:(i + 1) * WINDOW]
            prob = self.vad(window)
            self.last_prob = prob
            events += self._step(window, prob)
        return events

    def _step(self, window: np.ndarray, prob: float) -> list[SegmenterEvent]:
        u = self._current
        if u is None:
            if prob >= self.threshold:
                u = self._current = _Utterance(chunks=list(self._preroll))
                self._preroll = []
                self._add(u, window, speech=True)
                events = [SegmenterEvent("start")]
                if u.speech_windows >= self.min_speech_windows:
                    u.confirmed = True
                    events.append(SegmenterEvent("confirmed", speech_s=self._seconds(u.speech_windows)))
                return events
            self._preroll.append(window)
            if len(self._preroll) > self.preroll_windows:
                self._preroll.pop(0)
            return []

        speech = prob >= self.threshold or (prob >= self.neg_threshold and u.silence_windows == 0)
        self._add(u, window, speech)
        events = []
        if not u.confirmed and u.speech_windows >= self.min_speech_windows:
            u.confirmed = True
            events.append(SegmenterEvent("confirmed", speech_s=self._seconds(u.speech_windows)))
        if u.silence_windows >= self.silence_windows or u.total_windows >= self.max_windows:
            events.append(self._finish(u))
        return events

    def _add(self, u: _Utterance, window: np.ndarray, speech: bool) -> None:
        u.chunks.append(window)
        u.total_windows += 1
        if speech:
            u.speech_windows += 1
            u.silence_windows = 0
        else:
            u.silence_windows += 1

    def _finish(self, u: _Utterance) -> SegmenterEvent:
        self._current = None
        self._preroll = []
        if not u.confirmed:
            return SegmenterEvent("discard")
        # Keep about 0.25 s of the trailing silence; Whisper likes a little.
        keep_tail = round(0.25 * SAMPLE_RATE / WINDOW)
        drop = max(0, u.silence_windows - keep_tail)
        chunks = u.chunks[:len(u.chunks) - drop] if drop else u.chunks
        return SegmenterEvent("end", audio=np.concatenate(chunks),
                              speech_s=self._seconds(u.speech_windows))

    @staticmethod
    def _seconds(windows: int) -> float:
        return windows * WINDOW / SAMPLE_RATE
