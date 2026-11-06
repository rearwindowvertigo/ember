# Ember reference

## `emb choices`

Reads standing settings and one-time choices, claims and releases one-time
choices for runs, and sets settings and one-time choices at a terminal.

Settings are kept in `$XDG_CONFIG_HOME/ember/settings.db` (default
`~/.config/ember/settings.db`), mode 600 in a mode 700 folder. A read never
creates the database file and never changes its content. A read may leave
empty `settings.db-wal` and `settings.db-shm` files beside it, at mode 600.

Every reply is JSON on standard output, indented by two spaces. Errors go to
standard error as `emb choices: <message>`; skill-version warnings use
`WARNING:` followed by a `next:` or `owner:` line.

### Options

| Option | Does | Standard output |
|---|---|---|
| (none) or `--json` | Read | Read reply |
| `--id ID` | With a read or `--claim`: use the claim for `ID` | Read reply for `ID` |
| `--claim --id ID` | Claim the pending one-time choice for `ID` | Read reply |
| `--skill X.Y.Z` | With `--claim`: compare the loaded skill version with the installed skill | Adds `skill`, `loaded_skill_version` and `installed_skill_version` |
| `--finish ID` | Release the claim for `ID` | `{"claim": {"id", "status": "gone", "was"}, "generation", "errors"}` |
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
| `shortlist.opencode` | List of full `<integration>/<model>` ids, with optional `@<effort>`; integration `~?[A-Za-z0-9][A-Za-z0-9._-]*`, model one or more `/`-separated parts matching `~?[A-Za-z0-9][A-Za-z0-9._:-]*` | `[]` | standing |
| `single.writer`, `single.reviewer` | `ask`, or `<engine>:<model>` with the model on that engine's shortlist when saved | `ask` | request, one-time, standing |
| `multi.runs_as` | `ask`, `flow`, `workflow` | `ask` | request, one-time, standing |
| `flow.review_rounds` | `1`, `2`, `3`, `4` | `3` | request, one-time, standing |
| `gapcheck.reviewer`, `flow.writer`, `flow.reviewer` | `same`, or `<engine>:<model>` with the model on that engine's shortlist when saved | `same` | request, one-time, standing |
| `gapcheck.when` | `off`, `always`, `2`, `3`, `4`, `5`, `6`, `8`, `10`, `ask` | `always` | request, one-time, standing |
| `gapcheck.ask` | `high`, `medium`, `all` | `high` | request, one-time, standing |
| `flow.parallel` | `1`, `2`, `4`, `6` | `4` | request, one-time, standing |
| `flow.stall` | `5`, `10`, `20` | `10` | request, one-time, standing |
| `flow.whole_review` | `on`, `off` | `on` | request, one-time, standing |
| `workflow.agents` | `4`, `8`, `16` | `8` | request, one-time, standing |
| `workflow.late` | `ask`, `wait`, `stop` | `ask` | request, one-time, standing |
| `panel.members` | List of `<engine>:<model>` with each model on its engine's shortlist when saved, at most 8, no duplicates | `[]` | request, one-time, standing |
| `single.writer_as`, `single.reviewer_as` | `cli`, `subagent` | `cli` | request, one-time, standing |
| `provider.capacity_retries` | Integer strings `0` to `10`; most resends of one turn after a capacity error; `0` turns retries off | `2` | standing |
| `provider.capacity_wait` | Integer strings `5` to `600`; seconds before a capacity resend | `30` | standing |
| `sandbox.codex` | `on`, `off` | `on` when `bwrap` is on `PATH` or the `codex` on `PATH` has a bundled `bwrap`, else `off` | standing |
| `sandbox.opencode`, `sandbox.claude` | `on`, `off` | `on` when `bwrap` is on `PATH`, else `off` | standing |
| `sandbox.read` | List of absolute paths, no duplicates; paths inside Ember's configuration or state folders are refused | `[]` | standing |
| `terminal` | A non-empty command line in shell syntax | `xdg-terminal-exec` | standing |
| `app.autostart` | `on`, `off` | `on` | standing |
| `app.theme` | `system`, `light`, `dark` | `system` | standing |

`sandbox.read` accepts a JSON array of strings or a comma-separated list
at the terminal. Use a shell-quoted JSON array for paths containing commas
or quotes. Settings and `emb choices` check given and resolved paths when
refusing Ember's configuration and state folders.

A shortlist lists each model once. A request or one-time value for a
standing-only key is refused.

OpenCode ids include the integration that serves the model, such as
`anthropic/claude-sonnet-4` or
`openrouter/deepseek/deepseek-v4.1-flash-1017`. Stored ids are used as stored,
independently of connected integrations and the model catalogue.

Settings without the `opencode_id_format` marker are read with `openrouter/`
added to every stored OpenCode model unless it already has that prefix.
This covers the shortlist, worker rows and panel members in standing
settings, pending one-time choices and claimed copies. For example, an old
`anthropic/claude-sonnet-4` becomes
`openrouter/anthropic/claude-sonnet-4`. Efforts, timestamps and claim states
are kept. Reads, snapshots and dry runs return the converted values without
writing. The next settings change writes the conversion and marker in its
transaction, with the generation check and one generation increment.
Run, flow and panel records without the marker use the same conversion
on load and save with the marker. New records carry `opencode_id_format: 1`.

`gapcheck.reviewer`, `flow.writer` and `flow.reviewer` follow their
single-task row (`single.reviewer`, `single.writer`, `single.reviewer`)
while `same`. `menu` reports each of them as a worker row with `same`
true or false; a `same` row is its single-task row's. A Claude model in
one of them runs as its single-task row's `_as` value says.
`gapcheck.when` decides whether a plan gets a gap check: `off` never,
`always` every plan, a number from that many steps up, `ask` after the
owner says so. `gapcheck.ask` is the lowest gap harm put to the owner:
`high`, `medium` (high and medium) or `all`. `flow.parallel` and
`flow.stall` are the flow's tasks at once and stall minutes when the plan
and the command give none. `flow.whole_review` records whether Claude runs
one whole-flow review before merging a flow. `workflow.agents` and
`workflow.late` are what the skill follows for a Claude Workflow: the most
agents at once, and what to do when an agent runs past its deadline.

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
- `--finish ID` records `ID` as `gone`, also when `ID` was not known. `gone`
  is the status of every finished claim. The reply's `was` is the status
  before the finish: `claimed`, `none`, `gone` (finished before, or
  expired) or `unknown`.
- Claim status is `claimed`, `none`, `gone`, or `unknown` for an id never
  claimed. With `gone` or `unknown`, every key resolves to its default and
  `--request` values are not used.

### Loaded skill version

`--claim --id ID --skill X.Y.Z` compares the loaded version with the
`Skill version:` line in `SKILL.md` at the installed skill link's target
(see `emb skill`). The comparison runs on every claim, including retries
of a known id. It does not fall back to the source checkout.

| `skill` | When | Standard error |
|---|---|---|
| `current` | Both versions match | None |
| `stale` | Both versions are valid and unequal, newer or older | `WARNING: the loaded ember skill is <x>; the installed skill is <y>`, then `next: reload the ember skill before the next step` |
| `unknown` | The installed link or its Ember skill is missing or unreadable, or the version line is missing, duplicated or malformed | `WARNING: the installed ember skill version is missing, unreadable or malformed`, then `owner: repair or reinstall the ember skill, then reload it before starting a worker` |

All three outcomes exit 0 and keep the claim's result. With `--skill`, the
reply also has `loaded_skill_version` (the supplied string) and
`installed_skill_version` (the installed version string, or `null` for
`unknown`). These fields and `skill` are absent without `--skill`.
`skill` is separate from `claim.status`.

`--skill` accepts three dot-separated non-negative integers, without
leading zeros except `0` itself. An empty or malformed version exits 64
before the claim and leaves any pending one-time choice unchanged.
`--skill` without `--claim` exits 64.

The skill passes the version from its loaded text on each claim. On
`stale`, it reloads before the next step and keeps the same claim id. On
`unknown`, it keeps the claim, reports the problem to the owner and starts
no worker until a valid installed skill is loaded. Source-checkout edits
between releases that keep the same version are not detected.

### Generation

`--set`, `--clear`, `--once` and `--clear-once` raise `generation` by one
when they write a standing-setting or pending one-time-choice row. A claim
that consumes a pending one-time choice also raises it by one. Other claims,
claim reads, finishes, and expiry of claims or pending one-time choices leave
it unchanged. A write that changes no row leaves it unchanged.

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
| `opencode` | `opencode` on `PATH`, else `~/.opencode/bin/opencode` | `opencode auth list` (`--standalone` with OpenCode 2) | it exits 0 and lists at least one connected integration |
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
| `opencode` | `curl -fsSL https://opencode.ai/install \| bash` | `opencode auth login` |
| `claude` | `curl -fsSL https://claude.ai/install.sh \| bash` | `claude auth login` |

A sign-in command names the CLI by its path when it is not found on `PATH`.
OpenCode's sign-in requirement reads "at least one integration connected
in OpenCode (use /connect)".

### Models

| Engine | Source | `source` |
|---|---|---|
| `codex` | `codex debug models`, models whose visibility is `list` | `cli` |
| `opencode` | `opencode models`, full `<integration>/<model>` ids, with details from `https://models.dev/api.json` | `cli` |
| `claude` | The aliases Claude Code's `--model` takes: `fable`, `opus`, `sonnet`, `haiku`, each with efforts `low` to `max` | `cli` |

The `codex` and `opencode` lists are cached in
`$XDG_CACHE_HOME/ember/models-<engine>.json`, mode 600, and read from their
source again when the cache is 6 hours old, not valid, or with `--refresh`.
A list served from the cache has `source` `cache`. When the source cannot be
read, the cached list is used however old, with `reason` set. With no cache,
`source` names the source that was tried, `fetched_at` is `null` and `reason`
is set. The list is empty except for the OpenCode auth-list and catalogue
failures described below. A failed read leaves the cache as it was.

For OpenCode, `opencode models` supplies the models of its connected
integrations, including its own `opencode` provider when listed. The
models.dev catalogue supplies each model's context length (`limit.context`),
input and output prices (`cost.input`, `cost.output`) and tool support
(`tool_call`). Missing details are unknown. Efforts come from the values of
`reasoning_options` entries whose type is `effort`. `default_effort` is
`null`; OpenCode's own default applies when no effort is selected. Saved
efforts are kept.

