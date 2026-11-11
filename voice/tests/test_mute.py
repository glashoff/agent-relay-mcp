"""Mute detection on synthetic signals shaped like the Jabra Link 380
measurements (int16 units): boom up = constant pattern with RMS ~0.5 and 75 %
zeros; room noise RMS 5-30 with < 10 % zeros; speech RMS 400-4300; headset
off = only zeros."""

import numpy as np

from agent_relay_voice.mute import MuteDetector, block_stats

BLOCK = 1600  # 100 ms at 16 kHz
rng = np.random.default_rng(1)


def boom_up() -> np.ndarray:
    # 75 % exact zeros, the rest +-1: RMS = sqrt(0.25) = 0.5
    pattern = np.array([0, 0, 0, 1, 0, 0, 0, -1], dtype=np.int16)
    return np.tile(pattern, BLOCK // len(pattern))


def room_noise(rms: float = 12.0) -> np.ndarray:
    return np.round(rng.normal(0, rms, BLOCK)).astype(np.int16)


def speech(rms: float = 1500.0) -> np.ndarray:
    t = np.arange(BLOCK) / 16000
    return (np.sin(2 * np.pi * 220 * t) * rms * np.sqrt(2)).astype(np.int16)


def zeros() -> np.ndarray:
    return np.zeros(BLOCK, dtype=np.int16)


def test_stats_of_the_measured_patterns():
    rms, z = block_stats(boom_up())
    assert abs(rms - 0.5) < 0.01 and abs(z - 0.75) < 0.01
    rms, z = block_stats(room_noise(12))
    assert 5 < rms < 30 and z < 0.10
    rms, z = block_stats(speech())
    assert 400 < rms < 4300
    assert block_stats(zeros()) == (0.0, 1.0)


def test_boom_up_mutes_after_300_ms():
    m = MuteDetector()
    assert m.update(boom_up()) is False   # 100 ms
    assert m.update(boom_up()) is False   # 200 ms
    assert m.update(boom_up()) is True    # 300 ms
    assert m.update(boom_up()) is True


def test_headset_off_is_muted():
    m = MuteDetector()
    for _ in range(3):
        muted = m.update(zeros())
    assert muted


def test_room_noise_and_quiet_rooms_are_not_muted():
    m = MuteDetector()
    for rms in (5, 8, 12, 20, 30):
        for _ in range(10):
            assert m.update(room_noise(rms)) is False


def test_speech_unmutes_at_once():
    m = MuteDetector()
    for _ in range(5):
        m.update(boom_up())
    assert m.muted
    assert m.update(speech()) is False
    assert m.update(room_noise()) is False


def test_short_mute_like_gap_does_not_mute():
    m = MuteDetector()
    for _ in range(2):
        m.update(boom_up())
    m.update(room_noise())
    for _ in range(2):
        assert m.update(boom_up()) is False


def test_low_rms_without_zeros_is_not_muted():
    # very quiet but dithered input (no exact zeros) is a live microphone
    m = MuteDetector()
    quiet = np.tile(np.array([1, -1], dtype=np.int16), BLOCK // 2)
    for _ in range(10):
        assert m.update(quiet) is False


def test_thresholds_are_configurable():
    # a headset whose "muted" signal is faint noise with a few zeros
    m = MuteDetector(rms_threshold=40, zero_fraction=0.0, hold_s=0.2)
    assert m.update(room_noise(12)) is False
    assert m.update(room_noise(12)) is True
    m = MuteDetector()
    m.configure(rms_threshold=1.5, zero_fraction=0.5, hold_s=0.1)
    assert m.update(boom_up()) is True
