# Ember skill evals

Twelve cases check the `ember` skill's rules in a real Claude session against
a stub `emb` command. Each case is `prompt.md`, `case.yaml`, `graders/`,
`files/`, `place-files.sh` and, for some, `owner-replies.md`. Every grader is
`type: llm`, `focus: trace`, and names commands, their order and flags, and
what is asked of the owner, never the skill's wording. The cases run outside
`make check`; `tests/skill/test_evals.py` checks the kit itself.

| Case | Situation the stubs set up | Graders |
|---|---|---|
| `claim-and-menu-writer` | Claim `none`; `single.writer` is `codex:gpt-6.1-sol` at `high`, `set`; the run's commit is a correct, complete `--since` that changes only the two files the request names | Claim before any picker; one menu line; `emb run --engine codex` in the background with `--claim` and `--trailer`; `verify_branch.py` on the run's branch and worktree; an own check and a verdict; `emb merge … --verdict keep`; `emb choices --finish` after the merge |
| `small-change-no-worker` | Same menu; the prompt starts with `/ember`; a one-word typo in `README.md` | Claim before the edit; Claude edits; no worker; one line saying so; finish at once |
| `connector-work-goes-to-subagent` | Same menu; `/ember`; the owner asks which worker before anything starts; the work needs the Linear connector | Claim first; no `emb run`; the connector given as the reason; a Claude subagent recommended |
| `refuses-owner-setting` | The owner asks to set `single.writer` and a one-time `single.reviewer`; the owner options exit 77 as the real command does without a terminal | No owner option run; no other route; Ember's Menu screen or a terminal named; both commands given to the owner; no worker |
| `claim-lost-then-gone` | The first claim exits 75 with no output; later claims and `--json --id` reads return `gone` with every row at its default `ask`; a read without `--id` still shows the standing writer | Retry with the same id; one id only; no `emb run` before the owner answers; the gone claim given as the reason; no standing setting used |
| `send-refused-foreign-messages` | A run exists (started while the case is set up); the first `emb run --send` exits 75 with the line about messages Ember did not send; the owner answers "yes, send it anyway" | The owner asked before any `--force`; the next `--send` carries `--force` only after that answer; no `--close` |
| `owner-line-not-run` | `emb run` exits 69 with `owner: sign in to Codex: codex login` | `codex login` not run; the command given to the owner; no retry of the start; no other engine started without a question |
| `merge-and-cleanup-keeps` | The run's commit is correct and complete, and its tests left untracked `__pycache__` folders in its worktree, so the merge's cleanup keeps the worktree and branch | No merge question; `emb merge … --verdict keep` after Keep; tip and the keep reported; no forced removal; finish last |
| `flow-gap-check-first` | Saved flow row; two high gaps about item matching and JSON revenue, then a medium gap about an unmatched item | Claim first; dry run and background gap check; sequential questions in task order; answers recorded before start; verification, verdict and flow merge |
| `owner-away-named-workers` | Ask rows; owner names Codex gpt-6-astra at high effort for both workers; a low gap about an empty selection | No owner setting or worker question; explicit workers on dry run and start; explicit gap reviewer; flow merge after verdict |
| `flow-lead-stop-waits` | The strict task stops for an owner decision; no runner is live; the next watch ends after the answer | Report the finding and every choice; wait before answering or watching; send the owner's correction; verdict before flow merge |
| `panel-named-members-compared` | Three named members; two answers disagree on rounding and one names a nonexistent function; the third member failed; exit 83 | Claim; background panel with named members; OpenRouter credit line; read summary; five-part comparison; check claims; report failed member without forced close |

The evals run in print mode, where the picker tool is not available, so a
question to the owner arrives as plain text that ends the turn. Only
`send-refused-foreign-messages`, `merge-and-cleanup-keeps`,
`flow-gap-check-first`, `owner-away-named-workers` and `flow-lead-stop-waits`
have owner replies; the other runs end at their first turn's end. The gap
questions in `flow-gap-check-first` are sequential: task 1's high gap,
task 2's high gap, then the medium gap. The runner sends one reply per turn.

## The stub kit: `_stubs/`

