"""The service end to end, without models and microphone: control socket
protocol (docs/voice-plan.md section 8), turns with a fake `claude`, sentence
streaming into a fake TTS, barge-in, relay notices, settings persistence."""

import asyncio
import json
import time
from pathlib import Path

import numpy as np
import pytest

from agent_relay_voice import claude as claude_mod
from agent_relay_voice import stt as stt_mod
from agent_relay_voice import tts as tts_mod
from agent_relay_voice.prompt import SAY_AUTH_FAILED, SAY_NO_TOKEN
from agent_relay_voice.service import Paths, VoiceService, is_new_conversation_command
from agent_relay_voice.settings import Settings
from agent_relay_voice.textproc import split_sentences
from agent_relay_voice.vad import SegmenterEvent

FAKE = str(Path(__file__).with_name("fake_claude.py"))


class FakeTTS(tts_mod.Engine):
    sample_rate = 16000

    def __init__(self, delay=0.0):
        self.said = []
        self.delay = delay

    def synthesize(self, sentence):
        self.said.append(sentence)
        for _ in range(4):
            if self.delay:
                time.sleep(self.delay / 4)
            yield np.zeros(400, dtype=np.float32)


class FakeSTT(stt_mod.Engine):
    def __init__(self, text):
        self.text = text

    def transcribe(self, audio, **kw):
        return self.text


def make_service(tmp_path, token=True, tts_delay=0.0):
    paths = Paths(control_socket=tmp_path / "ctl" / "control.sock", relay_dir=tmp_path / "relay",
                  config_dir=tmp_path / "cfg", token_file=tmp_path / "token",
                  runtime_dir=tmp_path / "rt", home=tmp_path / "home", claude=FAKE, socat="true")
    if token:
        paths.token_file.write_text("fake-token-" + "x" * 60 + "\n")
    settings = Settings(paths.config_dir / "settings.json")
    service = VoiceService(paths, settings, load_models=False, capture=False)
    service.speaker.engine = FakeTTS(tts_delay)
    service.speaker.out_file = tmp_path / "out.wav"
    return service


def fake_log(service):
    path = service.paths.home / "work" / "fake-claude.log"
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines()]


def fake_stdin(service):
    return [json.loads(x["data"]) for x in fake_log(service) if x["kind"] == "stdin"]


async def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


class Client:
    """A control-socket client like the GNOME extension."""

    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.events = []
        self.replies = {}
        self.task = asyncio.create_task(self._read())
        self.next_id = 1

    @classmethod
    async def connect(cls, path):
        r, w = await asyncio.open_unix_connection(str(path))
        client = cls(r, w)
        # one round trip, so the server has registered us for pushed events
        await client.request("state")
        client.replies.clear()
        return client

    async def _read(self):
        while line := await self.reader.readline():
            msg = json.loads(line)
            if "event" in msg:
                self.events.append(msg)
            else:
                self.replies[msg["id"]] = msg

    async def raw(self, data: bytes):
        self.writer.write(data)
        await self.writer.drain()

    async def request(self, cmd, **args):
        rid = self.next_id
        self.next_id += 1
        await self.raw((json.dumps({"id": rid, "cmd": cmd, **args}) + "\n").encode())
        await wait_for(lambda: rid in self.replies)
        return self.replies[rid]

    def close(self):
        self.task.cancel()
        self.writer.close()


def run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------------------------------

def test_new_conversation_command():
    for text in ("Neues Gespräch.", "neues gespräch bitte", "Bitte ein neues Gespräch!",
                 "Neues Gespräch starten."):
        assert is_new_conversation_command(text), text
    for text in ("Wie war das neue Gespräch?", "Neues Gespräch mit Kalle über das Projekt"):
        assert not is_new_conversation_command(text), text


