# Host actions for agent-relay-mcp: plan

Status: final plan, decided 2026-11-10. Not built yet.

Host actions let Claude ask for something to be run on the laptop itself,
outside any dev container: fixed actions per project (deploy, approve,
install) and ad-hoc scripts. Claude can only ask. Every single run is
approved by the owner at the laptop; there is no approval for a period of
time. The action runs in a terminal window, where the owner answers
password, SMS-code and yes/no prompts.

This builds on the existing tools: `devcontainer-deploy` runs `deploy.py`
only if it is mounted read-only and asks in the terminal;
`devcontainer-approve` shows a diff and asks. Both are made for a terminal
window and have a `--pause` option.

## 1. Decisions taken

1. **The allowed actions are a section of the existing `config.toml`**, not
   a separate file (section 3).
2. **The output goes to Claude automatically**, without a question at the
   end. Principle: outputs are built so that they contain no secrets; only
   inputs (passwords, codes) are secret, and they are never recorded. A
   checkbox in the approval dialog keeps the output of that one run from
   Claude; Claude then gets the exit code only (sections 5 and 7).
3. **`install` for agent-relay-mcp is allowed with a warning**; a clean git
   state is not required. A project without a container has full access
   anyway when it is approved; the owner accepts that risk (section 3).
4. **Interpreters for ad-hoc scripts: `bash`, `sh` and `python3`.**
5. **Later, not part of this plan:** move agent-relay-mcp into a dev
   container and protect `install.sh` through the `devcontainer-approve`
   procedure like other projects (section 10).

## 2. Principles

- **Claude can only ask.** Running anything needs the owner's approval at
  the laptop, from GNOME Shell or a terminal, once per run.
- **Outputs contain no secrets; inputs are secret.** What the owner types is
  never recorded and never reaches Claude. Actions and scripts are built so
  that their output shows nothing secret; that is why it goes to Claude
  without a question.
- **What runs is exactly what was shown**: fixed command lines, checksums
  from approval to start.

## 3. Configuration of the allowed actions

- **Place:** an `[actions.<name>]` section in the existing
  `~/.config/agent-relay-mcp/config.toml`. The file lives on the host,
  outside all project folders; containers cannot see it. Neither MCP, D-Bus
  nor the extension can change it; only the owner can, with an editor.
- **Per action:** a description, the projects it applies to, the command
  with fixed arguments (placeholder `{project}` for the project folder) and
  the files that must be mounted read-only.
  ```toml
  # ... existing settings (projects_dir, public_hosts, ...) ...

  [actions.deploy]
  description = "Deploy from the host"
  command = ["~/.local/bin/devcontainer-deploy", "--pause", "{project}"]
  projects = ["fkirchhoff.com", "server"]
  requires_protected = ["deploy.py"]

  [actions.approve]
  description = "Apply the container's proposals for protected files"
  command = ["~/.local/bin/devcontainer-approve", "{project}"]
  projects = "*"

  [actions.install]
  description = "Install or update the relay"
  command = ["{project}/scripts/install.sh"]
  projects = ["agent-relay-mcp"]
  writable_by_host_runs = true   # deliberate exception, shown with a warning
  ```
- **Fixed arguments:** Claude cannot add arguments. A variant such as
  `--dry-run` is an action of its own, for example `deploy-dry`.
- **Loading:** the config loader, which rejects unknown keys today, learns
  the nested `actions` section and checks every action strictly: known
  fields only, `command` a non-empty list of strings, `projects` a list or
  `"*"`, action names only letters, digits and hyphens. An error stops the
  relay from starting, like other config errors; `install.sh` already
  validates the config before restarting, so a typo does not take down the
  running relay. Changes take effect at the next restart of the relay.
- **`config.example.toml`** gets the three examples, commented out. Without
  an `actions` section there are no host actions.
- **Protection check:** the command must lie outside the project folder (like
  the tools in `~/.local/share`), or be mounted read-only into the
  container. The relay checks this at approval and again right before the
  start, through the mounts (`docker inspect`), without following symlinks,
  and with a checksum. If the file changes between approval and start,
  nothing runs. `devcontainer-deploy` checks for itself as well.
- **Exception `writable_by_host_runs = true`:** without it, the relay
  refuses every command that a run could change. It is allowed only for
  projects without a container; on a container project it is a config
  error, because a container could otherwise change the script that then
  runs on the host. Used for `install.sh` of agent-relay-mcp (decision 3).

## 4. MCP tools

New tools: claude.ai picks up new tools reliably, but not schema changes to
existing ones. All parameters are plain strings, numbers or booleans.

- **`request_host_action(project, action, note="")`** asks for a fixed
  action. Reply: "waiting for approval at the laptop, number 47". Unknown
  actions or projects are refused, naming the allowed ones.
- **`request_host_script(project, script, purpose, interpreter="bash")`**
  asks for an ad-hoc script. Interpreters: `bash`, `sh`, `python3`. Text
  only, limited to about 64 KB.
- **`get_host_action(number)`** gives the state: waiting, denied, expired,
  running, finished with exit code and output (or exit code only).
- **`list_projects` and `get_updates`** mention open host actions of the
  project briefly.
- **Instructions to Claude:** ask only when the user explicitly wants it,
  tell the user the number, never try to work around an approval.
- Claude Code in a container cannot ask; it has no access to the relay. It
  can ask for it in its reply, and the chat Claude then makes the request.

## 5. Approval at the laptop: menu, notification, dialog

- **Notification:** "Host action: deploy for fkirchhoff.com? Number 47",
  with "Review…" and "Deny". Nothing can be run directly from a
  notification.
- **Menu:** a "Host actions" section with waiting and running actions, each
  with "Review…", "Deny" or its state.
