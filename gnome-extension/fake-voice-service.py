#!/usr/bin/env python3
"""Test helper: a fake voice service for the GNOME extension.

Implements the voice control socket of docs/voice-plan.md, section 8, on
$XDG_RUNTIME_DIR/agent-relay-voice/control.sock (or --socket), without any
audio, speech recognition or Claude: state, options, set, stop_speaking,
new_conversation, and the pushed "state" and "exchange" events. Only for
trying the extension's "Voice" submenu, e.g. in test-in-headless-shell.sh;
it is not part of the extension and is not installed.

    gnome-extension/fake-voice-service.py [--socket PATH] [--demo SECONDS]
    gnome-extension/fake-voice-service.py send '{"cmd": "state"}' [--socket PATH]

--demo plays a conversation in a loop (hearing, transcribing, thinking,
speaking, an exchange) every SECONDS. To drive it from a test, "send" also
accepts these commands that the real service does not have:

    {"cmd": "fake_phase", "phase": "muted", "error": null, "loading": null}
    {"cmd": "fake_exchange", "user": "...", "assistant": "..."}
    {"cmd": "fake_quit"}         (closes the socket and exits)
"""

import argparse
import asyncio
import json
import os
import socket
import sys

OPTIONS = {
    "stt": [
        {"id": "fw-turbo", "label": "faster-whisper large-v3-turbo"},
        {"id": "fw-turbo-de", "label": "faster-whisper turbo, German fine-tune"},
        {"id": "fw-small", "label": "faster-whisper small"},
        {"id": "wcpp-f16", "label": "whisper.cpp turbo f16"},
        {"id": "wcpp-q8", "label": "whisper.cpp turbo q8_0"},
    ],
    "tts": [
        {"id": "pocket-24l", "label": "Pocket TTS juergen (24 layers)"},
        {"id": "pocket-6l", "label": "Pocket TTS (6 layers)"},
        {"id": "piper-thorsten", "label": "Piper thorsten-high"},
    ],
    "claude_models": [
        {"id": "default", "label": "Claude Code default"},
        {"id": "haiku", "label": "Haiku"},
        {"id": "sonnet", "label": "Sonnet"},
        {"id": "opus", "label": "Opus"},
    ],
}
PHASES = {"starting", "listening", "paused", "muted", "hearing", "transcribing",
          "thinking", "speaking", "error"}
SETTING_LISTS = {"stt": "stt", "tts": "tts", "claude_model": "claude_models"}


def default_socket():
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return os.path.join(runtime, "agent-relay-voice", "control.sock")


