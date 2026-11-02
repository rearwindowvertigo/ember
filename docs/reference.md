# Ember reference

## `emb choices`

Reads standing settings and one-time choices, claims and releases one-time
choices for runs, and sets settings and one-time choices at a terminal.

Settings are kept in `$XDG_CONFIG_HOME/ember/settings.db` (default
`~/.config/ember/settings.db`), mode 600 in a mode 700 folder. A read never
creates the database file and never changes its content. A read may leave
empty `settings.db-wal` and `settings.db-shm` files beside it, at mode 600.

Every reply is JSON on standard output, indented by two spaces. Messages go to
standard error as `emb choices: <message>`.

### Options

| Option | Does | Standard output |
|---|---|---|
| (none) or `--json` | Read | Read reply |
| `--id ID` | With a read or `--claim`: use the claim for `ID` | Read reply for `ID` |
| `--claim --id ID` | Claim the pending one-time choice for `ID` | Read reply |
| `--finish ID` | Release the claim for `ID` | `{"claim": {"id", "status": "gone"}, "generation", "errors"}` |
| `--request KEY=VALUE` | With a read or `--claim`: a request-layer value; repeatable | |
| `--set KEY=VALUE` | Save a standing setting; repeatable | `{"settings": {...}, "generation", "errors"}` |
| `--clear KEY` | Remove a standing setting; repeatable | As `--set` |
| `--once KEY=VALUE` | Set the one-time choice; repeatable | `{"once": {...}, "generation", "errors"}` |
| `--clear-once` | Remove the pending one-time choice | `{"cleared": "one-time", "generation", "errors"}` |
| `--expect-generation N` | With `--set`, `--clear`, `--once` or `--clear-once`: refuse the write unless the generation is `N` | |

`--claim`, `--finish`, `--set`, `--clear`, `--once` and `--clear-once` cannot
be combined. A key may be given once per option.

`--set`, `--clear`, `--once` and `--clear-once` require standard input to be a
terminal.

A list value on the command line is comma-separated. An empty value is an
empty list.

`--json` is accepted and changes nothing.

### Keys

| Key | Values | Default | Layers |
|---|---|---|---|
| `worker.network` | `on`, `off` | `off` | standing |
| `engine.claude`, `engine.codex`, `engine.opencode` | `on`, `off` | `on` | standing |
| `shortlist.claude`, `shortlist.codex` | List of `<model>@<effort>`; model `[A-Za-z0-9][A-Za-z0-9._-]*`, effort `[a-z]+` | `[]` | standing |
| `shortlist.opencode` | List of `<provider>/<model>`, with optional `@<effort>`; each part `[A-Za-z0-9][A-Za-z0-9._-]*`, the provider part may start with `~` and the model part may also contain `:` | `[]` | standing |
| `single.writer`, `single.reviewer` | `ask`, or `<engine>:<model>` with the model on that engine's shortlist when saved | `ask` | request, one-time, standing |
| `multi.runs_as` | `ask`, `flow`, `workflow` | `ask` | request, one-time, standing |
| `panel.members` | List of `<engine>:<model>` with each model on its engine's shortlist when saved, at most 8, no duplicates | `[]` | request, one-time, standing |
| `single.writer_as`, `single.reviewer_as` | `cli`, `subagent` | `cli` | request, one-time, standing |
| `sandbox.codex` | `on`, `off` | `on` when `bwrap` is on `PATH` or the `codex` on `PATH` has a bundled `bwrap`, else `off` | standing |
| `sandbox.opencode`, `sandbox.claude` | `on`, `off` | `on` when `bwrap` is on `PATH`, else `off` | standing |
| `sandbox.read` | List of absolute paths, no duplicates | `[]` | standing |
| `terminal` | A non-empty command line in shell syntax | `xdg-terminal-exec` | standing |
| `app.autostart` | `on`, `off` | `on` | standing |
| `app.theme` | `system`, `light`, `dark` | `system` | standing |

A shortlist lists each model once. A request or one-time value for a
standing-only key is refused.

### Layers

Each key resolves to the first of:

1. `request`: a `--request` value.
2. `one-time`: the copy of the one-time choice claimed by `--id`.
3. `setting`: the standing setting.
4. `default`: the key's default.

`ask` is a value like any other: an `ask` in a higher layer wins.

Each answer is `{"value": ..., "layer": "request" | "one-time" | "setting" |
"default"}`.

When a stored setting, a claimed copy, or the pending one-time choice where
it is read is not valid, every key resolves to its default, `source_error` is `true`, `errors`
names the invalid values, `menu` is `null` and the exit code is 0.

### One-time choices and claims

- `--once` replaces the pending one-time choice whole. It expires 24 hours
  after it is set.
- An id matches `[a-z0-9][a-z0-9-]{5,63}`.
- `--claim --id ID` for a new id records `claimed` with a copy of the pending
  one-time choice and removes it, or records `none` when no one-time choice
  is pending. For a known id it changes nothing and returns the stored status.
- The pending one-time choice is read by a read without `--id` and by a
  claim of a new id. A read with `--id` and a claim of a known id do not read
  it. A claim of a new id does not consume a pending one-time choice that is
  not valid: it records nothing, and the reply has status `unknown`.
- A claim reads as `gone` 7 days after it was made.
- `--finish ID` records `ID` as `gone`, also when `ID` was not known.
- Claim status is `claimed`, `none`, `gone`, or `unknown` for an id never
  claimed. With `gone` or `unknown`, every key resolves to its default and
  `--request` values are not used.

### Generation

Each write that changes the database raises `generation` by one. A write that
changes nothing leaves it.

### Read reply

```json
{
  "answers": {"<key>": {"value": "...", "layer": "..."}},
  "claim": {"id": "...", "status": "..."},
  "generation": 0,
  "errors": [],
  "source_error": false,
  "menu": {
    "single.writer": {
      "value": "...", "state": "...", "engine": "...", "model": "...",
      "effort": "...", "run_as": "...", "reason": "..."
    },
    "single.reviewer": {},
    "multi.runs_as": {"value": "ask", "state": "ask", "reason": "asks at each run"},
    "panel.members": {"value": [], "state": "empty", "members": []}
  }
}
```

`claim` is `null` without `--id`.

`multi.runs_as` has only `value`, `state` (`set` or `ask`) and `reason`;
it names no worker. `ask` has reason `asks at each run`; `flow` and
`workflow` have state `set` and reason `null`.

`panel.members` has `value` (the list), `state` (`empty` or `set`) and
`members`, one row per entry with `value`, `state`, `engine`, `model`,
`effort` and `reason`, in the worker row states except `ask`; it has no
`run_as`. A non-empty list stays `set` even when none of its member rows
is `set`. Entries contain no `@effort`; the shortlist supplies the effort.
Its allowed values are every shortlisted `<engine>:<model>` in engine
order: Claude, Codex, OpenCode.

The exception to the default-on-error rule in Layers is `panel.members`:
when it is the only invalid key, `source_error` is `true`, `errors` names
the invalid values and `menu` is `null`. The other validated
settings and claimed values still resolve through their layers. Explicit
panel members can therefore use the saved engine and sandbox switches.
An invalid value for another key still makes every answer use its default.

Worker menu `state` is `set`, `ask`, `invalid`, `engine off`, `not connected` or
`not shortlisted`, checked in that order. `not connected` means the engine is
on and its status in the cache of `emb engines` says it is not connected; its
reason is `<engine> is not connected`. A read uses only that cache and never
runs an engine; with no cache, or a cache that is not valid, no row is
`not connected`. `effort` is the effort in the shortlist entry. `run_as` is the
`single.writer_as` or `single.reviewer_as` value for the `claude` engine and
`null` for the others. `reason` is `null` when `state` is `set`.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 64 | Usage error, unknown key, invalid value or invalid id |
| 74 | The settings database cannot be opened or read |
| 75 | Stale generation, or the database stayed busy for 5 seconds |
| 77 | An owner option without a terminal on standard input |
| 78 | The settings database has an unsupported schema |

## `emb engines`

Shows whether Codex, OpenCode and Claude Code are installed, signed in and
connected, or lists one engine's models.

| Option | Does |
|---|---|
| (none) | Print each engine's status |
| `--refresh` | Check the engines, or read the model list from its source, instead of using the cache |
| `--json` | Print JSON |
| `--models ENGINE` | Print the model list of `codex`, `opencode` or `claude` |

Exit codes: 0 whether or not the engines are connected and whether or not the
model list could be read; 64 for a usage error, such as an unknown engine.

### Status

An engine is `connected` when its CLI is found and its sign-in check passes.
Each check is a local command with a 10-second timeout:

| Engine | CLI | Sign-in check | Signed in when |
|---|---|---|---|
| `codex` | `codex` on `PATH` | `codex login status` | it exits 0 and names ChatGPT |
| `opencode` | `opencode` on `PATH`, else `~/.opencode/bin/opencode` | `opencode auth list` | it exits 0 and names OpenRouter |
| `claude` | `claude` on `PATH` | `claude auth status --json` | it prints `"loggedIn": true` |

Each CLI is also run with `--version`. `claude` runs without the variables
whose names start with `CLAUDE_CODE_`, `CLAUDECODE` or `ANTHROPIC_`.

`state` is the first that applies:

| State | When |
|---|---|
| `not installed` | The CLI is not found |
| `check failed` | A check could not run, exited with an unexpected code, printed something unexpected or timed out; `reason` names the command and, when it printed something, its first line of output |
| `not signed in` | The sign-in check ran and found no sign-in |
| `connected` | Every requirement is met |

`check failed` sets `signed_in` to `null` when the sign-in check is the one
that failed.

Results are cached in `$XDG_CACHE_HOME/ember/engines.json` (default
`~/.cache/ember/engines.json`), mode 600 in a mode 700 folder, replaced whole
on each write. `emb engines` uses the cache while it is valid, and checks the
engines when there is no valid cache or with `--refresh`. Nothing refreshes it
on a timer.

Text output has one line per engine, `<engine>: <state>[, <version>][:
<reason>]`, followed by one indented line per unmet requirement,
`<label>[: <fix command>]`. The fix command is left out when it is not known.

`--json` prints an object keyed by engine:

```json
{
  "codex": {
    "engine": "codex",
    "state": "connected",
    "reason": null,
    "installed": true,
    "binary": "/usr/bin/codex",
    "version": "codex-cli 1.2.3",
    "signed_in": true,
    "connected": true,
    "requirements": [{"label": "...", "met": true, "fix": null}],
    "checked_at": 1800000000.0
  }
}
```

The requirements are the engine's CLI, with its install command as the fix,
and its sign-in, with its sign-in command as the fix:

| Engine | Install | Sign in |
|---|---|---|
| `codex` | `npm install -g @openai/codex` | `codex login` |
| `opencode` | `curl -fsSL https://opencode.ai/install \| bash` | `opencode auth login --provider openrouter` |
| `claude` | `curl -fsSL https://claude.ai/install.sh \| bash` | `claude auth login` |

A sign-in command names the CLI by its path when it is not found on `PATH`.

### Models

| Engine | Source | `source` |
|---|---|---|
| `codex` | `codex debug models`, models whose visibility is `list` | `cli` |
| `opencode` | `https://openrouter.ai/api/v1/models`, ids of the form `<provider>/<model>` as `shortlist.opencode` takes them | `openrouter` |
| `claude` | The aliases Claude Code's `--model` takes: `fable`, `opus`, `sonnet`, `haiku`, each with efforts `low` to `max` | `cli` |

The `codex` and `opencode` lists are cached in
`$XDG_CACHE_HOME/ember/models-<engine>.json`, mode 600, and read from their
source again when the cache is 6 hours old, not valid, or with `--refresh`.
A list served from the cache has `source` `cache`. When the source cannot be
read, the cached list is used however old, with `reason` set; with no cache
the list is empty, `source` names the source that was tried, `fetched_at` is
`null` and `reason` is set. A failed read leaves the cache as it was.

`--json` prints:

```json
{
  "models": [
    {
      "id": "openai/gpt-4o", "label": "GPT-4o",
      "efforts": ["low", "high"], "default_effort": "low",
      "context_length": 128000,
      "prompt_price": 0.0000025, "completion_price": 0.00001,
      "supports_tools": true
    }
  ],
  "source": "openrouter",
  "fetched_at": 1800000000.0,
  "reason": null
}
```

Prices are US dollars per token. `context_length`, the prices and
`supports_tools` are `null` for `codex` and `claude` and when OpenRouter does
not report them.

Text output starts with `<engine> models from <source>, read <age> ago` (or
`never read`), then one line per model: its id, label, efforts, context
length, prices in US dollars per million tokens and `tools` when it takes tool
calls. `reason` goes to standard error as `emb engines: <reason>`.

## `emb run`

Starts one worker in its own worktree and tmux session and waits for its
turn, or works with an existing run. The engine's driver is the `driver()`
factory of the module `ember.engines.<engine>`; an engine without one exits 69
with `owner: this Ember build has no driver for <engine>; update Ember`.

Data goes to standard output. Messages go to standard error as
`emb run: <message>`; each non-zero exit then prints one `next:` or `owner:`
line from the exit table below.

### Options

| Option | Does |
|---|---|
| `--engine ENGINE --dir DIR --task-file FILE` | Start a run of `codex`, `opencode` or `claude` on the git checkout holding `DIR`, with `FILE` as the brief |
| `--model MODEL` | With a start: the model |
| `--effort EFFORT` | With a start: the effort |
| `--read-only` | With a start: work in the checkout itself and edit nothing |
| `--read PATH` | With a start: a path the run may read, in addition to `sandbox.read`; repeatable |
| `--trailer LINE` | With a start: a trailer line for each commit; repeatable; none by default |
| `--claim ID` | With a start: read the menu with this claim id |
| `--window` | With a start: also open a terminal window on the session |
| `--send RUN --message-file FILE` | Send `FILE` as the next message and wait for its turn |
| `--screen RUN` | Print the text of the session's pane |
| `--interrupt RUN` | Interrupt the current turn, as the engine's driver does it (see Engine drivers) |
| `--attach RUN` | Open a new terminal window on the session; prints `window <pid>` |
| `--close RUN` | End the session and record the run closed |
| `--force` | With `--close`: close even while a turn runs, another command waits on the run, or the session holds messages Ember did not send. With `--send`: send even when the session holds messages Ember did not send |
| `--recover RUN` | Collect and commit a turn that ended while no command waited, or wait for it |
| `--status [RUN]` | Print one run's record, or every run's |
| `--json` | With `--status`: print JSON |
| `--quiet` | Print only the first line and each turn's summary |
| `--stall MINUTES` | Minutes without a change on the session's screen before exit 124; default 10 |

A start needs `--engine`, `--dir` and `--task-file`. The start options cannot
be used with the other modes, and only one mode is given at a time.

### Start

A start checks, in this order: every file and path exists (66); the brief has
a non-empty line (64); `git` and `tmux` are on `PATH` (69); `--dir` is in a git
checkout (65); the claim is `claimed` or `none` (64 for `unknown` or `gone`);
the engine is on (64, also when named by `--engine`); the engine is connected
in the status cache of `emb engines`, which is checked once first when there
is no cache (69, naming the fix command); the driver exists (69); the model
and effort (64); for an editing run, the repository has a main checkout and
a commit (70); for a sandboxed Codex run, no `sandbox_mode` key (69); and,
for a sandboxed run, a `bwrap` that runs `/bin/true` in a minimal sandbox
within 10 seconds (69, with `owner: install bubblewrap (the bwrap command)
or make it work, or set sandbox.<engine> to off`). For Codex the `bwrap` is
the one on `PATH`, else the one bundled with Codex. No run folder, worktree
or branch is created before these checks pass.

A failure after the run folder exists records `status: failed` with its
reason in `last_reason` and exits 69 for a sandbox or runtime folder problem,
70 for a worktree problem, and 1 otherwise. The worktree and branch stay.

Model and effort:

- `--model` wins. Otherwise the model comes from the menu row
  `single.writer`, or `single.reviewer` for `--read-only`. The row must be
  `set` and name the same engine; a `claude` row whose run-as is `subagent` is
  refused (64). With `--model`, a `claude` row whose run-as is `subagent` is
  not read at all.
- `--effort` wins. Otherwise the effort is the row's, when the row names the
  same engine and model, else the driver's default for the model, which may
  be none.

An editing run gets the branch `ember/<engine>/<id>`, cut from the named
checkout's `HEAD`, in a worktree at `.ember/worktrees/<id>` at the top of the
repository's main checkout, as `git worktree list` reports it. `.ember/` is
added to `info/exclude` in the repository's git folder when it is not there.
A read-only run works in the checkout itself and has no branch.

