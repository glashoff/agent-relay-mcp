# Open conversations: plan

Status: decided and built 2026-11-11.

Until now the relay started one `claude -p` run per request and closed its
input as soon as a result came after the last message passed in. Since
Claude Code has background tasks (subagents started with the Agent tool in
the background, shell commands run in the background), a result no longer
means "done". This plan keeps Claude Code open as a conversation instead.

## 1. The problem

Seen on the real system: the main agent started a background subagent and
ended its turn with a result. The runner closed the FIFO at once and wrote
`closed`, so every further `send_message` was queued ("the request is
already ending") for many minutes. Worse, with its input closed Claude Code
waits for background tasks only up to a ceiling, then prints "Background
tasks still running after 600s; terminating" and kills them: the
subagent's work was lost.

Claude Code reports background tasks in its stream-json output:

- `{"type":"system","subtype":"background_tasks_changed","tasks":[...]}`: the
  background tasks of the main agent still open (`task_id`, `task_type`,
  `description`), after every change.
- `task_started` (`task_id`, `tool_use_id`, `is_backgrounded`, and
  `owned_by_subagent` for tasks of a subagent), `task_progress`,
  `task_updated` (`patch.status`: `completed`, `killed`, ...) and
  `task_notification` (`status`: `completed`, `stopped`, ...).
- With its input still open, when a background task ends Claude Code takes
  the notification up in a pass of its own: `system/init`, assistant
  messages, and a second `result`.

## 2. Design

- **One long-lived run per conversation** (project and kind). Every message
  sent while the run is open is passed in through the FIFO. The queue only
  remains while the run is being started (or is just ending), and for
  messages that wait for approval (unchanged).
- **A request is done, the run goes on.** A request (`Turn`) is done when a
  result has come after the last message passed in (`mark`: the size of
  `out.jsonl` when it was passed in), after any work since (a pass beginning,
  assistant output, a background task of the main agent ending), and no
  background task is open. `background_tasks_changed` is the authority for
  open tasks; `task_started` (backgrounded, not owned by a subagent) and
  `task_notification` / `task_updated` keep track where it is missing. If a
  background task ended and no pass takes it up within two minutes, the
  request counts as done anyway.
- That "done" drives everything a request's end drove before: `FINISHED` in
  `get_updates`, the `run_ended` event, keep-awake, the activity notices.
  While a background task runs, `get_updates` shows the reply so far and the
  open tasks, and `send_message` with `wait_seconds` returns once the message
  is answered.
- **Each message to an open run that is done starts a new request** (a new
  `Turn` sharing the run: same run directory and pid; its output starts where
  the previous one was read). The project log gets a "Request" header for it,
  as for a new run.
- If Claude Code begins a pass on its own after a request was done (a
  message that arrived just after the result, a background task the relay
  did not know of), the request is working again and is reported again.

## 3. States

Per turn: `starting` → `running` → `finished` or `failed`; `finished` with
the run still open means Claude Code waits for the next message. Per run:
open → closing (`close` requested; no message passes in any more) → ended
(Claude Code exited, `exit` written).

| Situation | Message sent | Counts as work (keep-awake, grant idle time) |
|---|---|---|
| starting | queued, passed in once it runs | yes |
| running (incl. background tasks) | passed in, same request | yes |
| finished, run open | passed in, new request | no |
| finished, run closing | queued, new run once it has ended | no |
| ended | new run with `--resume` | no |

## 4. What ends a run

The relay closes the run's input; Claude Code finishes what it has
(background tasks included) and exits. Ending is never by a result alone:

- `conversation_idle_minutes` (default 15) without a message, with the
  request done and no background task open. 0 closes right after each
  request.
- The grant covering it ends or is revoked: a run that waits is closed at
  once, a working one once it is done. "Revoke and stop" stops it.
- `stop`, `stop_project` (a waiting run is closed, a working one killed as
  before), `release_access`.
- A switch between dev container and host: as before only while the other
  side is idle, which now means "done"; its open run is closed first.
- A new conversation (`new_conversation`), and a new run of the same
  conversation (after a failure).
- The project gaining or losing its dev container.

`CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0` is set for every run, so when the
input does get closed while background tasks run, Claude Code waits for them
instead of killing them after ten minutes.

## 5. The runner

POSIX sh, as before (it also runs in dev containers through `docker exec`).
It starts Claude Code with its input from the FIFO, writes the first message,
and keeps the FIFO open on fd 3 until the file `close` appears in the run
directory; then it writes `closed`, closes fd 3, waits for Claude Code and
writes `exit`. The closing decision is the relay's now. The relay writes
`close` and passes messages in only while it holds `lock` (flock), and
`_pass_in` refuses once `close`, `closed` or `exit` exists, so a message is
either completely in the FIFO before it closes (Claude Code reads it before
the end of input) or refused and queued: none is lost.

## 6. Restart

The runner, not the relay, holds the FIFO open, so a run outlives a restart
of the relay (host runs in their own systemd scope, as before). `state.json`
keeps per turn the run id, pid, `base` (where its output starts), `mark`,
`done_at`, `done_pos`, `closed` and `ended`. After a restart the relay reads
the turn's output again from `base`, which rebuilds the open background
tasks, passes the next message into the same FIFO, and keeps applying the
idle timeout (from `done_at`). Each poll also reports whether `close` or
`closed` exists, so a close the state did not record yet is noticed. An open
run that waits is checked by the sweeper (every 10 seconds); a working one
is followed every 2 seconds, as before.

## 7. Compatibility

- Events: `run_started` and `run_ended` keep their names and fields and now
  mean the start and the end of a request; `run_ended` has `open: true` when
  Claude Code stays open. `process_ended` (`project`, `kind`, `reason`) is
  new: the run ended (idle, approval ended, stopped, switched, exited, ...).
  The GNOME extension, the voice service and `agent-relay-ctl watch` ignore
  or print events they do not know, so they work unchanged.
- `state()` (D-Bus `State`) has a new field per project, `open`: the kind
  whose run waits for the next message, or null. `busy` is unchanged: a
  waiting run is not busy. `agent-relay-ctl` shows it as "open for
  follow-ups".
- Runs the relay started before this change (their runner closes the FIFO by
  itself after a result) are still handled: their `state.json` entries load
  (missing fields have defaults), `_pass_in` still updates their `mark`, the
  `closed` file they write is noticed, so messages wait until they end and
  then resume the conversation in a new run.
- The run directory cleanup now keeps a directory older than 7 days while
  something in it is still written to.
- Dev containers: base-devcontainer's idle-stop counts a running `claude`
  process as activity, so an open run keeps its container up until the
  conversation idle timeout closes it. Accepted.