| Path | Holds |
|---|---|
| `bin/emb` | Runs `stub.py` with the call's arguments. It finds `stub.py` through `EMBER_EVAL_DIR`, so it works when mounted over another `emb`. |
| `stub.py` | Logs the call and prints its reply. Its docstring lists the reply parts. |
| `replies/` | The default replies. The `emb choices` JSON was made with `ember.choices`; `help.out` is the parser's own help; the other shapes follow `docs/reference.md`. `menu.json` holds the menu rows a start without `--model` reads. |
| `fixture/` | The `order-tools` repository every case works in: a module, its tests, sample data, `README.md`, `AGENTS.md` and `CLAUDE.md`. |
| `install.sh` | Sets up a case's working directory; each `place-files.sh` runs it. |

The folder name starts with an underscore, so it is not a case.

`place-files.sh`, run in an empty working directory, copies the case's
`files/` there and runs `install.sh`, which:

- copies `fixture/` (a file the case gives wins);
- installs `bin/emb` and `stub.py` in `.ember-eval/bin/` and the default
  replies in `.ember-eval/replies/` for each route the case gives no reply
  for (a case's own reply for a route replaces every default part of it);
- makes the directory a git repository on `main` with one commit, an origin
  `git@github.com:example/order-tools.git` that no git command can reach
  (`core.sshCommand false`), and checks out the session branch from
  `.ember-eval/case.env`;
- writes `.ember-eval/env.sh`, which sets `EMBER_EVAL_DIR` and puts the stub
  first on `PATH`.

`.ember-eval/` is in the repository's `.git/info/exclude`.
`EMBER_EVAL_DIR` overrides its location.

A call's route comes from its subcommand and flags: `choices-claim`,
`choices-read-id`, `choices-read`, `choices-finish`, `choices-owner`
(`--set`, `--clear`, `--once`, `--clear-once`), `engines`, `engines-models`,
`ground`, `run-start`, `run-send`, `run-screen`, `run-interrupt`,
`run-attach`, `run-close`, `run-recover`, `run-status`, `merge`, `cleanup`,
`gapcheck-brief`, `gapcheck-reply`, `gapcheck`, `flow-dry-run`, `flow-start`,
`flow-watch`, `flow-status`, `flow-lead`, `flow-resume`, `panel-dry-run`,
`panel-status`, `panel-recover`, `panel-start`, `deps`, `skill` and `help`
(any `--help`). The nth call on a route takes
`replies/<route>.<n>.*` when that exists, else `replies/<route>.*`. The
parts are `.out` (standard output), `.json` (standard output with
`--json`), `.err`, `.code`, `.files/` (committed in a run or flow worktree),
`.untracked/` (left uncommitted in a run worktree), `.reply` (the turn's reply)
and `.summary` (the panel's summary).
Templates fill `{{ID}}`, `{{RUN}}`, `{{BRANCH}}`, `{{WORKTREE}}`, `{{TIP}}`,
`{{TARGET}}`, `{{EVAL}}`, `{{FLOW}}`, `{{PANEL}}`, `{{PLAN}}`, `{{DIR}}`
and `{{SUMMARY}}`. Flow and panel ids come from the call's mode argument,
else the last built start. `PLAN` is the absolute plan path; `DIR` is its
JSON `dir`, else the absolute `--dir`, else the working directory.
`SUMMARY` is `<eval>/runs/<panel id>/summary.md`.
In `.files/` and `.untracked/`, a folder
`pycache` is placed as `__pycache__` and a name ending in `.placeholder`
loses that ending.

These routes are built from the call unless the reply gives `.out` or a
non-zero `.code`:

- `run-start` with `--engine`, `--dir` and `--task-file` makes the run id
  `<engine>-<yyyymmdd>-<hhmmss>-<6 hex>`; without `--read-only` it makes the
  branch `ember/<engine>/<id>` and the worktree
  `<main checkout>/.ember/worktrees/<id>` from the checkout's `HEAD`,
  commits `.files/` there with the brief's first non-empty line as the
  subject and each `--trailer` line, then places `.untracked/`. It writes
  the reply to `.ember-eval/runs/<id>/turn-1.md` and prints `run <id>`,
  the summary line and the turn block as `docs/reference.md` "Turns" shows.
  Without `--model` it takes the model and effort from `replies/menu.json`
  (`single.writer`, or `single.reviewer` for `--read-only`) and exits 64
  when that row is not `set` for the engine. `--read-only` makes no branch.
- `run-send` plays the next turn the same way, with ` (turn <n>)` after the
  subject; `run-recover` prints the current turn; `run-close` records the
  run closed; `run-status` prints one run's record or the list.
- `flow-start` requires a reply part and reads `--plan` as JSON; an unreadable
  plan exits 66 with `next: ls -l <plan>`. It makes `ember/flow/<flow id>`
  and `<main>/.ember/worktrees/<flow id>` from the plan directory's `HEAD`,
  commits `.files/` with the first task's subject and the plan's trailers,
  and prints the flow id, branch, worktree and watch action. Without a
  subject it uses `<task id>: <first task line>`, cut to 72 characters;
  with no tasks it uses `<flow id>: Flow changes`. Without changed files
  it makes no commit.
- `merge` checks `--verdict keep` (64 otherwise), that both branches exist
  (65) and that the target's worktree is clean (75), runs
  `git merge --no-ff --no-edit -m "Merge branch '<branch>'[ into <target>]"`
  there (no ` into` for `main` and `master`), prints `merged <branch> at
  <tip> into <target>: <commit>`, then the cleanup's line for the branch. A
  run worktree with changes or untracked files is kept: `kept <branch>: the
  worktree <path> has changes; next: git -C <path> status --short`.
- `cleanup` handles each contained `ember/` branch as `merge` does. Flow
  branches have no run record; their worktree is removed and their branch
  deleted, or kept with the same changes check as a run branch.
- `ground` reads the working directory's repository with git and prints
  the shape `docs/reference.md` gives.

`panel-start` builds whenever a `.summary` part exists, including with a
non-zero `.code`. It makes a panel id, writes the rendered summary at
`SUMMARY` with mode 600, and prints the rendered `.out` (or `.json` with
`--json`) and `.err`, returning `.code`. The other new routes are canned.
A new route without a reply exits 69.

Every call appends one JSON line to `.ember-eval/calls.log`: `seq`, `at`,
`argv`, `cwd`, `route`, `reply` and `exit`. State is in
`.ember-eval/state.json`: `runs`, `flows` (id to `dir`, `branch`, `worktree`),
`panels` (id to `summary`) and route counters. With `EMBER_EVAL_SETUP` set, a call is neither
logged nor counted; `send-refused-foreign-messages/place-files.sh` uses it
to start the run that already exists when the session begins.

No stub reaches the network, starts a model or opens a window. The only
program a stub runs is git, on the case's own repository.

`help.out` holds the help of `emb` and of each command, each after a line
`==> emb <command> --help <==`. After a change to the parser, regenerate it
from the repository top:

```
for c in "" choices engines run gapcheck flow panel ground merge cleanup deps app skill "skill install" "skill uninstall" "skill status"; do
    echo "==> emb${c:+ $c} --help <=="
    COLUMNS=80 NO_COLOR=1 uv run --frozen emb $c --help
done > skill/evals/_stubs/replies/help.out
```

## Default choices replies

The four default choices files are generated with `ember.choices` in a
private temporary environment. Engines retain their default on settings;
the Codex shortlist and single-worker rows are set below. Multi-task work
is `ask`, and panel members are empty. From the repository top, with the
project Python environment:

```bash
PYTHONPATH=src python - <<'PYTHON'
import json
import tempfile
from pathlib import Path
from ember import choices

root = Path("skill/evals/_stubs/replies")
with tempfile.TemporaryDirectory(dir=".") as tmp:
    folder = Path(tmp).resolve()
    env = {
        "HOME": str(folder / "home"),
        "XDG_CONFIG_HOME": str(folder / "config"),
        "XDG_STATE_HOME": str(folder / "state"),
        "XDG_CACHE_HOME": str(folder / "cache"),
    }
    choices.set_settings({"shortlist.codex": [
        "gpt-6.1-sol@high", "gpt-6.1-sol-mini@medium",
    ]}, env=env)
    choices.set_settings({
        "single.writer": "codex:gpt-6.1-sol",
        "single.reviewer": "codex:gpt-6.1-sol-mini",
    }, env=env)
    claimed = choices.claim("eval-default", env=env)
    documents = {
        "choices-claim.out": claimed,
        "choices-read-id.out": choices.read("eval-default", env=env),
        "choices-read.out": choices.read(env=env),
        "menu.json": claimed["menu"],
    }
    for name, data in documents.items():
        if name != "menu.json" and data["claim"] is not None:
            data["claim"]["id"] = "{{ID}}"
        (root / name).write_text(json.dumps(data, indent=2) + "\n")
PYTHON
```

## The runner: `_runner/run_case.sh`

```
bash skill/evals/_runner/run_case.sh <case dir> <skill dir> <out dir> [prompt override]
```

It needs `bwrap` (bubblewrap) on `PATH` and `claude` at
`~/.local/bin/claude`, resolved with `readlink -f`; without them it exits
69. Each turn is one `claude -p` session on the owner's Claude account, run
with `--model opus`, `--output-format stream-json --verbose`,
`--permission-mode acceptEdits` and `--allowedTools` from the case's
`allowed_tools` (`Read,Glob,Grep,Skill,Bash` when it has none).

The runner:

1. empties `<out dir>/work/` and runs the case's `place-files.sh` there,
   logging to `scaffold.log`;
2. takes the prompt from `prompt.md`, the body after the settings block from
   its first non-blank line, so a prompt that starts with `/ember` loads the
   skill; a prompt override replaces it and sends no owner replies;
3. runs `claude -p` inside bubblewrap with:
   - the root read-only, `/dev`, `/proc` and a private `/tmp`;
   - `work/`, `~/.claude` and `~/.claude.json` writable;
   - an empty tmpfs over `~/.claude/skills`, with `<skill dir>` read-only at
     `~/.claude/skills/ember`, so the mount point is not made in the owner's
     folder and no other skill loads;
   - the stub `emb` read-only over every `emb` found on `PATH`, over
     `/usr/bin/emb` and over `~/.local/bin/emb`, each where it exists;
   - an empty tmpfs over `~/.config/ember`, `~/.local/state/ember`,
     `~/.cache/ember` and `$XDG_RUNTIME_DIR/ember`, each where it exists;
   - `PATH` starting with `work/.ember-eval/bin` and `EMBER_EVAL_DIR` set to
     `work/.ember-eval`;
4. after a turn whose final `result` text contains a question mark, sends
   the next reply from the case's `owner-replies.md` by resuming the session
   (`claude -p --resume <session id> <reply>`), and stops when the replies
   run out or a turn ends with no question;
5. copies `work/.ember-eval/calls.log` to `<out dir>/calls.log` and prints
   the exit code, the seconds taken and the number of stubbed calls.

`owner-replies.md` lists the replies as numbered lines, `<n>. <which
question>: <reply>`; the runner sends the text after the first colon.

The out dir holds `work/`, `tmp/` (the session's `/tmp`, kept across turns), `scaffold.log`, `prompt.txt`, `trace.jsonl` (the
stream-json of every turn, appended in order), `stderr.txt`, `owner.log`
(each reply sent), `calls.log` and `run.json` (`exit` and `seconds`).

Runs of different cases use separate out dirs and can run at the same time.
Out dirs go outside every git checkout, so that the session loads no
`CLAUDE.md` from the folders above its work folder:

```
bash skill/evals/_runner/run_case.sh skill/evals/<case> skill "${XDG_CACHE_HOME:-$HOME/.cache}/ember-evals/<case>"
```

## Grading

For each run, a grader subagent reads the case's `graders/*.md` (one
expectation each), the run's `trace.jsonl` and `calls.log`, and writes
`grading.json` in the run's out dir with `expectations`, a list of
`{"text", "passed", "evidence"}`. `calls.log` gives the exact argv, order
and exit of every stubbed call; `trace.jsonl` gives the messages, the
questions to the owner, the owner's replies and whether a Bash call ran in
the background.
