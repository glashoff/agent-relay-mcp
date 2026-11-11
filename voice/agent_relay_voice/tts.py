"""Speech output engines (see test/tts/README.md for the measurements):

- Pocket TTS German 24 layers, voice "juergen": best quality, first chunk
  ~0.4 s, real-time factor ~1 on the i7-10750H (borderline);
- Pocket TTS German 6 layers: RTF ~0.3;
- Piper de_DE-thorsten-high: fastest, uses the pronunciation dictionary.

synthesize() takes one sentence and yields float32 mono chunks; Pocket TTS
streams several per sentence. Pocket TTS loads the ungated checkpoint
kyutai/pocket-tts-without-voice-cloning when the gated one is not accessible,
which has all preset voices.
"""

from __future__ import annotations

import logging
from typing import Iterator

import numpy as np

from .textproc import apply_dictionary, parse_dictionary

log = logging.getLogger("voice.tts")

PIPER_REPO = "rhasspy/piper-voices"
PIPER_REV = "cf910fb59f1d79ab3b625a63993a108751c6c25d"

ENGINES: dict[str, dict] = {
    "pocket-german-24l": {"family": "pocket", "language": "german_24l", "rtf": 1.3,
                          "label": "Pocket TTS German 24 layers, Jürgen"},
    "pocket-german": {"family": "pocket", "language": "german", "rtf": 0.45,
                      "label": "Pocket TTS German 6 layers, Jürgen (faster)"},
    "piper-thorsten-high": {"family": "piper", "quality": "high",
                            "label": "Piper Thorsten high (fastest)"},
}


class Engine:
    sample_rate = 24000
    gain_db = 0.0
    expected_rtf = 0.5        # synthesis time / audio time, a first guess for the pre-buffer

    def synthesize(self, sentence: str) -> Iterator[np.ndarray]:
        raise NotImplementedError

    def warmup(self) -> None:
        for _ in self.synthesize("Hallo."):
            pass


class PiperEngine(Engine):
    def __init__(self, quality: str, dictionary: str) -> None:
        from huggingface_hub import hf_hub_download
        from piper import PiperVoice

        base = f"de/de_DE/thorsten/{quality}/de_DE-thorsten-{quality}.onnx"
        model = hf_hub_download(PIPER_REPO, base, revision=PIPER_REV)
        hf_hub_download(PIPER_REPO, base + ".json", revision=PIPER_REV)
        self.voice = PiperVoice.load(model)
        self.sample_rate = self.voice.config.sample_rate
        self.set_dictionary(dictionary)

    def set_dictionary(self, text: str) -> None:
        self.entries = parse_dictionary(text)

    def synthesize(self, sentence):
        sentence = apply_dictionary(sentence, self.entries)
        chunks = [c.audio_float_array for c in self.voice.synthesize(sentence)]
        if chunks:
            yield np.concatenate(chunks).astype(np.float32)


class PocketEngine(Engine):
    def __init__(self, language: str, voice: str, gain_db: float, expected_rtf: float) -> None:
        from pocket_tts import TTSModel

        self.expected_rtf = expected_rtf
        self.model = TTSModel.load_model(language=language)
        self.sample_rate = self.model.sample_rate
        self.gain_db = gain_db
        self.state = self.model.get_state_for_audio_prompt(voice)

    def synthesize(self, sentence):
        for chunk in self.model.generate_audio_stream(self.state, sentence, copy_state=True):
            yield chunk.detach().cpu().numpy().astype(np.float32).reshape(-1)


def load(engine_id: str, *, dictionary: str, pocket_voice: str, pocket_gain_db: float) -> Engine:
    spec = ENGINES[engine_id]
    if spec["family"] == "piper":
        return PiperEngine(spec["quality"], dictionary)
    return PocketEngine(spec["language"], pocket_voice, pocket_gain_db, spec["rtf"])


def options() -> list[dict]:
    return [{"id": k, "label": v["label"]} for k, v in ENGINES.items()]
