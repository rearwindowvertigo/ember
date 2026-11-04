# Runs

## Start and quote

Complete the skill-version check at the claim (SKILL.md step 2) before
choosing or starting a worker. A stale skill is reloaded with the same
claim; an unknown installed version stops worker starts.

```bash
emb run --engine <codex|opencode|claude> --dir <repo> --task-file <brief> --trailer '<trailer>' --claim <id>
```

Start options: `--model`, `--effort`, `--read-only`, `--read <path>`
(repeatable), `--trailer <line>` (repeatable; none by default), `--claim`,
`--window`, `--dry-run` (`--json` for an object). Do not call `codex`,
`opencode` or `claude` directly: Ember owns the worktree, the sandbox, the
commits and the run record.

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

Before starting, add `--dry-run` to the start's exact options, including
`--claim`, and copy its standard-output line as the visible announce line:

```text
Team, Single tasks Writer: codex gpt-6.1-sol · high (single.writer)
```

With `--read-only`, the row is `Single tasks Reviewer`. Sources are the
Team row key, `--model` or `--effort`. When they differ, the suffix is
`(model: single.writer, effort: --effort)`, for example. A driver's default
effort has the model's source; an unset effort prints as `default`.
`--json` prints an object with `screen`, `row`, `engine`, `model`, `effort`,
`model_source` and `effort_source`; an unset effort is `null`.

A dry run checks inputs, the repository, the claim and worker selection,
through the commit and main-checkout checks below. It creates no cache
folder, worktree, branch, run record or session and runs no sandbox probe.
It exits 0 on success or with the failed check's exit code. Follow its
`next:` or `owner:` line on failure and start no worker. A later start
reads the settings again.

## What a start checks

In order: every file and path exists (66); the brief has a non-empty line
(64); `git` and `tmux` are on `PATH` (69); `--dir` is in a git checkout
(65); the claim is `claimed` or `none` (64); the engine is on (64); the
engine is connected (69, naming the fix command); the driver exists (69);
the model and effort (64); a commit and, for an editing run, a main
checkout (70); for a sandboxed Codex run, no `sandbox_mode` key (69); for a
sandboxed run, a working `bwrap` (69). A run refuses a worktree
folder inside any Git working tree or the source repository's git folder
(69), naming the folder and `XDG_DATA_HOME` or `HOME` that placed it there.
A sandboxed run refuses any kept path inside Ember's configuration or
state folder, or inside its worktrees folder outside the worker's own
worktree (69), naming both paths. Nothing is created before these pass.

## The session and its output

The first line on standard output is `run <id>`; keep it at once with the
claim. Before the first turn, the start prints the resolved worker line
on standard error, including with `--quiet`. `--quiet` keeps the run id,
worker line and turn summaries. The id is
`<engine>-<yyyymmdd>-<hhmmss>-<6 hex>`, also the tmux session's name.
An editing run works on `ember/<engine>/<id>` in the
worktree its `worktree` line names, `worktrees/<id>` in Ember's data folder
(`$XDG_DATA_HOME/ember`, default `~/.local/share/ember`), outside every
repository. A read-only run works in its own detached worktree in that
folder, at the `HEAD` of the checkout named by `--dir`, with no branch.
Its worktree is writable so builds and tests can create project files;
Ember never commits from it. The source checkout is separate.
It does not see uncommitted edits or later source-checkout changes. For
each uncommitted source path, start prints
`WARNING: read-only run <id> does not see uncommitted <path>` on standard
error. A successful run still exits 0; report the warnings. Tell a reviewer
to read its own working folder, using relative paths.
The run records the resolved worktree path, main checkout and common git
folder at start. Merge, cleanup and prune use the recorded paths, including
paths from runs created under the old layout. Do not construct a path from
the repository or a later value of `XDG_DATA_HOME`.

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
`(turn <n>, incomplete: <reason>)`. These commits stay on the worker branch;
`emb merge --subject '<commit subject>'` squashes its turns under the lead's
subject in the repository's commit convention. It preserves distinct
non-Ember trailers and records the verdict and worker tip. Run records live in
`$XDG_STATE_HOME/ember/runs/<id>/`; `turn-<n>.md` is the reply.

## Correct, inspect, collect

- `emb run --send <run> --message-file <file>` sends the next message and
  waits for its turn; the run keeps its model, effort and trailers.
- `--screen <run>` prints the pane; `--interrupt <run>` stops a turn going
  wrong; `--status <run> --json` prints the record with `session_state`
  and `turns`.
- `--recover <run>` waits for a sent turn, then collects and commits it, or
  commits a collected one. A commit already made is recorded, not repeated.

`--send` refuses with 75 when the session holds messages Ember did not
send, with `Claude asks the owner` in the message. Ask the owner about
those messages with one direct question, such as whether to send anyway.
Only with their go-ahead for that run repeat the command
with `--force`, which records the messages as accepted.

A run started by `emb gapcheck`, `emb panel` or a flow records that command
as its `parent`. When the command ends without collecting the run, the run
shows `orphaned: true` in `emb run --status --json`, with a `WARNING:` and a
`next:` line. Report each orphaned run to the owner, then
`emb run --recover <run>` and `emb run --close <run>` it when this session
started its parent command.

