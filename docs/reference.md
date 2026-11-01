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
| `shortlist.opencode` | List of `<provider>/<model>`, with optional `@<effort>`; each part `[A-Za-z0-9][A-Za-z0-9._-]*`, the model part may also contain `:` | `[]` | standing |
| `single.writer`, `single.reviewer` | `ask`, or `<engine>:<model>` with the model on that engine's shortlist when saved | `ask` | request, one-time, standing |
| `single.writer_as`, `single.reviewer_as` | `cli`, `subagent` | `cli` | request, one-time, standing |
| `sandbox.codex` | `bubblewrap`, `codex-bundled`, `none` | `bubblewrap` when `bwrap` is on `PATH`, else `none` | standing |
| `sandbox.opencode` | `bubblewrap`, `none` | `bubblewrap` when `bwrap` is on `PATH`, else `none` | standing |
| `sandbox.read` | List of absolute paths, no duplicates | `[]` | standing |
| `terminal` | A non-empty command line in shell syntax | `xdg-terminal-exec` | standing |
| `app.autostart` | `on`, `off` | `on` | standing |
| `app.theme` | `system`, `light`, `dark` | `system` | standing |
| `notify.run_finished`, `notify.run_failed` | `on`, `off` | `on` | standing |

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
| `opencode` | `curl -fsSL https://opencode.ai/install \| bash` | `opencode auth login` |
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

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 64 | Usage error or invalid value; `emb` with no command prints help to standard error and exits 64 |
| 74 | The settings database cannot be opened or read |
| 75 | Stale generation or busy database |
| 77 | An owner option without a terminal |
| 78 | Unsupported settings database schema |
