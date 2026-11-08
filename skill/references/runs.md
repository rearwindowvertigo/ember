# Runs

## Start and quote

The start command is in SKILL.md step 6. Start options: `--model`, `--effort`, `--read-only`, `--read <path>`
(repeatable), `--trailer <line>` (repeatable; none by default), `--claim`,
`--window`, `--dry-run` (`--json` for an object). Do not call `codex`,
`opencode` or `claude` directly: Ember owns the worktree, the sandbox, the
commits and the run record.

A worker cannot open an issue tracker. Put each source the brief cites in
the brief, or export it to a file in the scratchpad and pass it with
`--read`, naming that file in the brief.

Write every brief and correction to a file in the session's scratchpad
and pass it with `--task-file` or `--message-file`; there is no inline
option. Single-quote trailers so the shell cannot change them; for a
trailer with an apostrophe, join single-quoted pieces with `'\''`.

## Model and effort

- `--model` wins. Otherwise the model comes from the menu row:
  `single.writer`, or `single.reviewer` for `--read-only`. The row must be
  `set` and name the same engine, or the start exits 64 naming the row.
- An OpenCode `--model` is the full `<integration>/<model>` id from
  `emb engines --models opencode --json`, such as `anthropic/claude-sonnet-4`
  or `openrouter/deepseek/deepseek-v4.1-flash-1017`. Ember uses it unchanged.
- A `claude` row whose run-as is `subagent` is refused with 64: use a
  Claude subagent instead. With `--model`, that row is not read.
- `--effort` wins. Otherwise the effort is the row's when the row names the
  same engine and model, else the driver's default, which may be none.
  Pass the resolved effort when the choice has one.

The dry-run line (SKILL.md step 3) reads like
`Team, Single tasks Writer: codex gpt-6.1-sol · high (single.writer)`. With
`--read-only`, the row is `Single tasks Reviewer`. Sources are the
Team row key, `--model` or `--effort`. When they differ, the suffix is
`(model: single.writer, effort: --effort)`, for example. A driver's default
effort has the model's source; an unset effort prints as `default`.
`--json` prints an object with `screen`, `row`, `engine`, `model`, `effort`,
`model_source` and `effort_source`; an unset effort is `null`.

A dry run checks inputs, the repository, the claim and worker selection,
through the commit and main-checkout checks below. It creates no cache
folder, worktree, branch, run record or session and runs no sandbox probe.
It exits 0 on success or with the failed check's exit code. A later start
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

## Worktree preparation

A new worktree starts with tracked files, without installed dependencies,
generated files or ignored local configuration. The owner can set a shell
command per repository in Settings, Worktree preparation, Add repository,
or at a terminal with `emb choices --set 'worktree.prepare=<JSON object>'`.
The object maps absolute main-checkout paths to commands. Preserve the
current mapping when giving the owner a command; the value replaces the
whole mapping. Claude never sets it. No entry means no preparation; an
empty command also means no preparation and no proposal from Claude.
SKILL.md step 1 covers when to propose a command.

Ember matches worktrees by their common git folder, so an Ember or Claude
Code worktree of the configured repository matches; a separate clone does
not. Ember reads this standing setting, never a repository file. It runs
the command once after creating each worktree and before its first worker
session or flow check: editing and read-only runs, gap check reviewers,
flow worktrees, flow writers and reviewers, and panel members. Without a
command, their records, output and exits are unchanged.

Preparation runs on the lead's machine outside the worker sandbox, through
`/bin/sh -c` in the new worktree with the caller's environment. It has its
own process group and a 1800-second limit. Standard output and standard
error go to `prepare.log`, mode 600, beside the run or flow record.
The record's `preparation` holds its command, state, exit, seconds, log,
left-behind paths and message, plus the process id and start ticks.

A nonzero exit, timeout, start failure, interruption, failed `git status`
inspection or paths left in `git status --porcelain` fails preparation.
Ignored dependency and build files are allowed. Changed paths are named;
an editing worker turn commits with `git add -A`. On failure no worker
session starts for that run, exit 79 names the log, and the record and
worktree stay. Inspect the log and follow the printed `next:` retry: a
new start with the original options and saved brief, the original gap
check, a fresh panel, or `emb flow --resume <id>`. `--send` and `--recover`
never rerun preparation. Closing a run with preparation but no worker turn
removes its owned worktree, including changes left by preparation; its
editing branch stays.