Each run's sandbox follows the engine's switch, `sandbox.codex`,
`sandbox.opencode` or `sandbox.claude`. The run records who provides it in
`sandbox`: `codex` for Codex switched on (Codex's own sandbox with Ember's
permission profile), `bubblewrap` for OpenCode or Claude switched on
(Ember's `bwrap` command line), and `none` for a switch that is `off`. The
run also records `worker.network` in `network` and its read paths in
`read_effective`: the `sandbox.read` setting, then the `--read` paths. Every
turn uses the recorded values, whatever the settings say later.

A sandboxed worker sees the machine read-only. The home folder is hidden
except `~/.local/bin`, `~/.local/lib`, git's configuration, the worktree,
the repository's git folder, the read paths and the engine's own folders;
`$XDG_RUNTIME_DIR` and Ember's configuration and state folders are hidden.
The worktree is writable, or read-only for a read-only run. The engine's
own folders are:

| Engine | Writable | Read-only |
|---|---|---|
| `codex` | Those of Codex's `:workspace` profile | |
| `opencode` | `opencode` in `$XDG_DATA_HOME`, `$XDG_CACHE_HOME` and `$XDG_STATE_HOME` | `opencode` in `$XDG_CONFIG_HOME`; the folder of the `opencode` binary in the home folder |
| `claude` | `$CLAUDE_CONFIG_DIR` or `~/.claude`, `~/.claude.json`, `~/.claude.json.backup`, `claude` and `claude-cli-nodejs` in `$XDG_CACHE_HOME` | The folder the `claude` command resolves to, in the home folder |

Network: Codex has network only when `network` is true; its web search is
always on. OpenCode keeps network for OpenRouter. Claude keeps network for
its API, so its `Bash` commands keep network too; `network` controls only
its `WebSearch` and `WebFetch` tools.

A sandboxed Codex run with a `sandbox_mode` key in a Codex configuration
file exits 69 naming the files. The check runs again on the new worktree
once it is created, for a tracked `.codex/config.toml`; a conflict there
records the run failed and exits 69, and the worktree and branch stay. With
`none`, a run of any engine prints the `WARNING:` line of the sandbox.

The desktop app's sandbox check of an engine keeps its last result per
engine in `$XDG_CACHE_HOME/ember/sandbox.json` (default
`~/.cache/ember/sandbox.json`), mode 600; writers hold `sandbox.lock` in the
same folder while they read, merge and write it.

The session is a tmux session named by the run id. Its first message is the
line `Brief from Claude, the lead, for Ember run <id>.`, a blank line and the
brief, pasted once the engine is ready; a driver that puts the brief on the
engine's command line pastes nothing. The session runs with no window. With
`--window`, a window of the `terminal` setting also opens on the session; a
window that cannot open prints a `WARNING:` line and the run goes on.
`--attach` opens a window on a running session at any time.

The first line on standard output is `run <id>`.

### Turns

A command that waits for a turn observes it every second. It stops with exit
124 when the session's screen has not changed for `--stall` minutes and the
turn has not ended; the session keeps running. Each command, including each
`--recover`, starts its own count. Ctrl+C stops the waiting command with exit
74; the turn keeps running. Ctrl+C while a turn is being collected or
committed exits 74 with `next: emb run --recover <run> (finishes the saved
turn)`.

When the turn ends, its reply is written to `turn-<n>.md` and, for an editing
run, every change in the worktree is committed as one commit (`git add -A`).
The subject is the brief's first non-empty line, cut to 72 characters, with
` (turn <n>)` after turn 1, followed by the trailer lines. A turn with no
changes makes no commit.

A turn that fails (69, 71, 73), including an engine that exits during the
turn, or that ends with no reply (80) is committed the same way when the
worker changed files, with the subject
`<subject> (turn <n>, incomplete: <reason>)`, where `<subject>` is cut to 72
characters and `<reason>` is `sign-in failure` (69), `engine failure` (71),
`provider error` (73) or `no reply` (80). The turn's exit is unchanged. Nothing is committed when the worktree's branch or
`HEAD` is not where the run left it (76); a refused commit or failed
`git add` exits 77 and leaves the work uncommitted. A refused commit saves
its full stdout then stderr as UTF-8 with replacement for bad bytes in
`<run folder>/hook-refusal.txt`. A successful commit clears that file.

A read-only run has no commit step. Before and after each turn Ember reads
`git status --porcelain=v1 -z --untracked-files=all` and a SHA-256 hash of
each listed file, and prints `WARNING: read-only run <id> changed <path>` for
each path whose state changed; `last_reason` lists them.

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

A missing value prints as `-`, and an effort that is not set as `default`.
`window` is the terminal's process id of a start. `--quiet` prints the summary
line only.

`--send` refuses with 75 a turn that has not been collected. Without
`--force` it then asks the driver whether the session holds messages Ember
did not send, before it commits, saves a snapshot or changes the turn's
state; when it does, or when its history cannot be checked, the command
exits 75 with `emb run: the session of <run> holds messages Ember did not
send (<what the driver found>); Claude asks the owner before going on` and
`next: ask the owner about the messages in the session of <run>; with their
go-ahead, repeat the command with --force`. `--force` skips only this
check, and records the messages then in the session as accepted, so they do
not refuse later commands. `--send` then retries a collected turn's commit.
If it succeeds, the correction is sent as written. If `git commit` is
refused again, the stored hook output is refreshed and sent ahead of the
correction, under `The commit hook refused your last turn's commit:` in a
fenced block. Sent output is capped at its last 200 lines and last 64 KiB,
whichever is smaller, with a note when cut. The following turn makes one
commit with its normal subject and the files from both turns. A failed
`git add` still stops the send with exit 77 and points to `--recover`.
It sends only when the driver reports the session idle; a busy or unknown
session exits 75. The stored output is cleared after the driver confirms
delivery; a failed send keeps it. When the driver fails (71) or Ctrl+C (74)
stops the command before the message is known to be delivered, the run's
previous turn state is restored and the line is
`next: emb run --send <run> --message-file <file>
(the message was not sent)`.

`--recover` finishes the current turn from its recorded state: it waits for a
turn that was sent, then collects and commits it, or commits a turn that was
collected. A commit made for the turn but not recorded is recorded rather than
made again.

`--close` refuses with 75 while a turn runs or another command waits on the
run, unless `--force`. Without `--force` it also refuses with 75 an editing
run whose turn is collected but not committed, with `next: emb run --recover
<run> (commits the collected turn), then emb run --close <run>`, and a live
session that holds messages Ember did not send or whose history cannot be
checked, with the line `--send` prints. It ends the
session and keeps the worktree and branch. A command waiting on a run that is
closed stops with 72, also when the driver fails or the session is missing
after the close.

`emb run` never pushes, merges, rebases or deletes a branch, and never removes
a worktree.

### Locks

Start, `--send`, `--recover` and `--close` without `--force` hold the run's
lock, `$XDG_RUNTIME_DIR/ember/locks/<id>.lock`; a second of them on the same
run exits 75. `--screen`, `--status`, `--interrupt` and `--attach` take no
lock. Changes to `run.json` are made under the run's record lock,
`run.lock` in the run's folder.

### Run records

Each run has a folder `$XDG_STATE_HOME/ember/runs/<id>/` (default
`~/.local/state/ember/runs/<id>/`), mode 700.
The runs folder also holds flow folders, `runs/<flow id>/` (see
`emb flow`), and panel folders, `runs/<panel id>/` (see `emb panel`);
`--status` lists runs only.

Each run folder holds:

| File | Holds |
|---|---|
| `run.json` | The record, mode 600, replaced atomically |
| `brief.md` | The brief |
| `turn-<n>.md` | The reply of turn `n` |
| `turn-<n>.status.json` | For a read-only run: the checkout's state before turn `n` |
| `turn-<n>.outcome.json` | How turn `n` ended: its state, reason, flags and incomplete reason |

A run id is `<engine>-<yyyymmdd>-<hhmmss>-<6 hex digits>`, with the time in
UTC. It is also the tmux session's name.

`run.json` fields:

| Field | Type | Value |
|---|---|---|
| `id` | string | The run id |
| `engine` | string | `codex`, `opencode` or `claude` |
| `model` | string | The model |
| `effort` | string or null | The effort; null when the engine's default applies |
| `read_only` | boolean | Whether the run edits nothing |
| `repo` | string | The top of the checkout `--dir` is in |
| `worktree` | string | The worker's working folder: its worktree, or the checkout for a read-only run |
| `branch` | string or null | `ember/<engine>/<id>`; null for a read-only run |
| `base` | string or null | The commit the run started from |
| `base_branch` | string or null | The source branch at start; null for a detached checkout or a record without this field |
| `sandbox` | string | Who provides the sandbox: `codex`, `bubblewrap` or `none` |
| `network` | boolean | Whether `worker.network` was `on` at the start |
| `read` | list of strings | The `--read` paths |
| `read_effective` | list of strings | Every path the run may read: the `sandbox.read` setting at the start, then the `--read` paths |
| `trailers` | list of strings | The `--trailer` lines |
| `claim` | string or null | The `--claim` id |
| `status` | string | `starting`; `running` while a turn is active, also after exit 124 or 74; `waiting` once a turn's reply is collected; `failed` when the launch or a turn failed; `closed` |
| `turn` | integer | The current turn; 0 before the first message |
| `turn_state` | string or null | `sent`, `collected` or `committed` for the current turn |
| `reply` | string or null | The current turn's reply file, once collected |
| `expected_head` | string or null | The branch tip before the current turn, then its commit; null for a read-only run |
| `started_at` | number | Seconds since the epoch |
| `updated_at` | number | Seconds since the epoch |
| `ended_at` | number or null | When the session ended |
| `last_exit` | integer or null | The exit code of the last turn or failure |
| `last_reason` | string or null | Its message, or the paths a read-only turn changed |
| `commits` | list of strings | The run's commits, oldest first |
| `subject` | string | The brief's first non-empty line |

Paths are absolute.

### Status

`--status RUN --json` prints the record with `session_state`: `live`, `dead`
(the pane's command ended) or `missing` (no session). `--status --json`
prints a list of them, oldest first. A folder whose `run.json` cannot be read
is skipped with a message on standard error. Without `--json`, `--status RUN`
prints one `<field>: <value>` line per field, and `--status` one line per run:
`<id>  <status>  <session_state>  turn <n>  <subject>`.

### Exit codes

| Code | Meaning | Line |
|---|---|---|
| 0 | Success | |
| 1 | Unexpected error, or the settings database cannot be read | `next: emb run --status`; for the database, `owner:` |
| 64 | Usage error, unanswerable menu row, switched-off engine or invalid claim | `next: emb run --help`, or `owner:` naming the setting |
| 65 | Not a git checkout | `next:` a start inside a checkout |
| 66 | Missing file or path | `next:` a start with existing paths |
| 69 | Engine, driver, `git` or `tmux` missing, engine not signed in, a sandbox conflict, a switched-on sandbox without a working `bwrap`, or no usable runtime folder | `owner:` naming the fix |
| 70 | Worktree creation failed, or no main checkout or commit | `next: git -C <dir> worktree prune, then ...` |
| 71 | Engine or session failed | `next: emb run --status <run>`; for a failed send, `next: emb run --send <run> ...` |
| 72 | Session or window unavailable, or the run was closed | `next: emb run --status <run>`, or a new start |
| 73 | Provider limit or outage | `next: emb run --send <run> ...` |
| 74 | Interrupted | `next: emb run --recover <run>`; for an interrupted send, `next: emb run --send <run> ...` |
| 75 | Close refused while a turn runs or is collected but not committed, a busy session, another command on the run, or a session holding messages Ember did not send | `next: emb run --recover <run>` or `--status`; for messages Ember did not send, `next: ask the owner ...` |
| 76 | Branch or `HEAD` moved; nothing committed | `next: git -C <worktree> log --oneline -3` |
| 77 | `git commit was refused`, or `git add failed` | `next: emb run --send <run> --message-file <file>` sends hook output with the correction for a refused commit; a failed `git add` points to `next: emb run --recover <run>` |
| 80 | The turn ended with no reply | `next: emb run --screen <run>, then emb run --send <run> ...` |
| 124 | Stall; the turn may still be running | `next: emb run --recover <run>` |

### Engine drivers

| | Codex | OpenCode | Claude |
|---|---|---|---|
| Starts | Codex's interactive session in the pane: `codex -C <worktree> -m <model> -c check_for_update_on_startup=false [-c model_reasoning_effort=<effort>] -a never --search`, with Ember's permission profile in `-c` options, or `--sandbox danger-full-access` when the sandbox is off | OpenCode's interactive view in the pane, with its built-in server on a free port of `127.0.0.1` protected by a password, OpenRouter as the provider and the agent `ember-worker`, inside `bwrap` when the sandbox is on | A program of Ember's in the pane that runs one `claude -p --output-format stream-json` per turn in the worktree, inside `bwrap` when the sandbox is on, and shows its progress; the program itself runs outside the sandbox |
| Brief | Pasted once Codex's composer shows `? for shortcuts` | Sent through the server's session API once the server answers and the view has drawn its screen | Written as turn 1's request before the pane starts; given to the CLI on standard input |
| Turn end and reply | Read from Codex's rollout file: `task_complete` with its last message, or `turn_aborted`; an `error` event during the turn fails it | Read from the session status and messages: the turn's last assistant message, once the session is idle; its text parts are the reply | The CLI's exit; the reply is the assistant text after the turn's last tool result |
| Later messages | Pasted | Sent through the session API | A new `claude -p` that resumes the previous turn's CLI session |
| Read-only run | Ember's profile extends Codex's `:read-only` profile | The agent may not edit, write or patch files or reach folders outside the checkout, and the worktree is mounted read-only | The tools are `Read`, `Glob` and `Grep`, and the worktree is mounted read-only |
| Sandbox provider | `codex` | `bubblewrap` | `bubblewrap` |
| `--interrupt` | Presses Escape in the session | Aborts the turn through the session API | Sends `SIGINT` to the process group of the turn's CLI, as Ctrl+C in the pane does; the program takes the next turn |
| Messages Ember did not send | Each `user_message` in the rollout that does not match, in order, a message Ember pasted | Each user message whose id Ember did not send | None; the pane takes no typed messages |

Claude's turns use `--restricted`, `--safe-mode` and `--strict-mcp-config`;
the run's read paths are added with `--add-dir` and edits to them are
refused. While a tool call or a model message runs, Claude's pane prints a
line such as `· running Bash for 3 min` at least once a minute.

## `emb gapcheck`

Reviews a plan for undefined or contradictory behaviour in one read-only
`emb run` run. The run's folder keeps its record, brief and reply. After a
successful turn the command reads the reply and closes the run without
`--force`. A refused or failed close leaves it open and prints a warning.
The command creates and finishes no claim; `--claim` passes an existing
claim to the run.

### Options

| Option | Does |
|---|---|
| `--plan FILE` | Required: the plan to review; an empty or blank file exits 64 |
| `--dir DIR` | A folder in the checkout; defaults to the current folder. The brief and run use the checkout's top |
| `--engine ENGINE` | Use `codex`, `opencode` or `claude`; otherwise use `single.reviewer` |
| `--model MODEL` | Use this model, whether or not shortlisted; requires `--engine` |
| `--effort EFFORT` | Use this effort; requires `--engine` |
| `--claim ID` | Read choices with this claim and pass it to the run |
| `--read PATH` | Add a readable path to the run; repeatable |
| `--window` | Open a terminal window on the run's session |
| `--stall MINUTES` | Stop waiting after this many minutes without a screen change; default 10 |
| `--json` | Print the result as JSON |
| `--brief` | Print the brief and start nothing; accepts `--dir` |
| `--reply FILE` | Read a reply from another worker and start nothing; accepts `--json` |
| `--help` | Print the command's options |

`--brief` and `--reply` cannot be combined. Neither accepts `--engine`,
`--model`, `--effort`, `--claim`, `--read`, `--window` or `--stall`.
`--brief` accepts no `--json`; `--reply` accepts no `--dir`.

Without `--model`, the `single.reviewer` row must be `set`; when `--engine`
is given it must match the row. A Claude row that runs as a subagent exits
64 with the `--brief` and `--reply` route. With `--engine` and `--model`,
the row may be `ask`. The run checks that the engine is on and connected
and that its driver accepts the effort. A `gone` or `unknown` claim stops
before the reviewer row is inspected.

Checkout discovery and the run share an environment without `GIT_DIR`,
`GIT_WORK_TREE`, `GIT_COMMON_DIR`, `GIT_INDEX_FILE`, `GIT_OBJECT_DIRECTORY`,
`GIT_NAMESPACE` or `GIT_PREFIX`.

The brief contains the whole plan with line numbers. Its temporary file is
`$XDG_RUNTIME_DIR/ember/gapcheck-<pid>-<6 hex>.md`, mode 600, with the usual
Ember runtime fallback. It is removed after the start returns or fails;
the run keeps its own copy as `brief.md`.

### Plan steps

The gap check tries these splits in order:

1. A JSON object with a non-empty `tasks` array of objects gives one step
   per task. Its title is `task <id>: <summary>` when `id` is a string,
   otherwise `task <n>: <summary>`. The summary is the task's `task` value
   on one line; an empty summary leaves just `task <id>` or `task <n>`.
2. Two or more unindented numbered items (`1.` or `1)`, followed by
   whitespace and text) give one step per item.
3. Otherwise, sections at the shallowest heading level that occurs at
   least twice give one step per section. Headings start at the left margin
   with one to six `#` characters, whitespace and text. A section ends
   before the next heading of the same or a shallower level.
4. Otherwise, the whole file is one step titled `the whole plan`, with
   lines 1 to the line count (at least 1).

Numbered items and headings inside code fences are ignored. A fence starts
with three backticks or three tildes, with up to three spaces of indentation;
only the same marker closes it. Trailing blank lines are removed from
numbered-item and heading ranges. Each step has a consecutive number
starting at 1 and a one-line title cut to 100 characters in total. JSON
tasks have no line range; other ranges are one-based and inclusive. An
empty or blank file splits as a whole-file step; the command refuses it.

A JSON object has a failure model when its `failure_model` value is a
nonblank string, including when its tasks do not provide the step split.
A Markdown plan has one when a heading outside code fences contains
`failure model`, without regard to case. Body text alone does not count.
The brief asks the reviewer to report a missing failure model as the first
gap of step 1, with harm `high`.

### Reply

The reply schema is packaged as `ember/data/gapcheck-reply.schema.json`:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "ember/gapcheck-reply/1",
  "title": "Gap check reply",
  "description": "A read-only review of a plan, with one entry per step and a list of undefined or contradictory behaviours. A step with no gaps has an empty gaps list.",
  "type": "object",
  "additionalProperties": false,
  "required": ["steps"],
  "properties": {
    "steps": {
      "type": "array",
      "items": {"$ref": "#/$defs/step"}
    }
  },
  "$defs": {
    "step": {
      "type": "object",
      "additionalProperties": false,
      "required": ["step", "gaps"],
      "properties": {
        "step": {"type": "integer", "minimum": 1},
        "gaps": {
          "type": "array",
          "items": {"$ref": "#/$defs/gap"}
        }
      }
    },
    "gap": {
      "type": "object",
      "additionalProperties": false,
      "required": ["gap", "harm", "why", "default"],
      "properties": {
        "gap": {"type": "string", "minLength": 1},
        "harm": {"type": "string", "minLength": 1, "enum": ["high", "medium", "low"]},
        "why": {"type": "string", "minLength": 1},
        "default": {"type": "string", "minLength": 1}
      }
    }
  }
}
```

Each step entry names its step number and a list of gaps. Empty gap lists
and an empty `steps` list are valid for the schema. Every expected step
must still have a valid entry. Strings must contain at least one character;
whitespace counts as a character. Step numbers must be integers at least
1; JSON numbers such as `1.0` count as integers, and booleans do not.
No object may have keys beyond those in the schema.

The reader tries the whole reply as JSON, then the last backtick code block
(unlabelled or labelled `json`), then the span from the
first `{` to the last `}`. The first candidate that parses as an object is
used. If it fails the schema, each independently valid step entry is kept;
an entry with an invalid gap is discarded in full.

Duplicate step entries are joined. Covered steps appear in plan order.
Gaps within each step and in `ranked` sort by `high`, `medium`, `low`,
keeping reply order within each harm. `counts` counts only gaps for known
steps. `uncovered_steps` follows plan order; `unknown_steps` lists unknown
numbers once, in numeric order. Unknown steps are excluded from `steps`,
`ranked` and `counts`.

`parse_error` is null when the reply matches the schema and covers exactly
the expected steps. Otherwise it uses these message forms:

- `the reply is not a JSON object`
- `the reply does not match the schema at <path>: <message>`
- `the reply misses step <numbers>`
- `the reply names step <numbers>, which the plan does not have`
- `the run produced no reply` when a run has no reply file

A schema path starts at `$`, with fields and zero-based array indices,
for example `$.steps[2].gaps[0].harm`. Schema messages are `expected an
object`, `missing required key '<key>'`, `unexpected key '<key>'`,
`expected an array`, `expected an integer`, `expected an integer at least
1`, `expected a string`, `expected a non-empty string`, or `expected one
of high, medium, low`. The first schema error is reported: object type,
required keys in schema order, extra keys in alphabetical order, then
fields and list entries in order. Step and gap fields follow their schema
order. Schema, missing-step and unknown-step problems are joined with
`; ` in that order; number lists use `, `. A reply with no JSON object
reports only that error and leaves every step uncovered.

### Output

The default text has a block for each covered step:

```text
step 1: <title>
  high: <gap>
    why: <reason>
    default: <proposed behaviour>
step 2: <title>
  no gaps
counts: high 1, medium 0, low 0
uncovered steps: none
unknown steps: none
```

`--json` prints an object with these fields:

| Field | Value |
|---|---|
| `plan` | Absolute plan path |
| `plan_steps` | `tasks`, `numbered`, `headings` or `whole` |
| `failure_model` | Whether the plan has a failure model |
| `reviewer` | `engine`, `model`, `effort` and `source`; values come from the run record, and `source` is `option` when `--engine` was given, otherwise `menu` |
| `run` | `id`, `reply` (absolute reply path or null), `exit` (the start's exit), `closed` and `close_error` (null or a reason) |
| `steps` | Covered steps, each with `step`, `title` and `gaps` |
| `ranked` | Gaps with their `step`, ranked high, medium, low |
| `counts` | Gap counts for `high`, `medium` and `low`, including zero |
| `uncovered_steps` | Plan step numbers with no valid reply entry |
| `unknown_steps` | Reply step numbers absent from the plan |
| `parse_error` | Null, or the reply's validation or reading error |

For `--reply`, `reviewer` and `run` are null. Once a run id exists, a result
prints even when the run fails. Before a run starts, a failure prints no
result. A run with no reply reports `the run produced no reply` and every
step uncovered.

Standard error first receives all the run's messages, action lines and
warnings, including those from a successful run. Then come the gap check's
`emb gapcheck:` problem, warnings for a missing failure model or an open
run, and at most one action line from the gap check. A failed close prints
`WARNING: the run <id> stays open: <reason>; close it with emb run --close <id>`.
When no failure model is present, the warning is
`WARNING: the plan has no failure model; every unsettled case is reported as a gap`.

A non-zero run exit keeps the run open. Its follow-up names
`emb gapcheck --plan <plan> --reply <run folder>/turn-1.md` only when the
reply file exists; otherwise it names a new gap check. An unreadable
existing reply exits 66. A successful turn with no reply file exits 81.

### Exit codes

| Code | Meaning | Line |
|---|---|---|
| 0 | The reply covers every step, or the brief was printed | A failed close prints a warning |
| 1 | Settings database or run record cannot be read, or checkout discovery times out | `owner: repair or remove the settings database the message names`, `next: emb run --status <run>` or a new check |
| 64 | Usage error, invalid claim, reviewer row not `set`, Claude subagent row, or a run start check | `next: emb gapcheck --help`, the claim's `emb choices` line, `owner: name the reviewer for this gap check, or set Reviewer on the Menu screen`, the `--brief`/`--reply` route, or the run's line |
| 65 | `--dir` is not in a git checkout | `next: emb gapcheck --plan <plan> --dir <a folder in the checkout>` |
| 66 | Plan or reply cannot be read, or `--dir` does not exist or is not a folder | `next: ls -l <path>` |
| 69 | Git is unavailable, the runtime folder cannot be used, or the run exits 69 | `owner: install git`, the run's runtime-folder instruction, or the run's line |
| 74 | Interrupted | The run's recovery line when a run exists, then a gap check action |
| 81 | Invalid reply, missing or unknown steps, or a successful run without a reply file | `next: emb gapcheck --plan <plan> --reply <reply>` after fixing the reply, or a new check |
| Other | The run's exit from the `emb run` table | The run's line, then a gap check action |

## `emb flow`

A flow runs a JSON plan of tasks in waves, in a runner's own session. Each
task has a writer run and read-only review runs with fix rounds. Accepted
tasks merge into `ember/flow/<id>`; the plan's check runs after each wave's
merges. Tasks that need a decision produce lead stops. Claude answers the
stops, reviews the flow branch and merges it with `emb merge`.

### Options

```text
emb flow --plan FILE --dry-run [--writer W] [--reviewer W] [--claim ID] [--json]
emb flow --plan FILE --start [--writer W] [--reviewer W] [--claim ID] [--stall MINUTES]
emb flow --watch FLOW [--json]
emb flow --status [FLOW] [--json]
emb flow --lead FLOW TASK (--send FILE | --accept [--commit SHA] | --review [--reviewer W] | --worker W | --drop)
emb flow --resume FLOW
emb flow --cancel FLOW [--reason TEXT] [--json]
```

| Option | Does |
|---|---|
| `--plan FILE` | Read the JSON plan; required with `--dry-run` or `--start` |
| `--dry-run` | Validate the plan and workers, then print waves and a gap check command |
| `--start` | Create the flow branch, worktree and records, then start the runner |
| `--writer W`, `--reviewer W` | Name a worker as `<engine>:<model>[@<effort>]`; override the plan and menu |
| `--claim ID` | Read choices with this claim; store it on the flow without creating or finishing a claim |
| `--stall MINUTES` | With `--start`: minutes without screen changes before a task stops waiting; default 10 |
| `--watch FLOW` | Wait for a lead stop or terminal outcome, using the shared watch cursor |
| `--status [FLOW]` | Read one full record, or summaries of every flow, oldest first |
| `--lead FLOW TASK` | Queue exactly one answer for a task |
| `--send FILE` | Copy a nonblank UTF-8 correction for the existing writer |
| `--accept` | Accept the current task branch tip without another review |
| `--commit SHA` | With `--accept`: use this commit instead; resolve it to a full commit hash before queueing |
| `--review` | Collect a running writer turn and request fresh reviews with a new fix budget |
| `--reviewer W` | Also accepted with `--lead ... --review`; change this task's reviewer |
| `--worker W` | Replace this task's writer and run its pipeline from the flow tip |
| `--drop` | Drop the task; its dependants remain blocked |
| `--resume FLOW` | Start another runner attempt for an idle, unfinished flow |
| `--json` | Print JSON with `--dry-run`, `--watch` or `--status` |
| `--help` | Print the options |

Exactly one mode is required. `--plan`, `--writer` and `--claim` are
accepted only with `--dry-run` or `--start`. Answer options require
`--lead`, which accepts exactly one of `--send`, `--accept`, `--review`,
`--worker` and `--drop`. Other combinations exit 64 before reading files.

Validation reads the plan, checks that its directory exists and belongs to
a checkout, resolves the base commit, reads the claim and settings, then
resolves and checks both workers. All plan problems print together. A
`gone` or `unknown` claim exits 64 even with explicit workers. The command
resolves omitted effort from a matching set menu row, then the driver's
default. Stored workers include `effort_frozen: true`; `null` effort denotes
an engine default. Replacement-worker answers carry the same marker.

Dry runs create no flow record, branch, worktree or session. The gap check
command uses the resolved reviewer. Starting a flow does not run or require
a gap check. A start requires tmux and a usable private runtime folder.
A dirty source checkout is allowed; the flow starts from the base commit.

Startup commands serialize through `lifecycle.lock` in the flow folder.
`startup.json` records a timestamp and the last event sequence before the
attempt. A reservation allows 30 seconds for the runner to take its lifetime
lock. Concurrent starts of the same flow are refused during that interval.
A resume refuses a live runner or a completed flow, removes an existing
dead session, and starts a new session. A live session without a runner
lock gets up to 10 seconds to end. Once its pane dies or the session ends,
startup removes any dead session and launches the replacement. A session
still running after the wait is kept and reported with exit 75. Module
loading uses Python's `-I` mode and the absolute package root of the
invoking Ember installation.

### Plan

A flow plan is a UTF-8 JSON object. The packaged schema is
`ember/data/flow-plan.schema.json`, with id `ember/flow-plan/1`.
Unknown keys are refused at every object level. Validation collects every
problem before returning code 64. A missing, unreadable or non-UTF-8 plan
file gives code 66; an empty or blank file gives 64. An empty `tasks` list
is refused with code 64.

| Field | Default | Rule |
|---|---|---|
| `schema_version` | Omitted | Integer `1` |
| `dir` | Required | String starting with `/`; existence and checkout checks belong to the command |
| `base` | `HEAD` | Non-blank string |
| `trailers` | `[]` | List of non-blank strings without CR or LF |
| `writer`, `reviewer` | Menu row | Objects with required `engine` and `model`, optional `effort`, and no other keys |
| `rounds` | `2` | Integer from 0 to 3 |
| `parallel` | `4` | Integer from 1 to 8; maximum concurrent jobs within a wave |
| `check` | No check | Non-empty argv list of strings; first argument non-blank; no NUL in any argument |
| `check_timeout` | `1800` | Integer seconds, at least 1 |
| `context`, `failure_model` | `""` | Strings |
| `read` | `[]` | List of strings starting with `/`, without duplicates |
| `tasks` | Required | Non-empty list of task objects, in execution and merge order |

Integers exclude booleans and JSON numbers written with a decimal point.
Worker `engine` is `codex`, `opencode` or `claude`. The model is non-blank,
contains no whitespace or `@`, and need not be shortlisted. Effort matches
`[a-z]+`; its driver checks whether it accepts that effort.

| Task field | Default | Rule |
|---|---|---|
| `id` | Required | Unique string matching `[a-z0-9][a-z0-9-]{0,39}` |
| `subject` | `<id>: <first line of task>`, cut to 72 characters | Non-blank string without CR or LF, at most 72 characters; the writer brief's first line |
| `task` | Required | Non-blank string |
| `files` | Required | Non-empty list of unique relative POSIX paths; no leading slash, empty component, `.` component, `..` component or NUL |
| `after` | `[]` | List of ids of other tasks; every id exists, no self dependency and no cycle |
| `review` | `""` | String |

Cycle diagnostics name the path, such as `cycle: a -> b -> a`.
A wave takes eligible tasks greedily in plan order: all their dependencies
have merged, and none of their declared files overlaps an already selected
file. Equal paths and folder prefixes overlap; `src` overlaps `src/a.py`,
but `src/a` does not overlap `src/ab.py`. The dry-run waves assume every
preceding task merges. A wave can contain more than `parallel` tasks.

Warnings do not refuse a plan:

- `no failure_model; every unsettled case is a gap`
- `no check; waves are merged without a check`
- `<id>: <path> is a folder; list the files the task changes`, when the path
  is a tree at `base`, read with `git cat-file -t`
- `<id>: no test file in its files`, when no file is under `test` or `tests`,
  starts with `test_`, or has a name matching `*_test.*`

A failed or timed-out folder probe emits no folder warning. It does not
change plan validation. Git probes omit checkout override variables and
`EMBER_FLOW_RUNNER` from their environment.

### Workers

Each role resolves in this order: its `--writer` or `--reviewer` option,
its plan object, then `single.writer` or `single.reviewer` in the choices
reply. Options use `<engine>:<model>[@<effort>]`; model names may contain
colons and slashes. The result records `engine`, `model`, `effort` and
`source` (`option`, `plan`, `menu`, or `answer` for a lead answer).

Menu resolution requires a readable settings source and a row in state
`set`. A Claude row set to run as a subagent is refused: a flow runs CLI
workers. These refusals give code 64 with an `owner:` line naming the
role's explicit option and Menu screen. Explicit options and plan workers
work when the rows are `ask`; they do not require a shortlist entry.

The worker check requires its engine switch to be `on` (64), a connected
engine (69), an available driver (69), and an accepted explicit effort
(64). Connection status comes from the engine cache, or a status read
when no usable cache exists. The connection error names the unmet
requirement's fix when present.

The flow record stores the selected workers. The runner passes model and
effort to each run. A claim used to read choices is recorded on the flow;
individual runs receive no claim. Worker resolution preserves an omitted
effort as `null`; callers must freeze the effective effort before starting
runs that could otherwise reread menu effort.

### Runner

The runner runs in a tmux session named `<flow id>` on Ember's private
server. Attach with `tmux -S <socket> attach -t <flow id>`, using the same
socket as `emb run`. It holds an exclusive lock at
`$XDG_RUNTIME_DIR/ember/locks/<flow id>.lock` for its lifetime. A second
runner exits 75. The runner releases the lock before ending its session;
a crashed session remains available for inspection until resume replaces it.

The runner shares `<flow folder>/lifecycle.lock` with command startup,
resume and lead-answer enqueueing. It holds that lock from its final answer
read until after releasing its lifetime lock. Every attempt outcome uses
this handoff: completion, lead stops, provider halts, failed checks or
merges, signals and unexpected errors. When that read finds unread answers,
the runner takes them and continues before deciding whether to end. Valid
answers clear a provider halt; pending checks run before further work.
An answer queued during this handoff either reaches the runner before its
final read or sees no live
runner after acquiring the lifecycle lock. The runner releases the lifecycle
lock before continuing when that read finds more answers, and never holds
it during a wave. The command owns the `startup.json` reservation; the
runner leaves that file unchanged.

At each start the runner reloads the stored `plan.json`, refreshes the flow
branch tip, clears the previous end time and provider halt, and records a
new attempt. Events carry the attempt number. Worker effort is frozen
before starting runs: the stored effort, otherwise the driver's default
for the model. A driver without a default keeps `null`; runs do not fall
back to the current menu's effort. Every writer and reviewer receives the
stored model and effort, the plan's read paths, and no claim.

Between waves the runner takes queued answers, recomputes dependency
blocking, merges accepted tasks in plan order and runs the check when a
merge is pending. It then selects the next wave of pending tasks with
merged dependencies or an operation scheduled by a lead answer. File
declarations select disjoint tasks in plan order. At most `parallel` jobs
run at once; each job has its own Core and output streams. Every task
state change is saved under the runner's record mutex. Dependencies are
recomputed transitively after merges and before the runner ends.

Each job starts a writer in a new editing run from the flow tip, checks its
committed changes, and starts a fresh read-only review in the writer's
worktree. The run id is saved as soon as Core prints it. Must findings
within the round budget go back to the same writer as a fix message;
another fresh review follows. Immediately before sending a correction the
runner reads and saves the branch's actual tip, message file and target
turn. It compares the resulting tip against that saved tip, including on
recovery, and supplies it as the next review's fix base. Lead corrections
start a fresh round budget. Brief artifact numbers increase per task across
fresh budgets and replacement writers.

An unchanged first turn stops with `the writer made no change`; an
unchanged correction stops with `the fix made no change`. Changed paths
include both sides of renames. A change to another task's declaration in
the same wave stops before review with `changed <file>, which task <id> in
this wave declares`. Other outside paths are recorded and sent to the
reviewer. A failed run stops with Core's diagnostic and action line. An
unlaunched captured writer stops with a worker-replacement instruction.
Unreadable reviews, exhausted fix rounds, lead findings and fix verdicts
without blocking findings stop as described under Reviews. Each stop
emits a lead event with the available answer commands.

Accepted heads merge one at a time through `emb merge`'s implementation.
The branch tip must still equal the accepted or reviewed commit. A changed
tip stops with `the branch moved from <commit> to <tip>`. Merge conflicts
stop that task and leave the flow branch unchanged; other tasks continue.
Other merge failures fail the flow. A completed merge is recorded even when
its cleanup fails, with cleanup details kept separately. Equivalent changes
already contained in the flow can have a null `merge_commit`. Cleanup
closes writer runs without force and removes their merged branches and
worktrees when allowed. A refused close retains the branch and worktree
with the cleanup reason. Dropped and replaced branches stay in `abandoned`.

The check runs once after a wave's merges in the flow worktree, in its own
process group. Git, runs and checks receive the flow environment without
`GIT_DIR`, `GIT_WORK_TREE`, `GIT_COMMON_DIR`, `GIT_INDEX_FILE`,
`GIT_OBJECT_DIRECTORY`, `GIT_NAMESPACE`, `GIT_PREFIX` or
`EMBER_FLOW_RUNNER`. Each attempt writes a private
`check-<wave>-<attempt>.log`; its check entry names that file, and its event
contains the last 4000 characters. A timeout kills the process group and
records exit 124; failure to start records 127. After a check, the runner
resets and cleans the flow worktree. A failed check or cleanup fails the
flow with `check_pending` retained. Resume reconciles any recorded check
process, retries cleanup, and runs the check before further work. With no
check command the runner clears `check_pending` and returns to `running`.

Provider exit 73 stops the task and halts new runs. Active jobs finish;
queued writers remain pending. The flow stops with
`provider error on <engine>; new runs halted`. Resume clears that halt and
continues pending work; the task that failed still needs a lead answer.
Exit 124 stops with `stalled; the turn keeps running in <run>`;
a lead review answer collects that turn before reviewing it. An explicit
review answer reviews the branch's current tip against the task base even
when the writer's last turn failed or left no reply. The tip must differ
from the task base, and the usual changed-file checks still apply. The
explicit request is persisted for recovery; automatic recovery of a failed
writer turn still stops the task.

SIGHUP and SIGTERM request shutdown without writing from the signal
handler. The runner terminates an active check group, records `cancelled`
when a cancel request is pending, or `failed` with a resume instruction
otherwise. It appends an end event with exit 82 and releases its lock. Worker sessions remain. Each start aborts a recorded unfinished
merge, resets and cleans the flow worktree, and reconciles merging tasks
using the same containment rules as `emb merge`. Recovered merges set
`check_pending`. Writer metadata comes from its run record; recovered
turns receive the same change checks as uninterrupted turns. A missing
review id starts a new review; a completed or closed review is read from
its saved reply. Scheduled corrections reconcile the target turn before
another send. Answer effects and their consumption marker are saved
together, and pending closes can be repeated after a crash.

A flow waiting on stopped or conflicting tasks ends `stopped`, with no
end event. Otherwise it ends `done`, with end exit 0 when every task
merged or 82 when some were dropped or remain blocked. Failed and cancelled flows also
emit end exit 82. Flow folders, check logs and briefs remain on disk.

### Reviews

Each flow review is a fresh read-only run in the writer's worktree. The
review run is closed without `--force` after its reply is read. A refused
close leaves the run open and is reported by the runner.

The writer brief starts with the task's commit subject and preserves the
task text as written. It lists the task's files, the other tasks' files in
the wave, context and failure model when present, limits, the rule against
running git, checks and closing report headings. Lead corrections also
preserve their text as written.

The review brief starts with `Review of task <id>, round <n>, in flow
<flow id>: a read-only review; change no file.` It includes the task and
subject, declared files and the other tasks' files, review focus, context,
failure model, the base and head for `git diff` and `git log`, changed and
outside files, and up to 20,000 characters of the writer's last report.
It includes the review bar from `skill/SKILL.md` and the whole reply schema
from `ember/data/review-reply.schema.json` (`ember/review-reply/1`). Pasted
text in review briefs and generated fix messages is indented so it starts
no line with `@`.

The reviewer replies with one JSON object, no prose or code fence:

```json
{
  "verdict": "fix",
  "findings": [
    {
      "severity": "must",
      "file": "src/parser.py",
      "line": 12,
      "problem": "An empty input raises an exception.",
      "fix": "Return the empty result.",
      "scenario": "An empty input file crashes the command.",
      "earlier": null
    }
  ]
}
```

The schema requires `verdict` (`pass` or `fix`) and a `findings` array,
which may be empty. Each finding requires `severity` (`must`, `should` or
`lead`), `file` (a non-empty string or null), `line` (an integer or null),
`problem`, `fix` and `scenario` (strings), and `earlier` (an integer at least
1 or null). `note` is an optional string set by normalization. The schema
allows no other keys. The verdict is `fix` when a finding is `must` or
`lead`.

The reader tries the whole reply as JSON, the last backtick code block
(unlabelled or labelled `json`), then the span from the first `{` to the
last `}`. It uses the first candidate that parses as an object. A missing
object, a verdict other than `pass` or `fix`, or findings other than an
array makes the review unreadable. Extra keys are ignored by the reader.
Findings are normalized individually:

- A non-object becomes a finding whose problem is its JSON text.
- An unknown severity becomes `must`.
- `file` is kept only when it is a non-empty string. `line` is kept only
  when it is an integer, excluding booleans; otherwise each is null.
- `problem`, `fix` and `scenario` keep strings, use empty text for null,
  and use JSON text for other values.
- `earlier` is kept only when it is an integer from 1 through the number
  of earlier must findings; otherwise it is null. Booleans do not count.
- A `must` with no nonblank scenario becomes `should`, with
  `note: "demoted: no scenario"`. A `lead` with no nonblank problem becomes
  `should`, with `note: "demoted: no decision named"`. Other notes are empty.

The decision uses normalized findings in this order: an unreadable review
stops the task; any `must` with rounds left sends a fix turn to the same
writer; any `must` without rounds left stops with `unresolved after <n> fix
rounds`; any `lead` stops with `needs the lead`; verdict `pass` accepts;
otherwise the task stops with `the review says fix but names no must or
lead finding`. The round budget is the plan's `rounds`, including zero.

A fix message starts with `Fix round <n> of task <id> in flow <flow id>.`
It numbers only must findings, with location, problem, scenario and fix,
and asks the writer to fix each or explain it under `Disputed`. It repeats
the Git rule and closing report. Review rounds after the first include
that earlier numbered must list and the fix diff; they check those
findings and the fix only. A new must or lead must be caused by the fix.

After a writer or fix turn, a changed file declared by another task in the
same wave stops the task before review. Other changed files outside the
task's declarations are recorded in `outside_files` and included in the
review brief for the reviewer to judge.

### Lead stops

A failed writer or review, an unresolved review, a lead finding or a merge
conflict stops the task. Other tasks may continue. Claude reports each stop
to the owner with its findings and every available choice.

| Task state | Accepted answers |
|---|---|
| `stopped`, with a branch | `send`, `accept`, `review`, `worker`, `drop` |
| `stopped`, without a branch | `worker`, `drop` |
| `conflict` | `worker`, `drop` |
| `blocked` | `drop` |
| Other | None |

`--send` copies the correction to `answers/<n>.md` under the same lock
that allocates its number and appends its answer. Intentional commands in
the correction remain as written. The writer receives it, then gets a
fresh review and fix budget. After `git commit was refused` (exit 77), the
send retries the commit and, if refused again, delivers the refreshed hook
output ahead of the correction as `emb run --send` does. This send counts
as a fix round in the existing budget, even when all rounds are used; it
does not raise the limit. An unreadable correction exits 66; a blank
correction exits 64.

`--accept` records the task branch's tip at queue time, or the full commit
resolved from `--commit`. Invalid or ambiguous commits exit 64 before an
answer is appended. The runner checks the tip again before applying the
answer and preserves that approved commit through merging; a moved tip is
refused.

`--review` collects any running writer turn before a fresh review budget.
An optional reviewer applies to this task from then on. `--worker` closes
the old writer without force, retains its branch, and starts a new writer
from the flow tip. `--drop` closes the writer without force and retains its
branch. Dropped tasks leave their dependants blocked. Retained branches
are listed in the flow's `abandoned` field.

Answers are queued, then taken between waves. The runner checks each
answer again against the task's current state. A refused answer produces
a lead stop naming its number and reason. An answer refused at queue time
exits 75 and lists the allowed answers. Unknown flows or tasks exit 64;
completed flows refuse answers with 75. With no live runner or pending
startup, `--lead` starts a runner after queueing the answer. A launch
failure leaves the queued answer available for `--resume`.

### Cancellation

`--cancel FLOW [--reason TEXT]` cancels a flow that is `starting`, `running`,
`checking` or `stopped`. It stops the runner and waits for its lifetime lock
to be released. A running flow check ends through the runner's signal stop
path. Writer and reviewer turns keep running. Cancellation closes no task
run and deletes no branch or worktree.

Under `lifecycle.lock`, cancellation clears the startup reservation and
writes `cancel.json` with `at` (epoch seconds) and `reason` (text or null)
before signalling the runner. The runner reads that request when finishing
and records `status=cancelled`, `reason` and `ended_at`, then appends an
`end` event with the reason and exit 82. A runner that finds a pending
request at startup finishes without running a wave. If the runner releases
its lock without recording cancellation, the cancel command records it.

A `done`, `failed` or `cancelled` flow is refused with exit 75 and its record
is unchanged. A flow that ends before cancellation takes the lifecycle lock
is also refused. An unknown flow exits 65 for `--cancel`; other flow modes
return 64 for an unknown flow. Invalid flow id syntax returns 64 in every
mode. Cancellation waits until the runner releases its lifetime lock.

`--resume` restarts a cancelled flow from its recorded task states. Startup
clears the cancel request; the runner clears `reason` and `ended_at` when
it begins. `--lead` is refused with exit 75 on a cancelled flow, with
`next: emb flow --resume <id>`. Cancelled flows count as ended.

Cancellation output contains the flow id, `cancelled`, the reason, the flow
branch, each retained task branch and each open run's task, role and id.
Text prints one line per item. `--json` prints:

| Field | Value |
|---|---|
| `flow`, `status`, `reason` | Flow id, `cancelled`, and the supplied reason or null |
| `branch` | The flow branch |
| `task_branches` | Objects with `task` and `branch`; includes abandoned branches, with a null task when the record no longer identifies it |
| `open_runs` | Objects with `task`, `role` (`writer` or `reviewer`) and `run` |

Run ids come from the flow record's current writers and reviewers, previous
reviews, cleanup entries and pending writer replacement or drop effects. Each run's stored
record is read; `starting`, `running` and `waiting` count as open. Successful
cancellation exits 0 and prints `next: emb flow --resume <id>` on standard
error. A cancelled watch exits 82 with the same action. Status shows
`cancelled`; its separate `reason` field is null when no reason was supplied.
The text listing adds `: <reason>` after `cancelled` only when a reason is
present.

### Flow records

Flow folders share `$XDG_STATE_HOME/ember/runs/` with run folders. A flow id
is `flow-<yyyymmdd>-<hhmmss>-<6 hex digits>`, with a UTC timestamp. Its folder
is mode 700 and its record files are mode 600. Run listings skip flow ids;
flow listings skip run ids and report unreadable flow records separately.
Both listings sort oldest first.

| File | Holds |
|---|---|
| `flow.json` | The flow record, replaced atomically; written at creation, by the runner, or by cancellation after the runner releases its lock |
| `plan.json` | The plan's original source text |
| `cancel.json` | Pending cancellation time and reason; cleared when a runner is restarted |
| `lifecycle.lock` | Serializes startup, answers, cancellation and runner shutdown |
| `events.jsonl` | Numbered event objects, one per line |
| `answers.jsonl` | Numbered answer objects, one per line |
| `answers/<n>.md` | Private copy of the correction for answer `n` |
| `briefs/<task>-write-<n>.md`, `briefs/<task>-review-<n>.md`, `briefs/<task>-fix-<n>.md` | Writer briefs, review briefs and fix messages; artifact numbers increase across replacement writers and fresh round budgets |
| Check logs | Each check attempt's output in a distinct file named by its `checks` entry |
| `watch.json` | `{"seq": <last reported event>}` |
| `watch.lock` | The watch command's lock across event selection, output and cursor advancement |
| `cursor.lock` | Serializes cursor writes and prevents regression |
| `events.jsonl.incomplete-<timestamp>`, `answers.jsonl.incomplete-<timestamp>` | Preserved incomplete tails removed before another append |

The runner's lifetime lock is
`$XDG_RUNTIME_DIR/ember/locks/<flow id>.lock`. Record saving does not acquire
it; the runner serializes its writes. Creating an existing flow folder is
refused. `save` updates `updated_at` and atomically replaces `flow.json`.

| Flow field | Value |
|---|---|
| `id`, `plan` | Flow id and absolute source plan path |
| `dir`, `main` | Checkout top and main checkout |
| `base`, `tip` | Resolved base commit and current flow branch tip |
| `base_branch` | The local base branch at start, or null for a commit base or a record without this field |
| `branch`, `worktree` | `ember/flow/<id>` and `<main>/.ember/worktrees/<id>` |
| `writer`, `reviewer` | Stored worker objects: `engine`, `model`, `effort`, `source` |
| `rounds`, `parallel`, `check_timeout` | Plan limits |
| `check`, `trailers`, `read` | Plan argv, commit trailers and readable paths; lists |
| `stall_minutes` | Stall interval, default 10 |
| `claim` | Claim id or null; information for the flow |
| `status` | `starting`, `running`, `checking`, `stopped`, `failed`, `done` or `cancelled` |
| `reason` | Stop, failure or cancellation reason, or null |
| `wave` | Last wave started; initially 0 |
| `check_pending` | Whether a merge lacks a durably recorded passing check |
| `checks` | Entries with `wave`, `exit`, `log`, `at`; one per attempt |
| `answers_seen` | Complete answer lines already consumed; initially 0 |
| `halted` | Reason new runs are halted, or null |
| `abandoned` | Retained branches from dropped or replaced writers |
| `runner` | Last runner's `pid`, `started_at`, process start ticks (`start`) and `exiting` flag, or null; older records may omit the latter two fields |
| `started_at`, `updated_at`, `ended_at` | Epoch seconds; `ended_at` is null until an end |
| `tasks` | Task records in plan order |

| Task field | Value |
|---|---|
| `id`, `subject` | Plan values |
| `status` | `pending`, `writing`, `reviewing`, `fixing`, `accepted`, `merging`, `merged`, `stopped`, `conflict`, `blocked` or `dropped` |
| `reason` | Stop, conflict, blocking or drop reason, or null |
| `wave` | Last wave used, or null |
| `writer`, `reviewer` | Task-specific worker override, or null for the flow's worker |
| `writer_run`, `reviewer_run` | Current writer and review run ids, or null |
| `branch`, `worktree` | Writer branch and worktree, or null |
| `task_base`, `head` | Starting flow tip and latest reviewed task head, or null |
| `round` | Fix rounds in the current budget; initially 0 |
| `reviews` | Entries with `round`, `run`, `verdict`, `must`, `should`, `lead`, `at`; verdict is `pass`, `fix` or `unreadable` |
| `findings` | Latest findings and retained earlier `should` and `lead` findings |
| `changed_files`, `outside_files` | Last turn's changed paths and paths outside its declarations |
| `entry` | Next operation: `write`, `send`, `review` or null |
| `answer` | Answer number being applied, or null |
| `merge_tip`, `merge_commit` | Approved tip being merged and resulting merge commit, or null; containment may succeed without a merge commit |
| `cleanup` | Cleanup result objects from the merge |

Record loading checks required field types and status values. Additional
fields are retained for runner recovery metadata. Each task starts with
independent empty lists.

Events have `seq` starting at 1, `at` in epoch seconds, and `kind`:

| Kind | Fields |
|---|---|
| `flow` | `status`, `reason` |
| `task` | `task`, `status`, `reason` |
| `lead_stop` | `task` or null, `status`, `reason`, `choices` |
| `check` | `wave`, `exit`, `log`, `output` |
| `end` | `status`, `reason`, `exit` |

Creation writes event 1 with kind `flow` and status `starting`. Event reads
can start after a sequence number and skip unreadable complete lines.

An answer has `n`, `at`, `task`, `kind`, `file`, `commit`, `worker`. Kinds are
`send`, `accept`, `review`, `worker`, `drop`; optional values are null.
`worker` uses the worker option syntax. The answer append function takes
`flock` on `answers.jsonl`, allocates the number, copies a send source file
to `answers/<n>.md`, and appends the object with that path, all under the
same lock. Callers supply the source filename and do not allocate `n`.
A missing or empty correction is not appended.

Both logs use a single append write per complete line, followed by `fsync`.
Readers ignore incomplete last lines. Before appending, a writer preserves
any incomplete bytes in a private timestamped file and removes them from
the log while holding its lock. An unreadable complete answer line returns
`None` and still occupies a line number. Number allocation is greater than
the complete line count and every stored valid number. Answer numbers also
exceed every existing `answers/<n>.md` number, including copies left by an
interrupted append. Correction copies are created exclusively and never
replace an existing file. Cursor writes
cannot decrease `seq`; a missing cursor reads as 0, a malformed cursor is
an error.

### Output

Data goes to standard output; diagnostics and action lines go to standard
error. `--dry-run` text prints the resolved writer and reviewer with their
sources, the base commit, the check as shell words (or `none`), numbered
waves with each task's declared files, and `gap check: <command>`.
Warnings print as `WARNING: <text>`. JSON has `plan`, `dir`, `base`,
`writer`, `reviewer`, `check`, `waves` (arrays of task ids), `warnings` and
`gapcheck` (the shell command). Empty lists and zero finding counts remain
in JSON.

`--start` prints:

```text
flow <id>
branch ember/flow/<id>
worktree <path>
```

Its action line is `next: emb flow --watch <id> --json`. `--resume` prints
`flow <id>`. `--lead` prints `queued answer <n>: <kind> for <task>`, then
`flow <id>` when it starts a runner. Both end with the same watch action.

`--watch` polls files once a second. It returns on an unread lead stop or
the current attempt's terminal outcome. A missing runner in a nonterminal
flow gives `runner_lost` after the startup grace period. A stopped flow
without a runner returns immediately, including when its stop events were
already read. A completed flow returns its current terminal outcome even
when its end event was already read. Events before the latest startup or
`flow running` event remain history and do not end a resumed watch.
After a startup reservation, the stored status applies only when
`runner.started_at` is at or after the reservation timestamp. If the new
runner never writes its record and the reservation expires, watch reports
`runner_lost` with exit 72, even if the previous attempt failed or stopped.

Watchers share `watch.json`. A separate `watch.lock` serializes event
selection, reporting and cursor advancement; the cursor advances only
after output is flushed. An interrupted watch exits 74 without advancing
its cursor. Repeated reports of an idle stopped flow describe current task
states and choices without delivering the same event again.

The JSON watch report contains:

| Field | Value |
|---|---|
| `event` | `lead_stop`, `end` or `runner_lost` |
| `flow`, `status`, `reason` | Flow id and current state |
| `exit` | Null at a lead stop, 0 or 82 at the end, 72 when the runner is lost |
| `branch`, `worktree`, `tip`, `dir` | Flow branch, worktree, tip and source checkout |
| `tasks` | Task reports in plan order |
| `stops` | Unread lead-stop events with their sequence, task, reason and available choices |
| `abandoned` | Retained branches of dropped or replaced writers |
| `next` | The action line, also printed on standard error |

Each task report has `id`, `status`, `reason`, `round`, `writer_run`,
`branch`, `worktree`, `merge_commit`, `outside_files`, `cleanup` and
`choices` (the available `emb flow --lead` command forms). `findings`
counts `must`, `should` and `lead`; `finding_details` holds the finding
objects. Cleanup results retain the branch and reason when cleanup keeps
work; the task report includes its worktree.

Text reports print the flow state, one line per task with its reason,
finding objects, choices, retained cleanup details and stop events.
Retained abandoned branches print separately.

At a lead stop the action says to report every stop and choice to the
owner, then watch again if a runner is live; otherwise an answer starts
the runner. At a successful end it says to review the flow branch and run
`emb merge <flow branch> --into <your branch> --verdict keep --dir <dir>`.
An end with unmerged tasks first says to report them to the owner. A failed
pending check says to fix the flow branch with an `emb run` and merge,
then resume. Other failures name `--status`; runner loss names `--resume`.

`--status FLOW --json` prints `flow.json` plus `runner_live`. Text prints
every top-level field except `tasks`, then each task's state and reason.
`--status` alone lists flows oldest first as
`<id>  <status>  <n>/<m> merged  <dir>`, or as JSON objects containing `id`,
`status`, `reason`, `dir`, `branch`, `runner_live`, `merged` and `tasks`
(the task count). An unreadable flow is skipped with
`emb flow: skipped <error>` on standard error.

### Exit codes

| Code | Meaning | Action line |
|---|---|---|
| 0 | Dry run printed, flow started, answer queued, runner resumed, flow cancelled, lead stop reported, or every task merged | Watch reports give their next action |
| 1 | Unexpected error or unreadable settings database | `next: emb flow --status`; database errors name an owner action |
| 64 | Invalid option combination, plan, flow, task, worker, commit or claim | `next: emb flow --help`, a worker's owner action, the claim action or `--status` |
| 65 | The plan directory is not in a checkout, or `--cancel` names an unknown flow | Repeat `--dry-run` with `dir` inside a checkout, or `next: emb flow --status` |
| 66 | Unreadable plan or correction, or missing plan directory | `next: ls -l <path>` |
| 69 | Missing git, tmux, engine or driver, disconnected engine, or unusable runtime folder | `owner:` naming the fix |
| 70 | Flow branch or worktree creation failed | Prune worktrees, then repeat the start |
| 71 | Runner session failed to start | `next: emb flow --resume <id>` |
| 72 | Runner lost before the flow ended | `next: emb flow --resume <id>` |
| 74 | Interrupted watch; cursor unchanged | `next: emb flow --watch <id> --json` |
| 75 | Runner live or starting, answer not allowed, flow already done, cancellation pending, cancellation of an ended flow, or a lead answer on a cancelled flow | `--status`, `--watch`, or `--resume` for a cancelled lead answer |
| 82 | Flow failed, was cancelled, or ended with tasks unmerged | The watch report's next action |

Every nonzero exit prints an `emb flow:` diagnostic or watch report and
one `next:` or `owner:` line. Flow folders, queued answers and unmerged
work remain after a startup failure.

## `emb panel`

Asks members the same question in parallel read-only `emb run` runs.
Each run is closed after its answer is read. Ember writes `summary.md`
with the question, member outcomes and every available answer.
`--recover` collects unfinished turns without asking the question again.
`--retry` asks one failed or not-started member again. `--close` closes
open member runs without force.

### Options

| Option | Does |
|---|---|
| `--question-file FILE` | Start a panel with the UTF-8 question in `FILE`; it must contain a non-empty line |
| `--dir DIR` | A folder in the checkout members read; defaults to the current folder; the panel records the checkout top |
| `--member W` | A member as `<engine>:<model>[@<effort>]`; repeatable, replaces the saved member list |
| `--claim ID` | Resolve saved members with this claim's choices; member runs receive no claim |
| `--read PATH` | An additional path members may read; repeatable; each path must exist |
| `--stall MINUTES` | Positive minutes without a member's screen changing before a stall; default 10 |
| `--timeout MINUTES` | Positive minutes from the first start, recovery or retry until the panel stops waiting; default 30 |
| `--dry-run` | Validate inputs and members, print their resolved efforts and sources, and start nothing |
| `--status [PANEL]` | Print one panel record, or list panels oldest first |
| `--recover PANEL` | Collect unfinished members under the panel's lifetime lock |
| `--retry PANEL MEMBER` | Ask one failed or not-started member again, by its printed number |
| `--close PANEL` | Close every open current or retained member run without force |
| `--json` | Print JSON in every mode |

Exactly one mode is required: `--question-file`, `--status`, `--recover`,
`--retry` or `--close`.
`--dir`, `--member`, `--claim`, `--read` and `--dry-run` require
`--question-file`. `--stall` and `--timeout` apply to a start without
`--dry-run`, or to `--recover` or `--retry`. Minutes must be finite and greater than zero.
Invalid combinations exit 64 with `next: emb panel --help`.

Before a start, Ember reads the question, resolves the checkout, checks that
each `--read` path exists, reads choices and checks the claim, then selects
and validates members. A missing path fails this existence check; access
failures encountered later are reported by the member. Git location
variables are removed from the environment used by these checks and runs.
An invalid, gone or unknown claim exits 64 before any member starts.

Dry run stops after input and member validation. It checks neither tmux nor
runtime locking, record storage, sandbox readiness or member launch. A
start then requires tmux, acquires its new panel's lock, creates the record
and publishes its id. The lock remains held through execution and
finalization.

### Members

Panel members use repeatable `--member <engine>:<model>[@<effort>]`
options. Engines are `codex`, `opencode` and `claude`; models contain no
whitespace or `@`, and efforts contain lowercase letters. Model names
may contain slashes and colons. A panel has one to eight members. The
same `<engine>:<model>` twice is refused, even with different efforts.
Malformed, repeated or ninth members exit 64 with `next: emb panel --help`.

When any `--member` is given, those members are used in option order.
They do not require shortlist entries, and the saved `panel.members`
value is not used. Otherwise members come from `panel.members`, resolved
with `--claim` when supplied: request, claimed one-time choice, standing
setting, then the empty default. A saved row must be `set`. Each other
row is left out with its reason, as is a saved member that fails an
engine or effort check. Each exclusion is recorded in `left_out` and
reported on standard error as `emb panel: left out <value>: <reason>`.

Every explicit member is checked before any run starts: engine on (64),
connected (69), driver present (69), effort accepted (64). The first
failure stops selection. The connection uses the engine status cache,
or checks the engine when no usable cache exists. No member left exits
64; when every saved member was excluded, the diagnostic says
`no panel member to run: every saved member was left out`. Exclusions
are printed before the action line:
`owner: name the members with --member <engine>:<model>[@<effort>], or set Panel on the Menu screen`.

Effort comes from an explicit `@effort`, then a saved member's shortlist
entry, then the driver's default for the model. A null default leaves
the choice to the engine; only non-null efforts are validated. The
resolved effort and source (`option`, `request`, `one-time` or `setting`)
are recorded for each member. The single-worker reviewer row does not
supply a panel member's effort.

Claude members run through the Claude CLI. `single.writer_as` and
`single.reviewer_as` are not read; panels have no subagent members.

### Brief

Every panel member receives the same `brief.md`. Its first line is
`Panel <panel id>: <first non-empty question line, stripped>`. The run's
subject is that line, cut to 72 characters. A blank line separates it from
these rules:

```text
You are one of several models answering the same question on your own.
Someone else will compare the answers, so answer fully, in your own words.

Rules for this answer:
- Put your whole answer in your final message, in Markdown. Nothing you write to a file is read.
- Create, edit or delete no file, and change nothing in git.
- You may read the files in this checkout. Use the web only if your tools offer it.
- Name your sources: a file path, or a URL you opened.
- Say plainly where you are unsure or where the evidence is thin.
```

A blank line, `The question:`, another blank line and the question follow.
The brief ends with one newline. Members are not told the other members'
models.

### Running

Each member runs through its own Core instance in a daemon thread, with
its own output buffers. Threads start in member order, one second apart
(`START_SPACING`). Every run is read-only on the checkout top, with the
panel's `read` paths, no claim, no trailers and no terminal window. The
member's model and resolved effort are passed explicitly; single-run menu
rows do not supply the effort.

The coordinator observes stop requests and the deadline at intervals of at
most one second. `--stall` defaults to 10 minutes per member attempt. A
start that exits 124 is collected once with `Core.recover`; another 124
leaves the member `stalled` and its run open. No recovery sends another
question. `--timeout` defaults to 30 minutes from the first start. At the
deadline active jobs become `timed out`; member turns keep running.

SIGINT, SIGTERM and SIGHUP request a stop. Active members become
`interrupted`, pending members become `not started`, and the panel exits
74. A stop or deadline writes the record and summary, flushes captured
member diagnostics, then accepts no further panel-file or output writes
from member threads. Member runs are not interrupted or force-closed.

Member statuses:

| Status | Meaning |
|---|---|
| `pending` | The member thread has not begun |
| `starting` | The thread began; no run id has been saved |
| `running` | The run id has been saved |
| `answered` | Exit 0 with a nonblank reply |
| `failed` | Another exit, an unreadable run record or reply, a missing or blank successful reply, or an unexpected exception |
| `stalled` | The collection attempt exited 124 |
| `timed out` | The panel deadline ended the wait for this job |
| `interrupted` | A stop request or exit 74 ended the wait |
| `not started` | The panel stopped before the member began |

Every readable reply is retained and displayed, including replies from
failed members. A failed member's reason comes from its failed attempt;
recovery progress messages are excluded. An empty string from Core exits
80 with its no-reply diagnostic. A successful whitespace-only reply has
reason `empty answer`.

An answered or failed member's run is closed without `--force`. A refused
close sets `close_error` and emits a warning naming `emb run --close <run>`;
it does not change the member outcome. Closes admitted before the cutoff
may finish afterward. Such a member remains unfinished in the panel until
recovery reads its run's final state. No close is admitted after the cutoff.

Changed paths remain warnings: the member stays `answered`, its `changed`
list holds the paths, and the panel exits 83. Every member warning is
prefixed `WARNING: member <n> (<engine>:<model>): `. Core standard output
and standard error are appended to `members/<n>.log`; member action lines
stay in those logs. A persistence failure stops scheduling, preserves
existing artifacts and raises a panel error naming the panel; the command
reports exit 1 with the panel status action.

### Panel records

Panel folders share `$XDG_STATE_HOME/ember/runs/` with runs and flows. Their
ids are `panel-<yyyymmdd>-<hhmmss>-<6 hex digits>`, with a UTC timestamp.
Each panel folder and its `members/` folder are mode 700; files are mode
600. Creating an existing panel folder is refused.

| File | Holds |
|---|---|
| `panel.json` | The panel record, replaced atomically |
| `question.md` | The original question text |
| `brief.md` | The shared member brief |
| `summary.md` | The question, outcomes and full available replies, in member order |
| `members/<n>.log` | Member `n`'s captured Core output and diagnostics across attempts |

The caller holds an exclusive nonblocking `flock` on
`$XDG_RUNTIME_DIR/ember/locks/<panel id>.lock` before creating the panel
and throughout execution and finalization. A second holder is refused.
`live` probes this lock on a new open file description; the kernel drops
the lock when the process ends. Within the process, one mutex serializes
record, summary, log and warning writes. `save` validates the record,
updates `updated_at` and atomically replaces `panel.json`.

| Panel field | Value |
|---|---|
| `schema_version` | Integer 1 |
| `id` | Panel id |
| `status` | `running`, `done` or `incomplete`; retry requires every member to be answered for `done`, start and recovery require no unfinished members |
| `dir` | Absolute checkout top |
| `question_file` | Absolute source question path |
| `read` | Absolute additional read paths |
| `claim` | Claim used to select members, or null; not passed to runs |
| `stall_minutes`, `timeout_minutes` | Limits of the latest start, recovery or retry |
| `started_at`, `updated_at`, `ended_at` | Epoch seconds; `ended_at` is null until finalization |
| `exit` | Null while running, then 0, 83 or 74 |
| `members` | Member records in start order |
| `left_out` | Saved members excluded from selection, each with `value` and `reason` |

| Member field | Value |
|---|---|
| `n` | One-based position |
| `engine`, `model`, `effort`, `source` | Selected member; effort may be null for the engine default |
| `status` | One of the member statuses above |
| `run` | Run id, saved when Core prints it, or null |
| `exit`, `reason` | Last attempt's exit and failure reason, or null |
| `reply`, `bytes` | Reply path or null, and its UTF-8 byte count, initially 0 |
| `changed` | Changed paths without duplicates, initially empty |
| `seconds` | Elapsed time across member jobs for the current run, including collection and closing; reset on retry, null before a job begins |
| `closed`, `close_error` | Whether the run was closed, and any close refusal reason; panel close tracks remaining open current and retained runs |
| `attempts` | Attempts with `kind` (`start`, `stall`, `recover`, `retry`), epoch `at` and `exit`; optional `run` holds a retained run id or null |

Loading rejects missing or unknown fields, wrong types and unknown status
values. Panel listing returns panels oldest first by `started_at`, then
id, with unreadable panel folders reported separately. `emb run --status`
and Overview skip panel folders and list member runs as ordinary runs.
`emb flow --status` lists flows only.

Ember writes the summary; it does not ask another model to summarize. The
header contains the panel id, checkout, local ISO 8601 start time to the
second, member count, answered count and panel status. The question comes
next. Each member section contains its number, engine, model and effort
(`default` for null), then status, exit, elapsed seconds rounded to the
nearest integer, run id and byte count. Null exit, elapsed time and run id
are shown as `-`. Changed paths and a reason appear when present, followed
by the whole reply or `(no answer)`. An unreadable reply is shown as
`(the reply file cannot be read: <error>)`. A final `Left out` section
lists exclusions when present. The summary is replaced atomically after
every completed member, at finalization and after recovery.

### Recovery

Recovery requires the panel's lifetime lock. It collects members in
`starting`, `running`, `stalled`, `timed out` or `interrupted`, in parallel
without start spacing. Pending members left by a crash and unfinished
members without a saved run id become `failed` with reason
`the panel ended before this member's run started`; recovery starts no new
run. Failed and not-started members are not asked again. Runs retained
only in retry attempts are not recovered.

An already closed run is read without calling `Core.recover`. Its stored
terminal exit classifies the outcome: a successful nonblank reply is
answered; a stored failure or missing answer remains a failure. Other runs
receive one `Core.recover` call. A previously collected turn with a readable,
nonblank reply is answered even when its stored exit still describes an
earlier wait. When recovery collects nothing and there is no such answer,
the run's stored nonzero exit and reason are retained, including a launch
failure before any turn started. Readable replies and change evidence are
retained from the member record, run
record and original diagnostics. Missing new warnings do not erase earlier
changed paths.

Each attempt is saved with `exit: null` before it runs and filled afterward.
A start's exit 124 is saved before the `stall` attempt begins. Recovery
updates the stall and deadline limits and adds a `recover` attempt. An
active recovery job becomes `interrupted` or `timed out` when the current
stop condition ends its wait, regardless of its earlier status.

When there is nothing to collect, recovery still saves no-run failures,
reconciles the panel's terminal status and exit, and rebuilds the summary
before reporting that nothing remains to recover (75).

### Retry

`emb panel --retry PANEL MEMBER` holds the panel's lifetime lock for the
whole operation. `MEMBER` is the one-based number printed on member lines.
A failed, stalled, timed-out, interrupted or not-started member can be
retried. Answered, pending, starting and running members are refused (75).
An unknown panel or member number exits 65.

Before launching, retry closes the member's old run without force, even
when its record is failed or closed. A refused close exits 75 and names
that run; no new run starts. Each close result updates `closed` and
`close_error` and saves the panel. A successful close is followed by a
`retry` attempt with `at`, `exit` initially null and `run` (the old id, or
null when no run started). Its exit is filled when the new start returns.
Existing attempts remain. Records without the optional attempt `run`
field still load.

Retry starts one read-only run using the saved brief, checkout, engine,
model, effort and read paths. It waits as a panel start does, including
stall recovery and the panel deadline. `--stall` and `--timeout` have the
same defaults as start and recovery. While it runs, the panel is `running`
with `ended_at` and `exit` null. The member's previous outcome, reply,
changed paths and elapsed time are replaced; other members are not retried.

Afterward, retry sets `ended_at` to the current time and rewrites
`summary.md`. Its panel is `done` only when every member answered,
otherwise `incomplete`. Start and recovery retain their rule: `done` when
no member is unfinished, even when a member failed or never started.
Retry returns the recomputed panel exit: 74 if interrupted, 0 if every
member answered without changed paths, otherwise 83.

### Close

`emb panel --close PANEL` holds the panel's lifetime lock for the whole
operation. It inspects the current run and ids retained in `retry`
attempts. A run is open when its record status is neither `closed` nor
`failed`; the saved member `closed` flag does not determine this.
Each open run is closed as `emb run --close RUN`, without force. A refusal
does not stop attempts to close other runs. Duplicate ids within a member
are closed once. After each close, the member's `closed` flag reflects
whether any current or retained run remains open, `close_error` holds
refusal reasons or null, and the panel is saved. A missing or unreadable
run record is reported as a refusal. Member outcomes and the panel's
status and result exit are unchanged.

Text output has one line for each open run, with the member number, run
id and `closed` or the refusal reason:

```text
member 1 · run codex-20261102-101500-a1b2c3 · closed
```

JSON contains `panel`, the close command's `exit`, `members` and `next`.
Each `members` entry has `n`, `run`, `closed` and `close_error`. A member
with multiple open runs has multiple entries. Closed and failed runs are
omitted. No open runs prints no result lines, or an empty `members` list.
The command exits 0 when every open run closed or none was open, and 75
when any close was refused. An unknown panel exits 65. The next action is
`next: emb panel --status PANEL`, also printed on standard error.

Retry and close refuse a held panel lock with 75 and
`next: emb panel --status PANEL`.

### Output

Data goes to standard output. Diagnostics go to standard error as
`emb panel: <message>`. Saved members left out are named there before a
start or dry-run result. Each failed command prints one `next:` or `owner:`
line. Member diagnostics stay in their logs except the warnings described
under Running.

A start, recovery or retry in text mode first prints `panel <id>` and
flushes it. After waiting, it prints one line per member, in member order:

```text
member <n> <engine>:<model> · <effort or default> · <status> · <seconds>s · exit <exit or -> · <bytes> bytes · run <run or ->
```

Seconds are rounded to the nearest integer. Unknown elapsed time is `-`
without a seconds suffix. Exit zero is printed as `0`. These lines are
followed by `left out <value>: <reason>` for each excluded member and
`summary <absolute path to summary.md>`.

With `--json`, a start, recovery or retry prints one object, without an id
announcement before it:

```json
{
  "panel": "panel-20261102-101500-a1b2c3",
  "status": "done",
  "exit": 0,
  "dir": "/abs/checkout",
  "folder": "/abs/state/ember/runs/panel-20261102-101500-a1b2c3",
  "summary": "/abs/state/ember/runs/panel-20261102-101500-a1b2c3/summary.md",
  "members": [],
  "left_out": [],
  "next": "next: read <summary>, then compare the answers"
}
```

`members` contains each panel record member dictionary except `attempts`;
the empty list above stands for those dictionaries. `left_out` contains
`{"value", "reason"}` entries. `next` contains the same complete action
line printed on standard error.

A text dry run prints `checkout <dir>`, then
`member <n> <engine>:<model> · <effort or default> · <source>` per member,
then the left-out lines. Its JSON object has `dir`, `members` and
`left_out`. Each member has `n`, `engine`, `model`, `effort` and `source`.
A dry run prints no next action.

`--status PANEL` prints each top-level record field except `members` and
`left_out` as `<field>: <value>`, adds `live: true` or `live: false`, then
prints the member lines above. Its JSON object is the complete panel
record plus `live`, which reports whether the lifetime lock is held.

`--status` lists readable panels oldest first, by start time then id:

```text
<id>  <status>  <answered>/<n> answered  <dir>  <subject>
```

The subject is `Panel <id>: <first non-empty question line>`, cut to 60
characters. If the question file cannot be read, the listing names that
file on standard error and uses `(question unavailable)` as the question.
An unreadable panel record is skipped with `emb panel: skipped <error>`.
No panels prints no text, or `[]` with `--json`. The JSON list contains
objects with `id`, `status`, `live`, `dir`, `answered`, `members` (the count),
`exit` and `started_at`. Status prints no next action on success.

The result's next action is printed on standard error, including exit zero:

| Case | Line |
|---|---|
| Exit 0 | `next: read <summary>, then compare the answers` |
| Exit 83, no unfinished member | `next: read <summary>; report each member that is not answered or changed paths to the owner` |
| Exit 83 or 74, an unfinished member has a run id | `next: emb panel --recover <id> (collects <k> unfinished members), or read <summary> now and close each open run with emb run --close <run> --force` |
| Exit 74 without a known unfinished run, or exit 83 with unfinished members but no run ids | `next: emb panel --status <id> and emb run --status; panel recovery cannot collect a member without a saved run id` |

`k` counts unfinished members with saved run ids. A persistence failure
exits 1 naming the panel id and `next: emb panel --status <id>`; existing
artifacts remain available.

### Exit codes

| Code | Meaning | Line |
|---|---|---|
| 0 | Every member answered and changed nothing, a dry run or status printed, or close left no open runs | The result's next action; none for dry run or status |
| 1 | Unexpected error, broken panel record, persistence failure, or unreadable settings database | `next: emb panel --status [<id>]`; for the database, `owner: repair or remove the settings database the message names` |
| 64 | Usage error, empty question, invalid or repeated or ninth member, no member to run, explicit engine off or effort refused, invalid or gone or unknown claim, or unknown panel in status or recovery | `next: emb panel --help`, the member or engine owner action, the claim action, or `next: emb panel --status` |
| 65 | `--dir` is outside a git checkout, or retry/close names an unknown panel or retry names an unknown member | `next: emb panel --question-file <file> --dir <a folder in the checkout>` or `next: emb panel --status [<id>]` |
| 66 | Unreadable question, missing or non-directory `--dir`, or nonexistent `--read` path | `next: ls -l <path>` |
| 69 | Explicit engine disconnected or without a driver, git or tmux missing, unusable runtime folder, or panel creation fails with an operating-system error | `owner:` naming the fix; runtime and creation errors name `XDG_RUNTIME_DIR` |
| 74 | Ctrl+C, SIGTERM or SIGHUP while members ran; member turns keep running | The recovery action, or panel and run status when no run id is known |
| 75 | Recovery, retry or close while the panel lock is held; no unfinished member to collect; member cannot be retried; or a close was refused | `next: emb panel --status <id>` |
| 83 | A member did not answer or changed paths | The result's next action above |

## `emb ground`

Prints, in one call, what Claude reads before choosing a worker: the machine,
the Claude Code surface, Claude Code's `worktree.baseRef` setting and the
state of one folder. It changes nothing.

| Option | Does |
|---|---|
| `--dir DIR` | The folder to read; default the current folder |
| `--json` | Print one JSON object, indented by two spaces |

Without `--json` it prints one `<item>: <value>` line per item on standard
output, in the order of the table below; a value that cannot be read prints
as `null (<reason>)`.

| Item | Value |
|---|---|
| `machine` | The host name |
| `surface` | `entrypoint`: the value of `CLAUDE_CODE_ENTRYPOINT`, or null when it is unset or empty; `desktop`: true when it is `claude-desktop` |
| `worktree_base` | `value`: Claude Code's `worktree.baseRef` setting; `file`: the settings file it came from |
| `directory.path` | The folder, as an absolute path |
| `directory.git` | Whether the folder is in a git checkout |
| `directory.top` | The top of the checkout |
| `directory.branch` | `name`: the checked-out branch, null on a detached `HEAD`; `commit`: the full commit id of `HEAD`, null before the first commit |
| `directory.dirty` | `value`: whether `git status --porcelain` lists any path; `paths`: how many paths it lists. Ignored files are not counted, nor are the checkout's other worktrees inside its top, as `git worktree list` lists them, so an untracked folder such as `.claude/worktrees` that holds only worktrees is not counted |
| `directory.remote` | `name`: `origin`, or else the first remote `git remote` lists; `url`: its URL; `kind`: `github` when the URL's host is `github.com`, otherwise `other` |
| `directory.files` | Whether `CLAUDE.md` and `AGENTS.md` are files at the top of the checkout |

A value that cannot be read is null, and in JSON a sibling key
`<field>_reason` holds the reason: for example `"remote": null` with
`"remote_reason": "the repository has no remote"`. Outside a git checkout
`git` is false and `top`, `branch`, `dirty`, `remote` and `files` are null with
the reason `not a git checkout`. When git cannot run, `git` is null as well.

Git runs with `LC_ALL=C` and without the variables that point it at another
repository (`GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`, `GIT_COMMON_DIR`,
`GIT_OBJECT_DIRECTORY`, `GIT_NAMESPACE` and `GIT_PREFIX`). It runs with
`GIT_OPTIONAL_LOCKS=0`, so `git status` takes no `index.lock` and does not
rewrite the index.

`worktree.baseRef` is read from the user settings file, then, in a
checkout, from the top's `.claude/settings.json` and
`.claude/settings.local.json`; a later file wins. A missing file, or a file
without the key, is skipped. When no file sets it, or a file cannot be read,
is not a JSON object, has a `worktree` that is not an object or a `baseRef`
that is not a non-empty string, `worktree_base` is null with the reason. The
user settings file is `$CLAUDE_CONFIG_DIR/settings.json` when
`CLAUDE_CONFIG_DIR` is set and not empty, otherwise `~/.claude/settings.json`.
Managed settings are not read.

### Exit codes

| Code | Meaning | Line |
|---|---|---|
| 0 | Printed, whatever could not be read | |
| 64 | Usage error | |
| 66 | `--dir` does not exist or is not a folder, or, without `--dir`, the current folder cannot be read | `next: emb ground --dir <an existing folder>` |

## `emb merge`

```
emb merge BRANCH --into TARGET --verdict keep [--dir DIR] [--json]
```

Merges a reviewed branch into a local branch, then cleans it up as
`emb cleanup --merged-into TARGET --branch BRANCH` does. `DIR` is a folder in
the git checkout; the default is the current folder.

- Without `--verdict keep` it changes nothing and exits 64.
- `BRANCH` and `TARGET` must be existing local branches (65), and different
  (64).
- The merge happens in the worktree that has `TARGET` checked out, from
  `git worktree list`. It exits 75 when no worktree has it checked out.
- A branch already contained in `TARGET` (see `emb cleanup`) is not merged
  again; the first line is `already contained: BRANCH at <commit> in TARGET at
  <commit>`.
- Otherwise the target's worktree must have no unfinished merge, rebase,
  cherry-pick or revert (`MERGE_HEAD`, `REBASE_HEAD`, `rebase-merge`,
  `rebase-apply`, `CHERRY_PICK_HEAD` or `REVERT_HEAD` in its git folder) (75),
  and its tree must have no changes to tracked files and no untracked files
  that are not ignored (75); other worktrees of the repository inside it are
  not counted.
- Ember then runs `git merge --no-ff --no-edit -m <message> refs/heads/BRANCH`
  there, so a tag named `BRANCH` is never merged instead. `<message>` is git's
  default message for merging `BRANCH` by its short name, from
  `git fmt-merge-msg --no-log`: `Merge branch 'BRANCH' into TARGET`, without
  ` into TARGET` for a target that git's `merge.suppressDest` setting covers
  (`main` and `master` when it is not set). It prints `merged BRANCH at
  <commit> into TARGET: <merge commit>`.
- On a conflict it runs `git merge --abort`, which undoes only that merge,
  leaves `TARGET` as it was and exits 75. Any other merge failure exits 70.
- It never pushes.

The cleanup's lines follow the first line. A branch that is not an Ember run
branch is kept with `not an Ember run branch; next: git branch -d BRANCH`.

A flow branch `ember/flow/<flow id>` follows the run-branch cleanup path
without a run record, with its worktree at `.ember/worktrees/<flow id>` in
the main checkout. Cleanup keeps it while the flow's runner is live, with
`the flow <id> is running` and `next: emb flow --status <id>`. A task branch
merged into a flow branch is merged in the flow worktree and cleaned up by
the same rules.

`--json` prints one object:

| Field | Value |
|---|---|
| `branch` | `BRANCH` |
| `target` | `TARGET` |
| `merge_commit` | The merge commit; null when the branch was already contained |
| `contained_before` | Whether the branch was already contained |
| `cleanup` | The cleanup's list, as `emb cleanup --json` prints it |

## `emb prune`

```
emb prune --older-than DAYS [--dry-run] [--json]
```

Removes ended run, flow and panel folders under `runs_dir()`, including
logs inside those folders. `--older-than` is required and accepts a whole
number of days, zero or greater. Age is measured from `ended_at`, or
`updated_at` when there is no end time. A record qualifies at an age of at
least `DAYS × 86400` seconds; zero includes every ended record.

Ended runs are `closed` or `failed`. Ended flows are `done`, `failed` or
`cancelled`. Panels must be `done` or `incomplete`. A flow or panel is kept
when any current or former task or member run is open (`starting`,
`running` or `waiting`), busy or unreadable. The parent is reported as
skipped with the run id and reason. Missing child records do not count as
open. This guard includes cancelled flows with running writers or reviewers.

A flow or panel includes its ended task or member run records, regardless
of those runs' ages. Flow run ids are collected from the record and every
event with a `run` field, including check, fix and previous review runs.
Panel retry history is included. A run named by any parent record is
removed only together with all its parents, and only when those parents
qualify. For a parent that does not load, prune scans the files inside its
folder for run ids, including malformed records, events and logs. Those
runs are kept with the broken parent; unrelated runs are still pruned.
Symlinks are not followed when scanning these files.

Before removal, prune takes each record's lock without waiting and reloads
it. Flows use both `lifecycle.lock` and the lifetime lock; panels use their
panel lock; runs use their record lock. Busy records and records that no
longer qualify are skipped. Parent and child locks stay held through child
removal, including child records shared with a parent that is being kept.
Unreadable records are kept and reported on standard error. Folder names
outside the timestamped record id format are ignored. Record-folder
symlinks are refused. Files named by a record outside its folder are kept.

`--dry-run` lists the records that would be removed and removes nothing.
Text output has `removed <kind> <id>` lines (`would remove` for a dry run),
`skipped <id>: <reason>` lines, and kept branch and worktree lines with
removal commands. Existing worktrees are listed next to their branch.
Prune deletes no branch, worktree or tmux session. It lists no removal
command for a worktree used by an open run.

A branch is unmerged when it is not an ancestor of its target. Task
branches are checked against their flow branch. Run and flow records save
`base_branch` at creation and use it even if the source checkout switches
branches. If the target branch has been deleted, prune checks against the
owning flow's `base_branch`. If that branch is also absent, or the record
has no base branch, the branch is listed as unmerged. Missing target refs
are not errors and do not change the exit code. Saved commits are never
used as `--merged-into` targets. This check does not treat a squash merge
as ancestry.

Unmerged run, task and flow branches list
`git -C <dir> branch -D <branch>`. A run branch (`ember/<engine>/<id>`)
contained in an existing target branch can list
`emb cleanup --merged-into <target> --branch <branch> --dir <dir>`.
Records with a null or missing `base_branch` use the direct branch-deletion
form. Existing worktrees list `git -C <dir> worktree remove <path>`.
When both operations are listed, worktree removal precedes the branch
command with `&&`. Paths and branch names are shell-quoted. An absent
branch with an existing worktree lists just the worktree removal command.

`--json` prints an object with three arrays:

| Field | Entries |
|---|---|
| `removed` | `{id, kind}`, where kind is `run`, `flow` or `panel`; dry runs list candidates here |
| `skipped` | `{id, reason}` |
| `branches` | `{branch, command}`, including worktree removal commands when a worktree exists |

Read, lock, repository-query and removal failures are reported as
`emb prune: <message>` on standard error. Processing continues after each
failure. Exit 0 means processing completed without failures, including an
empty runs folder, no matches or busy skips. Exit 1 means one or more
operations failed; the output includes completed removals. Missing,
negative or non-whole `--older-than` values exit 64.

`ember.prune.select(older_than, record_ids=None, *, env=None, now=None)`
selects records without changing files.
`ember.prune.remove(older_than, record_ids=None, *, dry_run=False, env=None, now=None)`
selects and removes them with the lock checks above. Explicit ids ignore
age but still require ended records and qualifying selected parents.
Missing, unreadable and unfinished explicit ids are returned in `skipped`
with reasons. Supplying an empty id list selects nothing.

No pruning happens automatically. Settings and the app's state file are
not pruned.

## `emb cleanup`

```
emb cleanup --merged-into TARGET [--branch BRANCH] [--dir DIR] [--dry-run] [--json]
```

Removes the Ember run branches `TARGET` contains, with their run's worktree
and session. `DIR` is a folder in the git checkout; the default is the current
folder.

A run branch is `ember/<engine>/<run id>`; its worktree is
`.ember/worktrees/<run id>` in the repository's main checkout (the first entry
of `git worktree list`), also when `DIR` is in a linked worktree. No other
branch is ever removed, and a read-only run, which has no branch, is never
touched. Without `--branch` every run branch is handled; with it, only
`BRANCH`.

