import json
import stat

import pytest

from agent_relay_voice.settings import DEFAULTS, Settings


def test_defaults_without_file(tmp_path):
    s = Settings(tmp_path / "settings.json")
    assert s["stt"] == "large-v3-turbo"
    assert s["vad_silence_s"] == 0.8
    assert s["min_utterance_s"] == 0.3
    assert s["mute_rms"] == 1.5 and s["mute_zero_fraction"] == 0.5 and s["mute_hold_s"] == 0.3
    assert s["listening"] is True
    assert "Claude Code" in s["hotwords"]


def test_update_persists_and_reloads(tmp_path):
    path = tmp_path / "sub" / "settings.json"
    s = Settings(path)
    s.update(stt="small", tts="piper-thorsten-high", claude_model="haiku", listening=False,
             hotwords=["Foo", "Bar"], vad_silence_s=1)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    again = Settings(path)
    assert again["stt"] == "small"
    assert again["tts"] == "piper-thorsten-high"
    assert again["claude_model"] == "haiku"
    assert again["listening"] is False
    assert again["hotwords"] == ["Foo", "Bar"]
    assert again["vad_silence_s"] == 1.0 and isinstance(again["vad_silence_s"], float)
    assert set(json.loads(path.read_text())) == set(DEFAULTS)


def test_bad_file_values_fall_back_to_defaults(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"stt": 5, "listening": "yes", "unknown": 1, "mute_rms": 2,
                                "hotwords": ["a", 1]}))
    s = Settings(path)
    assert s["stt"] == DEFAULTS["stt"]
    assert s["listening"] is True
    assert s["mute_rms"] == 2.0
    assert s["hotwords"] == DEFAULTS["hotwords"]


def test_broken_json_falls_back(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json")
    assert Settings(path)["tts"] == DEFAULTS["tts"]


def test_update_rejects_unknown_and_wrong_types(tmp_path):
    s = Settings(tmp_path / "settings.json")
    with pytest.raises(KeyError):
        s.update(nope=1)
    with pytest.raises(TypeError):
        s.update(listening="no")
    with pytest.raises(TypeError):
        s.update(vad_silence_s=True)