def test_control_protocol(tmp_path):
    async def main():
        service = make_service(tmp_path)
        await service.startup()
        c = await Client.connect(service.paths.control_socket)
        other = await Client.connect(service.paths.control_socket)

        reply = await c.request("state")
        assert reply["ok"] is True and reply["id"] == 2
        state = reply["result"]
        assert set(state) == {"phase", "error", "loading", "settings", "last_user",
                              "last_assistant", "listening"}
        assert state["phase"] == "listening"
        assert state["error"] is None and state["loading"] is None
        assert state["settings"] == {"stt": "large-v3-turbo", "tts": "pocket-german-24l",
                                     "claude_model": ""}

        options = (await c.request("options"))["result"]
        assert set(options) == {"stt", "tts", "claude_models"}
        for key in options:
            assert all(set(o) == {"id", "label"} for o in options[key])
        assert {o["id"] for o in options["stt"]} == {
            "large-v3-turbo", "large-v3-turbo-german", "small", "wcpp-large-v3-turbo",
            "wcpp-large-v3-turbo-q8_0"}
        assert {o["id"] for o in options["tts"]} == {
            "pocket-german-24l", "pocket-german", "piper-thorsten-high"}
        assert [o["id"] for o in options["claude_models"]] == ["", "fable", "opus", "sonnet", "haiku"]

        # refusals
        for args, words in [({"stt": "nope"}, "unknown stt"), ({"tts": 3}, "unknown tts"),
                            ({"claude_model": "gpt"}, "unknown claude_model"),
                            ({"listening": "no"}, "true or false"), ({"volume": 3}, "unknown setting"),
                            ({}, "nothing to set")]:
            reply = await c.request("set", **args)
            assert reply["ok"] is False and words in reply["error"], (args, reply)
        reply = await c.request("fly")
        assert reply == {"id": reply["id"], "ok": False, "error": "unknown command 'fly'"}
        await c.raw(b"{broken\n")
        await c.raw(b"[1,2]\n")
        await c.raw(b'{"id": 99}\n')
        await wait_for(lambda: 99 in c.replies)
        assert c.replies[99] == {"id": 99, "ok": False, "error": "missing cmd"}

        # pause: state pushed to every client, setting persisted
        reply = await c.request("set", listening=False)
        assert reply["ok"] and reply["result"]["phase"] == "paused"
        await wait_for(lambda: any(e["event"] == "state" and e["state"]["phase"] == "paused"
                                   for e in other.events))
        saved = json.loads((service.paths.config_dir / "settings.json").read_text())
        assert saved["listening"] is False
        reply = await c.request("set", listening=True, stt="small", tts="piper-thorsten-high")
        assert reply["result"]["settings"]["stt"] == "small"
        assert reply["result"]["phase"] == "listening"
        saved = json.loads((service.paths.config_dir / "settings.json").read_text())
        assert (saved["stt"], saved["tts"], saved["listening"]) == ("small", "piper-thorsten-high", True)

        # model change restarts Claude Code with --model
        reply = await c.request("set", claude_model="haiku")
        assert reply["ok"]
        await wait_for(lambda: sum(1 for x in fake_log(service) if x["kind"] == "argv") == 2)
        argvs = [x["data"] for x in fake_log(service) if x["kind"] == "argv"]
        assert "--model" not in argvs[0]
        assert argvs[1][-2:] == ["--model", "haiku"]

        assert (await c.request("stop_speaking"))["ok"]
        assert (await c.request("new_conversation"))["ok"]
        await wait_for(lambda: sum(1 for x in fake_log(service) if x["kind"] == "argv") == 3)

        c.close()
        other.close()
        await service.shutdown()
        assert not service.paths.control_socket.exists()

    run(main())