A flow branch `ember/flow/<flow id>` is a run branch with no run record;
its worktree is `.ember/worktrees/<flow id>` in the main checkout. Before
reading a run record, cleanup checks the flow's runner lock and keeps the
branch when it is held or cannot be checked. With no live runner, the
ordinary containment and worktree checks apply; `--dry-run` reports
`would remove` when they pass. Flow records are kept.

A branch is contained in `TARGET` when it is an ancestor of `TARGET`, or when
merging it into `TARGET` gives no conflict and `TARGET`'s own tree, as after a
squash merge. The check changes no index, worktree or ref; with `--dry-run`
the objects of the trial merge go to a temporary folder.

For each contained run branch, in order:

1. The run record must belong to the branch: same engine, same branch, its
   worktree is the branch's worktree in this repository, and its checkout is
   in this repository (when that checkout still exists).
2. The branch must not be checked out in a worktree other than its run's, and
   the run's worktree, when it exists, must have the branch checked out, be
   unlocked, and have no changes to tracked files and no untracked files that
   are not ignored. Ignored files do not count.
3. A run that is not closed is closed through `emb run --close` without
   `--force`; the close's messages go to standard error.
4. Both tips are read again and containment is checked again.
5. The run's worktree is removed with `git worktree remove` (without
   `--force`), then `git worktree prune` runs, which also clears a worktree
   whose folder was deleted by hand.
