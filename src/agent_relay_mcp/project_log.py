"""A readable log of each project's conversation, in the project folder:
.relay/log.md, for following along live in an editor's Markdown preview.

It is written by the relay on this computer as the run's output comes in.
The folder is also writable from the project's dev container, so the relay
never follows a symlink there and only appends to a regular file: a container
cannot make the relay write somewhere else. Everything that comes from Claude
Code is written as text with HTML and images defused, so a preview loads
nothing on its own.
"""

from __future__ import annotations

import errno
import logging
import os
import stat
import time
from pathlib import Path

log = logging.getLogger("agent_relay_mcp")

DIR = ".relay"
NAME = "log.md"
OLD = "log.1.md"
MAX_BYTES = 1024 * 1024         # rotated to log.1.md beyond this
MAX_TEXT = 6000                 # characters of one entry
MAX_THINKING = 3000

_FLAGS = os.O_NOFOLLOW | os.O_CLOEXEC


def clock() -> str:
    return time.strftime("%H:%M:%S")


def _cut(text: str, limit: int) -> str:
    text = str(text).strip()
    return text if len(text) <= limit else text[:limit] + " …(shortened)"


def safe(text: str, limit: int = MAX_TEXT) -> str:
    """Markdown as Claude Code wrote it, but no HTML and no images: angle
    brackets and ampersands are escaped, and "![" loses its meaning."""
    text = _cut(text, limit)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return text.replace("![", "!\\[")


def quoted(text: str, limit: int = MAX_TEXT) -> str:
    return "\n".join("> " + line if line else ">" for line in safe(text, limit).splitlines())


def inline(text: str) -> str:
    """One line, for a step: no backticks, so it fits in a code span."""
    return " ".join(str(text).split()).replace("`", "'")[:300]


class ProjectLog:
    def __init__(self, folder: Path, max_bytes: int = MAX_BYTES):
        self.folder = folder
        self.max_bytes = max_bytes
        self._warned = False

    def _dir_fd(self) -> int:
        """The .relay folder, created if missing; never through a symlink."""
        base = os.open(self.folder, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            try:
                os.mkdir(DIR, 0o755, dir_fd=base)
            except FileExistsError:
                pass
            fd = os.open(DIR, os.O_RDONLY | os.O_DIRECTORY | _FLAGS, dir_fd=base)
        finally:
            os.close(base)
        try:
            # git ignores everything in the folder, this file included,
            # without a change to the project's own .gitignore.
            ignore = os.open(".gitignore", os.O_WRONLY | os.O_CREAT | os.O_EXCL | _FLAGS,
                             0o644, dir_fd=fd)
            with os.fdopen(ignore, "w") as f:
                f.write("*\n")
        except FileExistsError:
            pass
        return fd

    def _open(self, dir_fd: int) -> int:
        fd = os.open(NAME, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK | _FLAGS,
                     0o644, dir_fd=dir_fd)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise OSError(errno.EINVAL, f"{DIR}/{NAME} is not a regular file")
        return fd

    def write(self, text: str, *, new_request: bool = False) -> None:
        """Appends text. new_request: start of a request, where the log is
        rotated if it has grown too big. Never raises: the log must not get in
        the way of the work."""
        try:
            dir_fd = self._dir_fd()
            try:
                fd = self._open(dir_fd)
                if new_request and os.fstat(fd).st_size > self.max_bytes \
                        or os.fstat(fd).st_size > 4 * self.max_bytes:
                    os.close(fd)
                    os.replace(NAME, OLD, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
                    fd = self._open(dir_fd)
                    os.write(fd, f"*Older entries: [{OLD}]({OLD})*\n\n".encode())
                with os.fdopen(fd, "ab") as f:
                    f.write(text.encode("utf-8", errors="replace"))
            finally:
                os.close(dir_fd)
        except OSError as error:
            if not self._warned:
                log.warning("cannot write the log in %s: %s", self.folder, error)
                self._warned = True


# --- entries -----------------------------------------------------------------------

def request(message: str, *, where: str, continued: bool, earlier: str = "") -> str:
    how = "continues the conversation" if continued else "new conversation"
    text = f"\n---\n\n## {clock()} · Request ({how}, {where})\n\n{quoted(message)}\n\n"
    if earlier:
        text += f"*Reply to the request before, not reported in the chat yet:*\n\n{quoted(earlier)}\n\n"
    return text


def passed_in(message: str) -> str:
    return f"### {clock()} · Message passed in\n\n{quoted(message)}\n\n"


def queued(message: str) -> str:
    return f"### {clock()} · Message queued (follows as soon as possible)\n\n{quoted(message)}\n\n"


def waiting(number: int, messages: list[str], full_access: bool) -> str:
    warning = " · no dev container: full access" if full_access else ""
    body = "\n\n".join(quoted(m) for m in messages)
    return (f"\n---\n\n## {clock()} · Waiting for approval at the laptop "
            f"(number {number}){warning}\n\n{body}\n\n")


def note(text: str) -> str:
    return f"*{clock()} · {safe(text, 500)}*\n\n"


def step(summary: str) -> str:
    return f"- `{clock()}` `{inline(summary)}`\n"


def said(text: str) -> str:
    return f"\n{safe(text)}\n\n"


def thinking(text: str) -> str:
    return f"\n*Thinking:*\n\n{quoted(text, MAX_THINKING)}\n\n"


def reply(text: str, is_error: bool) -> str:
    title = "Ended with an error" if is_error else "Reply"
    return f"\n### {clock()} · {title}\n\n{safe(text)}\n\n"


def ended(phase: str, error: str) -> str:
    return f"\n### {clock()} · Ended: {safe(phase, 50)}\n\n{quoted(error, 1500)}\n\n"