class FakeVoice:
    def __init__(self):
        self.listening = True
        self.state = {
            "phase": "listening", "error": None, "loading": None,
            "settings": {"stt": "fw-turbo", "tts": "pocket-24l", "claude_model": "default"},
            "last_user": "", "last_assistant": "",
        }
        self.clients = set()
        self.server = None

    def push(self, message):
        line = (json.dumps(message) + "\n").encode()
        for writer in list(self.clients):
            try:
                writer.write(line)
            except Exception:
                self.clients.discard(writer)

    def change(self, **fields):
        self.state.update(fields)
        self.push({"event": "state", "state": self.state})

    def resting_phase(self):
        return "listening" if self.listening else "paused"

    async def load(self, what):
        self.change(loading=what)
        await asyncio.sleep(2)
        self.change(loading=None)

    def handle(self, request):
        cmd = request.get("cmd")
        if cmd == "state":
            return self.state
        if cmd == "options":
            return OPTIONS
        if cmd == "set":
            known = set(SETTING_LISTS) | {"listening"}
            unknown = [k for k in request if k not in known | {"id", "cmd"}]
            if unknown:
                raise ValueError(f"unknown setting: {', '.join(unknown)}")
            for key, options in SETTING_LISTS.items():
                if key in request and request[key] not in {o["id"] for o in OPTIONS[options]}:
                    raise ValueError(f"no such {key}: {request[key]}")
            if "listening" in request:
                if not isinstance(request["listening"], bool):
                    raise ValueError("listening must be true or false")
                self.listening = request["listening"]
                if self.state["phase"] in {"listening", "paused", "muted", "hearing"}:
                    self.change(phase=self.resting_phase())
            settings = dict(self.state["settings"])
            for key in SETTING_LISTS:
                if key in request and request[key] != settings[key]:
                    settings[key] = request[key]
                    if key == "claude_model":
                        # a new model starts a new conversation
                        self.state.update(last_user="", last_assistant="")
                    else:
                        label = next(o["label"] for o in OPTIONS[SETTING_LISTS[key]]
                                     if o["id"] == request[key])
                        asyncio.get_running_loop().create_task(self.load(label))
            self.change(settings=settings)
            return None
        if cmd == "stop_speaking":
            if self.state["phase"] == "speaking":
                self.change(phase=self.resting_phase())
            return None
        if cmd == "new_conversation":
            self.change(last_user="", last_assistant="")
            return None
        if cmd == "fake_phase":
            if request.get("phase") not in PHASES:
                raise ValueError("no such phase")
            self.change(phase=request["phase"], error=request.get("error"),
                        loading=request.get("loading"))
            return None
        if cmd == "fake_exchange":
            self.state.update(last_user=request.get("user", ""),
                              last_assistant=request.get("assistant", ""))
            self.push({"event": "exchange", "user": self.state["last_user"],
                       "assistant": self.state["last_assistant"]})
            return None
        if cmd == "fake_quit":
            asyncio.get_running_loop().call_later(0.1, self.quit)
            return None
        raise ValueError(f"unknown command: {cmd}")

    def quit(self):
        self.server.close()
        for writer in list(self.clients):
            writer.close()

    async def client(self, reader, writer):
        self.clients.add(writer)
        try:
            while line := await reader.readline():
                try:
                    request = json.loads(line)
                    rid = request.get("id")
                except (ValueError, AttributeError):
                    continue
                try:
                    reply = {"id": rid, "ok": True, "result": self.handle(request)}
                except ValueError as error:
                    reply = {"id": rid, "ok": False, "error": str(error)}
                writer.write((json.dumps(reply) + "\n").encode())
                await writer.drain()
        except ConnectionError:
            pass
        finally:
            self.clients.discard(writer)
            writer.close()

    async def demo(self, every):
        turns = [
            ("Wie ist das Wetter morgen in Hamburg?",
             "Morgen wird es in Hamburg bewölkt bei etwa zwölf Grad, "
             "am Nachmittag kann es regnen."),
            ("Schick bitte an das Projekt web: refactor den Parser und schreib Tests.",
             "Ich habe den Auftrag an das Projekt web geschickt. Es wartet auf deine "
             "Freigabe am Laptop, Nummer drei."),
        ]
        n = 0
        while True:
            await asyncio.sleep(every)
            if not self.listening or self.state["phase"] not in {"listening", "muted"}:
                continue
            user, assistant = turns[n % len(turns)]
            n += 1
            for phase, wait in [("hearing", 1.5), ("transcribing", 1.5), ("thinking", 2),
                                ("speaking", 3)]:
                self.change(phase=phase)
                await asyncio.sleep(wait)
                if self.state["phase"] != phase:     # stopped from the menu
                    break
            self.state.update(last_user=user, last_assistant=assistant)
            self.push({"event": "exchange", "user": user, "assistant": assistant})
            self.change(phase=self.resting_phase())


async def serve(path, demo):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    if os.path.exists(path):
        os.unlink(path)
    voice = FakeVoice()
    voice.server = await asyncio.start_unix_server(voice.client, path=path)
    os.chmod(path, 0o600)
    print(f"fake voice service on {path}", flush=True)
    if demo:
        asyncio.get_running_loop().create_task(voice.demo(demo))
    try:
        async with voice.server:
            await voice.server.serve_forever()
    except asyncio.CancelledError:
        pass
    finally:
        if os.path.exists(path):
            os.unlink(path)


def send(path, text):
    request = json.loads(text)
    request.setdefault("id", 1)
    with socket.socket(socket.AF_UNIX) as s:
        s.connect(path)
        s.sendall((json.dumps(request) + "\n").encode())
        for line in s.makefile():
            message = json.loads(line)
            if message.get("id") == request["id"]:
                print(json.dumps(message))
                return 0 if message.get("ok") else 1
    return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("action", nargs="?", choices=["serve", "send"], default="serve")
    parser.add_argument("request", nargs="?", help="JSON request, for send")
    parser.add_argument("--socket", default=default_socket())
    parser.add_argument("--demo", type=float, default=0,
                        help="play a conversation every SECONDS")
    args = parser.parse_args()
    if args.action == "send":
        return send(args.socket, args.request)
    try:
        asyncio.run(serve(args.socket, args.demo))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