- **Dialog for fixed actions:** project, action, description, the exact
  command line, the working directory, and Claude's note as a quote. Button
  "Run in a terminal".
  - For actions with `writable_by_host_runs`, a warning: "This command lives
    in the project and may have been changed by Claude since your last
    review." For information only, not as a condition: the script's
    checksum and whether it differs from the last commit ("unchanged since
    commit abc123" or "modified").
- **Dialog for ad-hoc scripts:** purpose, interpreter, line count and the
  shortened checksum, with the warning "Runs on this computer as you, with
  full access". "Review in terminal…" opens the complete script read-only in
  `less` in a terminal. "Run exactly this (sha256 1a2b…)" becomes active only
  after the review. The stored, checked copy is what runs.
- **Checkbox in both dialogs:** "Don't send the output to Claude (exit code
  only)". Off by default, for this run only, not remembered. It is decided
  before the start and cannot be changed during the run. Ad-hoc dialogs add
  the hint "Its output goes to Claude unless you tick the box."
- **Approval only by the owner:** as with grants, only from GNOME Shell or a
  process with a terminal. While the screen is locked, actions wait;
  requests expire after 30 minutes.
- **Command line:** `agent-relay-ctl actions`, `review-action 47`,
  `run-action 47 [--no-output]`, `deny-action 47`.

## 6. Running, with interaction

- **Terminal window:** after "Run", the relay opens a window
  (`gnome-terminal --wait -- agent-relay-action <id>`) in its own systemd
  scope, so it survives a restart of the relay.
- **The wrapper `agent-relay-action`** checks checksum and protection once
  more, starts the command in the project folder with a clean environment
  (`PYTHONSAFEPATH`, `bash --noprofile --norc`), and attaches it to a
  pseudo-terminal. The owner answers password, SMS and yes/no prompts there
  as usual.
- **Only the output is recorded**, never the keyboard input. Passwords
  without echo (sudo, ssh, getpass) therefore never show up. Echoed input,
  such as an SMS code or a "y", is replaced by `‹input›` on a best-effort
  basis.
- **No question at the end.** When the action finishes, the terminal shows
  the exit code and "Press Enter to close". The result goes back as soon as
  the action ends, not when the window is closed.
- **Results** in `~/.local/state/agent-relay-mcp/actions/<id>/` (exit code,
  cleaned output, times, checksum), mode 0600, always stored locally, also
  when the output is withheld from Claude. The relay reads them after a
  restart too.

## 7. Return to Claude and the log

- **Default:** exit code, duration and the cleaned output, colour codes
  removed, up to about 20 KB. Longer output is cut to its beginning and end,
  with a note.
- **With the checkbox:** exit code and "output withheld by the user" only.
- **Project log `.relay/log.md`:** summaries only: "Host action deploy
  requested (#47)", "approved" or "denied", "finished, exit 0", plus
  "output withheld" where it applies; never the output itself. For ad-hoc
  scripts, the checksum.
- **Full transcript:** only in the state folder on the host, readable only
  by the owner, expiring like the run dirs.
- **Journal:** requested, approved, denied, started, finished; no contents.

## 8. Security

- **No way through MCP:** Claude can only ask. Every run needs the owner's
  approval at the laptop, once, without a standing grant. The list of
  actions, their commands and arguments are in a file that Claude and
  containers cannot reach.
- **No way out of a container:** commands from the project folder only when
  mounted read-only, with a checksum from approval to start. Ad-hoc scripts
  are stored only in the state folder on the host. A clean environment, so
  that nothing from the project folder is loaded along (such as a planted
  `argparse.py`).
- **Limit of the review:** an ad-hoc script that calls project files
  (`./build.sh`) runs whatever the container wrote there. The dialog says so;
  the review has to catch it.
- **Passwords and output:** inputs never reach Claude. Output goes to Claude
  by default, on the principle that it contains no secrets. Remaining risk:
  a tool could still print a secret, such as a token in an error message.
  For fixed actions, the owner checks once when adding them to
  `config.toml`; for ad-hoc scripts, the dialog points out that the output
  goes to Claude. There is no automatic secret filter: it would suggest a
  safety it does not give.
- **Deception:** the matching number as with requests. Claude's note and
  purpose are shown as foreign text; what counts is the command or script
  shown.
- **Host project agent-relay-mcp:** `install.sh` can be changed by host runs.
  This is accepted (decision 3): a project without a container has full
  access to the host when approved anyway, so the action makes visible what
  is already possible. The checksum makes sure that nothing else runs than
  what the dialog showed.
- **No control interface:** nothing runs; actions wait.
- **Limits:** at most one open action per project and a few in total;
  notifications throttled.

## 9. Order of implementation

1. **Core:** load and check the `actions` section of `config.toml`; action
   requests with number, expiry and denial; the MCP tools
   `request_host_action` and `get_host_action`; tests.
2. **Running:** the wrapper `agent-relay-action` with pseudo-terminal,
   recording without input, result file; start through `gnome-terminal` in
   its own scope; protection check with checksum; the "exit code only"
   option; tests (the wrapper can be tested without a window through a
   pseudo-terminal).
3. **Control:** D-Bus methods to review, run (with the output option) and
   deny, with the known caller check, and the `agent-relay-ctl` commands.
4. **GNOME:** menu section, notification, dialogs with the checkbox; test in
   the headless shell.
5. **Ad-hoc scripts:** `request_host_script`, storage with checksum, review
   window, running the checked copy.
6. **Finish:** log entries, docs, and again an independent security review
   before going live.

## 10. Later

- Move agent-relay-mcp into a dev container and protect `install.sh` like
  the other projects: mounted read-only, changes applied only through
  `devcontainer-approve`. Then the `writable_by_host_runs` exception is no
  longer needed and can be removed from the code once no project uses it.