6. The branch is deleted at the commit checked in step 4
   (`git update-ref -d refs/heads/BRANCH <commit>`).

A branch with no run record follows the same steps without a session to
close. Run records are kept. `--dry-run` reports what each branch would get
and changes nothing; it does not ask the run's session whether it would close.
It reports a branch as kept, with the reason and line a close would give,
when the run record shows a turn that is sent (`turn <n> of <run> is
running`) or, on a run that writes, collected (`turn <n> of <run> is collected
but not committed`).

Everything else is kept, with a reason and what resolves it:

| Kept | Reason | Then |
|---|---|---|
| `TARGET` itself | `the merge target` | `next: emb cleanup --merged-into <the branch it was merged into>` |
| A branch that is not a run branch (with `--branch`) | `not an Ember run branch` | `next: git branch -d BRANCH` |
| A branch not contained | `not contained in TARGET` | `next: emb merge BRANCH --into TARGET --verdict keep (after a Keep verdict)` |
| A flow branch whose runner is live | `the flow <id> is running` | `next: emb flow --status <id>` |
| A flow branch whose runner lock cannot be checked | The lock error | `next: emb flow --status <id>` |
| A run record that is not this branch's | `the run record <run> is not this branch's: ...` | `next: emb run --status <run>` |
| A run record that cannot be read | `cannot read the run record: ...` | `owner: repair or remove <the run.json file>` |
| A branch checked out in another worktree | `checked out in <path>` | `next: git -C <path> switch --detach, then <the cleanup>` |
| A run worktree with another branch checked out | `the run's worktree <path> has <branch> checked out` | `next: git -C <path> status` |
| A locked run worktree | `the worktree <path> is locked` | `next: git worktree unlock <path>, then <the cleanup>` |
| A run worktree with changes | `the worktree <path> has changes` | `next: git -C <path> status --short` |
| A run whose close is refused | The close's message: a running or collected turn, a busy session, another command on the run, or messages Ember did not send | The close's `next:` line |
| A run whose close fails otherwise | The close's message | The close's `owner:` line, or `owner: repair what the message names (emb run --status <run>), then <the cleanup>` |
| A branch or target that moved | `<branch> moved` | `next: <the cleanup>` |