Timeout, cancellation, interruption and shutdown end the whole preparation
process group. Resume, close and prune end a recorded group left behind.
Flow and panel admission and retries are described in `references/flows.md`
and `references/panels.md`.

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
error. A finished turn exits 0 unless a pending owner wait makes the command
exit 84.
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
`--force` records the messages as accepted.

A run started by `emb gapcheck`, `emb panel` or a flow records that command
as its `parent`. A flow task run is not orphaned while its flow waits for
the lead. Its status line, `WARNING:` and Overview say it waits for the
lead's answer to the flow, with no recover-and-close `next:` line. Answer
the flow; do not recover and close the task run.
Otherwise, when the command ends without collecting the run, the run
shows `orphaned: true` in `emb run --status --json`, with a `WARNING:` and a
`next:` line. Report each orphaned run to the owner; follow `next:` only when
this session started its parent command.

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

Quote every `WARNING: <engine> <model> at capacity; retry <n> of <max> in
<secs>s` to the owner, including when the turn succeeds.

Exhausted or disabled retries end at exit 73. `usage_limit` and `outage`
exit 73 without retrying. Codex recognizes capacity and usage-limit
messages; Claude and both OpenCode drivers classify provider errors as
`outage`. Unmatched provider errors are `outage`.

Never switch workers silently. Only a Codex `usage_limit` permits offering
OpenCode in the next worker question.

Recovery keeps the retry budget and pending resend. `--interrupt` cancels
a pending resend. Earlier attempts' edits stay in the worktree and are
committed once after the final attempt; uncertain delivery leaves them
uncommitted. Turn seconds include waits; attempt seconds exclude them.
Both persist across recovery.

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

A read-only Claude CLI worker has `Bash` limited to these commands, each
with any arguments: `git diff`, `git log`, `git show`, `git status`,
`git blame`, `git grep`, `git ls-files`, `git rev-parse`, `make check`,
`make test`, `pytest` and `uv run pytest`. Claude Code also runs its own
read-only commands, such as `ls`, `cat` and `echo`, without a rule. Every
other command, including any that creates or changes a file, is refused.
The git write verbs stay refused as for editing runs.

Without a preparation command, a new run worktree has no installed
dependencies. Checks that need them require an install; see Worktree
preparation above. Within a worker sandbox, use the shared caches below;
network access follows `worker.network` as described below.

The workers' cache folder,
`$XDG_CACHE_HOME/ember/worker`, is writable in every
sandboxed run, including read-only runs, and shared between runs.
Each sandboxed Codex run, editing or read-only, gets a temporary folder at
`/tmp/ember-<6 hex>`, using the six-hex suffix of the run id.
The folder has mode 700. Ember creates it before the session starts and
exits 69 if any entry already exists there, including a symlink, or creation
fails. The error names the folder. The Codex profile makes it writable and
sets `TMPDIR` to it through `shell_environment_policy.set`. The shared `/tmp`
stays writable. Codex walks `/tmp` and denies every other entry matching
`ember-<6 hex>` by name. Ember removes the folder on close, worktree removal
or record pruning. An unscoped prune also removes matching folders no record
names, only when owned by the current user, not symlinks, and every record
could be read. It also sweeps unrecorded entries in
`$XDG_CACHE_HOME/ember/tmp` from 0.20.0 under the same record-readability rule.
Removal does not follow symlinks out of the folder. With `sandbox.codex` off,
Ember creates no temporary folder and sets no `TMPDIR`. Bubblewrap mounts a
private empty `/tmp` for each worker and gets no run temporary folder.
`XDG_CACHE_HOME`, `UV_CACHE_DIR`, `UV_PYTHON_INSTALL_DIR`, `PIP_CACHE_DIR`,
`npm_config_cache` and `GOMODCACHE` point into the workers' cache folder.
`CARGO_HOME` and
`GRADLE_USER_HOME` are unchanged because they hold configuration and
credentials as well as caches. An uncached download needs network access.
Codex has network only when `worker.network` is on; OpenCode
keeps network for its integrations; Claude keeps network for its API, and
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
matched at launch, or the resolved startup file inside a hidden folder;
`allow_path` is the folder Overview may offer, or null. Reasons are `hidden`,
`startup file`, `secret location` and `protected`.
The offered folder is built from the path that matched the hidden entry,
using the resolved target when a symlink reaches a hidden path, and must
contain the denied path after resolving symlinks. Secret, protected and
configuration checks apply to that folder. The `owner:` line and
`sandbox.read` suggestion use that offer.
Ember checks the output and screen for `Permission denied`, `Operation
not permitted` or `EACCES` naming an absolute or `~/` path. It keeps the
union across capacity attempts, deduplicated by resolved hidden path, and
prints one `WARNING: <run> was denied <path>` per path per turn. No detected
denials gives `[]`; an older outcome without the field also gives `[]`.
A broken outcome gives null, which is unknown. A diagnostic line found in
the run's brief, its turn messages, its `--read` files or its worktree's
tracked files is text the worker read, and is not recorded.
A denial inside a folder in Settings, Hidden notices
(`sandbox.hidden_notices`) keeps its `WARNING:` line and entry but prints
no `owner:` line and records no owner wait: report it without waiting.

