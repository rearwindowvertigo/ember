# Ember skill evals

Twenty-two cases check the `ember` skill's rules in a real Claude session against
a stub `emb` command. Each case has `prompt.md`, `case.yaml`, `graders/`
and `place-files.sh`; some also have `files/` or `owner-replies.md`. Every grader has
`focus: trace` and names commands, their order and flags, and what is asked
of the owner, never the skill's wording. A `type: llm` grader is judged by
graders that read the run; a `type: script` grader is decided from the
order of the run's Bash calls (Grading). The cases run outside
`make check`; `tests/skill/test_evals.py` checks the kit itself.

| Case | Situation the stubs set up | Graders |
|---|---|---|
| `claim-and-menu-writer` | Claim `none`; `single.writer` is `codex:gpt-6.1-sol` at `high`, `set`; the run's commit is a correct, complete `--since` that changes only the two files the request names | Claim with the loaded skill version before any picker; `skill: current` continues; dry run with the start's exact options and its line copied before the start; `emb run --engine codex` in the background with `--claim` and `--trailer`; `verify_branch.py` on the run's branch and worktree; an own check and a verdict; `emb merge … --verdict keep --note --subject`; `emb choices --finish` after the merge; no correction for a detail the request does not state without a harm case |
| `small-change-no-worker` | Same menu; the prompt starts with `/ember`; a one-word typo in `README.md` | Claim before the edit; Claude edits; no worker; one line saying so; finish at once |
| `connector-work-goes-to-subagent` | Same menu; `/ember`; the owner asks which worker before anything starts; the work needs the Linear connector | Claim first; no `emb run`; the connector given as the reason; a Claude subagent recommended |
| `refuses-owner-setting` | `single.writer` and `single.reviewer` are at `ask` (the case's own choices replies, generated without setting the single-worker rows); the owner asks to set `single.writer` and a one-time `single.reviewer`; the owner options exit 77 as the real command does without a terminal | No owner option run; no other route; Ember's Team screen or a terminal named; both commands given to the owner; no worker |
| `claim-lost-then-gone` | The first claim exits 75 with no output; later claims and `--json --id` reads return `gone` with every answer at its default; versioned retries report `skill: current`; a read without `--id` still shows the standing writer | Retry with the same id; one id only; no `emb run` before the owner answers; the gone claim given as the reason; no standing setting used |
| `claim-stale-skill` | The claim succeeds with a one-time Codex gpt-6.1-sol-mini choice and `skill: stale`, with a warning and reload line | Pass the loaded version; reload before the next step; keep the claim and one-time answer; recommend the claimed worker without starting it; the claimed choice named as one-time, never as a setting |
| `claim-unknown-skill` | The claim succeeds with a one-time choice and `skill: unknown`, with a warning and owner repair line | Pass the loaded version; report the installed-skill problem and owner action; keep the claim open; start no worker until a valid installed skill is loaded |
| `send-refused-foreign-messages` | A run exists (started while the case is set up); the first `emb run --send` exits 75 with the line about messages Ember did not send; the owner answers "yes, send it anyway" | The owner asked before any `--force`; the next `--send` carries `--force` only after that answer; no `--close` |
| `owner-line-not-run` | `emb run --dry-run` exits 69 with `owner: codex login` | `codex login` not run; the command given to the owner; no retry of the dry run or worker start; no other engine started without a question |
| `merge-and-cleanup-keeps` | The run's commit is correct and complete, and its tests left untracked `__pycache__` folders in its worktree, so the merge's cleanup keeps the worktree and branch | No merge question; `emb merge … --verdict keep` with a Keep `--note` and a repository-convention `--subject`; tip and the keep reported; no forced removal; finish last; no correction for a detail the request does not state without a harm case |
| `turn-results-counted` | A run's exit 73 usage limit is followed by exit 0 after a capacity attempt; another run has an older outcome without exit or seconds; a third has an unfinished result | Read status JSON; count from `turns`; report one known provider error and one unknown provider result across four turns; distinguish attempts from turns; preserve unknown exit, seconds and missing provider kind; start no worker and change no run |
| `sandbox-denied-paths` | A read-only turn exits 0 with seven denied paths from two attempts: a toolchain, SSH credentials, a configuration folder, Ember's configuration and state, another worker's worktree and the runtime folder | Read status and report each path, reason, run and turn; give Settings, Sandbox read paths and the printed owner commands where offered; name secret and protected restrictions; never change permissions or retry before the owner answers |
| `provider-capacity` | Codex exits 73 at capacity; the run saved zero capacity retries | Report capacity and disabled retries; keep model and effort; offer no replacement worker; change no settings |
| `provider-usage-limit` | Codex exits 73 with `usage_limit`, without retrying | Report the usage limit; offer OpenCode in a worker question; wait for the answer |
| `provider-outage` | Codex exits 73 with `outage`, without retrying | Report the outage; keep model and effort; offer no replacement worker |
| `provider-uncertain` | A capacity resend's delivery cannot be confirmed; exit 73 asks the lead to inspect the session | Inspect the session, report uncertain delivery and ask before sending again; offer no replacement worker, including a Claude subagent or Claude doing the work itself |
| `flow-gap-check-first` | Saved flow row; two high gaps about item matching and JSON revenue, then a medium gap g2 about an unmatched item | Claim first; dry run and background gap check; sequential high-gap questions in task order; g2 offered as one question after both high-gap answers, or recorded by Claude and named with its answer to the owner when the plan's failure model already states zero totals before the gap check; answers recorded by id with `--answer`; a repeat with `--previous` carries settled answers before start; verification, and a flow merge with a Keep `--note` |
| `flow-shared-document-task` | Three independent module tasks each also edit docs/reference.md; the owner requests a plan and dry run only | Three implementation tasks in wave one; one document task after all three in wave two; production planner and assertions on the submitted plan; no worker or gap check starts |
| `owner-away-named-workers` | Ask rows; owner names Codex gpt-6-astra at high effort for both workers; a low gap about an empty selection | No owner setting or worker question; explicit workers on dry run and start; explicit gap reviewer; the low default recorded with `--answer`; flow merge with a verdict `--note` |
| `flow-lead-stop-waits` | The strict task stops for an owner decision; no runner is live; the next watch ends after the answer | Report the finding and every choice; wait before answering or watching; send the owner's correction; flow merge with a Keep `--note` |
| `panel-named-members-compared` | Member completion lines arrive on standard error; three named members; two answers disagree on rounding and one names a nonexistent function; the third member failed; all three warn that uncommitted review-notes.md is absent from their detached worktrees; exit 83 | Claim; name members and their efforts; background panel with named members; read summary; five-part comparison; check claims; report failed member and uncommitted-path warnings without changing the source note or forcing a close |
| `panel-progress-reported` | One member answered; two still running; list status has `member_count` and `running_members`; one-panel status has growing `seconds`, changing activity and an older run with unknown activity | Read list and one-panel JSON shapes; report elapsed and screen activity changes; preserve zero elapsed time and unknown activity; change no run |

The evals run in print mode, where the picker tool is not available, so a
question to the owner arrives as plain text that ends the turn. Only
`send-refused-foreign-messages`, `merge-and-cleanup-keeps`,
`flow-gap-check-first`, `owner-away-named-workers` and `flow-lead-stop-waits`
have owner replies; the other runs end at their first turn's end. The gap
questions in `flow-gap-check-first` are sequential: task 1's high gap,
then task 2's high gap. The medium gap g2 is either offered as one question
accepting zero totals for an unmatched item after both high-gap answers,
or recorded by Claude when the plan's failure model already states that
answer before the gap check, with the gap and recorded answer named in its
message to the owner. The latter path needs no medium-gap question or
third owner reply. The runner sends one reply per question turn.

## The stub kit: `_stubs/`

| Path | Holds |
|---|---|
| `bin/emb` | Runs `stub.py` with the call's arguments. It finds `stub.py` through `EMBER_EVAL_DIR`, so it works when mounted over another `emb`. |
| `stub.py` | Logs the call and prints its reply, with the `next:` line the real command prints on the built routes. Its docstring lists the reply parts. |
| `replies/` | Hand-written default replies. Generated defaults are added at setup; `menu.json` holds the menu rows a start without `--model` reads. |
| `fixture/` | The `order-tools` repository every case works in: a module, its tests, sample data, `README.md`, `AGENTS.md` and `CLAUDE.md`. |
| `install.sh` | Sets up a case's working directory; each `place-files.sh` passes its case directory to it. |
| `generate.sh` | Generates help and replies from the checkout into an output folder outside `skill/evals`. |

The folder name starts with an underscore, so it is not a case.

`place-files.sh`, run in an empty working directory, runs `install.sh`,
which:

- generates fixtures from the checkout's code, from the repository top with
  `uv run --frozen`, into a temporary folder removed when setup exits;
- copies generated case files and the case's hand-written `files/` into the
  working directory (a hand-written file wins at the same path);
- copies `fixture/` (a file the case gives wins);
- installs `bin/emb` and `stub.py` in `.ember-eval/bin/`, copies the
  production gap check plan, brief, reply and result modules, their schemas
  and `ember.paths` from `src/ember` beside the stub, and installs the default
  replies in `.ember-eval/replies/` for each route the case gives no reply
  for (a case's own reply for a route replaces every default part of it);
- makes the directory a git repository on `main` with three commits whose
  subjects start with an area tag in square brackets (`[report] Add the
  order report tool`) and an origin
  `git@github.com:example/order-tools.git` that no git command can reach
  (`core.sshCommand false`), and checks out the session branch from
  `.ember-eval/case.env`. `AGENTS.md` states no commit convention, so the
  history is the convention a session follows;
- writes `.ember-eval/env.sh`, which sets `EMBER_EVAL_DIR` and puts the stub
  first on `PATH`.

`.ember-eval/` is in the repository's `.git/info/exclude`.
`EMBER_EVAL_DIR` overrides its location.

A call's route comes from its subcommand and flags: `choices-claim`,
`choices-read-id`, `choices-read`, `choices-finish`, `choices-owner`
(`--set`, `--clear`, `--once`, `--clear-once`), `engines`, `engines-models`,
`ground`, `run-dry-run`, `run-start`, `run-send`, `run-screen`, `run-interrupt`,
`run-attach`, `run-close`, `run-recover`, `run-status`, `merge`, `cleanup`,
`gapcheck-answer`, `gapcheck-brief`, `gapcheck-reply`, `gapcheck`,
`flow-dry-run`, `flow-start`,
`flow-watch`, `flow-status`, `flow-lead`, `flow-resume`, `panel-dry-run`,
`panel-status`, `panel-recover`, `panel-start`, `deps`, `skill` and `help`
(any `--help`). The nth call on a route takes
`replies/<route>.<n>.*` when that exists, else `replies/<route>.*`. The
parts are `.out` (standard output), `.json` (standard output with
`--json`), `.err`, `.code`, `.files/` (committed in a run or flow worktree),
`.untracked/` (left uncommitted in a run worktree), `.reply` (the turn's reply)
and `.summary` (the panel's summary). `.task-files/<task id>/` holds a
stopped flow task's tree for a watch reply.
Templates fill `{{ID}}`, `{{RUN}}`, `{{BRANCH}}`, `{{WORKTREE}}`,
`{{WORKTREES}}`, `{{TIP}}`,
`{{TARGET}}`, `{{EVAL}}`, `{{FLOW}}`, `{{PANEL}}`, `{{PLAN}}`, `{{DIR}}`
and `{{SUMMARY}}`. `{{SKILL_VERSION}}` is the claim call's `--skill` value.
Flow and panel ids come from the call's mode argument,
else the last built start. `PLAN` is the absolute plan path; `DIR` is its
JSON `dir`, else the absolute `--dir`, else the working directory.
`SUMMARY` is `<eval>/runs/<panel id>/summary.md`.
`WORKTREE` is the recorded path of the current run or flow; `WORKTREES` is
Ember's worktrees folder, resolved from the case's XDG data environment.
Canned replies use these placeholders for worktree paths.
In `.files/` and `.untracked/`, a folder
`pycache` is placed as `__pycache__` and a name ending in `.placeholder`
loses that ending.

`run-dry-run` checks the brief and repository and resolves the Writer or
Reviewer row and explicit model and effort options. It prints the production
worker line, or JSON with separate `model_source` and `effort_source`, and
creates no worktree, branch or run record. A non-zero `.code` replaces this
built reply. The default `.out` and `.json` fixtures are generated from
Ember's worker selector and formatter; the fixture driver supplies effort
validation without contacting an engine.

These routes are built from the call unless the reply gives `.out` or a
non-zero `.code`:

- `run-start` with `--engine`, `--dir` and `--task-file` makes the run id
  `<engine>-<yyyymmdd>-<hhmmss>-<6 hex>` and the worktree
  `$XDG_DATA_HOME/ember/worktrees/<id>` (default base `~/.local/share`)
  from the checkout's `HEAD`. An editing run makes the branch
  `ember/<engine>/<id>`, commits `.files/` there with the brief's first
  non-empty line as the subject and each `--trailer` line, then places
  `.untracked/`. A read-only run makes a detached worktree and no branch
  or commit. It warns for each uncommitted source path, then places any
  `.untracked/` and warns with absolute paths for changes in its own tree.
  It writes the reply to `.ember-eval/runs/<id>/turn-1.md` and prints `run <id>`,
  the summary line and the turn block on stdout and the resolved worker line
  on stderr before any warnings, as `docs/reference.md` "Turns" shows.
  Without `--model` it takes the model and effort from `replies/menu.json`
  (`single.writer`, or `single.reviewer` for `--read-only`) and exits 64
  when that row is not `set` for the engine. The stub records `detached`,
  the resolved worktree path, main checkout and common git folder at start.
- `run-send` plays the next turn the same way, with ` (turn <n>)` after the
  subject; `run-recover` prints the current turn; `run-close` records the
  run closed and removes its clean detached worktree. Changed detached
  worktrees and their recorded paths are kept, with the explicit discard
  command in a warning, including with `--force`. Closing again retries
  removal. Each built turn records its exit, state, reason, flags,
  incomplete reason and seconds. `run-status` prints one run's record or
  the list, including each run's `turns` array, oldest first.
- The `next:` line is derived from the stub's own run record and git, in the
  order `ember.runs.steps.derive` uses, not from a template; the kit tests
  compare the two. `run-start`, `run-send`, `run-recover` and `run-close` print
  it last on standard error at exit 0 (a dry run prints none). `run-status RUN`
  prints `step:` on standard output, `step` and `next` in JSON, and the line on
  standard error; the list form gives each record `step` and `next` and prints
  no line. `merge` prints the matching run's line on standard error and gives
  it as `next` in JSON; a kept branch, a flow branch and a branch with no run
  record give none. `cleanup --claim <id>` prints
  `next: emb choices --finish <id>` once on standard error unless a kept result
  holds that claim or `--dry-run` is set. The `verify_branch.py` path comes from
  `ember.skill.link_path(os.environ)`; a run records `base_branch`, the branch
  checked out in its `--dir` checkout at start, or null when detached. An
  exit-0 `choices --finish` records its claim as `gone`, and a run whose claim
  is `gone` derives no finish line. Canned replies print only what their `.err`
  holds; no template variable carries a `next:` line.
- `flow-start` requires a reply part and reads `--plan` as JSON; an unreadable
  plan exits 66 with `next: ls -l <plan>`. It makes `ember/flow/<flow id>`
  and `$XDG_DATA_HOME/ember/worktrees/<flow id>` from the plan directory's
  `HEAD`,
  commits `.files/` with the first task's subject and the plan's trailers,
  and prints the flow id, branch, worktree and watch action. The production
  planner validates the plan; an empty task list or a missing or blank task
  subject exits 64. Without changed files it makes no commit.
- `flow-watch` with `.task-files/<task id>/` creates that task's branch and
  worktree, named in the reply's `.json`, from the current flow tip. It
  commits the tree with the matching plan task's subject and the plan's
  trailers. Existing task branches are left alone. `flow-lead --send <file>`
  with `.files/` reads the answer file and commits the tree on the flow
  branch with the answered task's subject and the plan's trailers. A
  non-zero lead reply applies no tree. Both stages require a built flow;
  replies without these parts apply no files. Flow reports replace canned
  `report:` lines, preceding other stderr when the template starts with one.
  Built flows record each task's commit on the flow branch; watch and status
  JSON use those commits for merged tasks.
- `merge` checks for a one-line `--note` and `--verdict keep` (64 otherwise),
  and requires a nonblank one-line `--subject` for an Ember run branch.
  It uses the production merge implementation: run branches squash under
  that subject, with distinct non-Ember worker trailers, `Ember-Verdict`
  and `Ember-Task-Tip`; completed flow branches and other branches get merge
  commits. Conflicts leave the target unchanged. The target's `commit-msg`
  hook and `commit.gpgsign` apply to squashes. Text output prints
  `Verdict: <note>`, then `merged <branch> at <tip> into <target>: <commit>`,
  then cleanup's line. JSON includes `warnings`, `merge_commit`,
  `contained_before` and `cleanup`, as in the command reference. A run
  worktree with changes or untracked files is kept:
  `kept <branch>: the worktree <path> has changes; next: git -C <path> status --short`.
- `cleanup` uses production containment, including `Ember-Task-Tip` after
  later target edits, and handles each contained `ember/` branch as `merge` does. A
  branch's worktree is the one its run or flow in the stub's state names,
  else the one `git worktree list` shows holding the branch in a folder
  named by its id. Flow branches have no run record; their worktree is
  removed and their branch deleted, or kept with the same changes check as a
  run branch. Without `--branch`, cleanup also removes clean detached
  worktrees of closed read-only runs in the selected repository; changed
  worktrees are kept with the explicit discard command. `--dry-run`
  reports without removing them.
- `ground` reads the working directory's repository with git and prints
  the shape `docs/reference.md` gives.

`panel-start` builds whenever a `.summary` part exists, including with a
non-zero `.code`. It makes a panel id, writes the rendered summary at
`SUMMARY` with mode 600, and prints the rendered `.out` (or `.json` with
`--json`) and `.err`, returning `.code`. Other routes use canned replies,
apart from gap answer and repeat checks below.
A new route without a reply exits 69.

`flow-shared-document-task` places `.ember-eval/real-flow-planner` before
installation. The installer copies the production `ember` package beside
the stub for merge, containment and validation. With that marker,
`flow-dry-run` loads the
submitted plan with `ember.flows.plan` and uses the production dry-run
formatter. Choices come from the case's choices reply; no engine is
contacted. Waves, dependencies and warnings come from the submitted plan,
with no canned dry-run reply. The route saves that plan as
`.ember-eval/flow-plan.json`. The case's `.ember-eval/check-plan.py` loads
the saved plan through the same planner and asserts file ownership,
dependencies and membership of both waves. `grade.py batch` runs that
checker for each run and the grader reads its result;
the kit tests exercise the split plan, the original overlapping plan and
a document task missing one dependency.

`gapcheck-answer` uses Ember's result validator and answer writer to update
the saved file by id. A repeat `gapcheck --previous <file> --json` uses the
production reply parser and result validator. Without a
`replies/gapcheck-repeat.reply`, the stub supplies a review that settles
answered gaps and reopens unanswered ones. That optional reviewer reply
supplies explicit dispositions and new gaps for a changed plan. The stub
carries settled answers and closed history and continues ids from `next_id`.
`--brief --previous` includes the earlier context; `--reply --previous`
parses the supplied reviewer file. A flow dry run forwards `--previous`
into its printed gap check command.

Every call appends one JSON line to `.ember-eval/calls.log`: `seq`, `at`,
`argv`, `cwd`, `route`, `reply` and `exit`. State is in
`.ember-eval/state.json`: `runs`, `flows` (id to `dir`, `main`, `branch`, `worktree`,
task `subjects`, plan `trailers` and task `merge_commits` on the flow branch),
`panels` (id to `summary` and the source `HEAD` as `base`), `claims` (id to `gone`
once `emb choices --finish` exited 0) and route counters.
With `EMBER_EVAL_SETUP` set, a call is neither logged nor counted; `send-refused-foreign-messages/place-files.sh` uses it
to start the run that already exists when the session begins.

`turn-results-counted/files/.ember-eval/state.json` holds status documents
generated by `ember.runs.core.Core` from turn outcomes: a usage-limit failure
followed by success after a capacity attempt, an older outcome without exit
or seconds, and an unfinished result. The documents carry no `step` or `next`:
the stub derives both fresh and ignores stored keys. `tests/skill/test_evals.py`
checks the generated documents, including the older outcome's missing fields. The
generator builds records with `ember.runs.record` and runs `Core.status`
with live sessions supplied by a mock. Older outcomes
omit `provider_kind` and `attempts`; the successful turn's seconds include
the capacity wait while its attempt seconds exclude it.

`sandbox-denied-paths` generates warnings, owner actions and status with
`sandbox.hidden_snapshot`, the denial detector, `Core._save_outcome`,
`Core._warn_denial_owners` and `Core.status`. An isolated home holds toolchain,
credential, configuration and protected paths. An earlier capacity attempt
reports the toolchain;
the final attempt reports the other paths and exits 0. The turn keeps their
union. Fixture paths use `/eval`, independently of the owner's home.

The four `provider-*` cases' exit-73 streams and status replies are generated
with the Codex error classifier, `Core`'s retry loop, outcome writer, turn
formatter, error handler and status command. A fixture clock advances waits;
a mock driver fails during resend delivery. Capacity has zero retries;
usage limits and outages have one failed attempt. Uncertain capacity
delivery has a retry warning and a pending attempt with `sending: true`
and `sent: false`. No owner reply is supplied in these cases. Each provider case and
`sandbox-denied-paths` also has a `run-status.err` holding the `next:` line the
real status prints, derived by `Core.step` from the generated record; the
generator writes it only when it is not empty; the `sandbox-denied-paths`
generator also fails on any status warning.

`panel-named-members-compared` has text and JSON completion replies built
with the panel result formatter and member-line formatter. Member lines
are on standard error; standard output holds the panel id and summary in
text mode, or one JSON object. Its OpenCode member uses the full id
`openrouter/deepseek/deepseek-v4.1-flash-1017` in completion replies and
grader expectations. `panel-progress-reported` has three status
snapshots built with the panel status command: a list followed by two
one-panel records. A mock supplies the lifetime lock and clock; isolated
run records supply screen activity, including an older record without
`last_screen_change_at`. The kit tests exercise these generated replies.

The first two flow cases' choices and menu replies are generated with
`ember.choices`, including their planning rows. `flow-gap-check-first`
sets `gapcheck.ask` to `all`; `owner-away-named-workers` uses `high`.

The three flow cases' `gapcheck.reply` files hold reviewer replies.
`_stubs/make_replies.py` builds the canned JSON and text results through
Ember's result builder and formatter, validates them against
`ember/data/gapcheck-result.schema.json`, and generates the flow dry run
replies through the flow command. The kit tests exercise answer recording, repeat checks,
reopened links, settled answers, closed history and legacy results.

No stub reaches the network, starts a model or opens a window. The only
program a stub runs is git, on the case's own repository.

Generated fixtures are produced at case setup from the checkout's code;
they are not stored in the repository. They include:

- `_stubs/replies/help.out`, the help of `emb` and each command, each after
  a line `==> emb <command> --help <==`;
- the default `choices-claim.out`, `choices-read-id.out`, `choices-read.out`
  and `menu.json`, and the cases' choices replies, warnings and exit codes;
- `turn-results-counted`'s `.ember-eval/state.json` and the four `provider-*`
  cases' exit-73 streams and status replies, and `sandbox-denied-paths`'
  warnings and status replies;
- panel completion replies and progress snapshots, gap-check text and JSON
  results, flow dry-run text and JSON replies, and `emb run --dry-run`
  replies.

`_stubs/make_replies.py --list-files`, run with `uv run --frozen python`
from the repository top, lists every generated path except `help.out`.
The hand-written reviewer replies, worker replies, summaries and file trees
remain under `skill/evals`.

Inspect generated files with:

```
make eval-fixtures OUT=<folder>
```

The output folder mirrors the eval directory layout. `make eval-help` and
`make eval-replies` select one generator and also accept `OUT`. Without
`OUT`, each target creates a folder under the system temporary folder and
prints its path on exit. Output under `skill/evals` is refused. A generator
failure stops setup with a message naming the generator.

Tests use one session fixture that generates into a `tmp_path_factory`
folder for each pytest worker. `EMBER_EVAL_FIXTURES` lets the installer use
that generated folder when setting up test cases.

## Default choices replies

`_stubs/make_replies.py` generates the four default choices files in a
private temporary environment. Engines retain their default on settings;
the Codex shortlist has `gpt-6.1-sol@high` and
`gpt-6.1-sol-mini@medium`. The single writer is `codex:gpt-6.1-sol`, and
the single reviewer is `codex:gpt-6.1-sol-mini`. Multi-task work is `ask`,
and panel members are empty.

The generator also builds the cases' choices overrides. Claim replies and
warnings come from `emb choices --claim --skill` through the CLI, with an
isolated installed-skill link. Matching versions become
`{{SKILL_VERSION}}` placeholders; the stale case's installed version is
`999.0.0`, and the unknown case has no installed link. The stub omits all
three skill-version fields and their warnings when the call has no
`--skill`. Read replies have no skill-version fields. The kit tests check
current, stale, unknown,
repeated and unversioned claims.

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

1. makes `<out dir>/xdg/` with one empty folder, mode 700, for each XDG
   base (`config`, `state`, `data`, `cache` and `runtime`) and sets
   `XDG_CONFIG_HOME`, `XDG_STATE_HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME`
   and `XDG_RUNTIME_DIR` to them for the scaffold and every turn, so every
   folder Ember keeps, worktrees included, is the case's own;
2. empties `<out dir>/work/` and runs the case's `place-files.sh` there,
   logging to `scaffold.log`;
3. takes the prompt from `prompt.md`, the body after the settings block from
   its first non-blank line, so a prompt that starts with `/ember` loads the
   skill; a prompt override replaces it and sends no owner replies;
4. runs `claude -p` inside bubblewrap with:
   - the root read-only, `/dev`, `/proc` and a private `/tmp`;
   - `work/`, `~/.claude` and `~/.claude.json` writable;
   - an empty tmpfs over `~/.claude/skills`, with `<skill dir>` read-only at
     `~/.claude/skills/ember`, so the mount point is not made in the owner's
     folder and no other skill loads;
   - the stub `emb` read-only over every `emb` found on `PATH`, over
     `/usr/bin/emb` and over `~/.local/bin/emb`, each where it exists;
   - an empty tmpfs over the owner's `ember` folder in each of the owner's
     XDG bases (configuration, state, data, cache and runtime, with their
     defaults when unset), each where it exists;
   - `<out dir>/xdg/` writable and the XDG variables set to its folders;
   - `PATH` starting with `work/.ember-eval/bin` and `EMBER_EVAL_DIR` set to
     `work/.ember-eval`;
5. after a turn whose final `result` text contains a question mark, sends
   the next reply from the case's `owner-replies.md` by resuming the session
   (`claude -p --resume <session id> <reply>`), and stops when the replies
   run out or a turn ends with no question;
6. copies `work/.ember-eval/calls.log` to `<out dir>/calls.log` and prints
   the exit code, the seconds taken and the number of stubbed calls.

`owner-replies.md` lists the replies as numbered lines, `<n>. <which
question>: <reply>`; the runner sends the text after the first colon.

The out dir holds `work/`, `tmp/` (the session's `/tmp`, kept across turns), `xdg/` (the case's XDG base folders), `scaffold.log`, `prompt.txt`, `trace.jsonl` (the
stream-json of every turn, appended in order), `stderr.txt`, `owner.log`
(each reply sent), `calls.log` and `run.json` (`exit` and `seconds`).

Runs of different cases use separate out dirs and can run at the same time.
Out dirs go outside every git checkout, so that the session loads no
`CLAUDE.md` from the folders above its work folder:

```
bash skill/evals/_runner/run_case.sh skill/evals/<case> skill "${XDG_CACHE_HOME:-$HOME/.cache}/ember-evals/<case>"
```

## Grading

Each file in a case's `graders/` holds one expectation. Runs are graded by
expectation, not by run: every run of the case is judged against one
expectation by the same graders, who are not told which skill text a run
used.

```
python3 skill/evals/_runner/grade.py batch <case dir> <out dir> [--seed <n>]
python3 skill/evals/_runner/grade.py apply <case dir> <out dir> [--graders <n>]
```

`batch` removes each run's earlier `grading.json` and writes
`<out dir>/grading/<grader>/` for every grader file.

- A `type: script` grader has `check: order` with the patterns `first` and
  `then` in its front matter. It passes for a run when a Bash call matching
  `first` comes before the first Bash call matching `then` in the run's
  `trace.jsonl`. A backslash at a line's end joins the next line before
  matching. `batch` decides the grader for every run and saves `script.json`.
- A `type: llm` grader gets `expectation.md`, `task.md` and one folder per
  run under `runs/`, named `r01`, `r02` and so on in a shuffled order. Each
  holds the run's `calls.log` (the exact argv, order and exit of every
  stubbed call), `owner.log` and `trace.txt`, an ordered view of
  `trace.jsonl` with the messages, the questions to the owner, the owner's
  replies, every tool call and whether a Bash call ran in the background.
  A grader file may name `files`, paths under the run's work folder that
  are copied to the run's `files/`, and a `command`, which `batch` runs in
  the work folder, saving its output and exit code as `command.txt`.
  The run's path is replaced in every copied text, before a tool result is
  cut, also in the dashed form Claude Code gives a session's scratch
  folder. `run_case.sh` writes an `owner_reply` line into `trace.jsonl`
  before each reply it sends, and `trace.txt` shows the reply there.
  Clock times, run ids and any skill text a session printed are kept.
  The key from `r<nn>` to `<side>/<run>` is in
  `<out dir>/grading/keys/<grader>.json`; no grader is given it, and
  `task.md` tells a grader to open nothing outside its folder.

Two grader subagents grade each `type: llm` grader folder, each on its own.
Each follows `task.md`: it writes the one reading of the expectation it
applies to every run, lists the places where the words can be read two
ways, grades every run, and writes `verdict-<name>.json` in the folder.

`apply` needs `--graders` verdict files (default 2) for every `type: llm`
grader, each with a written `reading`, a list of `ambiguities` and a true
or false `passed` for every run. When the graders agree on every run, it
writes each run's `grading.json` with `expectations`, a list of
`{"text", "passed", "evidence"}` in the order of the case's
`graders/*.md` file names, and prints each grader's reading. The author
compares the readings: two graders who agree on every run through
different readings mean the text is still read two ways. When two graders
disagree on a run, or a grader file changed after `batch`, `apply` says
so, writes nothing and exits 65: the expectation's text is corrected, and
the runs are batched and graded again. It exits 64 on a usage error and 66
when the case has no graders or the out dir has no runs.

## Repeated runs: `_runner/run_repeat.sh` and `_runner/tally.py`

Before a change that moves, removes or rewrites rules in `skill/SKILL.md`
or `skill/references/` merges into `main`, its author lists the cases whose
graders exercise those rules. The author runs each listed case five times
with `bash skill/evals/_runner/run_repeat.sh --runs 5 <case dir> <out dir>`,
grades the runs as in Grading, and records `tally.py`'s counts on the
change's issue. The change merges when every listed case shows `5/5` in
the `skill` column of `all graders`. `--base origin/main` adds the same
number of runs with the skill text on `main`, for a comparison; the bar
applies to `skill` only.

When a case falls short, the author finds the root cause before changing
anything: what causes the miss, which other cases and rules the same cause
affects, and the fix that covers all of them, not only the run that
failed. Where a change to a design or a decision would serve the intended
behaviour better than a patch, the author says so to the owner. The cause
and the fix are recorded on the change's issue. The fix changes the skill
text, a grader or the fixture, and the case is then run five more times.
The same text is not run again to get a better batch.

```
bash skill/evals/_runner/run_repeat.sh [--runs <n>] [--jobs <n>] [--base <git ref>] <case dir> <out dir>
```

`run_repeat.sh` runs `run_case.sh` `--runs` times (1 to 50, default 3) for
one case, with this checkout's `skill/` folder, in `<out dir>/skill/run-<i>`.
With `--base`, it extracts the `skill/` folder at that ref with
`git archive` to `<out dir>/base-skill/skill` and runs the case the same
number of times with it, in `<out dir>/base/run-<i>`. Both sides use this
checkout's case files, stubs and runner, so only the skill text differs.
`--jobs` (1 to 8, default 1) sets how many runs go at once. Each run is a
separate `claude -p` session on the owner's account.

It prints one line per finished run, `<side> run-<i> <summary line>`, in the
order the runs finish. A run whose `run_case.sh` exits non-zero is reported
on standard error, and `run_repeat.sh` then exits with that code once every
run has finished. It exits 64 on a usage error or an unknown ref, and 66
when the case has no `prompt.md` or the ref has no `skill/SKILL.md`.

After the runs are graded as in Grading, `tally.py` counts the passes:

```
python3 skill/evals/_runner/tally.py <case dir> <out dir> [--json]
```

It prints one row per grader and an `all graders` row, with
`<passed>/<graded>` for each side. A run counts once its `grading.json` has
one expectation per grader, each with a true or false `passed`; other runs
are listed on standard error as not graded and left out. It exits 66 when
the case has no graders or the out dir has no runs.

```
<case name>
grader        skill              base
<grader>      <passed>/<graded>  <passed>/<graded>
all graders   <passed>/<graded>  <passed>/<graded>
```