Each act and keep is one line on standard output:
`removed BRANCH: <what was done>`, `would remove BRANCH: <what would be done>`,
or `kept BRANCH: <reason>; next: <command>` (`; owner: <step>` for something
only the owner does). With nothing to handle it prints nothing on standard
output and a message on standard error.

`--json` prints a list of objects:

| Field | Value |
|---|---|
| `branch` | The branch |
| `run` | The run id in the branch name, or null |
| `action` | `removed`, `kept` or `would remove` |
| `reason` | What was done, or why it was kept |
| `next` | For a keep, the line that resolves it, `next: <command>` or `owner: <step>`, the same text as the printed line ends with; null otherwise |

A git command that fails (worktree removal, `git worktree prune`, branch
deletion, or a git command that reads the repository) stops the cleanup at
once with exit 70. Everything done before it is already printed, or with
`--json` is in the list. The branch it failed on follows as kept; its reason
lists the steps already done to that branch (for example `closed <run>`,
`removed worktree <path>`) and the failed step with git's message, and its
`next` is the line printed on standard error. With `emb merge`, a merge
already made stays.

A git command that changes the repository (the merge, `git merge --abort`,
worktree removal, `git worktree prune`, branch deletion) runs without a time
limit. Every other git command may take 120 seconds; one that takes
longer fails.

