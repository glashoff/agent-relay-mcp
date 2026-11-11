"""Speech output: the pre-buffer keeps a slower-than-real-time engine from
running dry, stop() drops everything at once."""

import time

import numpy as np

from agent_relay_voice import speaker as speaker_mod
from agent_relay_voice.speaker import Speaker, prebuffer_seconds

RATE = 8000


class FakePlayback:
    """Plays in simulated real time: records when audio would run out."""
    instances = []

    def __init__(self, rate, out_file=None):
        self.rate = rate
        self.ends_at = None
        self.gaps = []
        self.written = 0.0
        self.aborted = False
        self.finished = False
        FakePlayback.instances.append(self)

    def write(self, pcm):
        if self.aborted:
            return False
        now = time.monotonic()
        duration = len(pcm) / 2 / self.rate
        if self.ends_at is not None and now > self.ends_at + 0.05:
            self.gaps.append(now - self.ends_at)
        self.ends_at = max(now, self.ends_at or now) + duration
        self.written += duration
        return True

    def finish(self, timeout=0):
        self.finished = True

    def abort(self):
        self.aborted = True


class SlowEngine:
    """Streams 0.1 s chunks at the given real-time factor."""
    sample_rate = RATE
    gain_db = 0.0

    def __init__(self, rtf, expected_rtf=None):
        self.rtf = rtf
        self.expected_rtf = expected_rtf if expected_rtf is not None else rtf
        self.said = []

    def synthesize(self, sentence):
        self.said.append(sentence)
        seconds = len(sentence) * speaker_mod.SECONDS_PER_CHAR
        for _ in range(int(seconds / 0.1)):
            time.sleep(0.1 * self.rtf)
            yield np.zeros(int(0.1 * RATE), dtype=np.float32)


def wait_idle(sp, timeout=20):
    end = time.monotonic() + timeout
    while sp.busy and time.monotonic() < end:
        time.sleep(0.02)
    assert not sp.busy


def test_prebuffer_formula():
    assert prebuffer_seconds(0.5, "x" * 50, 0) == 0.0
    assert prebuffer_seconds(1.3, "x" * 50, 0) > 1.0          # 3.5 s sentence at RTF 1.3
    assert prebuffer_seconds(1.3, "x" * 50, 5.0) == 0.0        # enough still queued


def test_slow_engine_plays_without_gaps(monkeypatch):
    monkeypatch.setattr(speaker_mod, "Playback", FakePlayback)
    FakePlayback.instances.clear()
    sp = Speaker()
    sp.engine = SlowEngine(rtf=1.5)
    sp.say("x" * 25)          # ~1.75 s of audio, 2.6 s to synthesize
    sp.say("y" * 15)
    sp.end()
    wait_idle(sp)
    pb = FakePlayback.instances[-1]
    assert pb.gaps == [], pb.gaps
    assert pb.finished and abs(pb.written - 2.8) < 0.2


def test_fast_engine_starts_at_once(monkeypatch):
    monkeypatch.setattr(speaker_mod, "Playback", FakePlayback)
    firsts = []
    sp = Speaker(on_first_audio=lambda g, t: firsts.append(t))
    sp.engine = SlowEngine(rtf=0.3)
    sp.say("x" * 30)
    sp.end()
    wait_idle(sp)
    assert firsts[0] < 0.3


def test_stop_drops_queued_sentences(monkeypatch):
    monkeypatch.setattr(speaker_mod, "Playback", FakePlayback)
    FakePlayback.instances.clear()
    busy = []
    sp = Speaker(on_busy=busy.append)
    sp.engine = SlowEngine(rtf=0.5)
    for i in range(5):
        sp.say(f"Satz {i} " + "x" * 20)
    sp.end()
    time.sleep(0.4)
    assert sp.busy
    gen = sp.stop()
    assert gen == 1 and not sp.busy and busy[-1] is False
    assert FakePlayback.instances[-1].aborted
    time.sleep(1.0)
    assert len(sp.engine.said) <= 2          # the rest was never synthesized
    # a stale generation is ignored, the new one works
    sp.say("alt", generation=0)
    sp.say("neu " + "x" * 5)
    sp.end()
    wait_idle(sp)
    assert sp.engine.said[-1].startswith("neu")
