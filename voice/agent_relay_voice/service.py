"""The voice service: microphone -> mute detection -> VAD -> Whisper ->
Claude Code -> sentence-wise TTS -> speakers, with barge-in, relay notices and
the control socket for the GNOME extension.

Everything that changes state runs on the asyncio loop. The capture thread
does mute detection and VAD itself and posts the results to the loop;
transcription and model loading run in executors; speech output has its own
thread (speaker.Speaker).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import claude as claude_mod
from . import stt as stt_mod
from . import tts as tts_mod
from .audio import Capture
from .control import CommandError, ControlServer
from .mute import MuteDetector
from .prompt import (SAY_API_ERROR, SAY_AUTH_FAILED, SAY_NEW_CONVERSATION, SAY_NO_CLAUDE,
                     SAY_NO_TOKEN, VOICE_SYSTEM_PROMPT)
from .relay import call_tool, notice_for, project_names, read_events, spoken_notice
from .settings import Settings
from .speaker import Speaker
from .textproc import SentenceStreamer

log = logging.getLogger("voice")

_NEW_CONVERSATION = re.compile(
    r"^(bitte\s+)?(ein\s+|ein\s+ganz\s+)?neues\s+gespräch(\s+(bitte|beginnen|starten|anfangen))?(\s+bitte)?$")


def is_new_conversation_command(text: str) -> bool:
    t = re.sub(r"[^\wäöüß ]+", " ", text.lower())
    return bool(_NEW_CONVERSATION.match(" ".join(t.split())))


@dataclass
class Paths:
    control_socket: Path = Path("/run/agent-relay-voice/control.sock")
    relay_dir: Path = Path("/run/agent-relay")
    config_dir: Path = Path("/config")
    token_file: Path = Path("/run/secrets/claude-token")
    runtime_dir: Path = Path("/tmp/agent-relay-voice")
    home: Path = Path(os.environ.get("HOME", "/home/voice"))
    claude: str = os.environ.get("CLAUDE_BIN", "claude")
    socat: str = "socat"
    forward_port: int = 8765

    @property
    def mcp_socket(self) -> Path:
        return self.relay_dir / "mcp.sock"

    @property
    def events_socket(self) -> Path:
        return self.relay_dir / "events.sock"


@dataclass
class Turn:
    user: str
    streamer: SentenceStreamer = field(default_factory=SentenceStreamer)
    text: str = ""
    deltas_since_message: int = 0
    silenced: bool = False                 # barge-in / stop: rest is not spoken
    api_error: str | None = None           # Claude Code reported an error instead of answering
    interrupted_at: float | None = None
    generation: int = 0
    started: float = field(default_factory=time.monotonic)
    utterance_end: float | None = None     # for "time to first audio"
    first_audio_logged: bool = False


class VoiceService:
    def __init__(self, paths: Paths, settings: Settings, *, load_models: bool = True,
                 capture: bool = True) -> None:
        self.paths = paths
        self.settings = settings
        self.load_models = load_models
        self.use_capture = capture
        self.loop: asyncio.AbstractEventLoop | None = None
        self.control = ControlServer(paths.control_socket, self.handle_command)

        # state
        self.starting = True
        self.loading: dict[str, str] = {}
        self.muted = False
        self.hearing = False
        self.transcribing = 0
        self.speaking = False
        self.capture_error: str | None = None
        self.claude_error: str | None = None
        self.api_error: str | None = None
        self.model_error: str | None = None
        self.last_user = ""
        self.last_assistant = ""
        self._last_state: dict | None = None

        # engines
        self.stt_engine: stt_mod.Engine | None = None
        self.stt_pool = ThreadPoolExecutor(1, thread_name_prefix="stt")
        self.stt_loader = ThreadPoolExecutor(1, thread_name_prefix="stt-load")
        self.tts_loader = ThreadPoolExecutor(1, thread_name_prefix="tts-load")
        self.speaker = Speaker(on_busy=self._threadsafe(self._on_speaker_busy),
                               on_first_audio=self._threadsafe(self._on_first_audio))

        # audio front end (used from the capture thread)
        self.mute = MuteDetector(settings["mute_rms"], settings["mute_zero_fraction"],
                                 settings["mute_hold_s"])
        self.segmenter = None
        self.capture: Capture | None = None

        # Claude
        self.claude: claude_mod.ClaudeProcess | None = None
        self.claude_started_with_relay = False
        self.turn: Turn | None = None
        self.last_turn: Turn | None = None
        self.turn_count = 0
        self.outbox: list[tuple[str, float | None]] = []
        self.notices: list[str] = []
        self.relay_projects: list[str] = []
        self.forwarder: subprocess.Popen | None = None
        self._tasks: list[asyncio.Task] = []
        self._stopping = asyncio.Event()
        self.timings: list[dict] = []          # recent turn timings, for logs and tests

    # ------------------------------------------------------------------ helpers
    def _threadsafe(self, fn):
        def call(*args):
            if self.loop and not self.loop.is_closed():
                self.loop.call_soon_threadsafe(fn, *args)
        return call

    def phase(self) -> str:
        if self.starting:
            return "starting"
        if self.transcribing:
            return "transcribing"
        if self.hearing:
            return "hearing"
        if self.speaking:
            return "speaking"
        if self.turn is not None:
            return "thinking"
        if self.capture_error or self.claude_error or self.api_error:
            return "error"
        if not self.settings["listening"]:
            return "paused"
        if self.muted:
            return "muted"
        return "listening"

    def state(self) -> dict:
        return {
            "phase": self.phase(),
            "error": self.capture_error or self.claude_error or self.api_error or self.model_error,
            "loading": ", ".join(self.loading.values()) or None,
            "settings": {k: self.settings[k] for k in ("stt", "tts", "claude_model")},
            "listening": self.settings["listening"],
            "last_user": self.last_user,
            "last_assistant": self.last_assistant,
        }

    def update_state(self) -> None:
        state = self.state()
        if state != self._last_state:
            if not self._last_state or state["phase"] != self._last_state["phase"]:
                log.info("phase %s", state["phase"])
            self._last_state = state
            self.control.broadcast({"event": "state", "state": state})
        if self.idle():
            self._deliver_pending()

    def idle(self) -> bool:
        return (not self.starting and not self.hearing and not self.transcribing
                and self.turn is None)

    # ------------------------------------------------------------------ lifecycle
    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._stopping.set)
        await self.startup()
        await self._stopping.wait()
        await self.shutdown()

    async def startup(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.paths.runtime_dir.mkdir(parents=True, exist_ok=True)
        await self.control.start()
        self.update_state()
        self._start_forwarder()
        self._tasks.append(asyncio.create_task(read_events(
            self.paths.events_socket, self._on_relay_event, self._on_relay_connected)))
        self._tasks.append(asyncio.create_task(self._refresh_projects_loop()))
        self._tasks.append(asyncio.create_task(self._watch_forwarder()))
        await self.start_claude()
        if self.use_capture:
            self._start_capture()
        if self.load_models:
            await asyncio.gather(self.load_stt(self.settings["stt"]),
                                 self.load_tts(self.settings["tts"]))
        self.starting = False
        self.update_state()

    async def shutdown(self) -> None:
        log.info("stopping")
        if self.capture:
            await asyncio.to_thread(self.capture.stop)
        self.speaker.close()
        if self.claude:
            await self.claude.stop()
        if self.forwarder and self.forwarder.poll() is None:
            self.forwarder.terminate()
        for task in self._tasks:
            task.cancel()
        if self.stt_engine:
            self.stt_engine.close()
        await self.control.close()

    def stop(self) -> None:
        self._stopping.set()

    # ------------------------------------------------------------------ models
    async def load_stt(self, model_id: str) -> None:
        label = stt_mod.MODELS[model_id]["label"]
        self.loading["stt"] = f"Speech recognition: {label}"
        self.update_state()
        t0 = time.perf_counter()
        try:
            engine = await self.loop.run_in_executor(self.stt_loader, self._load_stt_sync, model_id)
        except Exception as error:  # noqa: BLE001 - shown in the state
            log.exception("loading %s failed", model_id)
            self.model_error = f"Loading {label} failed: {error}"
        else:
            old, self.stt_engine = self.stt_engine, engine
            if old:
                self.stt_pool.submit(old.close)   # after a transcription that still uses it
            self.model_error = None
            log.info("speech recognition %s ready in %.1f s", model_id, time.perf_counter() - t0)
        finally:
            self.loading.pop("stt", None)
            self.update_state()

    @staticmethod
    def _load_stt_sync(model_id: str) -> stt_mod.Engine:
        engine = stt_mod.load(model_id)
        stt_mod.warmup(engine)
        return engine

    async def load_tts(self, engine_id: str) -> None:
        label = tts_mod.ENGINES[engine_id]["label"]
        self.loading["tts"] = f"Speech output: {label}"
        self.update_state()
        t0 = time.perf_counter()
        try:
            engine = await self.loop.run_in_executor(self.tts_loader, self._load_tts_sync, engine_id)
        except Exception as error:  # noqa: BLE001
            log.exception("loading %s failed", engine_id)
            self.model_error = f"Loading {label} failed: {error}"
        else:
            self.speaker.engine = engine
            self.model_error = None
            log.info("speech output %s ready in %.1f s", engine_id, time.perf_counter() - t0)
        finally:
            self.loading.pop("tts", None)
            self.update_state()

    def _load_tts_sync(self, engine_id: str) -> tts_mod.Engine:
        s = self.settings
        engine = tts_mod.load(engine_id, dictionary=s["pronunciation"],
                              pocket_voice=s["pocket_voice"], pocket_gain_db=s["pocket_gain_db"])
        engine.warmup()
        return engine

    # ------------------------------------------------------------------ audio in
    def _start_capture(self) -> None:
        from .vad import Segmenter, SileroStream

        s = self.settings
        self.segmenter = Segmenter(SileroStream(), s["vad_threshold"], s["vad_silence_s"],
                                   s["min_utterance_s"], s["max_utterance_s"])
        self.capture = Capture(self._on_block, self._threadsafe(self._on_capture_error))
        self.capture.start()

    def _on_capture_error(self, error: str | None) -> None:
        if error != self.capture_error:
            self.capture_error = error
            self.update_state()

    def _on_block(self, block: np.ndarray) -> None:
        """Capture thread: mute detection and VAD for one 100 ms block."""
        muted = self.mute.update(block)
        events = []
        if not self.settings["listening"] or muted:
            dropped = self.segmenter.reset()
            if dropped:
                events.append(dropped)
        else:
            events = self.segmenter.push(block.astype(np.float32) / 32768.0)
        if muted != self.muted or events:
            self.loop.call_soon_threadsafe(self._on_audio_events, muted, events, time.monotonic())

    def _on_audio_events(self, muted: bool, events: list, now: float) -> None:
        changed = muted != self.muted
        self.muted = muted
        for event in events:
            if event.kind == "confirmed":
                self.hearing = True
                self.barge_in()
            elif event.kind == "discard":
                self.hearing = False
            elif event.kind == "end":
                self.hearing = False
                if self.stt_engine is not None:
                    # counted now, so the phase goes straight from hearing to transcribing
                    self.transcribing += 1
                    asyncio.ensure_future(self.on_utterance(event.audio, now, counted=True))
                else:
                    log.info("utterance dropped: speech recognition not loaded yet")
            changed = True
        if changed:
            self.update_state()

    # ------------------------------------------------------------------ turns
    def barge_in(self) -> None:
        """The user speaks: stop what is being said and the answer behind it."""
        if not self.speaking:
            return
        log.info("barge-in: stopping speech output")
        self.speaker.stop()
        if self.turn and not self.turn.silenced:
            self.turn.silenced = True
            self.turn.interrupted_at = time.monotonic()
            if self.claude and self.claude.alive:
                asyncio.ensure_future(self.claude.interrupt())
                asyncio.get_running_loop().call_later(15, self._interrupt_timeout, self.turn)

    def _interrupt_timeout(self, turn: Turn) -> None:
        if self.turn is turn:
            log.warning("no result after an interrupt; treating the turn as ended")
            self._end_turn("")

    async def on_utterance(self, audio: np.ndarray, ended: float, counted: bool = False) -> None:
        if self.stt_engine is None:
            log.info("utterance dropped: speech recognition not loaded yet")
            if counted:
                self.transcribing -= 1
                self.update_state()
            return
        if not counted:
            self.transcribing += 1
            self.update_state()
        s = self.settings
        hotwords = list(s["hotwords"])
        if s["hotwords_from_projects"]:
            hotwords += [p for p in self.relay_projects if p not in hotwords]
        try:
            await self._transcribe_and_ask(audio, ended, hotwords)
        finally:
            self.transcribing -= 1
            self.update_state()

    async def _transcribe_and_ask(self, audio: np.ndarray, ended: float, hotwords: list[str]) -> None:
        s = self.settings
        t0 = time.perf_counter()
        engine = self.stt_engine
        try:
            text = await self.loop.run_in_executor(
                self.stt_pool, lambda: engine.transcribe(
                    audio, language=s["stt_language"] or None, prompt=s["stt_prompt"],
                    hotwords=", ".join(hotwords), beam_size=s["stt_beam_size"]))
        except Exception as error:  # noqa: BLE001
            log.exception("transcription failed")
            text = ""
            self.model_error = f"Transcription failed: {error}"
        else:
            if self.model_error and self.model_error.startswith("Transcription failed"):
                self.model_error = None
        stt_s = time.perf_counter() - t0
        log.info("transcribed %.1f s of audio in %.2f s: %s", len(audio) / 16000, stt_s, text)
        if not text or stt_mod.is_hallucination(text):
            return
        if is_new_conversation_command(text):
            self.last_user, self.last_assistant = text, SAY_NEW_CONVERSATION
            await self.new_conversation()
            self.speaker.say(SAY_NEW_CONVERSATION)
            self.speaker.end()
            self.control.broadcast({"event": "exchange", "user": text,
                                    "assistant": SAY_NEW_CONVERSATION})
            return
        self.timings.append({"audio_s": len(audio) / 16000, "stt_s": stt_s})
        self.timings = self.timings[-20:]
        await self.ask(text, ended)

    async def ask(self, text: str, utterance_end: float | None = None) -> None:
        """Sends a user message, or queues it while a turn is running."""
        if self.turn is not None:
            self.outbox.append((text, utterance_end))
            log.info("queued until the current turn ends: %s", text[:80])
            return
        if not self.claude or not self.claude.alive:
            await self.start_claude()
        if not self.claude or not self.claude.alive:
            reply = SAY_NO_TOKEN if not self.paths.token_file.is_file() else SAY_NO_CLAUDE
            self.last_user, self.last_assistant = text, reply
            self.speaker.say(reply)
            self.speaker.end()
            self.control.broadcast({"event": "exchange", "user": text, "assistant": reply})
            self.update_state()
            return
        if (not self.claude_started_with_relay and self.turn_count == 0
                and self.paths.mcp_socket.exists()):
            log.info("the relay socket appeared; restarting Claude Code so it has the relay tools")
            await self.start_claude()
        self.turn = self.last_turn = Turn(user=text, generation=self.speaker.generation,
                                          utterance_end=utterance_end)
        self.turn_count += 1
        self.last_user = text
        if not text.startswith("[Ereignis:"):
            self.last_assistant = ""
        self.update_state()
        await self.claude.ask(text)

    def _on_claude_event(self, event: claude_mod.Event) -> None:
        turn = self.turn
        if event.kind == "init":
            status = claude_mod.relay_status(event.data or {})
            log.info("Claude Code session %s, model %s, relay %s",
                     (event.data or {}).get("session_id"), (event.data or {}).get("model"), status)
            return
        if turn is None:
            return
        if event.kind == "text":
            turn.text += event.text
            turn.deltas_since_message += 1
            self._speak(turn, turn.streamer.feed(event.text))
        elif event.kind == "block_end":
            self._speak(turn, turn.streamer.flush())
        elif event.kind == "message_text":
            if turn.deltas_since_message == 0:
                # no partial messages for this one: speak the whole text
                turn.text += event.text
                self._speak(turn, turn.streamer.feed(event.text + "\n"))
            turn.deltas_since_message = 0
        elif event.kind == "api_error":
            turn.api_error = str((event.data or {}).get("error") or "error")
            log.warning("Claude Code error (%s): %s", turn.api_error, event.text)
            self.api_error = f"Claude Code: {event.text or turn.api_error}"
        elif event.kind == "tool":
            log.info("Claude uses %s", event.text)
        elif event.kind == "result":
            self._speak(turn, turn.streamer.flush())
            if turn.api_error:
                if not turn.silenced:
                    say = SAY_AUTH_FAILED if "auth" in turn.api_error else SAY_API_ERROR
                    turn.text = say
                    self._speak(turn, [say])
            else:
                self.api_error = None
                if not turn.text.strip() and event.text and not turn.silenced:
                    turn.text = event.text
                    self._speak(turn, turn.streamer.feed(event.text + "\n"))
            self._end_turn(turn.text)

    def _speak(self, turn: Turn, sentences: list[str]) -> None:
        if turn.silenced or turn.generation != self.speaker.generation:
            return
        for sentence in sentences:
            self.speaker.say(sentence, turn.generation)

    def _end_turn(self, text: str) -> None:
        turn, self.turn = self.turn, None
        if turn is None:
            return
        if not turn.silenced:
            self.speaker.end(turn.generation)
        answer = " ".join(text.split())
        self.last_assistant = answer
        log.info("turn done in %.1f s%s: %s", time.monotonic() - turn.started,
                 " (interrupted)" if turn.silenced else "", answer[:200])
        self.control.broadcast({"event": "exchange", "user": turn.user, "assistant": answer})
        if self.outbox:
            items, self.outbox = self.outbox, []
            text = " ".join(t for t, _ in items)
            asyncio.ensure_future(self.ask(text, items[-1][1]))
        self.update_state()

    def _on_first_audio(self, generation: int, synth_s: float) -> None:
        turn = self.last_turn
        now = time.monotonic()
        if turn and turn.generation == generation and not turn.first_audio_logged:
            turn.first_audio_logged = True
            since_send = now - turn.started
            since_speech = now - turn.utterance_end if turn.utterance_end else None
            log.info("first audio %.2f s after sending to Claude%s (first TTS chunk %.2f s)",
                     since_send, f", {since_speech:.2f} s after the user stopped speaking"
                     if since_speech is not None else "", synth_s)
            if self.timings:
                self.timings[-1].update(first_audio_after_send_s=since_send,
                                        first_audio_after_speech_s=since_speech)

    def _on_speaker_busy(self, busy: bool) -> None:
        self.speaking = busy
        self.update_state()

    # ------------------------------------------------------------------ Claude process
    def _token(self) -> str | None:
        try:
            token = self.paths.token_file.read_text().strip()
        except OSError:
            return None
        return token or None

    async def start_claude(self) -> None:
        if self.claude:
            await self.claude.stop()
            self.claude = None
        self.turn = None
        self.outbox = []
        self.turn_count = 0
        token = self._token()
        if not token:
            self.claude_error = ("No Claude token. On the host, run: agent-relay-voice token, "
                                 "then restart the service.")
            log.warning(self.claude_error)
            self.update_state()
            return
        mcp_file = self.paths.runtime_dir / "mcp.json"
        mcp_file.write_text(json.dumps(claude_mod.mcp_config(
            f"http://localhost:{self.paths.forward_port}/mcp")))
        command = claude_mod.build_command(self.paths.claude, mcp_file, VOICE_SYSTEM_PROMPT,
                                           self.settings["claude_model"])
        env = claude_mod.build_env(token, str(self.paths.home))
        self.claude_started_with_relay = self.paths.mcp_socket.exists()
        if not self.claude_started_with_relay:
            log.info("relay MCP socket %s missing; Claude Code starts without the relay tools",
                     self.paths.mcp_socket)
        proc = claude_mod.ClaudeProcess(command, env, self.paths.home / "work",
                                        self._on_claude_event, lambda c, e: None)
        proc.on_exit = lambda code, err, p=proc: self._on_claude_exit(p, code, err)
        try:
            await proc.start()
        except OSError as error:
            self.claude_error = f"Claude Code could not be started: {error}"
            log.error(self.claude_error)
            self.update_state()
            return
        self.claude = proc
        self.claude_error = None
        self.update_state()

    def _on_claude_exit(self, proc: claude_mod.ClaudeProcess, code: int, stderr: str) -> None:
        if proc is not self.claude or proc.closing:
            return
        self.claude = None
        tail = stderr.strip().splitlines()[-1] if stderr.strip() else ""
        self.claude_error = f"Claude Code exited ({code}){': ' + tail if tail else ''}"
        if self.turn:
            self._end_turn(self.turn.text)
        self.update_state()

    async def new_conversation(self) -> None:
        self.speaker.stop()
        await self.start_claude()

    # ------------------------------------------------------------------ relay
    def _start_forwarder(self) -> None:
        cmd = [self.paths.socat,
               f"TCP-LISTEN:{self.paths.forward_port},bind=127.0.0.1,reuseaddr,fork",
               f"UNIX-CONNECT:{self.paths.mcp_socket}"]
        try:
            self.forwarder = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as error:
            log.error("cannot start the relay forwarder: %s", error)

    async def _watch_forwarder(self) -> None:
        while True:
            await asyncio.sleep(5)
            if self.forwarder is not None and self.forwarder.poll() is not None:
                log.warning("relay forwarder exited (%s); restarting", self.forwarder.returncode)
                self._start_forwarder()

    def _on_relay_connected(self, connected: bool) -> None:
        if connected:
            asyncio.ensure_future(self.refresh_projects())

    def _on_relay_event(self, event: dict) -> None:
        if event.get("type") == "run_started" and event.get("project") not in self.relay_projects:
            asyncio.ensure_future(self.refresh_projects())
        notice = notice_for(event, self.settings["notice_events"])
        if notice:
            log.info("relay notice: %s", notice)
            self.notices.append(notice)
            if self.idle():
                self._deliver_pending()

    def _deliver_pending(self) -> None:
        if not self.notices or not self.idle():
            return
        notices, self.notices = self.notices, []
        if self.claude and self.claude.alive:
            asyncio.ensure_future(self.ask(" ".join(notices)))
        else:
            for notice in notices:
                self.speaker.say(spoken_notice(notice))
            self.speaker.end()

    async def refresh_projects(self) -> None:
        if not self.paths.mcp_socket.exists():
            return
        try:
            text = await asyncio.to_thread(call_tool, self.paths.mcp_socket, "list_projects")
        except Exception as error:  # noqa: BLE001 - only hotwords depend on it
            log.info("list_projects through the relay failed: %s", error)
            return
        names = project_names(text)
        if names != self.relay_projects:
            log.info("projects from the relay: %s", ", ".join(names))
        self.relay_projects = names

    async def _refresh_projects_loop(self) -> None:
        while True:
            await self.refresh_projects()
            await asyncio.sleep(600)

    # ------------------------------------------------------------------ control socket
    async def handle_command(self, cmd: str, args: dict):
        if cmd == "state":
            return self.state()
        if cmd == "options":
            return {"stt": stt_mod.options(), "tts": tts_mod.options(),
                    "claude_models": claude_mod.MODELS}
        if cmd == "set":
            return await self._set(args)
        if cmd == "stop_speaking":
            self.speaker.stop()
            if self.turn and not self.turn.silenced:
                self.turn.silenced = True
                if self.claude and self.claude.alive:
                    await self.claude.interrupt()
                    self.loop.call_later(15, self._interrupt_timeout, self.turn)
            self.update_state()
            return self.state()
        if cmd == "new_conversation":
            await self.new_conversation()
            self.last_user = self.last_assistant = ""
            self.update_state()
            return self.state()
        raise CommandError(f"unknown command {cmd!r}")

    async def _set(self, args: dict):
        allowed = {"stt", "tts", "claude_model", "listening"}
        unknown = set(args) - allowed
        if unknown:
            raise CommandError(f"unknown setting(s): {', '.join(sorted(unknown))}")
        if not args:
            raise CommandError("nothing to set")
        if "stt" in args and args["stt"] not in stt_mod.MODELS:
            raise CommandError(f"unknown stt {args['stt']!r}")
        if "tts" in args and args["tts"] not in tts_mod.ENGINES:
            raise CommandError(f"unknown tts {args['tts']!r}")
        if "claude_model" in args and args["claude_model"] not in {m["id"] for m in claude_mod.MODELS}:
            raise CommandError(f"unknown claude_model {args['claude_model']!r}")
        if "listening" in args and not isinstance(args["listening"], bool):
            raise CommandError("listening must be true or false")
        old = {k: self.settings[k] for k in args}
        self.settings.update(**args)
        if "listening" in args:
            if not args["listening"]:
                self.hearing = False
        if "stt" in args and args["stt"] != old["stt"] and self.load_models:
            # marked now, so the reply already says what is loading
            self.loading["stt"] = f"Speech recognition: {stt_mod.MODELS[args['stt']]['label']}"
            asyncio.ensure_future(self.load_stt(args["stt"]))
        if "tts" in args and args["tts"] != old["tts"] and self.load_models:
            self.loading["tts"] = f"Speech output: {tts_mod.ENGINES[args['tts']]['label']}"
            asyncio.ensure_future(self.load_tts(args["tts"]))
        if "claude_model" in args and args["claude_model"] != old["claude_model"]:
            await self.new_conversation()
        self.update_state()
        return self.state()