### Exit codes of `emb merge` and `emb cleanup`

Messages go to standard error as `emb merge: <message>` or
`emb cleanup: <message>`; each non-zero exit then prints one `next:` or
`owner:` line.

| Code | Meaning |
|---|---|
| 0 | Done, whatever was kept |
| 64 | Usage error, or no Keep verdict |
| 65 | The folder is not in a git checkout, or a named branch or target does not exist |
| 70 | A git command failed: the merge, making the merge message, finding the target's git folder, listing worktrees, `git status`, worktree removal, prune or branch deletion |
| 75 | Refused: the target is checked out nowhere, its worktree has an unfinished merge, rebase, cherry-pick or revert, or its tree is not clean, or a merge conflict was aborted |

## `emb deps`

```
emb deps [--refresh] [--json]
```

Shows each component Ember depends on: its installed version, the newest
version its source offers, its status and the command that updates or
installs it. It reads installed versions every time and newest versions when
due. It installs and updates nothing.

| Option | Does |
|---|---|
| `--refresh` | Read every newest version now, even when the cached one is recent |
| `--json` | Print JSON |

Exit codes: 0 whatever the statuses; 64 for a usage error; 70 when the
packaged component list cannot be read or is not valid, with the reason on
standard error as `emb deps: <reason>`.