Each sandboxed Codex run's commands get `XDG_CONFIG_HOME`, `XDG_DATA_HOME`
and `XDG_STATE_HOME` in the run's temporary folder, with symlinks to kept
and read paths inside the owner's matching bases. A tool that writes its
own configuration, data or state, such as Chrome, writes there; what it
writes lasts for the run only. `HOME` is unchanged.

When an `owner:` line offers reading, give the owner
the `Settings, Sandbox read paths; emb choices --set ...` action.
When the recorded turn exit is 0, say the turn finished. Read access is needed
only if the worker's report says the work needed the denied path. When the
report shows the work did not need it, say so.
The command keeps existing read paths and adds the offered folder, or an
addable secret location's matched path. Never run it, edit the settings
database, add a path to `sandbox.read`, or bypass the denial with `--read`
yourself.

A finished turn with pending owner actions (any printed `owner:` line,
including one for a denied path) prints no carry-on `next:` line. Start, `--send` and `--recover`
exit 84 when they would otherwise exit 0 with a pending owner wait. Each
pending `owner:` line prints on standard error, including when recovery has
nothing left to collect; the recorded turn exit stays unchanged. Put these
lines to the owner before any dependent command, even when the work did not
need a denied path. Wait for the owner's answer on every pending owner
action. After the owner answers a waiting run's
owner action, run `emb run --answered <run>`, then follow the `next:` line
it prints. `emb run --status <run>` shows the step as waiting for the owner
until it is answered, then prints the `next:` line.
Do not merge a run whose status step is `owner` until the owner answers and
`emb run --answered <run>` has run.

Home shell and line-editor startup files, including `.profile`, `.bashrc`,
`.bash_profile`, `.zshrc`, `.zprofile`, `.zshenv` and `.inputrc`, are
classified as `startup file`, including through symlinks. The full list is
`sandbox.STARTUP_HOME_FILES`. These entries have `allow_path: null`, no
`owner:` line, no `sandbox.read` suggestion and no Overview notice or Allow
reading button. Report them from `emb run --status`, even if they are the
turn's only denials. A tool that needs a startup file's settings can still
fail; the owner can add the file by hand. Older records keep their stored
reasons: a startup file recorded as `hidden` still shows a notice.

Overview groups notices by resolved folder. Its summary gives the report
count, distinct flows, panels and standalone runs, the first report date
and the latest report time in local time, including for a single report.
Show runs starts collapsed and lists one report per resolved folder, run
and turn, newest first, with the local time, kind, claim when present,
flow or panel when present, run and turn. Times come from turn outcome
files' modification times; missing times show as `time unknown` and sort
last, with ties sorted by run id and turn. Expansion is kept across
refreshes. Before read-path coverage checks and merging, a recorded offer
must equal, contain or lie inside the recorded hidden path after resolving
symlinks; an invalid offer is treated as no offer.

