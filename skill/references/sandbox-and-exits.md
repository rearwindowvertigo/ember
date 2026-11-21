# Sandbox, denied paths and exits

Read this file on every non-zero `emb run` exit, on a denied path or an
`owner:` line about a path, and before giving a worker anything outside
its worktree. `references/runs.md` covers starting, following and closing
a run.

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

An OpenCode worker asks the owner nothing in its window. It allows file
reads, including `*.env` and `*.env.*`; reading is limited by the sandbox
and the read paths.

A read-only Claude CLI worker has `Bash` limited to these commands, each
with any arguments: `git diff`, `git log`, `git show`, `git status`,
`git blame`, `git grep`, `git ls-files`, `git rev-parse`, `make check`,
`make test`, `pytest` and `uv run pytest`. Claude Code also runs its own
read-only commands, such as `ls`, `cat` and `echo`, without a rule. Every
other command, including any that creates or changes a file, is refused.
The git write verbs stay refused as for editing runs.

Without a preparation command, a new run worktree has no installed
dependencies. Checks that need them require an install; see Worktree
preparation in `references/runs.md`. Within a worker sandbox, use the
shared caches below; network access follows `worker.network` as described
below.

The workers' cache folder,
`$XDG_CACHE_HOME/ember/worker`, is writable in every
sandboxed run, including read-only runs, and shared between runs.
Each sandboxed Codex run, editing or read-only, gets a temporary folder at
`/tmp/ember-<6 hex>`, using the six-hex suffix of the run id.
The folder has mode 700. Ember creates it before the session starts.
Ember exits 69 with a diagnostic and owner action naming `/tmp` if that
volume reports fewer than 50,000 free inodes or less than 256 MiB free,
or with a folder creation error if the path already holds an entry
(including a symlink) or creation otherwise fails. The error names the folder.
The Codex profile makes it writable and sets `TMPDIR` to it through
`shell_environment_policy.set`. The shared `/tmp`
stays writable. Codex walks `/tmp` and denies every other entry matching
`ember-<6 hex>` by name. When a flow accepts a task, a writer whose turn is
still running keeps its files. For an open sandboxed Codex writer whose turn
is collected or committed, acceptance removes every top-level entry except
`config`, `data`, `state` and entries whose names start with `codex-`.
Ember removes the folder on close, worktree removal
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
