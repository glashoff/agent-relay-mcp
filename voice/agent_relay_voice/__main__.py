"""Command line of the voice service (inside the container).

  serve        the service (default)
  probe        live microphone levels: RMS, share of exact zeros, mute state, VAD
  listen       audio loop only: prints mute changes and transcripts
  say TEXT     speak TEXT with the configured (or --tts) engine; --out writes a WAV
  transcribe WAV  transcribe a file with the configured (or --stt) model
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path

from .settings import Settings


def _settings(args) -> Settings:
    return Settings(Path(args.config) / "settings.json")


def cmd_serve(args) -> None:
    from .service import Paths, VoiceService

    paths = Paths(config_dir=Path(args.config))
    if args.control_socket:
        paths.control_socket = Path(args.control_socket)
    if args.relay_dir:
        paths.relay_dir = Path(args.relay_dir)
    service = VoiceService(paths, _settings(args), load_models=not args.no_models,
                           capture=not args.no_capture)
    asyncio.run(service.run())


def cmd_probe(args) -> None:
    from .audio import Capture
    from .mute import MuteDetector
    from .vad import WINDOW, SileroStream

    s = _settings(args)
    mute = MuteDetector(s["mute_rms"], s["mute_zero_fraction"], s["mute_hold_s"])
    vad = SileroStream()
    started = time.monotonic()
    print(f"mute rule: RMS < {s['mute_rms']} and zeros > {s['mute_zero_fraction']:.0%} "
          f"for {s['mute_hold_s']} s. Ctrl+C to stop.", flush=True)
    print("  time     rms  zeros  state   vad", flush=True)

    def on_block(block):
        muted = mute.update(block)
        audio = block.astype("float32") / 32768.0
        prob = max(vad(audio[i:i + WINDOW]) for i in range(0, len(audio) - WINDOW + 1, WINDOW))
        bar = "#" * min(40, int(prob * 40))
        print(f"{time.monotonic() - started:6.1f} {mute.last_rms:7.1f} {mute.last_zeros:6.0%}  "
              f"{'MUTED ' if muted else 'open  '} {prob:4.2f} {bar}", flush=True)

    def on_error(error):
        if error:
            print(f"error: {error}", file=sys.stderr, flush=True)

    capture = Capture(on_block, on_error)
    capture.start()
    _wait(args.seconds)
    capture.stop()


def _wait(seconds: float | None) -> None:
    try:
        if seconds:
            time.sleep(seconds)
        else:
            while True:
                time.sleep(3600)
    except KeyboardInterrupt:
        pass


def cmd_listen(args) -> None:
    import queue

    import numpy as np

    from . import stt
    from .audio import Capture
    from .mute import MuteDetector
    from .vad import Segmenter, SileroStream

    s = _settings(args)
    model = args.stt or s["stt"]
    print(f"loading {model} ...", flush=True)
    t0 = time.perf_counter()
    engine = stt.load(model)
    stt.warmup(engine)
    print(f"ready in {time.perf_counter() - t0:.1f} s; speak (Ctrl+C to stop)", flush=True)
    mute = MuteDetector(s["mute_rms"], s["mute_zero_fraction"], s["mute_hold_s"])
    seg = Segmenter(SileroStream(), s["vad_threshold"], s["vad_silence_s"],
                    s["min_utterance_s"], s["max_utterance_s"])
    utterances: queue.Queue = queue.Queue()
    state = {"muted": None}

    def on_block(block):
        muted = mute.update(block)
        if muted != state["muted"]:
            state["muted"] = muted
            print("[muted]" if muted else "[listening]", flush=True)
        if muted:
            seg.reset()
            return
        for event in seg.push(block.astype(np.float32) / 32768.0):
            if event.kind == "end":
                utterances.put(event.audio)
            elif event.kind == "confirmed":
                print("[hearing]", flush=True)

    capture = Capture(on_block, lambda e: e and print(f"error: {e}", flush=True))
    capture.start()
    hotwords = ", ".join(s["hotwords"])
    try:
        while True:
            audio = utterances.get()
            t0 = time.perf_counter()
            text = engine.transcribe(audio, language=s["stt_language"] or None, prompt=s["stt_prompt"],
                                     hotwords=hotwords, beam_size=s["stt_beam_size"])
            print(f"({len(audio) / 16000:.1f} s audio, {time.perf_counter() - t0:.2f} s) {text}",
                  flush=True)
    except KeyboardInterrupt:
        pass
    capture.stop()
    engine.close()


def cmd_say(args) -> None:
    from . import tts
    from .speaker import Speaker
    from .textproc import split_sentences

    s = _settings(args)
    engine_id = args.tts or s["tts"]
    t0 = time.perf_counter()
    engine = tts.load(engine_id, dictionary=s["pronunciation"], pocket_voice=s["pocket_voice"],
                      pocket_gain_db=s["pocket_gain_db"])
    engine.warmup()
    print(f"{engine_id} loaded in {time.perf_counter() - t0:.1f} s", flush=True)
    done = []
    first = []
    speaker = Speaker(on_busy=lambda b: done.append(not b),
                      on_first_audio=lambda g, t: first.append(t),
                      out_file=Path(args.out) if args.out else None)
    speaker.engine = engine
    t0 = time.perf_counter()
    for sentence in split_sentences(args.text):
        speaker.say(sentence)
    speaker.end()
    while not (done and done[-1]):
        time.sleep(0.05)
    print(f"first audio after {first[0] if first else float('nan'):.2f} s, "
          f"done after {time.perf_counter() - t0:.2f} s", flush=True)


def cmd_transcribe(args) -> None:
    from faster_whisper.audio import decode_audio

    from . import stt

    s = _settings(args)
    model = args.stt or s["stt"]
    t0 = time.perf_counter()
    engine = stt.load(model)
    stt.warmup(engine)
    print(f"{model} loaded in {time.perf_counter() - t0:.1f} s", flush=True)
    audio = decode_audio(args.file, sampling_rate=16000)
    for _ in range(args.repeat):
        t0 = time.perf_counter()
        text = engine.transcribe(audio, language=s["stt_language"] or None, prompt=s["stt_prompt"],
                                 hotwords=", ".join(s["hotwords"]), beam_size=s["stt_beam_size"])
        print(f"({len(audio) / 16000:.1f} s audio, {time.perf_counter() - t0:.2f} s) {text}",
              flush=True)
    engine.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="agent-relay-voice")
    parser.add_argument("--config", default=os.environ.get("VOICE_CONFIG", "/config"),
                        help="directory with settings.json")
    parser.add_argument("-v", "--verbose", action="store_true")
    # without a subcommand: serve
    parser.set_defaults(func=cmd_serve, control_socket=None, relay_dir=None, no_models=False,
                        no_capture=False)
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("serve", help="run the service")
    p.add_argument("--control-socket")
    p.add_argument("--relay-dir")
    p.add_argument("--no-models", action="store_true", help="do not load STT/TTS (tests)")
    p.add_argument("--no-capture", action="store_true", help="no microphone (tests)")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("probe", help="show live microphone levels and the mute state")
    p.add_argument("--seconds", type=float)
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("listen", help="print mute changes and transcripts")
    p.add_argument("--stt")
    p.set_defaults(func=cmd_listen)

    p = sub.add_parser("say", help="speak a text")
    p.add_argument("text")
    p.add_argument("--tts")
    p.add_argument("--out", help="write a WAV file instead of playing")
    p.set_defaults(func=cmd_say)

    p = sub.add_parser("transcribe", help="transcribe an audio file")
    p.add_argument("file")
    p.add_argument("--stt")
    p.add_argument("--repeat", type=int, default=1)
    p.set_defaults(func=cmd_transcribe)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    for noisy in ("httpx", "httpx2", "huggingface_hub", "urllib3", "filelock"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if args.func is not cmd_serve:
        # docker stop / a killed docker CLI: end like Ctrl+C
        signal.signal(signal.SIGTERM, _interrupt)
    args.func(args)


def _interrupt(signum, frame):
    raise KeyboardInterrupt


if __name__ == "__main__":
    main()
