"""Text preparation for speech output: sentence splitting while text streams
in, cleanup of markup the model should not produce but sometimes does, and the
pronunciation dictionary (used for Piper only; it made Pocket TTS worse)."""

from __future__ import annotations

import re

DEFAULT_DICTIONARY = """\
# One entry per line: Term=Replacement. Case-insensitive, whole words only,
# longer terms win. Lines starting with # are ignored.
# Tuned against espeak-ng "de" (used by Piper); words espeak already gets
# right (Commit, Server, Push, Release, Workflow) are left out.
Claude Code=Klohd Kohd
Claude=Klohd
Pull Requests=Pull Rikwests
Pull Request=Pull Rikwest
Dev-Container=Däw-Kontehner
Container=Kontehner
Deploy=Diploy
deployen=diployen
deployt=diployt
Branches=Bräntsches
Branch=Bräntsch
Reverse Proxy=Riwörs Proxy
GitHub Actions=Gitt Habb Äktschens
GitHub=Gitt Habb
Rebase=Riebeis
Feature=Fietscher
Cache=Käsch
cached=käscht
Image=Immitsch
Pipeline=Peipleien
Merge=Mörtsch
Update=Apdeit
Bug=Bagg
Check=Tscheck
"""

# Abbreviations whose trailing dot does not end a sentence.
_ABBREVIATIONS = {
    "z.B", "z. B", "d.h", "d. h", "u.a", "usw", "bzw", "ca", "ggf", "evtl", "inkl",
    "vgl", "Nr", "Dr", "Prof", "etc", "bspw", "sog", "zzgl", "max", "min", "B", "h", "a",
}

# A sentence end: terminator(s), optional closing quote/bracket, then whitespace.
_END = re.compile(r"[.!?…:;]+[\"'“”»)]*(?=\s)")
# Longest piece handed to TTS at once; longer runs are cut at a comma or space.
MAX_SENTENCE = 260


def parse_dictionary(text: str) -> list[tuple[str, str]]:
    entries = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        term, repl = (part.strip() for part in line.split("=", 1))
        if term:
            entries.append((term, repl))
    # Longest first so "Claude Code" is replaced before "Claude".
    entries.sort(key=lambda e: len(e[0]), reverse=True)
    return entries


def apply_dictionary(text: str, entries: list[tuple[str, str]]) -> str:
    if not entries:
        return text
    lookup = {term.lower(): repl for term, repl in entries}
    pattern = re.compile(
        r"(?<![\w-])(" + "|".join(re.escape(t) for t, _ in entries) + r")(?![\w-])",
        re.IGNORECASE,
    )
    return pattern.sub(lambda m: lookup[m.group(0).lower()], text)


def clean_for_speech(text: str) -> str:
    """Removes markup that would be read aloud literally."""
    text = re.sub(r"```.*?(```|$)", " ", text, flags=re.S)       # code blocks
    text = re.sub(r"`([^`]*)`", r"\1", text)                     # inline code
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)       # links -> their text
    text = re.sub(r"https?://\S+", "", text)                      # bare URLs
    text = re.sub(r"(\*\*|__|\*|_)(?=\S)(.+?)(?<=\S)\1", r"\2", text)  # emphasis
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", text)             # headings
    text = re.sub(r"(?m)^\s*(?:[-*+•]|\d+[.)])\s+", "", text)     # list markers
    text = re.sub(r"(?m)^\s*\|.*\|\s*$", "", text)                # table rows
    text = text.replace("*", "").replace("#", "")
    return " ".join(text.split())


def _is_abbreviation(before: str, terminator: str) -> bool:
    if terminator != ".":
        return False
    last_word = before.rsplit(" ", 1)[-1] if before else ""
    # single letters: "z. B.", "d. h.", initials
    return (last_word in _ABBREVIATIONS or last_word.isdigit()
            or (len(last_word) == 1 and last_word.isalpha()))


def _cut_long(sentence: str) -> list[str]:
    out = []
    while len(sentence) > MAX_SENTENCE:
        cut = sentence.rfind(", ", 0, MAX_SENTENCE)
        if cut < MAX_SENTENCE // 3:
            cut = sentence.rfind(" ", 0, MAX_SENTENCE)
        if cut <= 0:
            cut = MAX_SENTENCE
        out.append(sentence[:cut + 1].strip())
        sentence = sentence[cut + 1:].strip()
    if sentence:
        out.append(sentence)
    return out


def split_sentences(text: str) -> list[str]:
    """Split a complete text at sentence ends and line breaks."""
    streamer = SentenceStreamer()
    return streamer.feed(text) + streamer.flush()


class SentenceStreamer:
    """Collects streamed text deltas and returns each sentence as soon as it is
    complete, i.e. when its terminator is followed by whitespace. Line breaks
    end a sentence too (list items, paragraphs). Fenced code blocks are
    skipped entirely."""

    def __init__(self) -> None:
        self._buffer = ""
        self._line_start = True
        self._in_code = False

    def feed(self, delta: str) -> list[str]:
        self._buffer += delta
        sentences: list[str] = []
        while True:
            newline = self._buffer.find("\n")
            if self._in_code or (self._line_start and self._maybe_fence()):
                # a whole line is needed to know whether a code block starts or ends
                if newline < 0:
                    break
                line = self._buffer[:newline + 1]
                if line.strip().startswith("```") or self._in_code:
                    if line.strip().startswith("```"):
                        self._in_code = not self._in_code
                    self._buffer = self._buffer[newline + 1:]
                    self._line_start = True
                    continue
                self._line_start = False      # an ordinary line after all
            # A line break ends whatever is before it.
            search_in = self._buffer if newline < 0 else self._buffer[:newline + 1]
            end = None
            for m in _END.finditer(search_in):
                if _is_abbreviation(search_in[:m.start()].rstrip(), m.group(0)):
                    continue
                end = m.end()
                break
            if end is None and newline >= 0:
                end = newline + 1
            if end is None:
                break
            piece, self._buffer = self._buffer[:end], self._buffer[end:]
            self._line_start = piece.endswith("\n")
            sentences += self._emit(piece)
        return sentences

    def _maybe_fence(self) -> bool:
        head = self._buffer.lstrip(" \t")[:3]
        return head.startswith("```") or (head != "" and "```".startswith(head)) or head == ""

    def flush(self) -> list[str]:
        piece, self._buffer = self._buffer, ""
        in_code, self._in_code = self._in_code, False
        self._line_start = True
        if in_code or piece.strip().startswith("```"):
            return []
        return self._emit(piece)

    @staticmethod
    def _emit(piece: str) -> list[str]:
        text = clean_for_speech(piece)
        # Pieces without a single letter or digit (a stray "-", "**") are not spoken.
        if not re.search(r"\w", text):
            return []
        return _cut_long(text)