def test_turn_is_spoken_sentence_by_sentence(tmp_path):
    async def main():
        service = make_service(tmp_path)
        await service.startup()
        c = await Client.connect(service.paths.control_socket)
        await service.ask("Hallo")
        await wait_for(lambda: any(e["event"] == "exchange" for e in c.events))
        reply = "Du hast gesagt: Hallo. Das ist der zweite Satz! Und ein dritter?"
        exchange = next(e for e in c.events if e["event"] == "exchange")
        assert exchange == {"event": "exchange", "user": "Hallo", "assistant": reply}
        await wait_for(lambda: not service.speaker.busy)
        assert service.speaker.engine.said == split_sentences(reply)

        def phases():
            return [e["state"]["phase"] for e in c.events if e["event"] == "state"]
        await wait_for(lambda: phases()[-1] == "listening")
        assert "thinking" in phases() and "speaking" in phases()
        state = service.state()
        assert state["last_user"] == "Hallo" and state["last_assistant"] == reply

        # without partial messages the complete message is spoken
        service.speaker.engine.said.clear()
        await service.ask("nopartial bitte")
        await wait_for(lambda: service.turn is None and not service.speaker.busy)
        assert service.speaker.engine.said == split_sentences(
            "Du hast gesagt: nopartial bitte. Das ist der zweite Satz! Und ein dritter?")
        c.close()
        await service.shutdown()

    run(main())


def test_utterance_goes_through_stt_to_claude(tmp_path):
    async def main():
        service = make_service(tmp_path)
        await service.startup()
        service.stt_engine = FakeSTT("Wie spät ist es?")
        service._on_audio_events(False, [SegmenterEvent("confirmed")], time.monotonic())
        assert service.phase() == "hearing"
        service._on_audio_events(False, [SegmenterEvent("end", audio=np.zeros(16000, np.float32))],
                                 time.monotonic())
        assert service.phase() == "transcribing"       # no flicker back to listening
        await wait_for(lambda: any(m.get("type") == "user" for m in fake_stdin(service)))
        user = [m for m in fake_stdin(service) if m.get("type") == "user"][0]
        assert user["message"]["content"][0]["text"] == "Wie spät ist es?"
        await wait_for(lambda: service.turn is None)
        assert service.timings[-1]["audio_s"] == 1.0

        # Whisper's inventions on noise are dropped
        service.stt_engine = FakeSTT("Untertitel der Amara.org-Community")
        await service.on_utterance(np.zeros(16000, np.float32), time.monotonic())
        assert len([m for m in fake_stdin(service) if m.get("type") == "user"]) == 1

        # "neues Gespräch" restarts Claude Code instead of being sent
        service.stt_engine = FakeSTT("Neues Gespräch.")
        await service.on_utterance(np.zeros(16000, np.float32), time.monotonic())
        await wait_for(lambda: sum(1 for x in fake_log(service) if x["kind"] == "argv") == 2)
        await service.shutdown()

    run(main())


def test_barge_in_stops_speech_and_interrupts(tmp_path):
    async def main():
        service = make_service(tmp_path, tts_delay=2.0)
        await service.startup()
        await service.ask("slow answer")
        await wait_for(lambda: service.speaking)
        assert service.phase() == "speaking"
        gen = service.speaker.generation
        # the user starts speaking
        service._on_audio_events(False, [SegmenterEvent("start"), SegmenterEvent("confirmed")],
                                 time.monotonic())
        assert service.speaker.generation == gen + 1
        assert service.turn.silenced
        await wait_for(lambda: any(m.get("type") == "control_request"
                                   and m["request"]["subtype"] == "interrupt"
                                   for m in fake_stdin(service)))
        # the interrupted turn ends; what the user says next is a normal turn
        await wait_for(lambda: service.turn is None)
        service._on_audio_events(False, [SegmenterEvent("discard")], time.monotonic())
        service.speaker.engine.delay = 0
        service.speaker.engine.said.clear()
        await service.ask("weiter")
        await wait_for(lambda: service.turn is None and not service.speaker.busy)
        assert service.speaker.engine.said[0] == "Du hast gesagt:"
        await service.shutdown()

    run(main())


def test_message_during_a_turn_waits_for_it(tmp_path):
    async def main():
        service = make_service(tmp_path)
        await service.startup()
        await service.ask("slow first")
        await service.ask("second")
        assert service.outbox == [("second", None)]
        await service.handle_command("stop_speaking", {})
        await wait_for(lambda: any(m.get("type") == "user" and
                                   m["message"]["content"][0]["text"] == "second"
                                   for m in fake_stdin(service)))
        await wait_for(lambda: service.turn is None)
        await service.shutdown()

    run(main())