Overview's Allow reading appears only for `allow_path`, with a separate
line naming the folder future workers can read. Adding it requires
confirmation that every future worker can read the folder and none can
change it.
For a `hidden` denial with no valid offer, the notice says `No folder can
be offered for automatic read permission.` A `hidden` denial recorded with
`allow_path: null` has an `owner:` line saying no folder can be offered
because each folder that holds the path also holds private files, with no
`sandbox.read` suggestion. Secret locations, including credentials and
browser profiles, and protected paths have no Allow reading button.
Folders inside `$XDG_CONFIG_HOME` are never offered; the owner can
add them and other secret locations by hand in Settings, Sandbox read paths.
Ember's configuration and state folders cannot be added; report the printed
reason. Other workers' worktrees and `$XDG_RUNTIME_DIR` are `protected`,
have no Allow reading button and receive no `sandbox.read` suggestion.
Dismiss lasts until a later run or turn reports the path again. Permission
changes apply to newly started runs; an existing run keeps its launch paths.
Don't show again adds that resolved folder to the `sandbox.hidden_notices`
setting: it hides notices for every later report, and a later denial
inside the folder prints no `owner:` line and records no owner wait.
Adding it also ends a pending denial-only wait for that folder. Settings,
Hidden notices lists each hidden folder with Show again, which removes it
from the setting and removes any Dismiss entry for that folder. Run status,
flow status and `WARNING:` lines still report denials. Never set
`sandbox.hidden_notices` yourself; when the owner answers that a folder
needs no change, point them to Don't show again. Hidden folders add no Overview notice; the sidebar
count counts live runs, flows and panels only.
A tool failure without a printed path, or with only a working-directory-relative
path, is not detected.

## Exits

SKILL.md step 6 covers the printed lines. These exits have additional rules.

| Exit | Meaning | Response |
|---|---|---|
| 64 | Usage error, unanswerable menu row, switched-off engine or invalid claim | Fix the call, or ask the named row live. Never edit settings. A `gone` or `unknown` claim stops the run (claims reference). |
| 69 | Codex does not trust the repository | A trust refusal in a dry run or start creates nothing, including no worktree. It prints the trust message and an `owner:` line asking the owner to run `codex` in the repository once, with no `next:` line. Do not follow close-and-restart for this refusal. |
| 70 | Worktree creation failed, or no main checkout or commit | Never force-remove a worktree. |
| 77 | A hook refused the commit | Inspect the uncommitted work with `git -C <worktree> status`. |
| 79 | Worktree preparation failed before a worker session started | Inspect the named log and follow the new-start retry line; `--send` and `--recover` do not prepare the worktree. |
| 84 | Pending owner wait after start, `--send` or `--recover` | Put every pending `owner:` line to the owner first. Run nothing that depends on it until the owner answers, then run `emb run --answered <run>` before merging. |

A stalled run's partial edits need the owner's approval before discard.

## Test builds

For a test build of any worker branch, including `ember/flow/<id>`, give
the owner one command to run in their own terminal outside the lead's
session. The command changes to the branch's recorded worktree, installs
the dependencies the build needs and starts it:
`cd '<worktree>' && <dependency install command> && <project run command>`.
Fill in the path and commands before handing it to the owner.

Before `emb merge` or `emb cleanup` of that branch, check that no process
still runs from its worktree. Run `ls -l /proc/*/cwd` and filter by the
worktree path, including its subfolders. If a process remains, wait for the
owner to quit it and check again before removing the worktree. This applies
to run and flow worktrees.

## Closing

Closing ends the session and keeps an editing worktree and
branch, except that a run with preparation and no worker turn has its owned
worktree removed as described above. It removes a clean read-only detached
worktree and its git registration, including when its only new files are
git-ignored. A detached worktree with tracked changes, untracked
non-ignored files or a moved
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
for a session with messages Ember did not send. A 75 refusal leaves the run
open: say why. Do not interrupt a turn to close it. Never close a run
another session started: it may hold the owner's work.