### Components

The list is `ember/data/components.toml` in the package:

| id | Label | Group | Installed version | Newest version | Update | Install |
|---|---|---|---|---|---|---|
| `codex` | Codex | `apps` | `codex --version`, `codex-cli (\S+)` | `version` of `https://registry.npmjs.org/@openai/codex/latest` | `codex update` | `npm install -g @openai/codex` |
| `opencode` | OpenCode | `apps` | `opencode --version`, `^(\S+)$` | `version` of `https://registry.npmjs.org/opencode-ai/latest` | `opencode upgrade` | `curl -fsSL https://opencode.ai/install \| bash` |
| `claude` | Claude Code | `apps` | `claude --version`, `^(\S+) \(Claude Code\)$` | the text of `https://downloads.claude.ai/claude-code-releases/latest` | `claude update` | `curl -fsSL https://claude.ai/install.sh \| bash` |
| `claude-desktop` | Claude Desktop | `apps` | apt `claude-desktop` | apt | `sudo apt install --only-upgrade claude-desktop` | none; note "Not installed." |
| `tmux` | tmux | `system` | apt `tmux` | apt | `sudo apt install --only-upgrade tmux` | `sudo apt install tmux` |
| `bubblewrap` | bubblewrap | `system` | apt `bubblewrap` | apt | `sudo apt install --only-upgrade bubblewrap` | `sudo apt install bubblewrap` |
| `git` | git | `system` | apt `git` | apt | `sudo apt install --only-upgrade git` | `sudo apt install git` |
| `python3` | Python | `system` | apt `python3` | apt | `sudo apt install --only-upgrade python3` | `sudo apt install python3` |
| `xdg-terminal-exec` | xdg-terminal-exec | `system` | apt `xdg-terminal-exec` | apt | `sudo apt install --only-upgrade xdg-terminal-exec` | `sudo apt install xdg-terminal-exec` |
| `pyside6` | PySide6 | `libraries` | Python package `PySide6-Essentials` | PyPI `PySide6-Essentials` | none; note "Updates with the next Ember release." | none; note "Reinstall Ember." |