The run record's `last_screen_change_at` is the epoch time when a wait
loop last observed a changed screen. `emb run --status <run> --json`
exposes it for standalone runs, flow workers and panel members. Null means
no recorded activity, including an older record without the field; zero
is a recorded value.

## Turn results

For counts of turn results, read `emb run --status <run> --json`, or
`emb run --status --json` for all runs, and count from each run's `turns`.
It has one entry per recorded turn number, oldest first: `turn`, `exit`,
`state`, `reason`, `provider_error`, `signed_out`, `incomplete`, `seconds`
and `denied_paths`.
New outcomes also expose `provider_kind`, the ordered `attempts` and retry
timestamps. `provider_kind` is `capacity`, `usage_limit`, `outage` or null;
older outcomes can omit it and `attempts`. Count turns separately from
attempts: a successful turn can include failed capacity attempts.
No recorded turns gives `turns: []`; no runs gives `[]`.
`last_exit` and `last_reason` describe only the latest turn or failure;
each summary line describes one turn. A turn with `exit: 73` and
`provider_error: true` still counts after
a later turn exits 0.

Each `turn-<n>.outcome.json` is written as unfinished when collection
starts and completed after commit handling with the final exit code.
Recovery updates the same result; its seconds accumulate across recovery.
An unfinished result has `exit: null`. Older outcomes without exit code or
seconds show null for those fields. A missing, unreadable or malformed
outcome leaves its numbered entry with all other fields null and prints a
`WARNING:` naming the file; other turns are unaffected. Report the warning
and unknown results. Null is not success or evidence of no provider error.

## Provider errors

Ember retries `capacity` with the same prompt, model and effort. Team's
Capacity retries (`provider.capacity_retries`, 0 to 10, default 2) limits
resends per turn; zero turns retries off. Wait before a retry
(`provider.capacity_wait`, 5 to 600 seconds, default 30) sets the wait.
Both are standing settings reported by `emb choices`. Each run saves them
at start for all its turns and recovery; later Team changes apply to new
runs. Never change these settings to clear an error.

Report every `WARNING: <engine> <model> at capacity; retry <n> of <max> in
<secs>s`, including when the turn succeeds. Exhausted or disabled retries
end at exit 73. `usage_limit` and `outage` exit 73 without retrying.
Codex recognizes capacity and usage-limit messages; Claude and both
OpenCode drivers classify provider errors as `outage`. Unmatched provider
errors are `outage`.

Recovery keeps the retry budget and pending resend. `--interrupt` cancels
a pending resend. If delivery is uncertain, inspect the session and ask
the lead before sending again, as `next:` says; never resend automatically.
Earlier attempts' edits stay in the worktree and are committed once after
the final attempt; uncertain delivery leaves them uncommitted. Turn seconds
include waits; attempt seconds exclude them. Both persist across recovery.

## Sandbox

Each engine's sandbox follows `sandbox.<engine>`. The record's `sandbox`
says who provides it: `codex` (Codex's own sandbox with Ember's profile),
`bubblewrap` (OpenCode and Claude) or `none`. With `none` the run prints a
`WARNING:` line; tell the owner.

A sandboxed worker sees the machine read-only and the home folder hidden
except its `.local/bin` and `.local/lib` folders, git's configuration,
the worktree, the repository's git folder, the read paths
(`sandbox.read`, then each `--read`), the workers' cache folder and the
engine's own folders. Ember's configuration and state folders are hidden
even through symlinked read paths. Each entry of the worktrees folder
other than the worker's own worktree is denied by name in both sandboxes,
including through symlink aliases; no kept worktree is reopened inside a
denied parent folder. A read-only run's detached worktree is writable in
the Codex profile and bubblewrap. The run has no branch or Ember commit
step and keeps its reviewer brief. Older read-only records that name a
source checkout keep that checkout protected as read-only.
The workers' cache folder,
`$XDG_CACHE_HOME/ember/worker`, is writable in every
sandboxed run, including read-only runs, and shared between runs.
Ember does not set `TMPDIR` for workers or create a per-run temporary
folder. Bubblewrap mounts a private empty `/tmp` for each worker. The
Codex profile makes `/tmp` writable, including for read-only runs.
`XDG_CACHE_HOME`, `UV_CACHE_DIR`, `UV_PYTHON_INSTALL_DIR`, `PIP_CACHE_DIR`,
`npm_config_cache` and `GOMODCACHE` point into the workers' cache folder.
`CARGO_HOME` and
`GRADLE_USER_HOME` are unchanged because they hold configuration and
credentials as well as caches. An uncached download needs network access.
Codex has network only when `worker.network` is on; OpenCode
keeps network for OpenRouter; Claude keeps network for its API, and
`worker.network` controls only its web tools.

A read-only run prints `WARNING: read-only run <id> changed <path>` for
each path whose state changed in its own worktree during a turn. The path
is absolute and names that worktree. Tracked changes and untracked
non-ignored files are reported; ignored build and test files produce no
change warning. Edits in the source checkout produce no change warning.
Report every warning.