Integration names and login methods come from `opencode auth list`
(`--standalone` with OpenCode 2). Ember reads OpenCode 1's `●  <name>
<method>` lines and OpenCode 2's `<name>  <method>  <state>` columns. The
methods `oauth` and `OAuth` give the login method `oauth`; `api` and
`API key` give `api`; `wellknown` and `env` connect the integration with an
unknown login method. A name
matches an integration id or its catalogue name, ignoring case. An unmatched
or ambiguous name assigns no login method. An integration without a matched
auth entry uses its catalogue name, or its id when that name is absent.
The `integrations` list includes matched connected integrations with no
models as well as those in the model list.

A failed or unreadable model command, auth-list command or catalogue uses
the cached OpenCode list with a reason. With no cache, an auth-list failure
returns the discovered models with unknown login methods and a reason, and
a catalogue failure returns them with unknown details and a reason; a failed
or unreadable model command returns an empty list. A cache without `format_version` is
refreshed even when less than 6 hours old. Only if the refresh fails is that
old cache used, with full `openrouter/` ids, provider OpenRouter, unknown
login methods and no default effort. Connection metadata stays as cached
until the next refresh.

`--json` prints:

```json
{
  "models": [
    {
      "id": "openrouter/openai/gpt-4o", "label": "GPT-4o",
      "efforts": [], "default_effort": null,
      "context_length": 128000,
      "prompt_price": 0.0000025, "completion_price": 0.00001,
      "supports_tools": true,
      "provider": "openrouter", "provider_name": "OpenRouter", "login": "api"
    }
  ],
  "integrations": [{"id": "openrouter", "name": "OpenRouter", "login": "api"}],
  "source": "cli",
  "fetched_at": 1800000000.0,
  "reason": null
}
```

Prices are US dollars per token. `context_length`, the prices and
`supports_tools` are `null` for `codex` and `claude` and when the catalogue
does not report them. Catalogue prices per million tokens are converted
to per-token values in JSON. `provider` is the integration id,
`provider_name` its display name, and `login` is `oauth`, `api`, or `null`
when unknown. These three fields are `null` for other engines, whose
`integrations` list is empty.

Text output starts with `<engine> models from <source>, read <age> ago` (or
`never read`), then one line per model: its id, label, efforts, context
length, prices in US dollars per million tokens and `tools` when it takes tool
calls. `reason` goes to standard error as `emb engines: <reason>`.

## `emb run`

Starts one worker in its own worktree and tmux session and waits for its
turn, or works with an existing run. The engine's driver is the `driver()`
factory of the module `ember.engines.<engine>`; an engine without one exits 69
with `owner: this Ember build has no driver for <engine>; update Ember`.

Data goes to standard output. Errors go to standard error as
`emb run: <message>`; each non-zero exit then prints its `next:` or `owner:`
line from the exit table below, subject to the owner wait described under
Status. Exits with a `report:` line print it on standard error after the
action lines.

After exit 0, a start, `--send`, `--recover`, `--close` (including
`--force`), `--answered` or `--status RUN` prints the run's derived next
line once on standard error when one applies. `--quiet` does not suppress
it. Dry runs and status lists print no derived next line. Runs with a
`parent` have no derived next line; status still prints the orphan
warnings described below.

### Options

| Option | Does |
|---|---|
| `--engine ENGINE --dir DIR --task-file FILE` | Start a run of `codex`, `opencode` or `claude` on the git checkout holding `DIR`, with `FILE` as the brief |
| `--dry-run` | With a start: check inputs and print the resolved worker without starting a run |
| `--model MODEL` | With a start: the model |
| `--effort EFFORT` | With a start: the effort |
| `--read-only` | With a start: review a writable detached worktree at the source checkout's `HEAD`, with no branch or Ember commits |
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
| `--answered RUN` | After the owner's reply: record the answer time, end the owner wait and print the current derived next line when one applies |
| `--status [RUN]` | Print one run's record, or every run's |
| `--json` | With `--status` or `--dry-run`: print JSON |
| `--quiet` | Limit progress output to the run id, resolved worker and each turn's summary; retain the next line |
| `--stall MINUTES` | Minutes without a change on the session's screen before exit 124; default 10 |

A start needs `--engine`, `--dir` and `--task-file`. The start options cannot
be used with the other modes, and only one mode is given at a time.

An explicit OpenCode `--model` is a full `<integration>/<model>` id and is
used unchanged. For example, `--engine opencode --model anthropic/claude-sonnet-4`
selects Anthropic's integration; an OpenRouter model uses
`--model openrouter/anthropic/claude-sonnet-4`.

### Start

A start checks, in this order: every file and path exists (66); the brief has
a non-empty line (64); `git` and `tmux` are on `PATH` (69); `--dir` is in a git
checkout (65); the claim is `claimed` or `none` (64 for `unknown` or `gone`);
the engine is on (64, also when named by `--engine`); the engine is connected
in the status cache of `emb engines`, which is checked once first when there
is no cache (69, naming the fix command); the driver exists (69); the model
and effort (64); Codex trusts the repository root (69); the checkout has a
commit and, for an editing run, the repository has a main checkout (70);
for a sandboxed Codex run, no `sandbox_mode` key (69); and, for a sandboxed
run, a `bwrap` that runs `/bin/true` in a minimal sandbox
within 10 seconds (69, with `owner: install bubblewrap (the bwrap command)
or make it work, or set sandbox.<engine> to off`), and the workers' cache
folder created (69). For Codex the `bwrap` is the one on `PATH`, else the
one bundled with Codex. No run folder, worktree or branch is created before
these checks pass.

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

For an untrusted Codex repository, both a dry run and a start exit 69 with
`Codex does not trust <repo>; the owner trusts it by running codex there once and choosing to trust the folder`
and `owner: run codex in <repo> once and choose to trust the folder`.
Neither creates a run, worktree, branch or session, or prints a `next:` line.

With `--dry-run`, the start runs the input, repository, claim and
worker-selection checks, including Codex project trust, through the commit
and main-checkout checks. It creates no cache folder, worktree, branch, run
record or session and runs no sandbox probe. It exits 0 on success, or with
the exit code of the failed check. It prints one line on standard output:

```text
Team, Single tasks Writer: codex gpt-6.1-sol · high (single.writer)
```

The row is `Single tasks Reviewer` with `--read-only`. Model and effort
sources are the Team row key, `--model` or `--effort`. An effort taken from
the driver's default has the model's source; an unset effort prints as
`default`. When sources differ, the line names each:

```text
Team, Single tasks Writer: codex gpt-6.1-sol · low (model: single.writer, effort: --effort)
```

With `--json`, the dry run prints one object instead:

```json
{
  "screen": "Team",
  "row": "Single tasks Writer",
  "engine": "codex",
  "model": "gpt-6.1-sol",
  "effort": "high",
  "model_source": "single.writer",
  "effort_source": "single.writer"
}
```

An unset effort is `null` in JSON. A dry run reads the settings when it
runs; a later start resolves them again.

An editing run gets the branch `ember/<engine>/<id>`, cut from the named
checkout's `HEAD`, in a worktree at `worktrees/<id>` in Ember's data folder,
`$XDG_DATA_HOME/ember` (default `~/.local/share/ember`), outside every
repository. The `worktrees` folder and its Ember parent are mode 700 and
owned by the current user. A symlinked Ember data folder or worktrees
folder is accepted when its resolved target is a folder owned by the
current user with mode 700. Symlinks are resolved before these checks.
The run records the resolved worktree path, the main checkout and the
common git folder at start. A worktree folder inside any Git working tree,
or in a checkout or the common git folder of the source repository, exits
69; the message names the worktree folder, the repository folder and the
variable that placed it there (`XDG_DATA_HOME`, or `HOME` when no absolute
`XDG_DATA_HOME` is set). Ember does not add `.ember/` to `.git/info/exclude`.
A read-only run has no branch and gets its own detached worktree in the
same worktrees folder, at the named checkout's `HEAD`. It does not see
uncommitted edits or later changes in the source checkout. At start it
prints `WARNING: read-only run <id> does not see uncommitted <path> (reads <hash>)` on
standard error for each uncommitted path in that checkout; these warnings
do not change a successful exit. `<hash>` is the `git rev-parse --short`
form of the commit used to create the detached worktree. Ember resolves
the source `HEAD` once for both the worktree and these warnings.

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
the repository's git folder, the read paths, the workers' cache folder and
the engine's own folders; `$XDG_RUNTIME_DIR` and Ember's configuration and
state folders are hidden. Ember walks its worktrees folder and denies each
entry other than this worker's own worktree by name, including through
symlinked read paths. Neither sandbox denies a parent folder and then
reopens a kept worktree inside it. The worktrees folder is created before
every sandbox starts, including a read-only worker's.
The worktree is writable for editing and read-only runs, including in the
Codex profile and bubblewrap. A detached review worktree permits builds and
tests to write project files while keeping the source checkout separate.
Legacy read-only records that name their source checkout keep that checkout
protected as read-only. The engine's own folders are:

| Engine | Writable | Read-only |
|---|---|---|
| `codex` | Those of Codex's `:workspace` profile | |
| `opencode` | `opencode` in `$XDG_DATA_HOME` and `$XDG_STATE_HOME` | `opencode` in `$XDG_CONFIG_HOME`; the folder of the `opencode` binary in the home folder |
| `claude` | `$CLAUDE_CONFIG_DIR` or `~/.claude`, `~/.claude.json`, `~/.claude.json.backup` | The folder the `claude` command resolves to, in the home folder |

The workers' cache folder is `worker` in Ember's cache folder,
`$XDG_CACHE_HOME/ember/worker` (default `~/.cache/ember/worker`), mode 700,
shared by every sandboxed run and kept between runs. A sandboxed run creates
it at the start; it is writable in every sandboxed run, read-only runs
included. Ember does not set `TMPDIR` for workers or create a per-run
temporary folder. Bubblewrap mounts a private empty `/tmp` for each worker.
The Codex profile makes `/tmp` writable, including for read-only runs.
Every sandboxed worker, the engine itself for OpenCode and Claude
and the commands Codex runs, gets these variables, which replace any value
the owner set:

| Variable | Value |
|---|---|
| `XDG_CACHE_HOME` | The workers' cache folder |
| `UV_CACHE_DIR` | `uv` in it |
| `UV_PYTHON_INSTALL_DIR` | `uv-python` in it |
| `PIP_CACHE_DIR` | `pip` in it |
| `npm_config_cache` | `npm` in it |
| `GOMODCACHE` | `go/mod` in it |

OpenCode's and Claude Code's caches are therefore in the workers' cache
folder while they run sandboxed. Ember leaves `CARGO_HOME` and
`GRADLE_USER_HOME` unchanged: they hold configuration and credentials as
well as caches. `UV_PYTHON_INSTALL_DIR` is set because uv downloads Python
there on demand. A tool that needs an uncached download still needs
network access.

A tool searching parent folders can reach files above Ember's worktrees
folder. A denied file there, such as `~/pyproject.toml`, still returns
`Permission denied`.

After each attempt of a sandboxed turn, Ember checks the output and screen
for `Permission denied`, `Operation not permitted` or `EACCES` naming an
absolute or home-relative (`~/`) path. It matches the path against the
hidden paths saved at launch, including their given and resolved names;
later settings changes do not reclassify the turn. A failure without a
printed path, or with only a working-directory-relative path, is not detected.

Each turn's `denied_paths` is a list of objects:

| Field | Value |
|---|---|
| `path` | The deepest matching hidden path in the launch inventory, or the hidden file itself; a startup file inside a hidden folder is reported under its resolved file path |
| `reason` | `hidden`, `startup file`, `secret location` or `protected` |
| `allow_path` | The folder Overview may offer for reading, or null |

`allow_path` is the deepest folder holding the denied path that contains
no secret or protected location. Given and resolved paths are checked.
No folder inside `$XDG_CONFIG_HOME` is offered. A hidden file can have
`allow_path: null`. Reports are deduplicated by resolved hidden path
across every attempt of the turn, including capacity retries. A restricted
report keeps `allow_path: null`; reasons rank `protected`, `secret location`,
`startup file`, then `hidden`. No denial gives `[]` and no denial warning.
Detection leaves the turn's exit code unchanged.

Shell and line-editor startup files in the home folder are classified as
`startup file`: `.profile`, `.bashrc`, `.bash_profile`, `.bash_login`,
`.bash_logout`, `.zshrc`, `.zprofile`, `.zshenv`, `.zlogin`, `.zlogout`,
`.shrc`, `.kshrc`, `.mkshrc`, `.cshrc`, `.tcshrc`, `.login`, `.logout`,
`.yashrc`, `.yash_profile`, `.inputrc` and `.editrc`. Ember matches their
given and resolved names, including symlink aliases. Startup-file denials
have `allow_path: null`, no Overview notice, no Allow reading button and
no `sandbox.read` suggestion. They remain in `denied_paths` and
`emb run --status`, including when they are a turn's only denials. A tool
that needs a startup file's settings still reports the denial; the owner
can add the file by hand. Other startup files outside the home folder are
not classified by this list.

Stored reasons are retained. An older run, flow or panel record with a
`hidden` denial of a startup file still reports `hidden`; Overview shows
its notice with the current hidden-path wording.

For each path, standard error prints a warning once per turn. When reading
is offered, it also prints an owner command:

```text
WARNING: <run> was denied <path>
owner: Settings, Sandbox read paths; emb choices --set 'sandbox.read=["<folder>"]'
```

The command preserves the current read list and adds `allow_path`, or an
addable secret location's matched path. When the final recorded turn exit
is 0, after retries and commit handling, the read-access `owner:` line says
the turn finished and read access is needed only if the worker's report
says the work needed the path.
A failed turn prints the unqualified line above. The `WARNING:` line and
`denied_paths` entry are unchanged by this qualification.
A startup, secret or protected warning appends `(startup file)`,
`(secret location)` or `(protected)`.
Startup-file denials receive no `owner:` line. A `hidden` denial with
`allow_path: null` receives `owner: <path> is hidden; no folder can be
offered because each folder that holds it also holds private files`, with no `sandbox.read`
suggestion. Protected runtime and other worker worktree paths also receive
no `sandbox.read` suggestion. A matched path inside Ember's own
configuration or state folder instead receives an owner line saying it
cannot be added.

A secret location's single `owner:` line starts with:

```text
owner: Overview offers no Allow reading for <path>; folders inside $XDG_CONFIG_HOME are never offered;
```

`$XDG_CONFIG_HOME` is printed literally. An addable
secret location keeps the Settings and `emb choices --set` instruction
after this prefix, including the exit-0 qualification when applicable.

Secret locations include `~/.ssh`, `~/.gnupg`, `~/.aws`, `~/.azure`,
`~/.config/gcloud`, `~/.config/gh`, `~/.docker/config.json`, `~/.netrc`,
`~/.kube`, `~/.password-store`, `~/.local/share/keyrings`, and Ember's
configuration and state folders. The inventory also includes Firefox,
Thunderbird, Chrome, Chromium, Brave, Edge, Vivaldi and Opera profiles in
the native, Flatpak and Snap locations listed in `sandbox.SECRET_HOME_PATHS`,
and configuration locations under a redirected `$XDG_CONFIG_HOME`.
Secret locations have no Allow reading button. The owner can add them by
hand except Ember's configuration and state folders: Settings and
`emb choices --set sandbox.read=...` refuse those paths, including symlink
aliases and descendants. Other workers' worktrees and `$XDG_RUNTIME_DIR`
are `protected` and have no Allow reading button.

Network: Codex has network only when `network` is true; its web search is
always on. OpenCode keeps network for its integrations. Claude keeps network
for its API, so its `Bash` commands keep network too; `network` controls only
its `WebSearch` and `WebFetch` tools.

A sandboxed run exits 69 when a kept path (the worktree, the repository's
git folder, a read path, or another path the sandbox keeps) is Ember's
configuration or state folder or lies inside one, as given or with symlinks
resolved, because the sandbox hides those folders. A kept path inside
Ember's worktrees folder outside the worker's own worktree is also refused.
The workers' cache folder is a kept path. The message names the kept path
and the folder; the `owner:` line names the variable or read path to change,
or `sandbox.<engine>` to switch off.

A sandboxed Codex run with a `sandbox_mode` key in a Codex configuration
file exits 69 naming the files. The check runs again on the new worktree
once it is created, for a tracked `.codex/config.toml`; a conflict there
records the run failed and exits 69, and the worktree and branch stay. With
`none`, a run of any engine prints the `WARNING:` line of the sandbox.

The desktop app's sandbox check of an engine keeps its last result per
engine in `$XDG_CACHE_HOME/ember/sandbox.json` (default
`~/.cache/ember/sandbox.json`), mode 600; writers hold `sandbox.lock` in the
same folder while they read, merge and write it.

The session is a tmux session named by the run id. It runs `launch.sh` in
the run folder, mode 600, which holds the engine's command, because tmux
refuses a command longer than about 16 KiB. Its first message is the
line `Brief from Claude, the lead, for Ember run <id>.`, a blank line and the
brief, pasted once the engine is ready; a driver that puts the brief on the
engine's command line pastes nothing. The session runs with no window. With
`--window`, a window of the `terminal` setting also opens on the session; a
window that cannot open prints a `WARNING:` line and the run goes on.
`--attach` opens a window on a running session at any time.

The first line on standard output is `run <id>`. At start, before the
first turn, the resolved worker line shown by `--dry-run` is printed on
standard error, including with `--quiet`.

### Turns

A command that waits for a turn observes it every second. It stops with exit
124 when the session's screen has not changed for `--stall` minutes and the
turn has not ended; the session keeps running. Each command, including each
`--recover`, starts its own count. Ctrl+C stops the waiting command with exit
74; the turn keeps running. During a capacity wait, Ctrl+C cancels the
pending resend; recovery collects the saved turn. Ctrl+C while a turn is
being collected or committed exits 74 with `next: emb run --recover <run> (finishes the saved
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
each listed file in the run's own worktree, and prints
`WARNING: read-only run <id> changed <path>` for each path whose state
changed. The warning and `last_reason` name absolute paths in that
worktree. Tracked changes and untracked non-ignored files are reported;
ignored build and test files produce no change warning. Edits in the source
checkout produce no change warning. Ember never commits a read-only run's
worktree.

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
delivery; a failed send keeps it. If an OpenCode 2 follow-up times out and
the delivery check cannot find its recorded message id, `--send` exits 71
with uncertain delivery, keeps the new turn recorded as `sent`, and prints
`next: emb run --status <run>`. It does not resend the message or restore
the previous turn. For other driver failures (71), or when Ctrl+C (74)
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
session and keeps an editing run's worktree and branch. It removes a clean
read-only run's detached worktree with `git worktree remove`. A detached
worktree with tracked changes or untracked non-ignored files, or a `HEAD`
different from its recorded base, is kept with its recorded path. After a
forced close ends the session, a clean detached worktree is removed even
when the turn is still recorded as sent or its delivery is uncertain.
Closing a run whose worktree is kept still succeeds and
prints a warning with the reason. For a changed worktree the warning names
`git worktree remove --force <path>` to discard it;
`--force` does not execute that command. Locked worktrees or removal
failures also produce warnings and retain the path. A worktree whose only
new files are ignored is removed. Closing an already closed run tries
detached-worktree removal again. Read-only records without
`detached` keep their source checkouts. Before ending the session Ember
records every process under the session's pane; after, it sends SIGTERM to
each one still running, then SIGKILL after 2 seconds. This ends processes a
worker's tools started in a session of their own, such as Codex's
`codex-linux-sandbox`. A process still running after SIGKILL is named on
standard error. A command waiting on a run that is
closed stops with 72, also when the driver fails or the session is missing
after the close.

`emb run` never pushes, merges, rebases or deletes a branch. Closing removes
only an owned, clean detached worktree.

### Locks

Start, `--send`, `--recover` and `--close` without `--force` hold the run's
lock, `$XDG_RUNTIME_DIR/ember/locks/<id>.lock`; a second of them on the same
run exits 75. `--screen`, `--status`, `--interrupt` and `--attach` take no
lock. Changes to `run.json` are made under the run's record lock,
`run.lock` in the run's folder. Panel close, prune and cleanup hold both
run locks while checking and removing a run's worktree. A collected writing
turn or a live session keeps the worktree. A sent turn (including uncertain
retry delivery) also keeps it unless the run is closed, read-only and has a
detached worktree; the other removal checks still apply. Failed status
alone does not allow removal.
A read-only collected turn has no commit step.

Every Git worktree add, list, remove and prune uses
`ember.paths.worktree_lock`, keyed by the resolved common git folder in
`$XDG_RUNTIME_DIR/ember/locks/worktrees-<sha256>.lock`. Lock order is flow or
panel locks, run command locks, run record locks, then the repository worktree
lock. No run, flow or panel lock is acquired while holding the worktree lock.
The repository lock is not reentrant. Dry runs also take it for worktree
queries and may create its runtime folder and lock file. Forced close ends
the session before attempting the command lock for removal; a busy lock
keeps the worktree with a warning.

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
| `launch.sh` | The command the run's tmux session runs, mode 600 |
| `turn-<n>.md` | The reply of turn `n` |
| `turn-<n>.status.json` | For a read-only run: its working folder's state before turn `n` |
| `turn-<n>.outcome.json` | Turn `n`'s final result, provider kind, ordered attempts, pending retry times, seconds and `denied_paths` |
| `turn-<n>.prompt.md` | The original prompt for capacity resends |
| `turn-<n>.retry-cancelled` | Cancellation marker for a pending capacity resend |

A run id is `<engine>-<yyyymmdd>-<hhmmss>-<6 hex digits>`, with the time in
UTC. It is also the tmux session's name.

`run.json` fields:

| Field | Type | Value |
|---|---|---|
| `id` | string | The run id |
| `engine` | string | `codex`, `opencode` or `claude` |
| `model` | string | The model |
| `effort` | string or null | The effort; null when the engine's default applies |
| `read_only` | boolean | Whether the run reviews without a branch or Ember commits |
| `detached` | boolean | Whether Ember owns a detached review worktree; false for a record without this field |
| `repo` | string | The top of the checkout `--dir` is in |
| `main` | string or null | The main checkout at start; null for a bare repository or a record without this field |
| `common` | string or null | The resolved common git folder at start; null for a record without this field |
| `worktree` | string | The worker's worktree path with symlinks resolved; older read-only records name the source checkout |
| `branch` | string or null | `ember/<engine>/<id>`; null for a read-only run |
| `base` | string or null | The commit the run started from |
| `base_branch` | string or null | The source branch at start; null for a detached checkout or a record without this field |
| `sandbox` | string | Who provides the sandbox: `codex`, `bubblewrap` or `none` |
| `sandbox_paths` | object | Launch-time hidden paths with given and resolved names, secret and protected locations, configuration folders, own worktree and home; `{}` for an older record without the field |
| `network` | boolean | Whether `worker.network` was `on` at the start |
| `capacity_retries` | integer | Capacity resend limit read at start; 2 when absent from an older record |
| `capacity_wait` | integer | Seconds before a capacity resend, read at start; 30 when absent from an older record |
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
| `parent` | object or null | The Ember command that started the run: `command` (`gapcheck`, `panel` or `flow`), `id` (the panel or flow id, null for a gap check), and `pid` and `start` (the process id and its start time in clock ticks since boot); null for `emb run` and for an older record without the field |
| `last_screen_change_at` | number or null | Epoch seconds when a wait loop last observed a changed screen; null before recorded activity or when absent from an older record |
| `last_exit` | integer or null | The exit code of the last turn or failure |
| `last_variant` | string or null | The exit variant used for action and report lines; null when absent from an older record |
| `last_start_failure` | boolean | Whether the last exit happened before the start completed; false when absent from an older record |
| `last_reason` | string or null | Its message, or the paths a read-only turn changed |
| `owner_wait` | list of strings | Pending `owner:` lines; empty when absent from an older record |
| `owner_read` | list of strings | Suggested `sandbox.read` values in owner-line order; an empty string for an action without one; empty when absent from an older record |
| `answered_at` | number or null | Epoch seconds when `--answered` ended the wait; null before an answer or when absent from an older record |
| `commits` | list of strings | The run's commits, oldest first |
| `subject` | string | The brief's first non-empty line |

Paths are absolute.

### Status

`--status RUN --json` prints the record with `session_state`: `live`, `dead`
(the pane's command ended) or `missing` (no session), `orphaned`,
`waiting_for_flow` (the parent flow id when waiting for its lead answer,
otherwise null), a `turns` array, `step` and `next`.
`--status --json` prints a list of these records, oldest first, each with
`turns`. A run with no recorded turns has `turns: []`; no runs gives `[]`.
A folder whose `run.json` cannot be read is skipped with a message on
standard error. Without `--json`, `--status RUN`
prints one `<field>: <value>` line per field except `next`, including
`step: <step>`. The `next` text goes to standard error when non-null.
`--status` prints one line per run:
`<id>  <status>  <session_state>  turn <n>  <subject>`.

`step` and `next` are derived from the existing run and claim records,
session and git history, and are not stored in `run.json`. `next` is the
complete `next:` or `owner:` line, or null. The first matching row applies:

| Condition | `step` | `next` |
|---|---|---|
| The run has a `parent` | `parent` | null |
| Pending owner actions recorded | `owner` | null |
| Closed and either read-only, without commits, with its last commit contained in its base branch, or with no branch left in its checkout; claim status is `gone` | `done` | null |
| Closed under the same conditions, with an open or unknown claim status, or no claim | `finish` | `next: emb choices --finish <claim>`; null without a claim |
| Closed with uncontained editing commits on a branch that still exists | `merge` | Verify and merge line below |
| Status `failed`, or a turn not `sent` whose `last_exit` is nonzero | `failed` | The exit line for `last_exit` and its saved variant; older records fall back to the provider kind; null for 0 or null, and the exit-1 line for an unknown code |
| Status `starting`, or turn 0 | `starting` | `next: emb run --status <run>` |
| Turn `sent` and session `live` | `running` | `next: emb run --status <run>` |
| Turn `sent`, or result collection remains unfinished | `recover` | `next: emb run --recover <run>` |
| Editing run with commits whose last commit is not contained in its base branch | `merge` | Verify and merge line below |
| Otherwise | `close` | `next: emb run --close <run>` |

The `done` step appears in text and JSON status with no `next:` line.
`session_state` still reports the tmux session's state. Runs without claim
data keep `finish`.

A run without a parent records a wait for printed `owner:` actions,
including failures and denied paths that print an owner line. Denials
without an owner line do not create a wait. While it waits, Ember omits
the carry-on `next:` line. `--status` repeats the pending `owner:` lines
and any applicable `report:` line; JSON has `"step": "owner"` and
`"next": null`. An owner wait takes precedence over a failed or closed
run's step.

The lead runs `emb run --answered <run>` only after the owner's reply.
It records the answer time, clears the wait and prints the derived next
line again. For an answered failure whose action was an `owner:` line,
this is a new start if no turn started, otherwise a send. The command
also works on a closed run. With nothing pending or a parent run, it
exits 0 and prints the current status next line when one applies; an
unknown run exits 64 with `next: emb run --status`.

A denial-only wait also ends when `sandbox.read` contains every value
the pending owner lines suggested: each `allow_path`, or the denied path
when that was suggested instead. Other actions need `--answered`.
Waiting does not refuse `--send`, `--recover`, `--close` or `emb merge`.
A later turn's result replaces the earlier turn's wait. Runs started by
flows, panels or gap checks do not record owner waits; their parent steps
and `waiting_for_flow` behavior apply.

For a `failed` or `owner` step, status prints the report line for the saved
exit and variant on standard error, including with `--json`. Older
records without a variant use the recorded provider kind when available.

Containment uses the same checks as cleanup, including `Ember-Task-Tip`
trailers, even after the worker branch is deleted. A missing base branch
or a failed containment check counts as not contained.

The verify and merge line is:

```text
next: python3 <installed skill>/scripts/verify_branch.py --repo <main or repo> --base <base_branch> --branch <branch> --worktree <worktree> [--expect-files <count>], then emb merge <branch> --into <base_branch> --verdict keep --note '<verdict>' --subject '<subject>'
```

Paths and branch names are shell-quoted. `--expect-files` is included only
on turn 1 when the run has a base commit and git can count the changed
paths between that base and its last commit. Missing branch names use
`<branch>` and `<session branch>` placeholders. The lead fills in `--note`
and `--subject`. There is no separate verification or cleanup step value.
`--status RUN --json` includes `step` and `next` and prints the next line on
standard error, as text output does. The `--status --json` list includes
`step` and `next` in each record and prints no next line. `--dry-run --json` has neither field;
`--json` remains limited to status and dry runs.

`orphaned` is true when the run records a `parent`, is `starting`, `running`
or `waiting`, and that parent process is no longer running, compared by
process id and start time, unless its parent flow is stopped for the lead.
Each orphaned run prints
`WARNING: run <id> is orphaned: emb <command> [<parent id>] ended without collecting it`
and `next: emb run --recover <id>, then emb run --close <id>` on standard
error. Its derived fields remain `step: parent` and `next: null`; the
orphan recovery line is separate. A run started by `emb run` has no parent
and is never orphaned.

A starting, running or waiting run whose parent flow has status `stopped`
has `orphaned: false`. Its text status and `WARNING:` line say
`waiting for the lead's answer to flow <id>`, with no recover-and-close
`next:` line. JSON retains the run's recorded `status`. A done, failed or
cancelled flow does not exempt its runs; neither does a missing or unreadable
flow record. Gap-check and panel runs still follow the parent-process check.

`turns` has one entry for every recorded turn number, oldest first, read
from `turn-<n>.outcome.json`:

| Field | Type | Value |
|---|---|---|
| `turn` | integer | The turn number, starting at 1 |
| `exit` | integer or null | The final exit code after commit handling; null while unfinished or absent from an older record |
| `state` | string or null | `done` or `failed`; null before the engine result is collected |
| `reason` | string or null | The engine result's reason |
| `provider_error` | boolean or null | Whether the engine reported a provider limit or outage |
| `provider_kind` | string or null | `capacity`, `usage_limit` or `outage`; null without a provider failure; omitted when absent from an older outcome |
| `attempts` | list of objects | Ordered attempts with delivery state, provider kind, result and seconds; omitted when absent from an older outcome |
| `retry_at`, `retry_clock` | number or null | Pending resend deadline and elapsed-time checkpoint in epoch seconds; omitted when absent from an older outcome |
| `signed_out` | boolean or null | Whether the engine reported a sign-in failure |
| `incomplete` | string or null | `provider error`, `sign-in failure`, `engine failure` or `no reply`; null for a complete reply |
| `seconds` | number or null | Elapsed seconds accumulated across collection and recovery; null when absent from an older record |
| `denied_paths` | list of objects or null | `{path, reason, allow_path}` entries from every attempt; `[]` when none were detected or the field is absent from an older outcome; null for an unreadable outcome |

Each turn has one result, written as unfinished when collection starts and
completed after commit handling with the final exit code. Recovery updates
that same result and accumulates its seconds. An outcome recorded without
`exit` or `seconds` returns null for those fields. A missing, unreadable or
malformed outcome file leaves its numbered entry with all other fields
null and prints a `WARNING:` naming the file on standard error; other turns
are unaffected.

Count turn results from `turns`. `last_exit` and `last_reason` describe only
the latest turn or failure. A turn with `exit: 73` and
`provider_error: true` remains in `turns` after a later turn exits 0.
Null fields are unknown; they do not count as successful turns or as
evidence that no provider error occurred.

### Exit codes

| Code | Meaning | Line |
|---|---|---|
| 0 | Success | The derived next line when applicable (see Status) |
| 1 | Unexpected error, or the settings database cannot be read | `next: emb run --status`; for the database, `owner:` |
| 64 | Usage error, unanswerable menu row, switched-off engine or invalid claim | `next: emb run --help`, or `owner:` naming the setting |
| 65 | Not a git checkout | `next:` a start inside a checkout |
| 66 | Missing file or path | `next:` a start with existing paths |
| 69 | Engine, driver, `git` or `tmux` missing, engine not signed in, Codex does not trust the repository root (also with `--dry-run`), a sandbox conflict, a kept path inside Ember's hidden folders outside its own worktree, a worktree folder inside any Git working tree or the source repository's git folder, a switched-on sandbox without a working `bwrap`, or no usable runtime folder | `owner:` naming the fix |
| 70 | Worktree creation failed, or no main checkout or commit | `next: git -C <dir> worktree prune, then ...` |
| 71 | Engine or session failed, or OpenCode 2 message delivery is uncertain | `next: emb run --status <run>`, including an uncertain follow-up whose new turn stays `sent`; for other failed sends, `next: emb run --send <run> ...` |
| 72 | Session or window unavailable, or the run was closed | `next: emb run --status <run>`, or a new start |
| 73 | Capacity retries exhausted or disabled, usage limit or outage | `next: emb run --send <run> ...` naming the kind; uncertain resend delivery asks the lead to inspect the session before sending again |
| 74 | Interrupted | `next: emb run --recover <run>`; a cancelled capacity resend collects the saved turn; uncertain delivery asks the lead to inspect the session before sending again; an interrupted send uses `next: emb run --send <run> ...` |
| 75 | Close refused while a turn runs or is collected but not committed, a busy session, another command on the run, or a session holding messages Ember did not send | `next: emb run --recover <run>` or `--status`; for messages Ember did not send, `next: ask the owner ...` |
| 76 | Branch or `HEAD` moved; nothing committed | `next: git -C <worktree> log --oneline -3` |
| 77 | `git commit was refused`, or `git add failed` | `next: emb run --send <run> --message-file <file>` sends hook output with the correction for a refused commit; a failed `git add` points to `next: emb run --recover <run>` |
| 80 | The turn ended with no reply | `next: emb run --screen <run>, then emb run --send <run> ...` |
| 84 | A start, `--send` or `--recover` otherwise succeeded but ends with a pending owner wait | Each pending `owner:` line on standard error, once per invocation |
| 124 | Stall; the turn may still be running | `next: emb run --recover <run>` |

Exit 84 (`OWNER_WAIT`) also applies to `--recover` with nothing left to
collect when an owner wait remains. Other nonzero exits keep their codes
and take precedence. The turn's recorded result, including `last_exit` and
the turn result's `exit`, is unchanged. Internal run calls by flows, gap
checks and panels do not return 84.

These `emb run` exits also print the following exact report lines.
Placeholders name the run's branch or worktree. Other exits and variants
have no report line. Report lines are not translated or configurable.

| Exit and case | Report line |
|---|---|
| 69, no turn started | `report: no worker started; do not retry the start or start another engine before the owner answers` |
| 73, `capacity` | `report: capacity; retries exhausted; offer only a retry with the same model and effort once capacity returns; offer no other model, engine, Claude subagent or Claude's own work` |
| 73, `outage` | `report: outage; offer only a retry with the same model and effort once it clears; offer no other model, engine, Claude subagent or Claude's own work` |
| 73, `usage_limit` | `report: usage_limit; a retry on the same worker needs no question; another worker or model needs the owner's answer; OpenCode may be offered` |
| 73 or 74, `uncertain` | `report: retry delivery is uncertain; do not send again before inspecting the session; then offer only a retry with the same model and effort; offer no other model, engine, Claude subagent or Claude's own work` |
| 74, default, `retry` or `complete`, editing run | `report: interrupted; partial edits may be committed on <branch>` |
| 76 | `report: branch or HEAD moved; nothing committed; recover only when the branch and HEAD are as recorded, otherwise leave the work to the owner` |
| 77, default (commit refused) | `report: the commit hook refused; the work is uncommitted in <worktree>` |
| 80 | `report: the turn ended with no reply` |

A sign-in failure during a turn prints its exit-69 owner line without the
no-worker-started report. Exit 74's `starting` and `send` variants print
no report; `uncertain` prints only the uncertain-delivery line. Read-only
runs omit the interrupted-partial-edits line. Exit 77's `add` variant
prints no report. These run report lines come from `emb run`, including
status, rather than from internal worker calls by other Ember commands.

### Provider errors and capacity retries

Provider errors have a `provider_kind`: `capacity`, `usage_limit` or
`outage`. Codex recognizes "Selected model is at capacity" as `capacity`
and "You've hit your usage limit" as `usage_limit`, ignoring case and
accepting curly apostrophes. Its other provider errors are `outage`.
Claude and both OpenCode drivers record provider errors as `outage`.
Sign-in failures are separate from provider errors.

On `capacity`, Ember resends the same turn with the same prompt, model and
effort after `provider.capacity_wait` seconds, up to
`provider.capacity_retries` times. The defaults are two resends, 30 seconds
apart; zero retries ends the turn at once. Before each retry, standard
error prints:

```text
WARNING: <engine> <model> at capacity; retry <n> of <max> in <secs>s
```

Success after a retry ends at exit 0. Exhausted capacity retries,
`usage_limit` and `outage` end at exit 73; the latter two are not retried.
The `next:` line names the kind. A run reads both settings once at start
and saves them for every turn and recovery. Changes apply to runs started
after the change.

The ordered `attempts` list in the turn outcome records each attempt's
`id`, `sent`, `sending`, `provider_kind`, `result` (`done`, `failed` or null)
and `seconds`. The retry budget and pending attempt are saved before a
resend. Recovery keeps that budget, observes a confirmed delivery and
continues a pending wait. When delivery cannot be confirmed, it stops
with a `next:` line to inspect the session and ask the lead before sending
again. `--interrupt` cancels a pending resend. Edits from earlier attempts
stay in the worktree; the turn commits once after its last attempt.
Uncertain delivery leaves edits uncommitted. Turn seconds include waits;
attempt seconds do not. Both totals persist across recovery.

### Engine drivers

| | Codex | OpenCode | Claude |
|---|---|---|---|
| Starts | Codex's interactive session in the pane: `codex -C <worktree> -m <model> -c check_for_update_on_startup=false [-c model_reasoning_effort=<effort>] -a never --search`, with Ember's permission profile in `-c` options, or `--sandbox danger-full-access` when the sandbox is off | OpenCode's interactive view in the pane, with its built-in server on a free port of `127.0.0.1` protected by a password, the stored model id passed unchanged to `--model` and the agent `ember-worker`, inside `bwrap` when the sandbox is on | A program of Ember's in the pane that runs one `claude -p --output-format stream-json` per turn in the worktree, inside `bwrap` when the sandbox is on, and shows its progress; the program itself runs outside the sandbox |
| Brief | Pasted once Codex's composer shows `? for shortcuts` | Sent through the server's session API once the server answers and the view has drawn its screen | Written as turn 1's request before the pane starts; given to the CLI on standard input |
| Turn end and reply | Read from Codex's rollout file: `task_complete` with its last message, or `turn_aborted`; an `error` event during the turn fails it | Read from the session status and messages: the turn's last assistant message, once the session is idle; its text parts are the reply | The CLI's exit; the reply is the assistant text after the turn's last tool result |
| Later messages | Pasted | Sent through the session API | A new `claude -p` that resumes the previous turn's CLI session |
| Read-only run | Ember's profile extends Codex's `:workspace` profile for the writable detached worktree; legacy source checkouts use `:read-only` | The agent may not edit, write or patch files or reach folders outside the checkout other than the folders of its read paths; the detached worktree is mounted writable | The tools are `Read`, `Glob` and `Grep`, plus limited `Bash` for detached runs inside `bubblewrap` as described below; the detached worktree is mounted writable |
| Sandbox provider | `codex` | `bubblewrap` | `bubblewrap` |
| `--interrupt` | Presses Escape in the session | Aborts the turn through the session API | Sends `SIGINT` to the process group of the turn's CLI, as Ctrl+C in the pane does; the program takes the next turn |
| Messages Ember did not send | Each `user_message` in the rollout that does not match, in order, a message Ember pasted | Each user message whose id Ember did not send | None; the pane takes no typed messages |

OpenCode requests zero data retention through OpenRouter's provider options
only for `openrouter/` models. Other integrations receive no OpenRouter
options. The driver's payment-error note names the serving integration:
`(<Integration> credit is exhausted)`. The name comes from the model list's
integration metadata, with the integration id as fallback.

The OpenCode column describes OpenCode 1. When `opencode --version` reports
version 2 or later (`opencode v2.2.5`), Ember uses its OpenCode 2 driver. The
pane runs, inside the same sandbox, a shell script that starts a private
`opencode serve --hostname 127.0.0.1 --port <free port>` protected by
`OPENCODE_SERVER_PASSWORD`, waits with `python3` until Ember's session
answers, then runs `opencode --server <url> --session <id>`. Ember chooses
the session id (`ses_` and 26 hex characters) and, once the server answers,
creates the session through `/api/session` with the agent `ember-worker`
and the model as `{id, providerID, variant: <effort>}`, splitting the full
model id at its first slash into `providerID` and `id`. Messages
go to `/api/session/<id>/prompt`; a turn has ended when the session is not in
`/api/session/active` and an `idle` message follows Ember's message, with
outcome `succeeded` (the reply is the newest assistant text) or `failed`
(the reason is its error message). `--interrupt` posts
`/api/session/<id>/interrupt`. The agent's permissions deny `question` and
Ember's denied git commands for `shell`; a read-only run denies `edit` and
allows `external_directory` only for the folders of its read paths. The
sandbox keeps the folder of the `opencode` executable, which the script
starts. OpenCode 2 keeps its credentials in its own store; the sign-in check
runs `opencode auth list --standalone`, so it never starts OpenCode 2's
shared background service.

Every OpenCode 2 prompt POST has a 120-second timeout, including the first
message, follow-ups and capacity resends. Status requests keep their
2-second timeout. Ember records the message id before sending. If the
prompt request times out, a separate delivery check searches the session's
message history for that id for up to 10 seconds. Finding it continues the
turn. An absent id or unreadable history reports uncertain delivery without
automatically resending. A missing recorded message id is not inferred.
When a start is declared failed, Ember stops its OpenCode server and client
and retains the run record and worktree. `emb run --close` and
`emb panel --close` can then close the failed start without `--force`.

Claude's turns use `--restricted`, `--safe-mode` and `--strict-mcp-config`;
the run's read paths are added with `--add-dir` and edits to them are
refused. While a tool call or a model message runs, Claude's pane prints a
line such as `· running Bash for 3 min` at least once a minute.

A read-only Claude CLI run gets `Bash` only when its worktree is detached
and its recorded sandbox is `bubblewrap`. This includes task and
whole-flow reviewers, gap check reviewers, panel members and
`emb run --read-only` when those conditions hold. Legacy runs using the
source checkout and runs with the sandbox off keep only `Read`, `Glob`
and `Grep`.

The shell allows these command prefixes with arguments:

- `git diff`, `git log`, `git show`, `git status`, `git blame`, `git grep`,
  `git ls-files`, `git rev-parse`;
- `make check`, `make test`;
- `pytest`, `uv run pytest`.

`--tools` includes `Bash`; `--allowedTools` contains the read tools and
one `Bash(<command>:*)` rule per listed prefix. Other commands have no
allow rule, including `git -C <dir> ...`. Claude Code checks the commands
of a compound shell command separately. Permission prompts are disabled.
Claude Code runs its own read-only commands, such as `ls`, `cat` and
`echo`, without an allow rule. Commands that create or change a file,
such as `touch`, are refused.
The git write verbs remain refused as for editing runs: `add`, `commit`,
`push`, `merge`, `rebase`, `reset`, `checkout`, `switch`, `branch`,
`cherry-pick`, `tag`, `stash`, `restore`, `rm`, `mv`, `am`, `apply`, `pull`
and `fetch`.

Arguments after an allowed prefix are not filtered. This includes git
`--output`, `git grep -O` and `--open-files-in-pager`, make `-f`, `--file`,
`-C`, `--eval` and `NAME=value` arguments, and pytest `--junitxml`,
`--basetemp` and `-p`. Commands run inside Ember's sandbox in the writable
detached worktree; the read-only change warnings still apply.

For read-only turns, Ember sets `PYTHONDONTWRITEBYTECODE=1`, appends
`-p no:cacheprovider` to `PYTEST_ADDOPTS`, and sets `RUFF_CACHE_DIR` to
`$XDG_CACHE_HOME/ember/worker/claude/<run>/ruff`, outside the worktree.
It sets `UV_FROZEN=1` and leaves uv's project environment location
unchanged. These values travel with each turn's request to the Claude
host. Editing runs keep their existing tools and environment.

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
| `--plan FILE` | Required except with `--answer`: the plan to review; an empty or blank file exits 64 |
| `--dir DIR` | A folder in the checkout; defaults to the current folder. The run reads its own detached worktree at that checkout's `HEAD` |
| `--engine ENGINE` | Use `codex`, `opencode` or `claude`; otherwise use the `gapcheck.reviewer` row |
| `--model MODEL` | Use this model, whether or not shortlisted; requires `--engine` |
| `--effort EFFORT` | Use this effort; requires `--engine` |
| `--claim ID` | Read choices with this claim and pass it to the run |
| `--read PATH` | Add a readable path to the run; repeatable; an empty or whitespace-only UTF-8 file exits 64. Also accepted with `--brief`, where it covers citations and lists sources. Citations in the plan that neither these paths nor the `sandbox.read` setting supply are warned about on standard error |
| `--window` | Open a terminal window on the run's session |
| `--stall MINUTES` | Stop waiting after this many minutes without a screen change; default 10 |
| `--json` | Print the result as JSON |
| `--brief` | Print the brief and start nothing; accepts `--dir` and `--read` |
| `--reply FILE` | Read a reply from another worker and start nothing; accepts `--json` |
| `--previous FILE` | Read an earlier complete result; the reviewer gives a disposition for every active and settled gap |
| `--confirmed` | Run the check although `gapcheck.when` skips the plan or leaves it to the owner |
| `--answer FILE` | Validate a result and record an answer on its active or settled gap |
| `--id GAP`, `--text ANSWER` | Required with `--answer`: the gap id and a nonblank answer |
| `--help` | Print the command's options |

`--brief` and `--reply` cannot be combined. Neither accepts `--engine`,
`--model`, `--effort`, `--claim`, `--window` or `--stall`.
`--brief` accepts `--read` but no `--json`; `--reply` accepts neither
`--read` nor `--dir`. `--previous`
works on the direct run, on `--brief` and on `--reply`.

After option validation and the empty-plan check, each `--read` path that
is a regular file (following symlinks) is read as UTF-8. Empty or
whitespace-only text (`str.strip()`) exits 64 before reading the previous
result or applying the skip or ask decision, including with `--brief`.
Standard error prints `emb gapcheck: the read file <path> is empty` for
each empty file in argument order, then one `next: emb gapcheck --help`.
`--reply --read` is refused by option validation before this check;
`--answer` does not perform it. The check skips folders and their contents,
FIFOs, devices, missing paths and files that cannot be read or decoded.
Other path checks still apply. It does not check `sandbox.read` paths.

`--answer` requires `--id` and `--text`, accepts `--json`, and accepts no
other mode or run options. It starts no run and reads no settings. Without
`--json`, it prints `recorded answer for <id> in <path>` on standard error;
with it, standard output holds the updated result. It reloads and validates
the result under an adjacent `<result>.lock` and replaces it atomically.
The lock file remains in place. Closed gap ids and unknown ids are refused.

After these input checks, `gapcheck.when` decides the plan unless
`--confirmed` is given (with `--reply` it is not applied). A plan it skips
(`off`, or fewer steps than its number) starts no run: the command prints
`gap check skipped: <reason>`, or JSON `{"plan", "status": "skipped",
"reason"}`, and exits 0. With `ask` it prints the same with status `ask`,
adds `owner: ask whether to run a gap check on this plan; to run it: …
--confirmed` on standard error and exits 0. With `--brief`, either line
goes to standard error and no brief is printed. A checked plan's result
has `status` `run`, `ask` (the `gapcheck.ask` value) and, on every gap,
`ask_owner`: whether its harm is at or above `gapcheck.ask`. Without
readable settings every gap has `ask_owner` true and `ask` is `all`. In
text, a gap whose default is taken reads `<harm> (default taken): …`.

Without `--model`, the `gapcheck.reviewer` row must be `set` (while `same`,
the `single.reviewer` row); its model and effort are the run's. When
`--engine` is given it must match the row. A Claude row that runs as a subagent exits
64 with the `--brief` and `--reply` route. With `--engine` and `--model`,
the row may be `ask`. The run checks that the engine is on and connected
and that its driver accepts the effort. A `gone` or `unknown` claim stops
before the reviewer row is inspected.

Checkout discovery and the run share an environment without `GIT_DIR`,
`GIT_WORK_TREE`, `GIT_COMMON_DIR`, `GIT_INDEX_FILE`, `GIT_OBJECT_DIRECTORY`,
`GIT_NAMESPACE` or `GIT_PREFIX`.

The brief contains the whole plan with line numbers and tells the reviewer
to read the repository in its own working folder, without naming the source
checkout path. Uncommitted plan text is included in the brief; repository
files come from the detached worktree's commit. A successful close removes
the clean detached worktree. Its temporary brief file is
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
For each high gap, it asks for the underlying cause and the other places
it affects in `why`, using the existing text fields.

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
    },
    "dispositions": {
      "type": "array",
      "items": {"$ref": "#/$defs/disposition"},
      "description": "With --previous, one entry for every active and settled earlier gap; closed history needs no entry."
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
        "default": {"type": "string", "minLength": 1},
        "earlier": {"type": "string", "minLength": 1, "description": "The id of the earlier gap this gap repeats or whose answer the plan change invalidates; left out for a new gap."}
      }
    },
    "disposition": {
      "type": "object",
      "additionalProperties": false,
      "required": ["id", "status", "reason"],
      "properties": {
        "id": {"type": "string", "minLength": 1, "pattern": "^g[1-9][0-9]*$"},
        "status": {"enum": ["settled", "reopened", "closed"]},
        "reason": {"type": "string", "minLength": 1}
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
No object may have keys beyond those in the schema. A gap's optional
`earlier` names the id of a gap in the `--previous` result that it repeats
or whose answer the plan change invalidates.

The reader tries the whole reply as JSON, then the last backtick code block
(unlabelled or labelled `json`), then the span from the
first `{` to the last `}`. The first candidate that parses as an object is
used. If it fails the schema, each independently valid step entry is kept;
an entry with an invalid gap is discarded in full.

Duplicate step entries are joined. Covered steps appear in plan order.
Gaps within each step and in `ranked` sort by `high`, `medium`, `low`,
keeping reply order within each harm. Each gap gets the id `g<n>`, numbered
in `steps` order from 1, or, with `--previous`, from the
previous result's `next_id`. Each result records the next unused number in
`next_id`, including when no new gaps are raised. `counts` counts only
gaps for known steps. `uncovered_steps` follows plan order; `unknown_steps`
lists unknown numbers once, in numeric order. Unknown steps are excluded from `steps`,
`ranked` and `counts`.

`parse_error` is null when the reply matches the schema and covers exactly
the expected steps and earlier-gap dispositions. Otherwise it uses these
message forms:

- `the reply is not a JSON object`
- `the reply does not match the schema at <path>: <message>`
- `the reply misses step <numbers>`
- `the reply names step <numbers>, which the plan does not have`
- `the run produced no reply` when a run has no reply file
- `the reply names earlier gap <ids>, which the previous result does not have as an active or settled gap`
- `the reply names earlier gap <ids>, but the check has no previous result`
- `the reply misses earlier gap <ids>`
- `<path>: unknown earlier id <id>`
- `<path>: duplicate disposition for <id>`
- `<path>: unanswered gap <id> cannot be settled`
- `reopened gap <id> needs exactly one new gap naming it`
- `gap <id> is <status> but a new gap names it as earlier`

A schema path starts at `$`, with fields and zero-based array indices,
for example `$.steps[2].gaps[0].harm`. Schema messages are `expected an
object`, `missing required key '<key>'`, `unexpected key '<key>'`,
`expected an array`, `expected an integer`, `expected an integer at least
1`, `expected a string`, `expected a non-empty string`, or `expected one
of high, medium, low` (or `settled, reopened, closed` for a disposition).
The first schema error is reported: object type,
required keys in schema order, extra keys in alphabetical order, then
fields and list entries in order. Step and gap fields follow their schema
order. Schema, missing-step, unknown-step and earlier-gap problems are joined
with `; ` in that order; number and id lists use `, `. A reply with no JSON object
reports only that error and leaves every step uncovered.

### Earlier results

`--previous FILE` reads an earlier complete `--json` result. The packaged
`ember/data/gapcheck-result.schema.json` defines the result. Validation checks
required fields, rejects unknown fields and checks that ids are unique
across `steps`, `settled` and `closed`, `ranked` refers to every active gap
once with its step and harm, `counts` counts active gaps, and `next_id`
exceeds every stored or referenced id. Incomplete checks cannot be reused.

Record owner answers and defaults with:

```bash
emb gapcheck --answer <result> --id <gap id> --text '<answer>'
```

An answer is stored once on its gap in `steps` or `settled`; `ranked` holds
only id, step and harm. Missing or null `answer` means unanswered. Results
without ids or answers are accepted: ids are assigned in file order and
missing answers remain unanswered. Legacy `ranked` copies must match
`steps`. Newly printed results use the id-reference shape.

The brief lists active and settled earlier gaps with their ids, original
step numbers and titles, gap text and answers, or `Unanswered.`. Answers
are identified context, not attached to new gaps by position or step.
With `--previous`, the reply supplies a `dispositions` entry
`{id, status, reason}` for every active and settled earlier gap:

- `settled`: the gap has an answer that remains valid. No new gap names it.
- `reopened`: the concern remains open or the change invalidates its answer.
  Exactly one new gap names its id in `earlier` and explains what reopens it.
- `closed`: the concern no longer applies. The reason is kept with the gap.

An unanswered gap must be reopened or closed. Missing, duplicate or unknown
dispositions and invalid reopened links leave the check incomplete (exit
81). Coverage of every current plan step remains required. Without
`--previous`, the brief asks for neither `earlier` nor `dispositions`.

Settled gaps carry into `settled` in each later result; closed gaps carry
with their reasons in `closed`. Neither is counted as active. Closed history
needs no disposition. A closed concern that matters again is raised as a
new gap without `earlier`.

When the earlier result's `plan` differs from the current plan path, Ember
prints a `WARNING:` naming both. The new result records `plan` and
`previous_plan`. The skill saves results as `gapcheck-<plan stem>-<n>.json`.

### Output

The default text has a block for each covered step:

```text
step 1: <title>
  g3 high: <gap>
    why: <reason>
    default: <proposed behaviour>
    earlier: g1, answered: <answer>
step 2: <title>
  no gaps
settled from <previous file>:
  g2 medium, earlier step 2: <gap>
    answer: <answer>
counts: high 1, medium 0, low 0
uncovered steps: none
unknown steps: none
```

The `earlier:` line appears for a gap that names an earlier gap, with
`unanswered` when that gap has no answer. The `settled from` block appears
with `--previous`, and reads `  none` when nothing is settled. A nonempty
`closed` list prints a `closed gaps:` block with each id, gap and reason.

`--json` prints an object with these fields:

| Field | Value |
|---|---|
| `plan` | Absolute plan path |
| `plan_steps` | `tasks`, `numbered`, `headings` or `whole` |
| `failure_model` | Whether the plan has a failure model |
| `previous` | Absolute path of the `--previous` result, or null |
| `previous_plan` | Plan path recorded in the earlier result, or null |
| `next_id` | Next unused gap number; repeat checks continue from this number |
| `dispositions` | Earlier active and settled gap ids, each with `status` and `reason` |
| `reviewer` | `engine`, `model`, `effort` and `source`; values come from the run record, and `source` is `option` when `--engine` was given, otherwise `menu` |
| `run` | `id`, `reply` (absolute reply path or null), `exit` (the start's exit), `closed` and `close_error` (null or a reason) |
| `steps` | Covered steps, each with `step`, `title` and `gaps`; each gap has `id`, `gap`, `harm`, `why`, `default`, `earlier` (an earlier gap id or null), `answer` (null until the lead records one) and `ask_owner` |
| `settled` | Earlier gaps with a `settled` disposition, their original step and title, gap fields and answer |
| `closed` | Closed history with original gap fields, step, title, answer if present, and closure reason |
| `status`, `ask` | `run`, and the applied owner-question threshold (`high`, `medium` or `all`) |
| `ranked` | Each gap's `id`, `step` and `harm`, ranked high, medium, low |
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
| 0 | The reply covers every step and earlier gap, the brief was printed, an answer was recorded, or settings skip or ask | A failed close prints a warning |
| 1 | Settings database or run record cannot be read, or checkout discovery times out | `owner: repair or remove the settings database the message names`, `next: emb run --status <run>` or a new check |
| 64 | Usage error, empty or whitespace-only plan or `--read` file, invalid claim, reviewer row not `set`, Claude subagent row, or a run start check | `next: emb gapcheck --help` for usage or empty files, the claim's `emb choices` line, `owner: name the reviewer for this gap check, or set Gap check reviewer on the Team screen`, the `--brief`/`--reply` route, or the run's line |
| 65 | `--dir` is not in a git checkout, or the `--previous`/`--answer` file is not a valid complete result | `next: emb gapcheck --plan <plan> --dir <a folder in the checkout>`, or `next: correct <file> to the emb gapcheck result format, with answers on their gaps in steps or settled` |
| 66 | Plan, reply, `--previous` or `--answer` file cannot be read, or `--dir` does not exist or is not a folder | `next: ls -l <path>` |
| 69 | Git is unavailable, the runtime folder cannot be used, or the run exits 69 | `owner: install git`, the run's runtime-folder instruction, or the run's line |
| 74 | Interrupted by `SIGINT`, `SIGTERM` or `SIGHUP`, or an answer cannot be written | The run's recovery line when a run exists, then a gap check action; `next: ls -l <result>` for an answer write failure |
| 81 | Invalid reply, incomplete earlier-gap dispositions or links, missing or unknown steps, or a successful run without a reply file | `next: emb gapcheck --plan <plan> --reply <reply>` after fixing the reply, or a new check |
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
| `--previous FILE` | With `--dry-run` only: pass this earlier result to the printed gap check command |
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
`--worker` and `--drop`. `--previous` without `--dry-run` exits 64 with
`next: emb flow --help`. Other combinations exit 64 before reading files.

Validation reads the plan, checks its read files for empty text, then
checks that its directory exists and belongs to
a checkout, resolves the base commit, reads the claim and settings, then
resolves and checks both workers. All plan problems print together. A
`gone` or `unknown` claim exits 64 even with explicit workers. The command
resolves omitted effort from a matching set menu row, then the driver's
default. Stored workers include `effort_frozen: true`; `null` effort denotes
an engine default. Replacement-worker answers carry the same marker.

With `--dry-run` or `--start`, immediately after loading the plan and before
directory or git checks, each plan `read` path that is a regular file
(following symlinks) is read as UTF-8. Empty or whitespace-only text
(`str.strip()`) exits 64. Standard error prints
`emb flow: the read file <path> is empty` for each empty file in plan order,
then one `next: emb flow --help`. The check skips folders and their
contents, FIFOs, devices, missing paths and files that cannot be read or
decoded. Other path checks still apply. It does not check
`sandbox.read` paths. Loading saved plans and resuming flows do not perform
this check.

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
| `rounds` | `flow.review_rounds` minus one | Integer from 0 to 3; fix rounds per task |
| `parallel` | `flow.parallel` (default `4`) | Integer from 1 to 8; maximum concurrent jobs within a wave |
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
| `subject` | Required | Non-blank string without CR or LF, at most 72 characters; the task squash commit subject and the writer brief's first line |
| `task` | Required | Non-blank string |
| `files` | Required | Non-empty list of unique relative POSIX paths; no leading slash, empty component, `.` component, `..` component or NUL |
| `after` | `[]` | List of ids of other tasks; every id exists, no self dependency and no cycle |
| `review` | `""` | String |

Recorded flows retain their resolved task subjects when loading their saved
plans. New plans require an explicit subject for every task.

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
- `<id>: no test file in its files`, when no file is under `test`, `tests`
  or `__tests__`, starts with `test_`, or has a name matching `*_test.*`,
  `*.test.*` or `*.spec.*`
- `<where> cites <reference>, which no read path supplies`, once per
  reference at its first citation, where `<where>` is `the context`, `the
  failure model` or `task <id>` (its subject, task text and review focus).
  A reference is a URL, `#<n>` or `<owner>/<repo>#<n>`. The read paths are
  the `sandbox.read` setting and the plan's `read`. A read path that is a
  file, or a file directly inside a read folder, supplies a reference when
  its first 20 lines hold the same whole reference (`#930` does not supply
  `#93`, and a longer URL does not supply its prefix), or, for `#<n>` and
  `<owner>/<repo>#<n>`, when its file name holds the number as its own
  token, such as `issue-93.md`. The warning applies whatever the engines'
  sandbox switches say. The printed gap check command passes each of the
  plan's `read` paths as `--read`.

A failed or timed-out folder probe emits no folder warning. It does not
change plan validation. Git probes omit checkout override variables and
`EMBER_FLOW_RUNNER` from their environment.

### Workers

Each role resolves in this order: its `--writer` or `--reviewer` option,
its plan object, then the `flow.writer` or `flow.reviewer` row in the
choices reply, which is the `single.writer` or `single.reviewer` row while
`same`. Options use `<engine>:<model>[@<effort>]`; model names may contain
colons and slashes. The result records `engine`, `model`, `effort` and
`source` (`option`, `plan`, `menu`, or `answer` for a lead answer).

Menu resolution requires a readable settings source and a row in state
`set`. A Claude row set to run as a subagent is refused: a flow runs CLI
workers. These refusals give code 64 with an `owner:` line naming the
role's explicit option and Team screen. Explicit options and plan workers
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

Accepted heads squash one at a time through `emb merge`'s implementation,
one commit per task under its plan `subject`. The commit records the
accepted review's verdict in `Ember-Verdict` (`keep` when there is no review)
and the approved worker tip in `Ember-Task-Tip`. Distinct non-Ember worker
trailers are kept as described under `emb merge`.
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

A task's capacity error uses its run's saved retry settings; other tasks
keep running. A capacity exit 73 stops that task without halting new runs.
A `usage_limit` or `outage` exit 73 stops the task and halts new runs, as
does a provider error without a recorded kind. Active jobs finish; queued
writers remain pending. The flow stops with
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
using the same containment rules as `emb merge`. A reachable commit's
`Ember-Task-Tip` identifies the task squash commit. Recovered merges set
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

Each flow review is a fresh read-only run in its own detached worktree at
the writer's committed `HEAD`. Uncommitted edits in the writer's worktree
produce start warnings and are absent from the review tree. The review run
is closed without `--force` after its reply is read, removing its clean
detached worktree. Changed worktrees and their recorded paths are kept
with a warning naming the explicit discard command. A refused close leaves
the run open and is reported by the runner.

The writer brief starts with the task's commit subject and preserves the
task text as written. It lists the task's files, the other tasks' files in
the wave, a `## Sources to read` section listing the read files of the
`sandbox.read` setting and the plan's `read` when there are any, context and failure model when present, limits, the git rule
(git commands that only read are allowed; Ember makes the commit), checks
and closing report headings. Lead corrections also
preserve their text as written.

The review brief starts with `Review of task <id>, round <n>, in flow
<flow id>: a read-only review; change no file.` It includes the task and
subject, declared files and the other tasks' files, the `## Sources to read`
section when the plan has read files, review focus, context,
failure model, the base and head for `git diff` and `git log`, changed and
outside files, and up to 20,000 characters of the writer's last report.
The `Changed outside the declared files` section lists Ember's recorded
`outside_files`, independently of the writer's report.

After a lead send answer is applied, later reviewer briefs for that task
include its correction text under `Lead corrections`, in answer order,
labelled `Answer <n>`. They describe these as settled decisions that the
reviewer does not reopen. Any file a correction names counts as in scope
for that review, even when it remains in `outside_files`. Ember does not
parse correction text or remove paths from `outside_files`. This history
is included only in that task's reviewer briefs, not in whole-flow review
briefs or replacement writer briefs.

The brief lists merged sibling tasks whose recorded squash commits are not
ancestors of the task's reviewed head, with each task's id, subject and
commit. It says their changes are on the flow branch and absent from the
task branch. With no such siblings, it omits the list. The brief names no
worktree path.
It includes the review bar from `skill/SKILL.md` and the whole reply schema
from `ember/data/review-reply.schema.json` (`ember/review-reply/1`). For each
`must`, it asks for the underlying cause and the other places it affects in
`problem`, using the existing text fields. Pasted
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
| `conflict` | `accept`, `worker`, `drop` |
| `pending`, `blocked` | `drop` |
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

A merge conflict leaves the task `conflict` with its approved tip in
`merge_tip`. Its reason names the hand merge and how it is recorded. After
the lead merges that tip into the flow branch, the runner records the task
as `merged` whenever it runs (start and resume, before a pending check,
after each answer and before each wave), with `merge_commit` the squash
commit that records the tip, else the oldest flow branch commit that holds
it. `--accept` on a `conflict` task takes only `merge_tip`: `--commit` with
another commit exits 64. It exits 75 with a `next:` line naming the hand
merge when the flow branch does not hold `merge_tip`, and the runner
refuses it again for either reason. "Holds" is the
containment test cleanup uses: the commit is an ancestor of the flow branch,
or merging it changes nothing.

`--review` collects any running writer turn before a fresh review budget.
An optional reviewer applies to this task from then on. `--worker` closes
the old writer without force, retains its branch, and starts a new writer
from the flow tip. `--drop` closes the writer without force and retains its
branch. A pending task that has not started is dropped without starting a
writer. Dropped tasks leave their dependants blocked. Retained branches
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
`checking` or `stopped`. It stops the runner and waits up to 60 seconds from
writing `cancel.json` for its lifetime lock to be released, then exits 75
if the deadline passes. A running flow check ends through the runner's
signal stop path. Writer and reviewer turns keep running. Cancellation
closes no task run and deletes no branch or worktree.

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
mode. Cancellation exits 75 if the 60-second deadline passes, leaves
`cancel.json` in place, and prints `emb flow: the runner for <id> is still
running after 60 seconds` and `next: emb flow --status <id>` on standard
error with nothing on standard output. A later `--cancel` waits again with
the same request.

`--resume` restarts a cancelled flow from its recorded task states. Startup
clears the cancel request; the runner clears `reason` and `ended_at` when
it begins. `--lead` is refused with exit 75 on a cancelled flow, with
`next: emb flow --resume <id>`. Cancelled flows count as ended.

Cancellation output contains the flow id, `cancelled`, the reason, the flow
branch, each retained task branch and each open run's task, role and id.
Text prints one line per item; the `reason` line is left out when no reason
was supplied. `--json` prints:

| Field | Value |
|---|---|
| `flow`, `status`, `reason` | Flow id, `cancelled`, and the supplied reason or null |
| `branch` | The flow branch |
| `task_branches` | Objects with `task` and `branch`; includes abandoned branches, with a null task when the record no longer identifies it |
| `open_runs` | Objects with `task`, `role` (`writer` or `reviewer`) and `run` |

Run ids come from the flow record's current writers and reviewers, previous
reviews, cleanup entries and pending writer replacement or drop effects. Each run's stored
record is read; `starting`, `running` and `waiting` count as open. A run
whose record does not load is left out of the listing, and a `WARNING:` line
on standard error names its task, role and id. Successful
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
| `common` | Resolved common git folder at start; null for a record without this field |
| `base`, `tip` | Resolved base commit and current flow branch tip |
| `base_branch` | The local base branch at start, or null for a commit base or a record without this field |
| `branch`, `worktree` | `ember/flow/<id>` and `worktrees/<id>` in Ember's data folder, with symlinks resolved; the flow branch is created at this path. A record made before Ember 0.13.0 names `<main>/.ember/worktrees/<id>` |
| `writer`, `reviewer` | Stored worker objects: `engine`, `model`, `effort`, `source` |
| `rounds`, `parallel`, `check_timeout` | Plan limits; `parallel` is `flow.parallel` when the plan sets none |
| `check`, `trailers`, `read` | Plan argv, commit trailers and readable paths; lists |
| `stall_minutes` | Stall interval: `--stall`, else `flow.stall` (default 10) |
| `whole_review` | Whether `flow.whole_review` was on at start; the skill then runs one whole-flow review before merging |
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
| `reviews` | Entries with `round` (fix rounds), `review_round` (one-based round from the review brief), `run`, `verdict`, `must`, `should`, `lead`, `at`; verdict is `pass`, `fix` or `unreadable`; older entries may omit `review_round` |
| `findings` | Latest findings and retained earlier `should` and `lead` findings; each records `round` (one-based round from the review brief) and `run` (reviewer run id); older findings may omit them |
| `changed_files`, `outside_files` | Last turn's changed paths and paths outside its declarations |
| `entry` | Next operation: `write`, `send`, `review` or null |
| `answer` | Answer number being applied, or null |
| `corrections` | Private paths of applied send corrections in answer order; defaults to `[]` when missing |
| `merge_tip`, `merge_commit` | Approved worker tip and resulting squash commit, or null; containment may succeed without a new commit |
| `cleanup` | Cleanup result objects from the merge |
| `warnings` | Merge warnings when present, including a worker breaking change without `!` in the task subject |
| `denied_paths` | Union of worker denials, each with `path`, `reason`, `allow_path`, `run` and `turn`; empty when none |

Record loading checks required field types and status values. Additional
fields are retained for runner recovery metadata. Each task starts with
independent empty lists.

Events have `seq` starting at 1, `at` in epoch seconds, and `kind`:

| Kind | Fields |
|---|---|
| `flow` | `status`, `reason` |
| `task` | `task`, `status`, `reason`; a merged task also records `cleanup` and `warnings` |
| `lead_stop` | `task` or null, `status`, `reason`, `choices`; `provider_kind` from the run's last outcome when present |
| `denied_paths` | `task`, `role`, `denied_paths`; each new denial includes `path`, `reason`, `allow_path`, `run` and `turn` |
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
error. `--dry-run` text prints one announce line for the writer and one
for the reviewer on standard output, in the `emb run --dry-run` form:

```text
Team, Flow writer: codex gpt-6.1-sol · high (flow.writer)
Team, Flow reviewer: claude opus · high (flow.reviewer)
```

A flow row set to Same as names `Single tasks Writer` or `Single tasks
Reviewer` and the corresponding `single.writer` or `single.reviewer`
source. Explicit worker options and plan workers use their source label
in place of the screen and `writer` or `reviewer` as the row, for example
`option, writer: claude opus · high (option)` or
`plan, reviewer: claude opus · default (plan)`. Unset effort prints as
`default`; differing model and effort sources are labelled separately as
in single-run announcements. JSON retains the `writer` and `reviewer`
objects with `engine`, `model`, `effort` and `source`; it includes no
announce lines or additional announcement fields.

The text then prints the base commit, `rounds <n> fix rounds, at most <n+1> reviews
per task (<source>)`, the check as shell words (or `none`), numbered waves
with each task's declared files, `parallel <n> (<source>), stall <n> min,
whole-flow review on|off`, and the gap check line: `gap check: <command>`,
`gap check: skipped, <reason>`, or `gap check: ask the owner, then
<command>`. The rounds
source is `plan` when the plan sets `rounds`, otherwise `setting` (the
owner's `flow.review_rounds` minus one). Warnings print as
`WARNING: <text>`. JSON has `plan`, `dir`, `base`, `writer`, `reviewer`,
`check`, `rounds`, `rounds_source`, `parallel`, `parallel_source` (`plan`
or `setting`), `stall_minutes`, `whole_review`, `waves` (arrays of task
ids), `warnings`, `gapcheck` (the shell command, or null when skipped),
`gapcheck_status` (`run`, `skipped` or `ask`) and `gapcheck_reason`. The
decision follows `gapcheck.when` and the plan's task count. The command
names the flow's reviewer with `--engine`, `--model` and `--effort` when
that reviewer came from `--reviewer` or the plan; otherwise it names none
and `emb gapcheck` uses the `gapcheck.reviewer` row. With `ask` it carries
`--confirmed`. It always carries `--json`, and carries `--previous FILE`
when the dry run receives it. Empty lists and zero finding counts remain
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
| `report` | List of report lines, also printed on standard error after `next`; empty when none apply |

Each task report has `id`, `status`, `reason`, `round`, `writer_run`,
`branch`, `worktree`, `merge_commit`, `outside_files`, `denied_paths`, `cleanup` and
`choices` (the available `emb flow --lead` command forms). `findings`
counts `must`, `should` and `lead` across latest and retained findings;
`finding_details` holds the finding objects, including each finding's
one-based review `round` and reviewer `run`. Missing round or run values
are null, not inferred. `latest_review` holds `round`, `run`, `verdict` and
`findings` (severity counts from that review alone), or null with no review.
Missing fields in older review entries are null.
Cleanup results retain the branch and reason when cleanup keeps
work; the task report includes its worktree.

Text reports print the flow state, one line per task with its reason,
latest review, finding objects, choices, retained cleanup details and stop events.
Retained abandoned branches print separately.

At a lead stop the action says to report every stop and choice to the
owner, then watch again if a runner is live; otherwise an answer starts
the runner. At a successful end it says to review the flow branch and run
`emb merge <flow branch> --into <your branch> --verdict keep --note '<verdict line>' --dir <dir>`.
An end with unmerged tasks first says to report them to the owner. A failed
pending check says to fix the flow branch with an `emb run` and merge,
then resume. Other failures name `--status`; runner loss names `--resume`.

The following report lines follow the action line on standard error,
including with `--watch --json`:

| Case | Report line |
|---|---|
| Lead stop, no live runner | `report: lead stop on <task>; no runner is live and the owner's answer starts it; list every printed choice and recommend one` |
| Lead stop, live runner | `report: lead stop on <task>; the runner is still live for the other tasks; list every printed choice and recommend one` |
| Lead stop after capacity | `report: capacity on <task>; recommend only the same-worker retry` |
| Exit 82 | `report: tasks unmerged; report every task's status and reason; leave the unmerged work and the flow branch to the owner` |
| Exit 72 | `report: the runner is gone; work may still be running` |

Watch prints one report line per delivered stop event. With no new stop
events, it prints one per task currently `stopped` or `conflict`. A
flow-level stop uses the flow id in place of `<task>`. A stop with saved
`provider_kind: capacity` prints only the capacity line for that stop.
Every user-facing flow command that exits 82 or 72 prints the corresponding
report line after its action line. Flow `next:` lines remain in place
when workers have pending owner actions; the single-run owner wait does
not suppress them.

`--status FLOW --json` prints `flow.json` plus `runner_live`. Each task has
`latest_review` as in watch reports, and its `findings` objects include
`round` and `run`, with null for missing values. Text status prints the
latest review's counts separately and each finding with its round and run.
Each task's `denied_paths` combines retained and current writer and reviewer
denials, with the run and turn for each entry. Top-level `denied_paths` is their
union with a `task` id on each entry; flow listings include this union too.
No denials gives `[]`. Watch task reports include retained `denied_paths`.
Text status names each denied path, reason, run and turn. Text prints
every top-level field except `tasks`, then each task's state and reason.
`--status` alone lists flows oldest first as
`<id>  <status>  <n>/<m> merged  <dir>`, or as JSON objects containing `id`,
`status`, `reason`, `dir`, `branch`, `runner_live`, `merged`, `tasks`
(the task count) and `denied_paths`. An unreadable flow is skipped with
`emb flow: skipped <error>` on standard error.

### Exit codes

| Code | Meaning | Action line |
|---|---|---|
| 0 | Dry run printed, flow started, answer queued, runner resumed, flow cancelled, lead stop reported, or every task merged | Watch reports give their next action |
| 1 | Unexpected error or unreadable settings database | `next: emb flow --status`; database errors name an owner action |
| 64 | Invalid option combination, plan, flow, task, worker, commit or claim, or an empty or whitespace-only plan `read` file on `--dry-run` or `--start` | `next: emb flow --help` for invalid options, plans or empty read files, a worker's owner action, the claim action or `--status` |
| 65 | The plan directory is not in a checkout, or `--cancel` names an unknown flow | Repeat `--dry-run` with `dir` inside a checkout, or `next: emb flow --status` |
| 66 | Unreadable plan or correction, or missing plan directory | `next: ls -l <path>` |
| 69 | Missing git, tmux, engine or driver, disconnected engine, unusable runtime folder, or a worktree folder inside any Git working tree or the source repository's git folder | `owner:` naming the fix |
| 70 | Flow branch or worktree creation failed | Prune worktrees, then repeat the start |
| 71 | Runner session failed to start | `next: emb flow --resume <id>` |
| 72 | Runner lost before the flow ended | `next: emb flow --resume <id>` |
| 74 | Interrupted watch; cursor unchanged | `next: emb flow --watch <id> --json` |
| 75 | Runner live or starting, answer not allowed, flow already done, cancellation pending, cancellation of an ended flow, the 60-second cancellation deadline passed, or a lead answer on a cancelled flow | `--status` for the cancellation deadline, `--status`, `--watch`, or `--resume` for a cancelled lead answer |
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
may contain slashes and colons. OpenCode members use full ids, such as
`--member opencode:openrouter/deepseek/deepseek-v4.1-flash-1017` or
`--member opencode:anthropic/claude-sonnet-4`. A panel has one to eight
members. The same `<engine>:<model>` twice is refused, even with different efforts.
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
`owner: name the members with --member <engine>:<model>[@<effort>], or set Panel on the Team screen`.

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
- Read the repository in your own working folder. Use the web only if your tools offer it.
- Name your sources: a file path, or a URL you opened.
- Say plainly where you are unsure or where the evidence is thin.
```

A blank line, `The question:`, another blank line and the question follow.
The brief ends with one newline. Members are not told the other members'
models.

### Running

Each member runs through its own Core instance in a daemon thread, with
its own output buffers. Threads start in member order, one second apart
(`START_SPACING`). Before any member starts, the panel records the source
checkout's `HEAD` as `base`. Every member reads that commit in its own
detached worktree, with the panel's `read` paths, no claim, no trailers and
no terminal window. Uncommitted source paths produce read-only start
warnings; source edits during a turn do not appear in the member's tree.
The shared brief tells members to read their own working folders. The
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

An answered or failed member's run is closed without `--force`, removing
its clean detached worktree. Changed worktrees are kept with a warning
naming the explicit discard command. A refused close sets `close_error` and emits a warning naming `emb run --close <run>`;
it does not change the member outcome. Closes admitted before the cutoff
may finish afterward. Such a member remains unfinished in the panel until
recovery reads its run's final state. No close is admitted after the cutoff.

Changed paths remain warnings: the member stays `answered`, its `changed`
list holds absolute paths in its own worktree, and the panel exits 83.
Uncommitted-path start warnings do not populate `changed` or cause exit 83.
Every member warning is prefixed `WARNING: member <n> (<engine>:<model>): `. Core standard output
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
| `base` | Commit every member reads, including retries; null before pinning or in a record without this field |
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
| `denied_paths` | Retained denials with `path`, `reason`, `allow_path`, `run` and `turn`; absent in older records or empty when none |
| `seconds` | Elapsed time across member jobs for the current run, including collection and closing; reset on retry, null before a job begins |
| `closed`, `close_error` | Whether the run was closed, and any close refusal reason; panel close tracks remaining open current and retained runs |
| `attempts` | Attempts with `kind` (`start`, `stall`, `recover`, `retry`), epoch `at` and `exit`; optional `run` holds a retained run id or null |

Loading defaults a missing `base` to null and rejects other missing or
unknown fields, wrong types and unknown status values. Panel listing returns
panels oldest first by `started_at`, then id, with unreadable panel folders reported separately. `emb run --status`
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
model, effort and read paths, in a new detached worktree at the panel's
saved `base`, even if the source checkout's `HEAD` moved. A panel without
`base` first takes a retained member run's base when available, otherwise
the checkout's current `HEAD`. It waits as a panel start does, including
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
attempts. Runs whose record status is neither `closed` nor `failed`, and
failed runs with a sent turn, need session closure; the saved member
`closed` flag does not determine this.
Each such run is closed as `emb run --close RUN`, without force. The
command also retries detached-worktree removal for closed and failed runs,
including retained attempts. It reloads each run under its command and record
locks. A sent turn, uncertain delivery, a collected writing turn or a live
session refuses removal and leaves the member open with the reason.
A closed read-only run with a detached worktree is exempt from the
sent-turn refusal: after `emb run --close RUN --force`, panel close removes
its clean detached worktree if still present and does not refuse because
the turn remains recorded as sent.
Clean detached worktrees are removed; changed ones and their recorded paths are kept with a warning naming
`git worktree remove --force <path>`. These warnings do not make the close
fail. A refusal does not stop attempts to close other runs. Duplicate ids within a member
are closed once. After each close, the member's `closed` flag reflects
whether any current or retained run remains open, `close_error` holds
refusal reasons or null, and the panel is saved. A missing or unreadable
run record is reported as a refusal. Member outcomes and the panel's
status and result exit are unchanged.

Text output has one line for each close or detached-worktree cleanup,
with the member number, run id and `closed` or the refusal reason:

```text
member 1 · run codex-20261102-101500-a1b2c3 · closed
```

JSON contains `panel`, the close command's `exit`, `members` and `next`.
Each `members` entry has `n`, `run`, `closed` and `close_error`. A member
with multiple affected runs has multiple entries. Closed and failed runs
appear when their detached worktrees are removed or kept with a warning.
When no run needs closing or worktree cleanup, there are no result lines,
or an empty `members` list.
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
flushes it. In both text and JSON modes, each member's line is printed and
flushed on standard error when that member ends:

```text
member <n> <engine>:<model> · <effort or default> · <status> · <seconds>s · exit <exit or -> · <bytes> bytes · run <run or ->
```

Seconds are rounded to the nearest integer. Unknown elapsed time is `-`
without a seconds suffix. Exit zero is printed as `0`. After waiting, text
mode prints `left out <value>: <reason>` for each excluded member and
`summary <absolute path to summary.md>` on standard output.

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
prints the member lines above with ` · last screen change <epoch or ->`
appended. Its JSON object is the complete panel record plus `live`, which
reports whether the lifetime lock is held, and `last_screen_change_at` on
each member. This activity time comes from the member's current run record,
and is null (`-` in text) when no activity is recorded or the run record
cannot be read. Zero is a recorded timestamp, not an absent value.

For a `starting` or `running` member while the panel is `running`, status
adds the current collection interval to the member's stored `seconds`.
Elapsed time accumulates across recovery; a retry's replacement run starts
its own timing. Status snapshots do not write these values to `panel.json`.

Member diagnostics retain `denied_paths` across attempts. Status combines
them with denials from current and retained runs, each with `path`, `reason`,
`allow_path`, `run` and `turn`. Top-level `denied_paths` is the union with
`member` set to the member number; panel listings include this union too.
No denials gives `[]`. Text status includes denial lines for members with
reported paths, including ended members. Panels add no denial event stream.

`--status` lists readable panels oldest first, by start time then id:

```text
<id>  <status>  <answered>/<n> answered  <dir>  <subject>
```

Each panel line is followed by status member lines for its `starting` and
`running` members and members with denied paths, with elapsed seconds and
last screen-change time.
The subject is `Panel <id>: <first non-empty question line>`, cut to 60
characters. If the question file cannot be read, the listing names that
file on standard error and uses `(question unavailable)` as the question.
An unreadable panel record is skipped with `emb panel: skipped <error>`.
No panels prints no text, or `[]` with `--json`. The JSON list contains
objects with `id`, `status`, `live`, `dir`, `answered`, `member_count` (an
integer), `running_members`, `denied_paths`, `exit` and `started_at`; there is no `members`
key in this list form. `running_members` contains `n`, `seconds` and
`last_screen_change_at` for each `starting` or `running` member, or `[]`
when there are none. In one-panel status, `members` remains the array of
member records. Status prints no next action on success.

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
emb merge BRANCH --into TARGET --verdict keep --note TEXT [--subject TEXT] [--dir DIR] [--json]
```

Merges a reviewed branch into a local branch, then cleans it up as
`emb cleanup --merged-into TARGET --branch BRANCH` does. `DIR` is a folder in
the git checkout; the default is the current folder. `DIR` may be inside
`BRANCH`'s own worktree: `emb merge` and `emb cleanup` run their git commands
from the repository's main checkout, which cleanup never removes.

- `--note TEXT` is the lead's verdict line, such as `Keep, with a docstring
  fix`. It is required, and it must be one non-blank line (64 otherwise).
  The output starts with `Verdict: TEXT`, before the merge line, also when
  the branch is already contained. The landed commit carries it as the trailer
  `Ember-Verdict: TEXT`, and `--json` gives it as `note`.
- `--subject TEXT` is required for an Ember run branch, including a flow
  task branch. It must be one nonblank line without CR, LF or NUL (64 with
  a `next:` line otherwise). The lead writes it in the repository's commit
  convention. A completed `ember/flow/<id>` branch and other branches get
  merge commits; `--subject` does not change their messages.
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
- For an Ember run branch, `git merge-tree --write-tree` computes the
  squash tree without changing the target's index or worktree.
  `git commit-tree` writes one commit whose parent is the target tip, with
  the supplied subject. Ember then fast-forwards the target in its checked
  out worktree. The worker's turn commits do not become target ancestors.
- The squash message keeps every distinct Git trailer from the worker
  commits since the common base, oldest first. Identical key/value pairs
  appear once; keys are compared without case. Git's trailer grammar and
  configured `trailer.separators` apply. Keys starting with `Ember-` are
  excluded without regard to case. Ember writes `Ember-Verdict: TEXT` and
  `Ember-Task-Tip: <worker tip>` once with the actual verdict and tip.
- The target repository's `commit-msg` hook runs on the squash message and
  may edit it. `commit.gpgsign` enables `git commit-tree -S`. A hook or
  commit-writing refusal exits 77 and moves nothing. Worker `pre-commit`
  hooks are not run again. If either branch moves while the squash is built,
  Ember exits 76 before advancing the target.
- A squash conflict exits 75 naming the conflicting files, leaving the
  target unchanged with no merge to abort. Resolve on the worker branch,
  review, then repeat `emb merge` with `--subject`.
- For other branches, Ember runs
  `git merge --no-ff --no-edit -m <message> refs/heads/BRANCH` in the target
  worktree. `<message>` comes from `git fmt-merge-msg --no-log`:
  `Merge branch 'BRANCH' into TARGET`, without ` into TARGET` when
  `merge.suppressDest` covers the target (`main` and `master` by default).
  A conflict runs `git merge --abort`, leaves the target unchanged and
  exits 75. Other Git failures exit 70.
- Both paths print `merged BRANCH at <worker tip> into TARGET: <landed commit>`.
  They resolve `BRANCH` as a local branch even when a tag has the same name.
- When worker history has a breaking Conventional Commit subject or a
  `BREAKING CHANGE:` or `BREAKING-CHANGE:` footer, and the squash subject has
  no `!` before its colon, standard error includes
  `WARNING: BRANCH holds a breaking change; the subject has no !`.
- It never pushes.

The cleanup's lines follow the first line. A branch that is not an Ember run
branch is kept with `not an Ember run branch; next: git branch -d BRANCH`.

After successful cleanup, the command derives the next line from the
newest matching run without a parent and prints it once on standard error
when non-null, using the rules in `emb run` Status. Cleanup normally closes
the run, so a contained, closed run with an unfinished claim gives
`next: emb choices --finish <claim>`. A kept branch, a cleanup failure, or
no readable matching run without a parent gives no derived next line.

When the newest matching run without a parent has a pending owner wait,
the command prints each complete pending `owner:` line in recorded order
on standard error, including with `--json`. It prints no derived `next:`
line for that run. The wait does not refuse the merge or change a successful
exit from 0, including when cleanup keeps the branch.

A flow branch `ember/flow/<flow id>` follows the run-branch cleanup path
without a run record, with the worktree its flow record names. Cleanup keeps it while the flow's runner is live, with
`the flow <id> is running` and `next: emb flow --status <id>`. A task branch
merged into a flow branch is merged in the flow worktree and cleaned up by
the same rules.

`--json` prints one object:

| Field | Value |
|---|---|
| `branch` | `BRANCH` |
| `target` | `TARGET` |
| `note` | `TEXT` from `--note` |
| `merge_commit` | The squash or merge commit; null when the branch was already contained |
| `warnings` | List of warning strings, also printed on standard error |
| `contained_before` | Whether the branch was already contained |
| `cleanup` | The cleanup's list, as `emb cleanup --json` prints it |
| `next` | The derived next line printed on standard error, or null |
| `owner_wait` | Ordered array of complete pending `owner:` lines, also printed on standard error; present only when nonempty |

The merge JSON object has no `step` field.

## `emb prune`

```
emb prune --older-than DAYS [--dry-run] [--json]
```

Removes ended run, flow and panel folders under `runs_dir()`, including
logs inside those folders, clean owned detached read-only worktrees, and
orphaned worktrees in Ember's worktrees folder. `--older-than` is required and accepts a whole number of days, zero
or greater. Age is measured from `ended_at`, or
`updated_at` when there is no end time. A record qualifies at an age of at
least `DAYS × 86400` seconds; zero includes every ended record.

Ended runs are `closed` or `failed`, with no sent turn, no collected writing
turn and no live session. A closed read-only run with a detached worktree
may still have a sent turn. Uncertain retry delivery on a failed run leaves
the turn sent and keeps the worktree and record with the reason. Ended flows
are `done`, `failed` or `cancelled`. Panels must be `done` or `incomplete`.
A flow or panel is kept
when any current or former task or member run is open (`starting`,
`running` or `waiting`), has a sent or uncommitted writing turn or a live
session, is busy or is unreadable; the same closed read-only exception
applies to sent turns. The parent is reported as
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
panel lock; runs use their command lock, then their record lock. Busy records and records that no
longer qualify are skipped. Parent and child locks stay held through child
removal, including child records shared with a parent that is being kept.
Unreadable records are kept and reported on standard error. Folders outside
the timestamped record id format that contain a run, flow or panel record
are reported as invalid ids; other folder names are ignored. Record-folder
symlinks are refused. Files named by a record outside its folder are kept.

`--dry-run` lists the same record and orphaned-worktree candidates and
removes nothing.
Text output has `removed <kind> <id>` lines (`would remove` for a dry run),
`skipped <id>: <reason>` lines, and kept branch and worktree lines with
removal commands. Text output lists every remaining worktree of a removed
record unless an open run uses it. Each worktree appears under its branch
line with its removal command, including when the branch is merged or
already deleted. When an open run uses the worktree, an unmerged branch's
deletion command is still listed, without a worktree-removal step.
Prune deletes no branch or tmux session. Editing worktrees whose git
folder still exists are retained. An ended read-only run's owned detached
worktree is removed when clean, including its git registration and ignored
build and test files. Tracked changes, untracked non-ignored files or a
`HEAD` different from its recorded base keep both
worktree and record, with the explicit `git worktree remove --force <path>`
command in the error. A locked detached worktree is also kept.

For other worktrees, when removing a record, prune also removes a worktree
it names if no open
run uses it, it is a real folder inside Ember's resolved worktrees folder,
and its regular, non-symlink `.git` file is valid and names a git folder
that no longer exists. A worktree outside Ember's worktrees folder is
kept, including one in a removed clone's old layout.

An unscoped `emb prune` also inspects folders in Ember's worktrees folder
that no record names, including flow task paths. It removes one only when
every record could be read, the folder and its `.git` file pass the same
checks, and the folder's modification time is at least `DAYS × 86400`
seconds old. Other folders are kept and reported with the reason. A prune
with explicit record ids, including Clear in the app, does not run this
sweep.

Prune lists branch-deletion commands only for branches that exist and are
unmerged. A branch is unmerged
when it is not an ancestor of its target. Task branches are checked against
their flow branch. Run and flow records save
`base_branch` at creation and use it even if the source checkout switches
branches. If the target branch has been deleted, prune checks against the
owning flow's `base_branch`. If that branch is also absent, or the record
has no base branch, the branch is listed as unmerged. Missing target refs
are not errors and do not change the exit code. Saved commits are not used
as targets. This check does not treat a squash merge as ancestry.

Every listed unmerged run, task and flow branch gets
`git -C <dir> branch -D <branch>`. Records with a null or missing
`base_branch` list their existing branches as unmerged using this command.
When the worktree exists and no open run uses it,
`git -C <dir> worktree remove <path>` precedes branch deletion with `&&`.
Paths and branch names are shell-quoted. Merged and absent branches have
no branch-deletion command; their remaining worktrees still have worktree
removal commands. Prune never prints `emb cleanup`.

Each worktree is the path its record names, wherever it is, including
worktrees at `.ember/worktrees/<id>` from before Ember 0.13.0.
For a run or flow record with `common`, `<dir>` is its recorded `main` when that folder
exists, otherwise its recorded common git folder. Records without these
fields use the first existing source checkout, main checkout or parent
flow or panel checkout; if none exists, they use the main checkout from
`git worktree list` in the recorded worktree. When no folder is found,
the recorded source folder is used and its git error is reported.

`--json` prints an object with four arrays:

| Field | Entries |
|---|---|
| `removed` | `{id, kind}`, where kind is `run`, `flow`, `panel` or `worktree`; a worktree's id is its absolute folder path; dry runs list candidates here |
| `skipped` | `{id, reason}` |
| `branches` | `{branch, command}`; a branch command already includes its worktree removal when listed in `worktrees` |
| `worktrees` | `{branch, worktree, command}` for every worktree listed in text output, including merged and deleted branches; empty when there are none |

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
emb cleanup --merged-into TARGET [--branch BRANCH] [--claim ID] [--dir DIR] [--dry-run] [--json]
```

Removes the Ember run branches `TARGET` contains, with their run's worktree
and session. `DIR` is a folder in the git checkout; the default is the current
folder.

A run branch is `ember/<engine>/<run id>`; its worktree is the path its run
record names: `worktrees/<run id>` in Ember's data folder, or
`.ember/worktrees/<run id>` in the main checkout for a run started before
Ember 0.13.0. A run branch with no record has the worktree `git worktree
list` shows holding it in a folder named by its run id, other than the main
checkout, or none. Records with `common` identify their repository by that
recorded git folder; older records query their checkout or worktree. Git
commands run from the main checkout (the first entry of `git worktree list`),
also when `DIR` is in a linked worktree. No other branch is ever removed.
Without `--branch` or `--claim` every run branch is handled, along with owned
detached worktrees of closed or failed read-only runs in the selected repository.
Removal checks the reloaded record under the run command and record locks.
Sent turns, uncertain delivery, collected writing turns and live sessions
keep the worktree with the reason, except that a closed read-only run's
detached worktree can be removed despite a recorded sent turn.
Clean detached worktrees are removed; changed or locked ones are kept. A changed
worktree's reason names `git worktree remove --force <path>`; cleanup does
not run it. Records retain their worktree paths. With `--branch`, only
`BRANCH` is handled; detached read-only worktrees are excluded. Older
read-only records without `detached` keep their source checkouts.

`--claim ID` handles only branches whose run or flow record has exactly
that claim id, and applies the same filter to detached read-only worktrees.
It needs no live claim. Records with no claim do not match; an unmatched id
handles nothing. With both `--branch` and `--claim`, the named branch is
handled only if its recorded claim matches. Without `--claim`, cleanup
remains repository-wide unless narrowed by `--branch`.

A flow branch `ember/flow/<flow id>` is a run branch with no run record;
its worktree is the path its flow record names, or, with no flow record,
the one `git worktree list` shows as for a run branch with no record.
Cleanup checks the flow's runner lock and keeps the branch when it is held
or cannot be checked. A flow record that cannot be read, names another
branch, names a worktree not named by the flow id or the main checkout, or
names a main checkout in another repository keeps the branch. With no live
runner, the ordinary containment and worktree checks apply; `--dry-run`
reports `would remove` when they pass. Flow records are kept.

A branch is contained in `TARGET` when it is an ancestor of `TARGET`, a
reachable commit records its tip in an `Ember-Task-Tip` trailer, or merging
it into `TARGET` gives no conflict and `TARGET`'s own tree. The trailer
recognizes an Ember squash even after later target edits. The check changes
no index, worktree or ref; with `--dry-run`
the objects of the trial merge go to a temporary folder.

For each contained run branch, in order:

1. The run record must belong to the branch: same engine, same branch, its
   worktree is a folder named by the run id other than the main checkout,
   and its recorded common git folder identifies this repository. Older
   records check their checkout or worktree when one still exists.
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

Everything else is kept, with a reason and an action when available:

| Kept | Reason | Then |
|---|---|---|
| `TARGET` itself | `the merge target` | `next: emb cleanup --merged-into <the branch it was merged into>` |
| A branch that is not a run branch (with `--branch`) | `not an Ember run branch` | `next: git branch -d BRANCH` |
| A branch not contained | `not contained in TARGET` | With `--claim` matching its recorded claim: `next: emb merge BRANCH --into TARGET --verdict keep --note '<verdict line>' --subject '<commit subject>' (after a Keep verdict)`; without `--claim`, no merge suggestion |
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
or `kept BRANCH: <reason>; claim <id>`, followed by `; next: <command>` or
`; owner: <step>` when there is an action. Every kept line names the recorded
claim, or says `no claim is recorded`. Without `--claim`, no kept line
suggests a merge. A detached worktree uses its run id in place of
`BRANCH`. With nothing to handle it prints nothing on standard
output and a message on standard error.

After exit 0 with `--claim ID`, unless `--dry-run`, the command prints
`next: emb choices --finish <id>` once on standard error if no kept result
has that claim, including when there are no results. Without `--claim`, it
prints no claim-finish line. The command does not finish the claim itself.

`--json` prints a list of objects:

| Field | Value |
|---|---|
| `branch` | The branch, or null for a detached read-only worktree |
| `run` | The run id, or null for a branch without a run id |
| `claim` | Recorded run or flow claim id, or null when none is recorded |
| `action` | `removed`, `kept` or `would remove` |
| `reason` | What was done, or why it was kept |
| `next` | For a keep with an action, `next: <command>` or `owner: <step>`, the same text as the printed line ends with; null otherwise, including an uncontained branch without `--claim` |

Cleanup JSON has no `step` field or top-level `next` field. Its per-result
`next` fields do not contain the claim-finish line printed on standard error.

A git command that fails (worktree removal, `git worktree prune`, branch
deletion, or a git command that reads the repository) stops the cleanup at
once with exit 70. Everything done before it is already printed, or with
`--json` is in the list. The branch it failed on follows as kept; its reason
lists the steps already done to that branch (for example `closed <run>`,
`removed worktree <path>`) and the failed step with git's message, and its
`next` is the line printed on standard error. With `emb merge`, a merge
already made stays.

A git command that changes the repository (writing or signing a commit,
running a hook, the merge, `git merge --abort`, worktree removal,
`git worktree prune`, branch deletion) runs without a time limit. Every other git command may take 120 seconds; one that takes
longer fails.

### Exit codes of `emb merge` and `emb cleanup`

Messages go to standard error as `emb merge: <message>` or
`emb cleanup: <message>`; each non-zero exit then prints one `next:` or
`owner:` line.

For an exit-75 merge conflict, the action is
`owner: resolve the conflict on <branch>, or ask for a new run cut from the updated target`
in place of a `next:` line. It is followed on standard error by
`report: merge conflict; do not retry or force it; ask the owner: a new run cut from the updated target, or the owner resolves it`.
Other merge and cleanup exits, including 76 and 77, print no report line.

| Code | Meaning |
|---|---|
| 0 | Done, whatever was kept |
| 64 | Usage error, no Keep verdict, or missing or invalid `--subject` for an Ember run branch |
| 65 | The folder is not in a git checkout, or a named branch or target does not exist |
| 70 | A git command failed: the merge, making the merge message, finding the target's git folder, listing worktrees, `git status`, worktree removal, prune or branch deletion |
| 75 | Refused: the target is checked out nowhere, its worktree has an unfinished merge, rebase, cherry-pick or revert, or its tree is not clean, or a merge conflict |
| 76 | The target or worker branch moved while building a squash; target not advanced |
| 77 | The target's `commit-msg` hook, commit identity or signing configuration refused the squash; nothing moved |

## `emb deps`

```
emb deps [--refresh] [--json]
```

Shows each component Ember depends on: its installed version, the newest
version its source offers, its status and the command that updates or
installs it. It reads installed versions every time and newest versions when
due. The `ember` skill link shows its state in place of a version. It installs
and updates nothing.

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
| `codex` | Codex | `apps` | `codex --version`, `codex-cli (\S+)` | `version` of `https://registry.npmjs.org/@openai/codex/latest` | Detected install method | `npm install -g @openai/codex` |
| `opencode` | OpenCode | `apps` | `opencode --version`, `1.19.5` or `opencode v2.2.5` | the highest non-draft, non-prerelease tag of `https://api.github.com/repos/anomalyco/opencode/releases?per_page=100` | `opencode upgrade --major` with OpenCode 1 installed; with OpenCode 2, the detected install method, else `opencode upgrade` | `curl -fsSL https://opencode.ai/v2/install \| bash` |
| `claude` | Claude Code | `apps` | `claude --version`, `^(\S+) \(Claude Code\)$` | the text of `https://downloads.claude.ai/claude-code-releases/<channel>`, where `<channel>` is `autoUpdatesChannel` from Claude Code's `settings.json` (in `CLAUDE_CONFIG_DIR`, else `~/.claude`): `stable`, or `latest` when it is unset or anything else; a version cached from another channel is read again | Detected install method | `curl -fsSL https://claude.ai/install.sh \| bash` |
| `claude-desktop` | Claude Desktop | `apps` | apt `claude-desktop` | apt | `sudo apt install --only-upgrade claude-desktop` | none; note "Not installed." |
| `ember-skill` | ember skill | `apps` | `emb skill status` link state | none | none | By link state, below |
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
| `installed` | `kind = "apt"` with `package`; `kind = "command"` with `pattern`, a regular expression whose group 1 is the version, and either `engine` (the engine's CLI run with `--version`) or `argv`; `kind = "python"` with `package`; or `kind = "skill"` with an optional `commands` map from link states to install commands |
| `newest` | `kind = "apt"` with `package` (only with an apt `installed`); `kind = "json"` with `url` and `field`; `kind = "text"` with `url`; `kind = "pypi"` with `package`; or `kind = "none"` for no newest version (required for `skill`) |
| `update` | Optional: the command shown while an update is available |
| `methods` | Optional: ordered `[[components.methods]]` tables with `path` and `update`, or `kind = "dpkg"` and `update`; replaces `update` for engines |
| `updater` | Required with engine methods: `codex update`, `opencode upgrade` or `claude update`, used when no method matches |
| `install` | Optional: the command shown while the component is missing |
| `update_note` | Optional: shown while an update is available and there is no `update` |
| `install_note` | Optional: shown while the component is missing and there is no `install` |

### Skill link

The `ember-skill` component uses the same source candidates and link path as
`emb skill status`, including `CLAUDE_CONFIG_DIR`. Its installed source kind
is `skill`; its newest source kind is `none`.

| Link state | Status | Command or note |
|---|---|---|
| `installed` | `current` | none |
| `missing` | `missing` | `emb skill install` |
| `broken link` | `check failed` | `emb skill install` |
| `other link` | `check failed` | `emb skill install --force` |
| `folder` | `check failed` | Move the link path, then run `emb skill install`; no command button |
| `file` | `check failed` | Move the link path, then run `emb skill install`; no command button |

When no source is found, every link state shows `Reinstall Ember.` with no
command. For `check failed`, `error` is the link state. The installed source's
`commands` map supplies commands for `missing`, `broken link` and `other link`
before status-based command selection.

The CLI and Dependencies screen show the link state in place of versions.
JSON includes `link_state`, with null `installed` and `newest` versions. Every
skill status except `current` counts in `counts` and the sidebar badge;
other components count only when missing or when an update is available.
Components with newest kind `none` are skipped in every newest mode,
including `--refresh`, and excluded from newest-read summaries.

### Update methods

For Codex, OpenCode and Claude Code, Ember finds the launcher on `PATH`, or
at an engine's known location outside `PATH` (`~/.opencode/bin/opencode` for
OpenCode), and follows its symlinks. It matches this resolved path against
`[[components.methods]]` in order. The first matching path supplies the
update command. The dpkg method is tried after all path methods.

For a regular script of at most 64 KiB whose first line starts with `#!`,
Ember reads the first quoted `$basedir/<relative path>` containing
`/node_modules/`. It makes that path absolute against the script's folder
and normalizes it without following symlinks for path-method matching.
The fully resolved target supplies `resolved_path`, dpkg's ownership lookup,
and the permission check outside npm. This covers
pnpm's global shell shims, such as a launcher in `$PNPM_HOME` pointing to
`$basedir/global/5/node_modules/@openai/codex/bin/codex.js`. ELF binaries
are not read as scripts. A read or resolution failure, or no such quoted
path, leaves the launcher as the path to match. Updater commands keep the
original launcher name.

| Path pattern | Update template |
|---|---|
| `{prefix}/lib/node_modules/{package}` | `npm install -g --prefix {prefix} {package}@latest` |
| `{prefix}/global/*/node_modules/{package}` | `pnpm add -g {package}@latest` |
| `{prefix}/install/global/node_modules/{package}` | `bun add -g {package}@latest` |
| `{prefix}/Cellar/{formula}` | `brew upgrade {formula}` |
| `~/.local/share/claude/versions/*` (Claude Code) | `{launcher} update` |
| `~/.opencode/bin/opencode` (OpenCode) | `{launcher} upgrade` |
| `kind = "dpkg"`, no path | `sudo apt install --only-upgrade {package}` |

A pattern starts at the beginning of the candidate path and matches either
the whole path or a leading part ending at a `/` boundary. `{prefix}`
captures any path, `{package}` captures a name or exactly two segments for
an `@scope/name`, `{formula}` captures one segment, and `*` stays within one
segment. Package names cannot start with `.`, so `.pnpm` store folders do
not match as packages. `~` uses the owner's home folder after resolving its
symlinks. Captured values are shell-quoted in the command. `{launcher}` is the full unresolved launcher path.

The dpkg method runs `dpkg-query -S <resolved path>` with the shared command
deadline. One package owning that path supplies `{package}`; a failed,
timed-out, malformed or ambiguous lookup is no match. This lookup runs only
when installed versions are being read, including `emb deps` and the
Dependencies screen's Check now action.

Ember tests `os.access(folder, W_OK)` on `{prefix}/lib/node_modules` for npm,
and on the resolved file's parent otherwise. If that folder is not writable,
it prefixes the command with `sudo`, unless it already starts with `sudo`.
Ember itself runs no `sudo`; the command runs in the owner's terminal.

With no match, an available update keeps status `update available` and
`command_kind` `update`. Its command is the full unresolved launcher path
with the `updater` arguments. The CLI and Dependencies screen show the
resolved path and, with that update command, "Ember could not tell how the
tool was installed." Codex has no vendor path method. When no executable
is found, the component shows its existing install command and both
`resolved_path` and `method` are null.

The resolved path and method are read again on every read and are not
cached. On a read using cached installed versions, path methods are still
matched; dpkg is skipped and its method remains null.

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
is killed. Every network read has a 10-second timeout. Requests to outside websites, here and for the models.dev catalogue, send `User-Agent: ember/<version>`.

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

When the source reports a version older than the installed one, such as apt
lists read before a newer package was installed, `newest` is the installed
version.

`command` is the update command while an update is available and the install
command while missing, with `command_kind` `update` or `install`; otherwise
both are null, except for skill commands and notes described above. `note` is `update_note` or `install_note` for that status when
there is no command for it, or the could-not-tell note alongside an update
command when no install method matched; otherwise null.

### Cache

The last reads are cached in `$XDG_CACHE_HOME/ember/dependencies.json`
(default `~/.cache/ember/dependencies.json`), mode 600 in a mode 700 folder,
replaced whole on each write. Per component it holds the last installed read
with its `read_at` and the last newest version read with its `read_at`, in
seconds since the epoch. A newest version is read again when its cached read
is missing or six hours old or more; `--refresh` reads it now. A newest read
that fails is reported and not cached, so the next run reads it again. A cache
that cannot be read is treated as empty; one that cannot be written is left as
it was. The skill installed read caches its link state and note.

### Output

Text output has one line per component, in the order of the list:
`<label>: <status>[, installed <version>][, newest <version>][: <error>]`,
then an indented `update: <command>` or `install: <command>` line when there
is a command. An engine shows `resolved path: <path>` only with an update
command, alongside the could-not-tell note when no method matched. JSON
keeps `resolved_path` and `method` for every component. Skill rows show the
link state and any move-path or reinstall note.

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
      "note": null,
      "resolved_path": null,
      "method": null
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
| `components[].resolved_path` | Engine launcher target after following symlinks; null when no executable is found or for other components |
| `components[].method` | Matched method table (`path` and `update`, or `kind = "dpkg"` and `update`); null when none matched |
| `installed_read_at` | When each component's installed version was read, by id; null when it was never read |
| `newest_read_at` | When each component's reported newest version was read, by id; null when none is reported |
| `counts` | Non-current skill components, plus other components that are `update available` or `missing` |
| `newest_read_oldest` | The oldest `newest_read_at` among components that are not missing and have a newest source other than `none`; null when there is none |
| `newest_read_complete` | Whether every component that is not missing and has a newest source other than `none` has a newest version read |

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

`SKILL.md` carries `Skill version: <x.y.z>`, updated by Release Please.
An upgrade changes the link's target content; a session retains its loaded
copy until it reloads. `emb choices --claim --skill <x.y.z>` compares that
loaded version with the installed version on each claim.

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
at a time. A start that finds a running app passes its `XDG_ACTIVATION_TOKEN`
on the single-instance socket (`show <token>`), and the running app uses it
when it raises its window, so the desktop's busy cursor for the launch ends.

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

On every such connection, and 2 seconds after the folder holding the
installed `ember-app` metadata (`ember_app-<version>.dist-info`) last
changed, the running app reads the installed version from disk and compares
it with the version it started as. An upgrade installed while Ember runs
therefore shows the offer without another start. When they differ, the
window shows a small info box in the full sidebar, directly above the version
line: `Ember <version> is installed.` with a Restart button and no Dismiss.
In the collapsed sidebar, a round 32-pixel button with `refresh-cw` in
`text-accent` on `bg-accent` replaces the box, has the tooltip
`Ember <version> is installed. Restart` and restarts Ember when clicked.
The box stays until Ember restarts; a later installed version replaces its
text. No alert shows above the screen for it. Restart closes the socket, starts the installed app
with the running interpreter (`python -I -m ember.cli app`) and quits. Runs
keep running in their tmux sessions.

At start the app:

- shows the window, unless `--hidden` was given and a tray is available;
- applies the Fusion style with the colours of the desktop's light or dark
  scheme;
- reads the settings, the engine status (checking every engine), the
  dependency report (installed versions now, newest versions when due) and
  the run, flow and panel records, each off the window's thread;
- once the settings are read, applies `app.theme` and writes or removes the
  autostart entry to match `app.autostart`; it does so again each time
  either setting changes.

Until the settings are read the content area shows "Reading settings"; when
they cannot be read it shows the reason and Try again.

### Window

The window opens at 1100 by 720 pixels and is never smaller than 640 by 480.
Its size, last screen and whether the sidebar is hidden are kept between
starts. Each screen's content is at most 960 pixels wide, padding excluded,
centred in the space beside the sidebar; narrower windows fill the available
space. No screen scrolls sideways; a table on Engines may scroll sideways
inside its own frame.

At window widths of 960 pixels and above, the sidebar is 220 pixels wide
and shows the Ember mark and name, one row per screen (Overview, Team,
Engines, Dependencies, Settings) and `Ember <version>` at the bottom.
Overview's row counts
top-level live items: standalone live runs plus every flow and panel in
Live, including stopped flows waiting for Claude and gone runners or panel
processes. Workers nested under a Live parent add nothing. A live worker
whose parent is not in Live counts as a top-level run, including a running
writer under a cancelled flow in Finished, as in the tray. The accessible
description is `<n> live`; the count and description are hidden at zero.
The count updates from the shared run, flow and panel models on every screen.

Dependencies' row shows the number of components with an update available
or missing, only above zero. A screen is created the first time it is shown.

Below a window width of 960 pixels, the sidebar collapses to 64 pixels wide:
the Ember mark without its name, followed by centred 40 by 36 pixel rows
showing only each screen's icon. Each row's tooltip and accessible name are
the screen's name; a count above zero appears as a pill at the icon's top
right, in `type-caption` with `on-accent` text (`#ffffff` in both themes)
on `accent`. The pill is 18 pixels tall and at least 18 pixels wide, and
sits in the row's top-right corner; a wider count extends it to the left.
A 2-pixel ring matches the row's current background: sidebar, hover or
selected. Inside the ring, 2 pixels of padding on the left and right keep
the count clear of the ring. The count is added
to the tooltip, such as `Overview · 2 live`, and keeps the row's accessible
description. Full sidebar counts use a 22-pixel-tall badge. The version
line is hidden and the mark's tooltip is `Ember <version>`.

A ghost button with the `panel-left` icon and tooltip `Hide sidebar` sits
at the top right of the full sidebar, on the line of the mark and name, or
under the mark when collapsed. Clicking it hides the whole sidebar. A button
with the same icon and tooltip `Show sidebar` then appears before the current
screen's title, on the same line; clicking it shows the sidebar. Tab reaches
both buttons. The hidden state is saved with the window size as
`sidebar_hidden` in `app.json`; a missing field means shown. A shown sidebar
still collapses below 960 pixels. Until settings load and a screen is
created, the sidebar stays shown and its hide button is disabled.

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
| Ctrl+1 to Ctrl+5 | Open Overview, Team, Engines, Dependencies, Settings |
| Ctrl+W | Hide the window to the tray; quit when there is no tray |
| Ctrl+Q | Quit |

A control shows the blue focus ring only when it took focus from the
keyboard: Tab, Shift+Tab or a shortcut. A control focused by a mouse
click shows no ring. When the app moves focus itself, the ring follows the
last input: a key press shows it, a mouse press does not.

Tooltips draw in `fill-primary` with `on-primary`, 6-pixel corners and
12-pixel text. A tooltip wider than 320 pixels wraps at 320 pixels, keeps
its line breaks and may break a path after each `/`; a shorter one shows on
one line. Every setting row on Team and Settings is its name with an info
icon after it, and no line under the name; the icon's tooltip holds what
the setting does. Hovering the icon, focusing it from the keyboard or
clicking it shows the tooltip under it, in Ember's own window with 6-pixel
rounded corners; it hides when the pointer leaves an unfocused icon, focus
moves on, or Escape is pressed. The collapsed sidebar's rows, mark and
restart button, and the hide and show sidebar buttons, also use Ember's
tooltip window on hover or keyboard focus. Other controls, cut-off text,
model names, file buttons and picker items do not open tooltips. Qt's own
tooltip window is never used. The tray icon's tooltip is drawn by the desktop.

While the widest visible setting row fits, a screen's setting controls
share one column as wide as its widest control, at least 280 pixels:
pickers fill it, and segmented controls, switches and buttons sit at its
right edge. When a card cannot fit a name, its info icon and the column,
the column narrows: pickers shrink to a minimum of 200 pixels, and a closed
picker that cannot show its whole value ends it with `…`, without a tooltip.
Segmented controls, switches and buttons never shrink. When the column
cannot narrow further, every setting row on that screen stacks together:
the name and info icon on the first line, the control 8 pixels below at the
left edge. A stacked picker keeps the column's last width, limited to the
available width inside its card. The rows return to one shared column when
the window widens enough.

### Tray

The tray icon shows whether any run, flow or panel is live, always in the
drawings for a dark top bar. Its tooltip prefixes the menu's status line
with `Ember · `. The menu holds:

- a status line counting top-level items in run, flow, panel order, with
  non-zero parts only: `1 run, 1 flow live`, `2 panels live`, or
  `No runs live` when there are none;
- one line per top-level live run, `<Engine> · <repository folder> · <HH:MM>`
  with the local start time, which opens a terminal window on the run as
  `emb run --attach` does;
- one line per live flow or panel, `<Flow|Panel> · <record id> · <HH:MM>`,
  with the id elided to 40 characters including an ellipsis and the local
  start time, which shows the window on Overview;
- each flow or panel's live worker runs immediately below it, indented,
  each opening its own terminal, without repeating or counting those runs
  at the top level;
- Open Ember and Quit Ember.

Flows and panels follow Overview's Live states, including gone runners and
panel processes. Top-level items and each parent's workers are oldest first
by start time and then id. A run named by multiple parents belongs to the
oldest parent by start time and then id. A live run whose parent is in
Finished appears and counts as a top-level run in the tray.

Clicking the icon also shows the window. Closing the window hides it to the
tray. When the desktop has no tray, the window shows at start and closing it
quits. A menu click gives Ember an activation token; Quit Ember uses it on
the window before quitting when the window is shown. With the window hidden,
a separate invisible helper window is shown, activated and hidden to use the
token before quitting; the main window stays hidden.

A run, flow or panel change updates the icon and tooltip at once. The menu
keeps its lines until it next opens, when it rebuilds them. A click from a
stale menu still triggers its action after the menu closes. When a run's
line cannot open its terminal, such as for a run closed while the menu was
open, the window shows with the error in its alert. Quit Ember hides
the window and the tray icon at once, then waits up to 12 seconds for running
background tasks to end before the app exits.

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

The app also reads `flow.json`, each flow's `events.jsonl` and `panel.json`
off the window's thread and watches their folders and record files, with
the same sixty-second backstop and coalesced reads. It takes no lock and
reads lifetime lock ownership from `/proc/locks`. One shared flow and panel
model stays active until the app stops, including while Overview or the
window is hidden.

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

Overview shows Live and Finished. Live lists starting, running and waiting
records, oldest first by start time and then id. A live run whose flow or
panel has ended is a top-level Live row, not nested under that parent; its
detail line starts with the parent's type and first fact, such as
`Panel · 7 members`. Finished lists ended
records, newest first by end time, update time and id, with update time used
when end time is absent. Its cap of twenty applies to top-level rows across
runs, flows and panels. Each empty section shows its empty state.

Live comes first. Finished starts as a preview of its three newest
top-level rows, each with its nested runs, and a `Show all <n>` button in
its header, where `<n>` is the number of rows the full list shows. The
button reads `Newest 20` once Finished holds twenty rows, the most it
reads, and is hidden when the preview already holds every row. It opens
the full list with the Type, Status and Engine pickers and Clear; `Show
fewer` returns to the preview. The pickers and Clear show only while the
full list is open; with twenty rows, `Filters cover the newest 20 records.`
shows beside the pickers. Whether it is open is kept in `app.json` as
`finished_open` and restored when the app starts.

Finished filters this capped list with three pickers: Type (All types, Run,
Flow, Panel), Status (All statuses, Done, Incomplete, Failed) and Engine (All
engines, Codex, OpenCode, Claude).
The filters combine and test top-level rows: Run selects standalone runs,
Status matches the parent's own chip, and a flow or panel matches Engine
when any of its readable runs, nested or shown in Live, used that engine. A parent without
readable nested runs matches only Engine=all. Filters start at all when
the app starts and keep their values across reloads for the app session.

Clear is a red outline button with a trash icon. Its accessible
description is `Remove shown records`. Clear is hidden when no records are
shown, where the list's empty line says so, and disabled while a clear is
pending. It
captures the shown top-level ids and calls `prune.remove` with
`older_than=0` and those ids, first as a dry run and then, after confirmation,
for removal. Both calls run off the GUI thread. The confirmation lists the
dry run's records, including nested runs, skipped ids with their reasons,
and retained branches with removal commands. Worktree commands appear under
their branches. Remove confirms; Cancel is the default and Escape cancels.
Clear removes records and logs, clean owned detached read-only worktrees,
and orphaned worktrees under Ember's worktrees folder. It retains branches
and editing worktrees whose git folder still exists. Detached worktrees
with changes or locks keep their records. Clear does not sweep unrecorded
worktrees. Prune rechecks records and skips those that are busy or no
longer ended.

A dry run with no removable records shows its report without confirmation.
A dry-run error shows a warning naming the error and removes nothing.
After removal a message lists removed records, skipped ids with reasons,
and errors. Closing the message reloads both record models and reapplies
the filters. Confirmation and result lists scroll within the available
screen height.

Every row has the same columns, each a fixed width, so rows line up: a
round expand button with an outline and a chevron, one of six status chips,
the text, and the time and actions on the right. The chip column is as wide
as the widest chip. The expand button and the chip are centred vertically
on the text lines. The title starts with the row's type: `Run`, `Flow` or
`Panel`. The title and second line each show one line, cut with an
ellipsis; the whole text is in the expanded details. Chips are 22 pixels tall with
6-pixel corners. The status chips:

| Chip | States | Colour |
|---|---|---|
| Starting | A run or flow's `starting` | Grey |
| Running | `running`, a flow's `checking` | Blue |
| Waiting for Claude | A run's `waiting`, a flow's `stopped`, or a starting, running or waiting run whose parent flow is stopped for the lead | Amber |
| Orphaned | A run whose starting command ended without collecting it (`orphaned` in `emb run --status`) | Amber |
| Done | A run's `closed`, a flow or panel's `done` | Green |
| Incomplete | A panel's `incomplete`, a flow's `cancelled` | Purple |
| Failed | `failed`, a gone flow runner or panel process | Red |

The second line does not repeat the status the chip shows; the expanded
record shows it. A flow's second line includes any reason. A cancelled
flow appears in Finished with Incomplete and its reason. A starting,
running or checking flow whose lifetime lock is not held and whose record
has not changed for more than sixty seconds stays in Live with Failed and
`runner gone`. A running panel uses Failed and `panel process gone` under
the same conditions. A stopped flow needs no runner. An unreadable lock
table or runtime folder leaves the stored status on screen. A run whose
parent flow is stopped for the lead says `Waiting for Claude's answer to
flow <id>` on its second line and is not shown as Orphaned.

Worker runs are nested under their parent, in the parent's section, and
keep their own status chip and controls. Their ids are collected from the
same record fields and flow events as Clear uses. A run named by multiple
parents appears under the oldest parent by start time and then id. A run
with a parent never appears at the top level in Overview, including when
that parent falls beyond the Finished cap. A live worker under a Finished
parent stays nested there; the tray and sidebar count it at the top level.
Every readable nested run of a shown parent appears, whatever its age,
oldest first by start time and then id, indented one column. A nested row
shows its times as any run row does. In Finished, a parent's nested runs are hidden until its
details are expanded, and its second line adds `1 run` or `<n> runs`; a
Finished parent with a live nested run keeps them shown. The parent's
second line starts with `1 live run` or `<n> live runs` when that count is
above zero.

A flow's title, after `Flow`, shows the repository folder, task count and
merged count.
Its second line includes its branch, active plan wave and reason. Expanded,
it shows the pass count and task, writer and review details. A panel's title,
after `Panel`, shows its member and answer counts; its second line includes the repository
folder. Expanded, a panel shows its whole question under `Question`, then
member status, worker, duration, reason and changed paths, and left-out
members.

Expanded panel details have a Summary button for `summary.md`. Expanded
flow details have Briefs for the `briefs/` folder and one Check logs button.
Check logs considers only files matching `check-*.log` directly in the flow
folder. It selects the highest integer `(wave, attempt)` from
`check-<wave>-<attempt>.log`; if no name parses, it selects the newest file
by modification time. The buttons stay enabled; one whose file or folder is
absent says why on a line under the buttons, such as `Summary: the panel
writes summary.md when it ends.` Targets are checked when details expand,
on every click and on every model reload
while those details are drawn. Collapsed rows do not check targets.
Buttons pass a file URL to `QDesktopServices.openUrl` for the desktop's
default viewer. If it returns false, the line under the buttons reads
`No viewer opened <path>.`

A run's title, after `Run`, shows its subject on one line, or engine, model
and effort for a run without one. Its second line shows engine, model and
effort when the title holds the subject, then the repository folder, the
result and the times:

- live: `turn <n>` and the commits so far (`1 commit`, `<n> commits`), or
  `read-only`; then `started <start> · <elapsed>`, the elapsed time to the
  second, such as `4 min 12 s`;
- done: the commit count or `read-only`; failed: the last reason, or
  `No reason recorded`; then `<start> to <end>`, such as `08:17 to 08:33`.

The start names its day only when it falls on an earlier day than the end,
or than now while live: `yesterday 23:50 to 00:10`. Flow and panel rows end
their second line with the same times. The right column of an ended row
shows `Elapsed` and its duration: `Elapsed 2 min 5 s` under an hour,
`Elapsed 1 h 12 min` from an hour on. It is empty while the row is live,
and has no tooltip. The branch is in the expanded record. Live runs have Open terminal, which
opens a terminal window on the run as `emb run --attach` does. Expanding a
run shows its record, including last exit and reason; a live run's Ended is
`Not yet`. The record's run id, repository and branch are set in the code
font. Rows and expanded
details survive unrelated record changes. Elapsed times update every second
while shown.

One live run and one ended run can be expanded at a time. An expanded live
run's record follows the run while it is live, and collapses when the run
ends. Overview shows no worker's screen; Open terminal does. One flow and one panel can also have
expanded details.

The subtitle uses the tray status line, counting the same top-level live
runs, flows and panels. Above the sections, an alert names each enabled
engine whose sandbox setting gives it no sandbox, with Open Engines.

A notice says `A worker could not read <path>` for each reported hidden
path whose offered folder (or matched path when none is offered) is not
already covered by `sandbox.read`, except entries with reason `startup file`.
It names the run and its flow or panel when present. Allow reading appears
only for a non-null `allow_path`.
Its confirmation says `Every future worker can read <folder>. No worker
can change it.` Confirming adds that folder to `sandbox.read`; newly
started runs use it. The app checks the folder's eligibility again on
confirmation.

A secret location's notice says the folder holds credentials; a protected
location's says it is protected from workers. Both have Dismiss only.
A `hidden` notice without an offered folder says `No folder can be offered
because each folder that holds it also holds private files.` It has Dismiss only and gives no
`sandbox.read` suggestion. Older records that classify a startup file as
`hidden` still show this notice.
Dismiss hides the path until a later run or turn reports it again.
Dismissals persist in the app's state file.

Live has one line naming the non-zero counts of unreadable runs, flows and
panels; the line is hidden when every count is zero.

When the runs folder cannot be read, the screen shows the reason and Try
again in place of the sections. A failed flow and panel read shows an alert
with Try again and keeps the previous records. The initial loading state
waits for both record models.

### Team

Team shows the standing worker menu in seven cards: Single tasks, Planning,
Work of several tasks, Flows, Claude Workflows, Provider capacity and Panel.
Single tasks holds the Writer and Reviewer pickers
(`single.writer`, `single.reviewer`). Each lists Ask each time (`ask`), then,
for Codex, OpenCode and Claude in that order, each engine that is on and has
shortlisted models, under the engine's name, with its models as
`<model> · <effort>` (OpenCode entries without an effort show the model
alone). The engine names are headers and cannot be chosen. The closed
picker names the engine: `Codex · gpt-6-astra · high`. While a picker
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

Planning holds Gap check reviewer (`gapcheck.reviewer`), a picker of Same
as Reviewer (`same`) and the same models as Reviewer; closed on Same as
Reviewer it shows what that resolves to, such as `Same as Reviewer · Codex ·
gpt-6-astra · high`, as Flow writer and Flow reviewer do. Run the gap check
(`gapcheck.when`) is a picker of Off, Always, 2+ tasks to 10+ tasks and Ask
each time; in its open list each option has a hint line with its effect,
such as "Plans of 1 to 4 tasks start without one." under 5+ tasks; and
Gaps Claude asks you about (`gapcheck.ask`): High only, High and medium,
All.

Work of several tasks holds Multi-task work (`multi.runs_as`): Ask each time
(`ask`), Flow (`flow`) or Claude Workflow (`workflow`). Flows shows unless
it is Claude Workflow, and Claude Workflows unless it is Flow.

Flows holds Flow writer and Flow reviewer (`flow.writer`, `flow.reviewer`),
pickers that start with Same as Writer and Same as Reviewer; Max review
rounds (`flow.review_rounds`): 1, 2, 3 or 4, the most times a reviewer
can check one flow task; Max tasks at once (`flow.parallel`): 1, 2, 4, 6,
the most flow tasks that run at the same time; Stall
after (`flow.stall`): 5, 10, 20 min; and Review the whole flow
(`flow.whole_review`), a switch. Claude Workflows holds Max agents
at once (`workflow.agents`): 4, 8, 16, the most agents that run at the
same time; and When an agent runs late
(`workflow.late`): Ask me, Wait once more, Stop. These are available even
when no engine offers a model.

Provider capacity holds Capacity retries (`provider.capacity_retries`):
Off (`0`) or Up to 1 through Up to 10, default Up to 2; and Wait before a
retry (`provider.capacity_wait`): 5s through 600s, default 30s. The retries
row names the limit on resends of one turn. A change applies to runs
started after it; existing runs keep both settings for later turns and
recovery. The model and effort stay the same on a retry.

The Panel card's description is the tooltip of an info icon after its
title.

Panel lists `panel.members` in saved order. A member shows
`<Engine> · <model> · <effort>`, omitting effort when none is set. A member
whose engine is off, was not connected at its last check, or whose model is
not shortlisted shows its saved `<engine>:<model>` value and a warning
explaining how to change it. An invalid value also shows a removal warning.
Each member has a Remove button. Add member lists shortlisted models of
engines that are on, grouped by engine, excluding saved members. Choosing
one appends it and returns the picker to Add member. With no members the
card says "No members. Claude asks for members, or uses the ones it is given,
each time." At eight members Add member is hidden and the card says
"A panel has at most eight members."

A member missing from its shortlist hides Add member, even when its
engine is off or not connected. The warning asks the owner to remove those
members or shortlist them in Engines. Remove saves only when every remaining
member is shortlisted. Otherwise it sends no save, names the first remaining
member to shortlist, and shows Remove all, which saves an empty list. An
engine being off or not connected alone does not block saving. Add member
is also hidden when no unused shortlisted model is offered: with no
shortlist the screen says to shortlist models in Engines, and with every
shortlisted model already a member the card says "Every shortlisted model is
a member. Shortlist more in Engines to add one."

One-time choices are not shown. Each change saves at once with the settings
generation check; choosing an already saved multi-task value saves nothing.
Panel mutation controls stay disabled until the save resolves and the screen
refreshes. A stale save reloads the settings and shows the app's stale alert.

### Engines

Engines shows each engine's connection status, Use and Sandbox switches,
requirements and model list. Refresh list reads models from their source.
Model switches save shortlist entries; OpenCode entries keep the full
integration-prefixed id and any selected effort.

The OpenCode table has Model, Provider, Context, Input $/M and Output $/M
columns, alongside effort and shortlist controls. Provider is the serving
integration's name in plain text. Both price cells show "Subscription" for
models whose integration's login method is OAuth. Other prices are shown
per million tokens; unknown figures show a dash.

The Model column is as wide as the longest id when the other columns leave
room; otherwise it takes the room they leave, down to 140 logical pixels,
and longer ids end in an ellipsis. Provider fits the longest integration
name, up to 130 pixels; Context and the price columns fit their longest
values and headers. A window too narrow for the table at that width scrolls
the table sideways inside its own area; the screen never widens past the
window.

Search models matches the model id or label, ignoring case. The filter row
has two lines: Provider and Tools, with the model count at the end, then
Max price and Min context. Each label is followed by an info icon.
Provider offers All providers followed by each integration by name, including
integrations with no models. Each option has its total model count as a hint
line, before other filters. After a refresh, the selection is kept by
integration id; if it is absent, the picker returns to All providers.
Provider filters the serving integration; a model served through OpenRouter
has provider OpenRouter regardless of its maker.

The info icons read:

| Filter | Tooltip |
|---|---|
| Provider | "The OpenCode integration that serves the model. Ember lists the integrations you connected with /connect in OpenCode." |
| Tools | "Shows only models that take tool calls. A worker needs tool calls to read and change files." |
| Max price | "No more than this input price per million tokens. Models that show Subscription always pass." |
| Min context | "At least this many tokens of context: how much text the model takes in at once." |

Tools starts on and keeps models that report tool support. Switching it off
removes that restriction. The price and context pickers start at Any. Filters
combine; a model with an unknown detail passes Any and fails a restriction
on that detail. OAuth models always pass the price filter. Other filters
still apply to them.

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
is not added again. These paths are readable by future sandboxed workers.
Secret and configuration folders can be added by hand, except Ember's
configuration and state folders. Adding either of those, a descendant or
a symlink alias is refused with the reason. The same restriction applies
to `emb choices --set sandbox.read=...`.

### Files

| File | Holds |
|---|---|
| `$XDG_STATE_HOME/ember/app.json` | The window's `width`, `height`, last `screen` and `sidebar_hidden` (defaults to `false`), and Overview's `finished_open` and `denied_dismissals` (resolved paths to dismissed run-and-turn occurrences), mode 600; content that cannot be read gives the defaults |
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