The groups are `apps` (engines and apps), `system` (system tools) and
`libraries` (Ember's libraries).

Each entry is a `[[components]]` table with these fields; any other field,
a missing required field, an unknown kind or a repeated `id` makes the list
not valid:

| Field | Holds |
|---|---|
| `id` | Unique, `[a-z0-9-]+` |
| `label` | The shown name |
| `group` | `apps`, `system` or `libraries` |
| `installed` | `kind = "apt"` with `package`; `kind = "command"` with `pattern`, a regular expression whose group 1 is the version, and either `engine` (the engine's CLI run with `--version`) or `argv`; or `kind = "python"` with `package` |
| `newest` | `kind = "apt"` with `package` (only with an apt `installed`); `kind = "json"` with `url` and `field`; `kind = "text"` with `url`; or `kind = "pypi"` with `package` |
| `update` | Optional: the command shown while an update is available |
| `install` | Optional: the command shown while the component is missing |
| `update_note` | Optional: shown while an update is available and there is no `update` |
| `install_note` | Optional: shown while the component is missing and there is no `install` |

### Reads

| Kind | Read |
|---|---|
| `apt` installed | `dpkg-query -W -f='${db:Status-Status}\t${Version}' <package>`; exit 1, or a status other than `installed`, means missing |
| `command` installed | The engine's CLI as `emb engines` finds it, or `argv` with its program found on `PATH`, matched against `pattern`; a program that is not found means missing. `claude` runs without the variables whose names start with `CLAUDE_CODE_`, `CLAUDECODE` or `ANTHROPIC_` |
| `python` installed | The package's version in the Python that runs Ember; a package that is not installed means missing |
| `apt` newest | The `Candidate:` line of `apt-cache policy <package>`; `(none)` or no such line is a check failure |
| `json` newest | The string at `field` in the JSON object at `url` |
| `text` newest | The body at `url`, stripped; it must be one word |
| `pypi` newest | `info.version` of `https://pypi.org/pypi/<package>/json` |

Every command runs with `LC_ALL=C`, standard input from `/dev/null` and a
10-second deadline, in its own process group; at the deadline the whole group
is killed. Every network read has a 10-second timeout.

Versions read with apt are compared with `dpkg --compare-versions`, with the
epoch and revision. Every other version is compared as dotted numbers: a
leading `v` is removed and trailing `.0` parts are ignored, so `v1.2.0` equals
`1.2`. A version that is not dotted numbers is a check failure; it is never
compared as text.

### Status

`status` is the first that applies:

| Status | When |
|---|---|
| `missing` | The installed read found nothing; the newest version is not read |
| `check failed` | A read failed, a needed version was neither read nor cached, or the versions cannot be compared; `error` names each read that failed (`installed version`, `newest version` or `comparison`) and why. The installed version is kept when it was read |
| `update available` | The installed version is older than the newest |
| `current` | Otherwise |

`command` is the update command while an update is available and the install
command while missing, with `command_kind` `update` or `install`; otherwise
both are null. `note` is `update_note` or `install_note` for that status when
there is no command for it; otherwise null.

### Cache

The last reads are cached in `$XDG_CACHE_HOME/ember/dependencies.json`
(default `~/.cache/ember/dependencies.json`), mode 600 in a mode 700 folder,
replaced whole on each write. Per component it holds the last installed read
with its `read_at` and the last newest version read with its `read_at`, in
seconds since the epoch. A newest version is read again when its cached read
is missing or six hours old or more; `--refresh` reads it now. A newest read
that fails is reported and not cached, so the next run reads it again. A cache
that cannot be read is treated as empty; one that cannot be written is left as
it was.

### Output

Text output has one line per component, in the order of the list:
`<label>: <status>[, installed <version>][, newest <version>][: <error>]`,
then an indented `update: <command>` or `install: <command>` line when there
is a command.

`--json` prints one object:

```json
{
  "components": [
    {
      "id": "tmux",
      "label": "tmux",
      "group": "system",
      "status": "update available",
      "installed": "3.6a-2ubuntu0.1",
      "newest": "3.6a-2ubuntu0.2",
      "error": null,
      "command": "sudo apt install --only-upgrade tmux",
      "command_kind": "update",
      "note": null
    }
  ],
  "installed_read_at": {"tmux": 1800000000.0},
  "newest_read_at": {"tmux": 1800000000.0},
  "counts": 1,
  "newest_read_oldest": 1800000000.0,
  "newest_read_complete": true
}
```

| Key | Holds |
|---|---|
| `installed_read_at` | When each component's installed version was read, by id; null when it was never read |
| `newest_read_at` | When each component's reported newest version was read, by id; null when none is reported |
| `counts` | How many components are `update available` or `missing` |
| `newest_read_oldest` | The oldest `newest_read_at` among components that are not missing; null when there is none |
| `newest_read_complete` | Whether every component that is not missing has a newest version read |

## `emb skill`

```
emb skill install [--force] [--json]
emb skill uninstall [--json]
emb skill status [--json]
```

Installs, removes or shows the `ember` skill for Claude Code. The skill is a
symbolic link:

- The link is `$CLAUDE_CONFIG_DIR/skills/ember` when `CLAUDE_CONFIG_DIR` is
  set and not empty, otherwise `~/.claude/skills/ember`.
- The source is the first of the source checkout's `skill/` folder (two
  folders above the `ember` package), used when `emb` runs from a checkout,
  and `/usr/share/ember/skill`. A folder is a source when its `SKILL.md` has
  `name: ember` in its front matter.

Ember creates, replaces and removes only links; it never removes a folder or
a file at the link path.

| Option | Does |
|---|---|
| `install` | Create the link to the source |
| `--force` | With `install`: replace a link to another folder |
| `uninstall` | Remove the link |
| `status` | Print the link, its state, its target and the source |
| `--json` | Print JSON |

`state` is one of:

| State | When |
|---|---|
| `installed` | A link whose resolved target is the source |
| `missing` | Nothing at the link path |
| `other link` | A link to another path that exists |
| `broken link` | A link whose target does not exist |
| `folder` | A folder |
| `file` | Any other file |

`target` is the absolute resolved destination of a link, and null for
anything that is not a link. `status` prints one `<field>: <value>` line each
for `link`, `state`, `target` (`null` when there is none) and `source`
(`null (<reason>)` when none is found); with `--json` it prints
`{"link", "state", "target", "source", "source_reason"}`, where
`source_reason` is null when a source is found.

`install` needs a source. It creates the missing parent folders at mode 755,
then the link with the source's absolute path as its target, through a
temporary link in the same folder renamed over the link path.

| State | `install` | `uninstall` |
|---|---|---|
| `installed` | Changes nothing; `already installed: <link> -> <source>` | Removes the link; `removed: <link>` |
| `missing` | Creates the link; `installed: <link> -> <source>` | Changes nothing; `not installed: <link>` |
| `broken link` | Replaces the link | Removes the link |
| `other link` | Refused (75); with `--force`, replaces the link | Removes the link when its target holds a `SKILL.md` with `name: ember`; otherwise refused (75) |
| `folder`, `file` | Refused (75) | Refused (75) |

`uninstall` needs no source. With `--json`, `install` and `uninstall` print
`{"link", "action", "target"}`: `action` is `installed`, `already installed`,
`removed` or `not installed`; `target` is the source for an install, the
link's resolved destination read before removal for an uninstall, and null
for `not installed`.

The package does not create the link; the owner runs `emb skill install`
once.

### Exit codes

Messages go to standard error as `emb skill: <message>`; each non-zero exit
then prints one `next:` or `owner:` line.

| Code | Meaning | Line |
|---|---|---|
| 0 | Done, or nothing to do | |
| 64 | Usage error | |
| 66 | `install` found no skill source | `owner: reinstall Ember; the skill is missing from <paths tried>` |
| 73 | The link cannot be created or removed | `owner: check <folder>: <error>` |
| 75 | Refused: a link to another folder without `--force`, a folder or file at the link path, or, for `uninstall`, a link to a folder that is not an Ember skill | `next: emb skill install --force` for another link; otherwise `owner: move or remove <path>, then emb skill install` |

## `emb app`

```
emb app [--hidden]
```

Starts the desktop app, or brings the running one forward. One instance runs
at a time.

| Option | Does |
|---|---|
| `--hidden` | Start in the tray with the window hidden; without a tray the window shows. Given to a running app, it changes nothing |

Without `WAYLAND_DISPLAY`, `DISPLAY` and `QT_QPA_PLATFORM` it prints
`emb app: no graphical session (WAYLAND_DISPLAY and DISPLAY are unset)` to
standard error.

| Code | Meaning |
|---|---|
| 0 | Quit Ember was chosen, or the running app took over |
| 64 | Usage error |
| 69 | No graphical session, or no runtime folder for the single-instance socket |

`emb` imports Qt only for `emb app`.

## Desktop app

### Start

The app listens on `$XDG_RUNTIME_DIR/ember/app.sock`. A second start
connects to it, sends `show` (nothing with `--hidden`) and exits 0; the
running app then shows, raises and activates its window. A socket file with
no app listening is removed and the start goes on.

At start the app:

- shows the window, unless `--hidden` was given and a tray is available;
- applies the Fusion style with the colours of the desktop's light or dark
  scheme;
- reads the settings, the engine status (checking every engine), the
  dependency report (installed versions now, newest versions when due) and
  the run records, each off the window's thread;
- once the settings are read, applies `app.theme` and writes or removes the
  autostart entry to match `app.autostart`; it does so again each time
  either setting changes.

Until the settings are read the content area shows "Reading settings"; when
they cannot be read it shows the reason and Try again.

### Window

The window opens at 1100 by 720 pixels and is never smaller than 900 by 600.
Its size and last screen are kept between starts. The sidebar shows the Ember
mark and name, one row per screen (Overview, Menu, Engines, Dependencies,
Settings) and `Ember <version>` at the bottom. Overview's row shows the
number of live runs and Dependencies' row the number of components with an
update available or missing, each only above zero. A screen is created the
first time it is shown.

Saves run one at a time: a change made while a save is in flight is sent
after it, with the settings generation that save produced. Choosing the
value already saved saves nothing. A save refused because the settings
changed elsewhere drops the changes waiting behind it, reloads the settings
and shows "Settings changed elsewhere. This screen now shows the new values.
Make your change again." above the screen. Other failures of a save or
command show what happened and what to do there, with Dismiss, and the next
waiting change is sent.

With `app.theme` set to `system`, the app follows the desktop when it
switches between light and dark.

Screens show times in local time as `Today 10:42`, `Yesterday 17:03` or
`30 Oct 14:05`, with English month abbreviations in every locale, and
durations as `18 min`, `1 h 5 min` or `2 h`.

### Keyboard

| Keys | Do |
|---|---|
| Ctrl+1 to Ctrl+5 | Open Overview, Menu, Engines, Dependencies, Settings |
| Ctrl+W | Hide the window to the tray; quit when there is no tray |
| Ctrl+Q | Quit |
| Left, Right, Home, End | In a screen mirror that has focus: scroll it sideways |
| Up, Down, Page Up, Page Down | In a screen mirror that has focus: scroll it, or the screen around it when it shows every line |

A screen mirror takes focus with Tab and then shows the focus ring. Keys
pressed in it never reach the run's session.

### Tray

The tray icon shows whether any run is live, always in the drawings for a
dark top bar. Its tooltip is `Ember · 2 runs live` or `Ember · No runs live`.
The menu holds:

- a status line: `No runs live`, `1 run live` or `2 runs live`;
- one line per live run, `<Engine> · <repository folder> · <HH:MM>` with the
  local start time, which opens a terminal window on the run as
  `emb run --attach` does;
- Open Ember and Quit Ember.

Clicking the icon also shows the window. Closing the window hides it to the
tray. When the desktop has no tray, the window shows at start and closing it
quits.

While the menu is open it keeps its lines; a change to the live runs
redraws it after it closes. Quit Ember hides the window and the tray icon at
once, then waits up to 12 seconds for running background tasks to end before
the app exits.

### Run records

The app reads every run record off the window's thread. It watches the runs
folder (created at mode 700 when missing), each run's folder and each run's
`run.json`, and reads again after a change; it also reads every sixty
seconds, while the window is hidden too. At most one read runs at a time and
one more follows it when a change came in meanwhile. Live runs are those
`starting`, `running` or `waiting`, oldest first; finished runs are the last
twenty `closed` or `failed`, newest `ended_at` first. A record that cannot be
read is skipped and counted. When the runs folder itself cannot be read, the
app keeps the records it read last and Overview shows the reason with Try
again, which reads the folder again at once. A run whose turn failed since
the last read makes the app check the engines again.

While Overview is shown, the app also reads `flow.json` and `panel.json`
off the window's thread and watches their folders and record files, with
the same sixty-second backstop and coalesced reads. It takes no lock and
reads lifetime lock ownership from `/proc/locks`. It stops these reads and
watches when Overview is left or the window is hidden. Unreadable flow and
panel folders are counted in their cards.

### Commands

An update, install or sign-in command runs only after the "Run this
command?" dialog and Run in terminal. It starts detached in a new window of
the `terminal` setting's terminal:

```
<terminal> sh -c 'sh -c "$1"; code=$?; printf "\nExit code %s. Press Enter to close.\n" "$code"; read -r _' ember-command <command>
```

The command is passed as an argument, never written into the script. When
the terminal cannot start, the app names the terminal command and says to
pick another terminal in Settings.

Known terminals, in the order Settings offers them; Other command takes any
command line:

| Terminal | `terminal` value |
|---|---|
| System default | `xdg-terminal-exec` |
| GNOME Terminal | `gnome-terminal --` |
| Ptyxis | `ptyxis --new-window --` |
| Console | `kgx --` |
| kitty | `kitty` |
| Alacritty | `alacritty -e` |
| foot | `foot` |
| WezTerm | `wezterm start --` |
| Ghostty | `ghostty -e` |
| Konsole | `konsole -e` |
| Xfce Terminal | `xfce4-terminal -x` |
| XTerm | `xterm -e` |

### Overview

Overview's subtitle is the tray's status line. Above the lists, an alert
names each engine that is on and whose `sandbox.<engine>` setting gives it
no sandbox, read from the last settings snapshot, with Open Engines; an
alert counts run records that could not be read. The cards appear in order:
Flows, Panels, Live, Finished. Flows and Panels are hidden when they have
neither readable nor unreadable records.

The Flows card lists every active flow (`starting`, `running`, `checking`,
`stopped`), oldest first, followed by the last five ended flows (`done`,
`failed`), newest end time first. Each row shows an expand arrow, a badge,
`<repository folder> · <n> tasks · <merged>/<n> merged`,
`ember/flow/<id> · wave <n>` with its reason when set, and elapsed time or
end time and duration.

| Flow status | Badge |
|---|---|
| `starting` | Starting |
| `running` | Running |
| `checking` | Checking |
| `stopped` | Waiting for Claude |
| `done` | Done |
| `failed` | Failed |
| Runner missing after the grace period | Runner gone |

Expanded, a flow shows its tasks in plan order: task id, status, round,
writer engine and model, reason, number of reviews, last verdict and its
must, should and lead counts. A task-specific writer overrides the flow's
writer. Current writer and review run ids are selectable text. Show run
appears beside an id with a live or finished row on Overview; it expands
that row and scrolls to it.

The Panels card lists every running panel, oldest first, followed by the
last five ended panels (`done`, `incomplete`), newest end time first. A row
shows the badge Running, Done, Incomplete or Process gone,
`Panel · <n> members · <answered>/<n> answered`, the repository folder and
the first non-empty question line cut to eighty characters, and elapsed
time or end time and duration. Expanded, it shows each member's number,
engine, model, effort (or default), status, seconds, reason and changed
paths, with its run id and Show run when available. It then lists left-out
members and their reasons and the selectable path to `summary.md`.

A starting, running or checking flow shows Runner gone, and a running
panel shows Process gone, when its lifetime lock is not held and its
record has not been updated for more than sixty seconds. A stopped flow
needs no runner. An unreadable lock table or runtime folder leaves the
stored status on screen. The app never takes these locks or creates lock
files. Flow and panel rows are read-only; one row per card can be expanded.
Rows and expanded details survive unrelated record changes. Elapsed times
update every thirty seconds while shown, without rereading records or
rebuilding expanded details.

The Live card holds one row per live run, oldest first: an expand arrow,
the status badge (Starting, Running, Waiting for Claude),
`<Engine> · <model> · <effort>`, `<repository folder> · <branch or
read-only> · turn <n>`, the time since the start, redrawn every 30 seconds
while the screen shows, and Open terminal, which opens a terminal window on
the run as `emb run --attach` does. The Finished card holds the last twenty
finished runs, newest first: the badge (Closed, Failed), the same first
line, `<subject> · <branch or read-only>`, and the end time with the
duration. Expanding a finished row shows its record: run, subject, engine,
repository, branch, sandbox, turns and commits, start, end, last exit and
last reason. One row of each card is expanded at a time.

Rows are kept per run id across reloads. A finished row is redrawn only
when a value it shows changes, so focus, an expanded record and selected
text stay when another run saves its record.

Expanding a live row shows the run's screen mirror. The mirror is a tmux
control-mode client attached to the run's session with the `read-only` and
`ignore-size` flags, so it cannot type into the pane or change its size.
After the pane writes output it captures the pane with its colours and
cursor and redraws; nothing runs on a timer. It closes
when the row collapses, the run leaves the live list, the screen is left or
the window hides, and shows "Session ended" when the session ends.

When the runs folder cannot be read, the screen shows the reason and Try
again in place of the cards. When only the flow and panel read fails, an
alert replaces those two cards: "Ember could not read the flow and panel
records: <reason>." Try again reads them again. Unreadable individual
records are counted inside their card, such as "1 flow record could not be
read." The initial loading state waits for both record models. When there
are no runs, flows or panels, Overview shows "No runs yet" and "Runs, flows
and panels appear here when Claude starts them with `emb run`, `emb flow`
or `emb panel`."

### Menu

Menu shows the standing worker menu in three cards: Single tasks, Work of
several tasks and Panel. Single tasks holds the Writer and Reviewer pickers
(`single.writer`, `single.reviewer`). Each lists Ask each time (`ask`), then,
for Codex, OpenCode and Claude in that order, each engine that is on and has
shortlisted models, under the engine's name, with its models as
`<model> · <effort>` (OpenCode entries without an effort show the model
alone). The engine names are headers and cannot be chosen. While a picker
holds a Claude model, a row under it picks how it runs, CLI or Subagent
(`single.writer_as`, `single.reviewer_as`).

A saved value that the picker no longer offers stays chosen and shows as
`<engine>:<model> (not on the shortlist)` or `<engine>:<model> (<Engine> is
off)`, with a warning line giving that reason. A saved model whose engine was
not connected at its last check shows a warning line too; the check is the
app's last engine status read, or before the first read the status cache the
settings snapshot reports. When no engine
offers a model, the pickers hold Ask each time only and the screen shows
"Shortlist models in Engines to offer them here." with Open Engines.

Work of several tasks holds Multi-task work (`multi.runs_as`): Ask each time
(`ask`), Flow (`flow`) or Claude Workflow (`workflow`). It is available even
when no engine offers a model.

Panel lists `panel.members` in saved order. A member shows
`<Engine> · <model> · <effort>`, omitting effort when none is set. A member
whose engine is off, was not connected at its last check, or whose model is
not shortlisted shows its saved `<engine>:<model>` value and a warning
explaining how to change it. An invalid value also shows a removal warning.
Each member has a Remove button. Add member lists shortlisted models of
engines that are on, grouped by engine, excluding saved members. Choosing
one appends it and returns the picker to Add member. With no members the
card says "No members. Claude asks for members, or uses the ones it is given,
each time." At eight members Add member is disabled and the card says
"A panel has at most eight members."

A member missing from its shortlist disables Add member, even when its
engine is off or not connected. The warning asks the owner to remove those
members or shortlist them in Engines. Remove saves only when every remaining
member is shortlisted. Otherwise it sends no save, names the first remaining
member to shortlist, and shows Remove all, which saves an empty list. An
engine being off or not connected alone does not block saving. Add member
is also disabled when no unused shortlisted model is offered.

One-time choices are not shown. Each change saves at once with the settings
generation check; choosing an already saved multi-task value saves nothing.
Panel mutation controls stay disabled until the save resolves and the screen
refreshes. A stale save reloads the settings and shows the app's stale alert.

### Settings

Settings has two groups. Each change saves at once.

| Group | Row | Key | Control |
|---|---|---|---|
| General | Start at login | `app.autostart` | Switch |
| General | Theme | `app.theme` | System, Light, Dark; applies at once |
| Workers | Worker network | `worker.network` | Switch |
| Workers | Terminal | `terminal` | Picker; Other command shows a text field |
| Workers | Sandbox read paths | `sandbox.read` | One row per folder with a remove button; Add folder |

After Start at login changes, the app writes or removes the autostart entry
and shows the reason under the switch when it could not.

Terminal lists System default first, marked "(not installed)" when
`xdg-terminal-exec` is not on `PATH`, then each other known terminal found on
`PATH`, then Other command. A saved known terminal that is not installed
shows as an unavailable entry, marked "(not installed)", until another value
is chosen. A saved value that matches no known terminal shows as Other
command with that value. Other command's text is saved on Enter or when the
field loses focus; empty text or text that is not a valid command line is not
saved, and the line under the field says why. Leaving the field with such
text returns the picker to the saved value.

Add folder opens the desktop's folder chooser. A folder already in the list
is not added again.

### Files

| File | Holds |
|---|---|
| `$XDG_STATE_HOME/ember/app.json` | The window's `width`, `height` and last `screen`, mode 600; content that cannot be read gives the defaults |
| `$XDG_RUNTIME_DIR/ember/app.sock` | The single-instance socket |
| `$XDG_CONFIG_HOME/autostart/io.github.rearwindowvertigo.Ember.desktop` | The autostart entry while `app.autostart` is on, mode 600: `Exec=<absolute path of emb> app --hidden`. When `emb` is not on `PATH` nothing is written, and the app prints the reason to standard error and shows it in Settings |

## Installed files

The `.deb` package (`ember_<version>_amd64.deb`) installs these files and
has no maintainer scripts:

| Path | Holds |
|---|---|
| `/usr/bin/emb` | The `emb` command, mode 755 |
| `/usr/lib/ember/` | A virtual environment on `/usr/bin/python3.14` with Ember and PySide6-Essentials |
| `/usr/lib/ember/lib/python3.14/site-packages/ember/` | Ember |
| `/usr/share/ember/skill/` | The `ember` skill, without its eval cases |
| `/usr/share/applications/io.github.rearwindowvertigo.Ember.desktop` | The desktop entry: `Exec=emb app` |
| `/usr/share/icons/hicolor/scalable/apps/io.github.rearwindowvertigo.Ember.svg` | The icon |
| `/usr/share/doc/ember/copyright` | The copyright file |
| `/usr/share/doc/ember/icu-LICENSE` | The licence of the ICU libraries Qt bundles |
| `/usr/share/doc/ember/changelog.gz` | The changelog, when the build had `CHANGELOG.md` |

`/usr/bin/emb`:

```sh
#!/bin/sh
exec /usr/lib/ember/bin/python -I -m ember.cli "$@"
```

`-I` keeps the caller's `PYTHONPATH` and user site folder out of Ember's
interpreter. The package installs no autostart entry; the app writes the
user's own (see Files under Desktop app). The package does not link the
skill into Claude Code; `emb skill install` does.

## Exit codes

These are the exit codes of `emb choices` and `emb engines`; `emb run`,
`emb gapcheck`, `emb flow`, `emb panel`, `emb ground`, `emb merge`, `emb cleanup`, `emb prune`, `emb deps`, `emb skill` and
`emb app` have their own tables above.

| Code | Meaning |
|---|---|
| 0 | Success |
| 64 | Usage error or invalid value; `emb` with no command prints help to standard error and exits 64 |
| 74 | The settings database cannot be opened or read |
| 75 | Stale generation or busy database |
| 77 | An owner option without a terminal |
| 78 | Unsupported settings database schema |