def test_relay_notices_between_turns(tmp_path):
    async def main():
        service = make_service(tmp_path)
        await service.startup()
        await service.ask("slow first")
        service._on_relay_event({"type": "run_ended", "project": "web", "phase": "finished"})
        service._on_relay_event({"type": "grant_added", "grant": "g"})
        await asyncio.sleep(0.2)
        users = [m for m in fake_stdin(service) if m.get("type") == "user"]
        assert len(users) == 1                  # waits while the turn runs
        await service.handle_command("stop_speaking", {})
        await wait_for(lambda: len([m for m in fake_stdin(service) if m.get("type") == "user"]) == 2)
        users = [m for m in fake_stdin(service) if m.get("type") == "user"]
        assert users[1]["message"]["content"][0]["text"] == \
            "[Ereignis: Der Auftrag in Projekt web ist fertig.]"
        await wait_for(lambda: service.turn is None)
        await service.shutdown()

    run(main())


def test_relay_events_socket_feeds_notices(tmp_path):
    async def main():
        service = make_service(tmp_path)
        service.paths.relay_dir.mkdir()

        async def serve(reader, writer):
            writer.write(b'{"type": "request_added", "project": "kalender", "number": 4}\n')
            await writer.drain()

        server = await asyncio.start_unix_server(serve, path=str(service.paths.events_socket))
        await service.startup()
        await wait_for(lambda: any(m.get("type") == "user" for m in fake_stdin(service)))
        user = [m for m in fake_stdin(service) if m.get("type") == "user"][0]
        assert "Nummer 4" in user["message"]["content"][0]["text"]
        server.close()
        await service.shutdown()

    run(main())


def test_without_token(tmp_path):
    async def main():
        service = make_service(tmp_path, token=False)
        await service.startup()
        state = service.state()
        assert state["phase"] == "error" and "token" in state["error"]
        await service.ask("Hallo")
        await wait_for(lambda: SAY_NO_TOKEN in service.speaker.engine.said)
        # notices are spoken directly when there is no instance
        service._on_relay_event({"type": "run_ended", "project": "web", "phase": "finished"})
        await wait_for(lambda: "Der Auftrag in Projekt web ist fertig." in service.speaker.engine.said)
        await service.shutdown()

    run(main())


def test_claude_crash_is_reported_and_restarted(tmp_path):
    async def main():
        service = make_service(tmp_path)
        await service.startup()
        await service.ask("crash now")
        await wait_for(lambda: service.claude_error is not None)
        assert service.turn is None and "exited (3)" in service.claude_error
        assert service.state()["phase"] == "error"
        await service.ask("Hallo")
        await wait_for(lambda: service.turn is None and service.last_assistant.startswith("Du hast"))
        assert service.claude_error is None
        await service.shutdown()

    run(main())


def test_auth_error_is_not_read_aloud_in_english(tmp_path):
    async def main():
        service = make_service(tmp_path)
        await service.startup()
        await service.ask("autherror")
        await wait_for(lambda: service.turn is None and not service.speaker.busy)
        assert service.speaker.engine.said == [SAY_AUTH_FAILED]
        state = service.state()
        assert state["phase"] == "error" and "401" in state["error"]
        await service.ask("Hallo")                 # a working answer clears it
        await wait_for(lambda: service.turn is None)
        assert service.state()["error"] is None
        await service.shutdown()

    run(main())


def test_mute_and_pause_phases(tmp_path):
    async def main():
        service = make_service(tmp_path)
        await service.startup()
        service._on_audio_events(True, [], time.monotonic())
        assert service.state()["phase"] == "muted"
        service._on_audio_events(False, [], time.monotonic())
        assert service.state()["phase"] == "listening"
        await service.handle_command("set", {"listening": False})
        assert service.state()["phase"] == "paused"
        await service.shutdown()

    run(main())


@pytest.mark.parametrize("phase", ["starting"])
def test_starting_phase_before_startup(tmp_path, phase):
    service = make_service(tmp_path)
    assert service.state()["phase"] == phase
