"""The utterance segmenter with a fake VAD (speech = loud window)."""

import numpy as np

from agent_relay_voice.vad import WINDOW, Segmenter

SR = 16000


class EnergyVad:
    def __init__(self):
        self.resets = 0

    def __call__(self, window):
        return 0.9 if np.abs(window).mean() > 0.05 else 0.02

    def reset(self):
        self.resets += 1


def tone(seconds):
    t = np.arange(int(seconds * SR)) / SR
    return (0.3 * np.sin(2 * np.pi * 200 * t)).astype(np.float32)


def silence(seconds):
    return np.zeros(int(seconds * SR), dtype=np.float32)


def feed(seg, audio, block=1600):
    events = []
    for i in range(0, len(audio), block):
        events += seg.push(audio[i:i + block])
    return events


def kinds(events):
    return [e.kind for e in events]


def test_utterance_after_silence():
    seg = Segmenter(EnergyVad(), silence_s=0.8, min_speech_s=0.3)
    events = feed(seg, np.concatenate([silence(0.5), tone(1.0), silence(1.0)]))
    assert kinds(events) == ["start", "confirmed", "end"]
    end = events[-1]
    # 1 s of speech plus up to 0.3 s preroll and 0.25 s trailing silence
    assert 1.0 <= len(end.audio) / SR <= 1.6
    assert abs(end.speech_s - 1.0) < 0.05


def test_pause_shorter_than_silence_keeps_one_utterance():
    seg = Segmenter(EnergyVad(), silence_s=0.8)
    audio = np.concatenate([tone(0.6), silence(0.5), tone(0.6), silence(1.0)])
    events = feed(seg, audio)
    assert kinds(events) == ["start", "confirmed", "end"]


def test_short_noise_is_discarded():
    seg = Segmenter(EnergyVad(), min_speech_s=0.3)
    events = feed(seg, np.concatenate([tone(0.15), silence(1.0)]))
    assert kinds(events) == ["start", "discard"]


def test_end_needs_the_configured_silence():
    seg = Segmenter(EnergyVad(), silence_s=0.8)
    events = feed(seg, np.concatenate([tone(0.5), silence(0.7)]))
    assert "end" not in kinds(events)
    events = feed(seg, silence(0.2))
    assert kinds(events) == ["end"]


def test_max_length_cuts():
    seg = Segmenter(EnergyVad(), max_s=2.0)
    events = feed(seg, tone(3.0))
    assert kinds(events)[:3] == ["start", "confirmed", "end"]
    assert len(events[2].audio) / SR <= 2.4


def test_reset_drops_the_utterance_in_progress():
    vad = EnergyVad()
    seg = Segmenter(vad)
    feed(seg, tone(0.5))
    assert seg.active
    assert seg.reset().kind == "discard"
    assert not seg.active and vad.resets == 1
    assert seg.reset() is None
    assert kinds(feed(seg, silence(1.0))) == []


def test_odd_block_sizes():
    seg = Segmenter(EnergyVad())
    audio = np.concatenate([silence(0.3), tone(0.8), silence(1.0)])
    events = feed(seg, audio, block=777)
    assert kinds(events) == ["start", "confirmed", "end"]
    assert len(events[-1].audio) % WINDOW == 0


def test_silero_stream_runs_on_windows():
    from agent_relay_voice.vad import SileroStream

    vad = SileroStream()
    probs = [vad(silence(WINDOW / SR)) for _ in range(10)]
    assert all(0.0 <= p < 0.5 for p in probs)
