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
    "single.reviewer": {}
  }
}
```

`claim` is `null` without `--id`.

Menu `state` is `set`, `ask`, `invalid`, `engine off`, `not connected` or
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
`HEAD` is not where the run left it (76); a refused commit exits 77 and
leaves the work uncommitted.

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
not refuse later commands. `--send` then commits a turn that was collected but not committed.
It sends only when the driver reports the session idle; a busy or unknown
session exits 75. The message is sent as
written. When the driver fails (71) or Ctrl+C (74) stops the command before
the message is known to be delivered, the run's previous turn state is
restored and the line is `next: emb run --send <run> --message-file <file>
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
`~/.local/state/ember/runs/<id>/`), mode 700, holding:

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
| 77 | A hook refused the commit | `next: git -C <worktree> status` |
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

`--json` prints one object:

| Field | Value |
|---|---|
| `branch` | `BRANCH` |
| `target` | `TARGET` |
| `merge_commit` | The merge commit; null when the branch was already contained |
| `contained_before` | Whether the branch was already contained |
| `cleanup` | The cleanup's list, as `emb cleanup --json` prints it |

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

## Exit codes

These are the exit codes of `emb choices` and `emb engines`; `emb run`,
`emb ground`, `emb merge` and `emb cleanup` have their own tables above.

| Code | Meaning |
|---|---|
| 0 | Success |
| 64 | Usage error or invalid value; `emb` with no command prints help to standard error and exits 64 |
| 74 | The settings database cannot be opened or read |
| 75 | Stale generation or busy database |
| 77 | An owner option without a terminal |
| 78 | Unsupported settings database schema |