## Denied paths

After every sandboxed turn, read `denied_paths` in its status and report
each entry's `path`, `reason`, run and turn to the owner, even at exit 0.
Each entry has `{path, reason, allow_path}`. `path` is the hidden path
matched at launch; `allow_path` is the folder Overview may offer, or null.
Ember checks the output and screen for `Permission denied`, `Operation
not permitted` or `EACCES` naming an absolute or `~/` path. It keeps the
union across capacity attempts, deduplicated by resolved hidden path, and
prints one `WARNING: <run> was denied <path>` per path per turn. No detected
denials gives `[]`; an older outcome without the field also gives `[]`.
A broken outcome gives null, which is unknown.

Report every printed `owner:` line. When it offers reading, give the owner
the `Settings, Sandbox read paths; emb choices --set ...` action.
The command keeps existing read paths and adds the offered folder, or the
matched path when none is offered. Never run it, edit the settings database,
add a path to `sandbox.read`, or bypass the denial with `--read` yourself.
Wait for the owner before retrying work that needs the denied path.

Overview's Allow reading appears only for `allow_path`, after confirmation
that every future worker can read the folder and none can change it.
Secret locations, including credentials and browser profiles, have Dismiss
only. Folders inside `$XDG_CONFIG_HOME` are never offered; the owner can
add them and other secret locations by hand in Settings, Sandbox read paths.
Ember's configuration and state folders cannot be added; report the printed
reason. Other workers' worktrees and `$XDG_RUNTIME_DIR` are `protected`,
have no Allow reading button and receive no `sandbox.read` suggestion.
Dismiss lasts until a later run or turn reports the path again. Permission
changes apply to newly started runs; an existing run keeps its launch paths.
A tool failure without a printed path, or with only a working-directory-relative
path, is not detected.

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
| 69 | Engine, driver, `git` or `tmux` missing, not signed in, a sandbox conflict, a kept path in a hidden Ember folder outside its own worktree, a worktree folder inside any Git working tree or the source repository's git folder, no working `bwrap`, or no runtime folder | Give the owner the `owner:` command and wait. Do not run it; do not retry the start or start another engine without a question. |
| 70 | Worktree creation failed, or no main checkout or commit | Follow `next:`; never force-remove a worktree. |
| 71 | Engine or session failed | Run `--status <run>`; report; follow `next:`. |
| 72 | Session or window unavailable, or the run was closed | Run `--status <run>`; a new start is a new run. |
| 73 | Capacity retries exhausted or disabled, usage limit or outage | Tell the owner the kind from `next:` or `provider_kind`. After it clears, follow `next:`. Only for Codex `usage_limit`, offer OpenCode in the next worker question; never switch silently. For uncertain resend delivery, inspect the session and ask the lead before sending again. |
| 74 | Interrupted; partial edits may be committed; a pending capacity resend is cancelled | Tell the owner; `--recover <run>`, unless `next:` says delivery is uncertain: inspect the session and ask the lead before sending again. |
| 75 | Refused: a turn runs or is collected, a busy session, another command on the run, or messages Ember did not send | Do not retry. Recover or check status as `next:` says; for foreign messages, ask the owner. |
| 76 | Branch or `HEAD` moved; nothing committed | Tell the owner. Read the record's branch, then `HEAD` and `git status` of the worktree. Recover only when both are as recorded; otherwise leave the work for the owner. |
| 77 | A hook refused the commit | Inspect the uncommitted work with `git -C <worktree> status`; tell the owner. |
| 80 | The turn ended with no reply | Tell the owner; `--screen <run>`, then `--recover` or a `--send`. |
| 124 | Stall; the turn may still run | `--recover <run>`. |

Run `--recover` after 124, 74 (except uncertain delivery), 76 (with the
check above) or 80 as a background command. Tell the owner about 73, 74, 76 and 80, and about
every `WARNING:`. A stalled run's partial edits need the owner's approval
before discard.

## Closing

Park with `emb run --close <run>` only runs this session started, after
the turn ended, its work was committed and its reply was read and
reported. Closing ends the session and keeps an editing worktree and
branch. It removes a clean read-only detached worktree and its git
registration, including when its only new files are git-ignored. A detached
worktree with tracked changes, untracked non-ignored files or a moved
`HEAD` is kept with its recorded path; closing succeeds with a warning
naming `git worktree remove --force <path>`. `--close --force` still closes
only the session and does not discard those changes. Report the warning;
discard needs the owner's instruction. Closing again retries worktree
removal. Cleanup without `--branch` removes clean detached worktrees of
closed read-only runs in that repository; prune removes them with ended
records. Older read-only records without `detached` keep their source
checkouts.

`--close` refuses with 75 while a turn runs or another command waits, for
an editing turn collected but not committed (`--recover`, then close), or
for a session with messages Ember did not send. Leave such a run open and
say why. Do not interrupt a turn to close it. Never pass `--force` without
the owner's instruction for that run, and never close a run another
session started: it may hold the owner's work.
