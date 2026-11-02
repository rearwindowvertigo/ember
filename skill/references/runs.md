# Runs

## Start and quote

```bash
emb run --engine <codex|opencode|claude> --dir <repo> --task-file <brief> --trailer '<trailer>' --claim <id>
```

Start options: `--model`, `--effort`, `--read-only`, `--read <path>`
(repeatable), `--trailer <line>` (repeatable; none by default), `--claim`,
`--window`. Do not call `codex`, `opencode` or `claude` directly: Ember
owns the worktree, the sandbox, the commits and the run record.

Write every brief and correction to a file in the session's scratchpad
and pass it with `--task-file` or `--message-file`; there is no inline
option. Single-quote trailers so the shell cannot change them; for a
trailer with an apostrophe, join single-quoted pieces with `'\''`.

Run starts, `--send` and `--recover` as background Bash commands; take
the exit code and do not pipe the output.

## Model and effort

- `--model` wins. Otherwise the model comes from the menu row:
  `single.writer`, or `single.reviewer` for `--read-only`. The row must be
  `set` and name the same engine, or the start exits 64 naming the row.
- A `claude` row whose run-as is `subagent` is refused with 64: use a
  Claude subagent instead. With `--model`, that row is not read.
- `--effort` wins. Otherwise the effort is the row's when the row names the
  same engine and model, else the driver's default, which may be none.
  Pass the resolved effort when the choice has one.

## What a start checks

In order: every file and path exists (66); the brief has a non-empty line
(64); `git` and `tmux` are on `PATH` (69); `--dir` is in a git checkout
(65); the claim is `claimed` or `none` (64); the engine is on (64); the
engine is connected (69, naming the fix command); the driver exists (69);
the model and effort (64); for an editing run, a main checkout and a
commit (70); for a sandboxed Codex run, no `sandbox_mode` key (69); for a
sandboxed run, a working `bwrap` (69). Nothing is created before these
pass.

## The session and its output

The first line on standard output is `run <id>`; keep it at once with the
claim. The id is `<engine>-<yyyymmdd>-<hhmmss>-<6 hex>`, also the tmux
session's name. An editing run works on `ember/<engine>/<id>` at
`.ember/worktrees/<id>` in the main checkout; a read-only run works in the
checkout itself, with no branch.

The session runs with no window until `--window` at start or
`emb run --attach <run>`. Closing a window leaves the run going. The owner
may type into a Codex or OpenCode session through a window; a Claude
session takes no typed messages.

Each turn prints:

```text
<engine> <model> · <effort> · <secs>s · exit <code> · <n> files · turn <n>
run <id>
worktree <path>
branch <branch>
commit <commit>
window <pid>
reply <path>
--- reply ---
<reply text>
```

A missing value prints as `-`. An editing turn commits every change as one
commit, subject the brief's first non-empty line (` (turn <n>)` after turn
1), with the trailers. A failed turn that changed files is committed with
`(turn <n>, incomplete: <reason>)`. Run records live in
`$XDG_STATE_HOME/ember/runs/<id>/`; `turn-<n>.md` is the reply.

## Correct, inspect, collect

- `emb run --send <run> --message-file <file>` sends the next message and
  waits for its turn; the run keeps its model, effort and trailers.
- `--screen <run>` prints the pane; `--interrupt <run>` stops a turn going
  wrong; `--status <run> --json` prints the record with `session_state`.
- `--recover <run>` waits for a sent turn, then collects and commits it, or
  commits a collected one. A commit already made is recorded, not repeated.

`--send` refuses with 75 when the session holds messages Ember did not
send, with `Claude asks the owner` in the message. Ask the owner about
those messages. Only with their go-ahead for that run repeat the command
with `--force`, which records the messages as accepted.

## Sandbox

Each engine's sandbox follows `sandbox.<engine>`. The record's `sandbox`
says who provides it: `codex` (Codex's own sandbox with Ember's profile),
`bubblewrap` (OpenCode and Claude) or `none`. With `none` the run prints a
`WARNING:` line; tell the owner.

A sandboxed worker sees the machine read-only and the home folder hidden
except its `.local/bin` and `.local/lib` folders, git's configuration,
the worktree, the repository's git folder, the read paths
(`sandbox.read`, then each `--read`) and the engine's own folders. A read-only run's worktree is
read-only. Codex has network only when `worker.network` is on; OpenCode
keeps network for OpenRouter; Claude keeps network for its API, and
`worker.network` controls only its web tools.

A read-only run prints `WARNING: read-only run <id> changed <path>` for
each path whose state changed during a turn. Report it.

## Exits

Read standard error after every run, also at exit 0. Every non-zero exit
prints one `next:` or `owner:` line. Follow `next:` within the skill's
rules; put `owner:` to the owner and wait. Do not retry a refusal.

| Exit | Meaning | Response |
|---|---|---|
| 0 | Success | Read the reply and every `WARNING:`. |
| 1 | Unexpected error, or the settings database cannot be read | Show the error; run `emb run --status`; an `owner:` line goes to the owner. |
| 64 | Usage error, unanswerable menu row, switched-off engine or invalid claim | Fix the call, or ask the named row live. Never edit settings. A `gone` or `unknown` claim stops the run (claims reference). |
| 65 | Not a git checkout | Use a git checkout, or a read-only Claude subagent. |
| 66 | Missing file or path | Check `--dir`, the brief and `--read` paths. |
| 69 | Engine, driver, `git` or `tmux` missing, not signed in, a sandbox conflict, no working `bwrap`, or no runtime folder | Give the owner the `owner:` command and wait. Do not run it; do not retry the start or start another engine without a question. |
| 70 | Worktree creation failed, or no main checkout or commit | Follow `next:`; never force-remove a worktree. |
| 71 | Engine or session failed | Run `--status <run>`; report; follow `next:`. |
| 72 | Session or window unavailable, or the run was closed | Run `--status <run>`; a new start is a new run. |
| 73 | Provider limit or outage | Tell the owner. After it clears, follow `next:`. On a Codex usage-limit message, offer OpenCode in the next worker question; never switch silently. |
| 74 | Interrupted; partial edits may be committed | Tell the owner; `--recover <run>`. |
| 75 | Refused: a turn runs or is collected, a busy session, another command on the run, or messages Ember did not send | Do not retry. Recover or check status as `next:` says; for foreign messages, ask the owner. |
| 76 | Branch or `HEAD` moved; nothing committed | Tell the owner. Read the record's branch, then `HEAD` and `git status` of the worktree. Recover only when both are as recorded; otherwise leave the work for the owner. |
| 77 | A hook refused the commit | Inspect the uncommitted work with `git -C <worktree> status`; tell the owner. |
| 80 | The turn ended with no reply | Tell the owner; `--screen <run>`, then `--recover` or a `--send`. |
| 124 | Stall; the turn may still run | `--recover <run>`. |

Run `--recover` after 124, 74, 76 (with the check above) or 80 as a
background command. Tell the owner about 73, 74, 76 and 80, and about
every `WARNING:`. A stalled run's partial edits need the owner's approval
before discard.

## Closing

Park with `emb run --close <run>` only runs this session started, after
the turn ended, its work was committed and its reply was read and
reported. Closing ends the session and keeps the worktree and branch.

`--close` refuses with 75 while a turn runs or another command waits, for
an editing turn collected but not committed (`--recover`, then close), or
for a session with messages Ember did not send. Leave such a run open and
say why. Do not interrupt a turn to close it. Never pass `--force` without
the owner's instruction for that run, and never close a run another
session started: it may hold the owner's work.
