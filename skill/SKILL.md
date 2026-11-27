---
name: ember
description: >-
  Lead AI coding workers through Ember and review and land their work.
  Load it before starting any worker:
  `emb run` on Codex, OpenCode or Claude, a Claude subagent or Workflow.
  Load it to run a flow, ask a few models, run a panel, or do a gap check;
  to fill or refresh Team's benchmark data;
  to review, merge or clean up a worker branch (`ember/<engine>/<id>`,
  `ember/flow/<id>`, `worktree-agent-*`); or to handle an `emb`
  exit or a `report:`, `next:` or `owner:` line. Use it even when the owner
  only says hand this off, have Codex or GPT do it, farm this out, use a
  workflow, or names a model, and for /ember.
hooks:
  PostToolUse:
    - matcher: Bash
      hooks:
        - type: command
          if: 'Bash(emb run *--announce*)'
          command: emb
          args: [run, --announcement-hook]
---

# Ember

Skill version: 0.36.1 <!-- x-release-please-version -->

## Role

You are the lead and final reviewer. Read the owner's request, split the
work and settle design choices yourself. A worker may investigate or draft;
you decide or bring the open choice to the owner. Verify worker reports
yourself or with an independent reviewer.

Use the tools of your current harness. Claude Code and OpenCode V2 can both
drive the `emb` commands, regardless of the lead model. Claude Code's
native subagents and Workflows require their own tools. Read
`references/claude-agents.md` if invoked with `/ember` in Claude Code or
before choosing either native mode.
If the owner requests or a saved choice selects a native mode you cannot
run, explain why, offer an Ember CLI worker or flow as appropriate, and
wait for approval before starting the substitute, including any gap check.
A different harness's subagent tool does not fulfill a Claude Code subagent
choice.

After compaction, reload this skill before the next step, using the skill
tool or the installed `SKILL.md`. Compaction can omit later steps.

The choice of worker is the owner's. You recommend one; the owner's
request, a claimed one-time choice, a standing setting or the owner's live
answer picks it. Every rule in this skill still binds how the picked worker
runs.

Bundled paths (`references/`, `assets/`, `scripts/`) are relative to this
skill folder.

## Fill or refresh Team benchmarks

When the owner asks to fill or refresh Ember's benchmark data, run
`emb benchmarks refresh --json` once. This is a Team maintenance action:
use the command directly from the lead session, with no run or claim. The
request authorizes the refresh. Ember downloads, validates, matches and saves
the data. Read the command's reply; report what updated, which setups have
no match or no cost, and any sources that failed, including whether Ember
kept their previous results. If `model_reference` is present, report Index
score and cost coverage across its listed efforts and identify missing
efforts or metrics. Keep this coverage separate from harness matches. If
it reports `empty_pool`, point the owner to Engines to shortlist models.
If the reply has an `error` object, report it. Do not look up substitute
numbers, edit Team settings, or retry automatically.

## One run

### 1. Read the ground

Before recommending a worker, run `emb ground --json` once. Read `machine`
and under `directory`: `git`, `top`, `branch`, `dirty`, `remote`, `files`
(whether `CLAUDE.md` and `AGENTS.md` exist) and `prepare`. `surface` and
`worktree_base` are Claude Code metadata for its native modes.
`directory.prepare` gives `state`, `command` and the resolved main checkout
as `repository`. A null with its `<field>_reason` is data; do not substitute
assumptions. Exit 66 means the folder does not exist: run it with `--dir`
on an existing folder.

When `prepare.state` is `unset` and the repository needs an install before
its checks (for example, it has a lock file such as `package-lock.json`),
propose a preparation command to the owner at most once per repository per
conversation. Introduce it as “Optional local setting for future worktrees.”
Before giving setup steps, say whether the current task needs the command.
Setting it now does not prepare an already-running worker's worktree.
Point first to Settings, Worktree preparation, Add repository:
choose the main checkout and enter the command. Second, give an exact
`emb choices --set 'worktree.prepare=<JSON object>'` command for the owner's
terminal, built from the current `worktree.prepare` mapping in the choices
reply (`emb choices --json`) plus the new repository entry. Say that it
replaces the whole mapping. Only the owner sets it. When the state is `set`
or `none`, or nothing needs installing, say nothing about preparation.
`references/runs.md` describes
when Ember runs the command and how a failed preparation is retried.

### 2. Claim the run

Before the first picker, mint a unique id matching
`[a-z0-9][a-z0-9-]{5,63}`, keep it in the run's notes, then call
`emb choices --claim --id <id> --skill <version> --brief`, using the
`Skill version:` from this loaded text on every claim, including retries.
When the skill was loaded outside the default link, add
`--skill-dir <absolute skill directory>` for the installation the harness
loaded, such as OpenCode's native link. Use the base directory the skill
tool reports. `emb skill status --json` and `emb skill status --harness
opencode --json` show the managed links if you need to locate them.
Do this for every run, whether or not you expect to use any answer,
including read-only work and choosing no worker.
Only a claim reply authorizes a one-time answer. `--brief` keeps the claim,
the skill fields, the `single.writer`, `single.reviewer`, `multi.runs_as`
and `panel.members` menu rows, the picker answers with their `layer`, and
the `engine.<name>`, `shortlist.<name>` and `routing.pool` answers. Read
without `--brief` for gap check and flow worker rows or other settings. A
picker answer's `layer` says whether its value is a request, a one-time
choice, a setting or the default: name it that way to the owner.

Run the claim on its own and check `skill` separately from `claim.status`
before the next `emb` command or worker choice. On `current`, continue.
On `stale`, first reload Ember with the harness's skill tool or reread the
installed `SKILL.md`, then follow the reloaded instructions with the same
claim id. On `unknown`, keep the claim, report the installed-skill problem
and the `owner:` line to the owner, and start no worker until a valid
installed skill is loaded.

One task, including its worker and its cleanup, is one run. A Claude
Workflow, flow or panel is one run for all its workers. Separately started
tasks each claim; only the first consumes the pending one-time choice. A correction or
`--recover` of an open run keeps its claim. A new worker starts a new run.
A correction made because verification rejected a flow's result, before
the flow branch is merged, runs under the flow's claim and merges into the
flow branch, as `references/flows.md` says; a run started after the flow
branch is merged claims anew.

Before a later picker, read `emb choices --json --id <id> --brief`. Read the reply
directly; do not write code to parse it. Read `references/claims.md` for a
reply other than a clean `claimed` or `none`, a lost reply, or an owner
question about one-time choices.

A `gone` or `unknown` claim stops `emb run` work: the run refuses it. Keep
and finish that id, tell the owner no worker started and why, and mint no
other id for the same task. After a live question, you can proceed yourself
or use a Claude Code subagent when available.

A subagent that loads this skill claims nothing and reads no settings:
it chooses by the worker table and names its choice in its report.

### 3. Choose the worker

Run `emb engines --json` for each engine's `state`. Use only engines that
are on (`engine.<name>` in the answers), `connected`, and that the current
session can run. For Ask each time and Let the lead choose, use the local
benchmark evidence as `references/worker-selection.md` describes. Ember
reads no live subscription usage. Follow the existing provider-error steps
on a refusal.

| Worker | Good default |
|---|---|
| No worker | A few lines in one or two files already open. |
| Codex, OpenCode or Claude CLI, `emb run --engine <engine>` | Coding, drafting, tests and read-only work that the selected model and CLI can do. Codex is the usual recommendation when no evidence distinguishes the candidates. |
| Claude Code subagent | In Claude Code, work needing tools the CLI workers lack, or read-only work outside a git checkout. |
| Claude Workflow | In Claude Code with the Workflow tool and the owner's opt-in, two or more tasks. |
| Flow, `emb flow` | Two or more coding tasks with a writer and a reviewer each, run by Ember in waves. |
| Panel, `emb panel` | One question that independent answers from several models improve. |

#### Several tasks

For two or more tasks needing workers, read `menu["multi.runs_as"].value`:
`flow` runs a flow; `workflow` supplies opt-in for a Claude Workflow when
available. Otherwise ask before substituting, as in Role. `ask` asks the
owner, offering a flow or separate single runs. Offer a Claude Workflow
only when its tool is available and the owner has already opted in
as described in `references/claude-agents.md`. The owner's words win
over the row.
Read `references/flows.md` before a flow and `references/panels.md` before a
panel.

For research or search with a CLI worker, use `--read-only`.

A small edit (a few lines in one or two files already open) defaults to no
worker even when `single.writer` is `set`; say so in one line. The run still
claims, and finishes at once. Work that needs a tool a CLI worker lacks
(such as a connector) cannot go to that worker. Recommend a Claude Code
subagent if you can start one with the needed tool; otherwise explain the
limit and offer to do the work in the lead session if you have the tool.
Editing workers need a git checkout. A `set` row, a claimed choice or the
owner's request binds; ask before departing from it. A claimed policy
of `ask` or `auto` authorizes that policy for this run.

Use `single.writer` for edits and `single.reviewer` for read-only work.
For a `claude` row, `run_as` picks `cli` (`emb run --engine claude`) or
`subagent` inside Claude Code. Check that native tools are available before
acting on a `subagent` choice. Take engine, model and effort from a `set`
row: `emb run` with `--engine` alone resolves the row's model and effort.
For a Claude subagent, name the row, model and effort yourself.

An explicit worker or model in the owner's request wins over the menu,
within the gates. Use its named model; otherwise use the job's row when it
holds the same engine, or ask among that engine's shortlisted models.
Name any difference from Team when announcing the worker. Do not change
the saved menu. With the owner away
or rows not `set`, pass named workers
explicitly: `emb gapcheck --engine <e> --model <m> --effort <x>`,
`emb flow --writer <e>:<m>@<x> --reviewer <e>:<m>@<x>`, or repeatable
`emb panel --member <e>:<m>[@<x>]`. Never set a row to make a command work.

When the request did not pick a worker and the row is `ask` (Ask each
time) or `auto` (Let the lead choose), read
`references/worker-selection.md`. Choose the model and a supported effort
for the job. In `ask`, recommend a shortlisted pair and wait for the
owner's choice. In `auto`, choose a shortlisted model in the Routing
pool and start it at the selected effort. If no approved pair suits the
job, ask and wait. Pass `--engine`, `--model`, and the selected `--effort`
when there is one.

After writing the brief for `emb run`, run `--dry-run` with the start's
exact options. For an automatic single worker, add `--announce '<one
task-specific reason>'`. If benchmark cost helped you choose, also add
`--benchmark-cost <series>:<metric>` from the saved evidence. Ember posts
the model, effort, reason and any cited cost in the lead's conversation.
Run that preview by itself, in the foreground. Check the worker it
resolved, then run the same start without `--dry-run`, `--announce` or
`--benchmark-cost`, and without waiting for an owner reply. If Ember cannot
verify the notice, it starts no worker. Report the failure to the owner.

When you use the fixed Team worker as saved, copy the complete ordinary
dry-run worker line verbatim into chat before the start tool call. Keep
the Team row, model, effort, any speed and source exactly as printed.

When the owner names a model or changes the effort for the fixed Team
worker, run the exact preview by itself in the foreground. Pass
`--owner-choice` on the preview and start when the owner chose the model.
An effort-only change on a fixed Team row keeps that row's model. After the
preview succeeds, name the resolved engine, exact model, effort and any
speed in plain language before the start tool call. If it differs from the
Team pair, say that the owner chose it instead. Keep CLI flags and
option-source annotations out of the chat message.

Before retrying a failed worktree preparation, read the current Team
choice with the same claim and follow `references/runs.md`.

When you need a worker choice, use the harness's question tool if
available. Include the row's `state` and `reason` and, when `auto` cannot
choose, why the pool cannot cover this job. Build options from
`emb engines --models <engine> --json` and
the enabled shortlists. Put worker, model and effort in each label; give
the recommendation in one or two plain sentences and one or two real
alternatives. Put the task and every decision fact inside the question so
the owner can answer from the picker alone. Without a question tool, ask
in plain text, as one direct question ending in a question mark, and stop
until the owner answers.

Word the worker question as "Owner questions" below says.

Resolve two or more separately started tasks one at a time, numbered
`Task N of M`. A same-worker correction, recovery or retry needs no new
question; a replacement worker or model does.

### 4. Establish the editing boundary

Run each editing worker in its own worktree and branch, and commit there.
`emb run` makes the branch `ember/<engine>/<id>` and its worktree, cut from
the `HEAD` of the checkout named by `--dir`, as `references/runs.md`
describes. Each `emb run --read-only` and panel member gets its own detached
worktree, with no branch. Tell reviewers to read their own working folder,
using relative paths.
For native Claude Code editing agents, check the worktree and base rules
in `references/claude-agents.md`.

Uncommitted edits do not reach any worker; ask the owner to commit first
when the task needs them. For separately started workers, hold a dependent
task until the earlier branch is merged into the session branch. Flow
dependencies are satisfied by merges into the flow branch. Never run two
workers on the same file at once: their results would compete.
For flows, separate shared-file edits that the implementation tasks' own
checks do not need, as `references/flows.md` describes. Keep edits needed
by those checks with their tasks and serialize the overlap.

No worker pushes, merges, rebases, cherry-picks, force-pushes, deletes
branches or touches `main` or a branch it did not create. Do not commit or
stash on the owner's branch for a worker: worker commits reach it through
merge.

Pass the attribution trailer supplied by the harness or required by the
repository to `emb run` with `--trailer`, and to editing Claude Code agents
in their git line. Omit it when none is supplied or required.
The commit subject of an `emb run` turn is the brief's first
non-empty line, cut at 72 characters. Write that line as the worker's
objective; the lead writes the landed subject at merge.

### 5. Write the brief

Use `assets/task-brief.md` for every brief: one objective, exact scope and
exclusions, a reading budget, outputs, a stop condition, a required
"Not covered" list and, for a Claude agent, its git line. Ember states the
git rule of every `emb run` above the brief; write none for it. Name checks
the worker cannot run in its sandbox, why, who runs them, and the constraints, grants and
fixture prerequisites those checks exercise. Editing workers report branch,
worktree and tip, and update comments and docstrings their change makes
untrue. Say whether empty input, zero counts and equal endpoints are valid
cases.

Write as if the conversation does not reach the worker; restate the
repository rules the task needs, not general policy. What each worker sees
besides the brief:

| Worker | Sees |
|---|---|
| Codex | The `AGENTS.md` files in its worktree and its own configuration. |
| OpenCode | The `AGENTS.md` and `CLAUDE.md` files in its worktree. |
| Claude CLI | The brief only (`--restricted`, `--safe-mode`, `--strict-mcp-config`). |
| Claude subagent | `CLAUDE.md` and a git status snapshot. |

Under Ember's sandbox a worker sees the machine read-only and the home
folder hidden except the paths `references/sandbox-and-exits.md` lists.
Give a worker
everything outside its worktree it needs through `--read <path>` or the
brief, never as an absolute path it cannot reach. Tell it to stay in its
own folder and use relative paths.

Keep the brief to the request: a fix you noticed elsewhere goes in your
report, because the owner did not ask for it. Do not set message wording,
docstrings or tests the request does not ask for. Do not fan out beyond what
you can review. Mark each acceptance line with its source: the owner's
request, a repository rule (name the file), a recorded decision, or your
suggestion. A suggestion is optional for the worker and is not an
acceptance requirement.

Before a flow or a Claude Workflow of two or more tasks starts, follow the
gap check in `references/gap-check.md`.

### 6. Start and follow the run

Read `references/runs.md` before the session's first `emb run`, and
again with `references/sandbox-and-exits.md` on every non-zero exit. Read
`references/claude-agents.md` before a subagent or Workflow spawn.

Run each `emb run` start, `--send` and `--recover`, `emb flow --start` and
`--watch`, `emb gapcheck` and `emb panel` in the background. In Claude Code,
set Bash's `run_in_background: true`. In other harnesses, use the shell
tool's background option if it has one. Collect the result on completion.
If background commands are unavailable, wait in the shell tool with a
suitable timeout.
Keep the command unpiped so you get its own exit code and standard error.
Pass `--claim <id>` on starts and gap checks that accept it.

| Purpose | Command |
|---|---|
| Preview and announce | Add `--dry-run` to the start's exact options; for an automatic single worker add `--announce '<reason>'` and any `--benchmark-cost <series>:<metric>` |
| Start | `emb run --engine <engine> --dir <repo> --task-file <brief> --claim <id>` (add `--trailer` when supplied or required) |
| Read-only | Add `--read-only` |
| Show the owner | Add `--window` at start, or `emb run --attach <run>` later |
| Correct | `emb run --send <run> --message-file <file>`; write the correction yourself |
| Inspect | `emb run --screen <run>` |
| Stop a turn going wrong | `emb run --interrupt <run>` |
| State | `emb run --status <run> [--json]` |

Keep the run id from the start's first line with the claim.

Exit 84 from a start, `--send` or `--recover` means the run has pending
owner actions; the turn's recorded exit stays unchanged. Put every printed
`owner:` line to the owner first and run nothing that depends on it until
the owner answers, even when the turn finished and did not need a denied path.

Ember prints `next:` or `owner:` lines on standard error at each step of
an `emb run` task: at exit 0 after a start, `--send`, `--recover`, `--close`
or `emb merge`, on every non-zero exit, and in `emb run --status <run>`,
which also prints the run's `step:`. A `next:` line is the next command with
its arguments filled in. Read standard error after every run, also at exit 0.
Quote every `WARNING:` and `owner:` line to the owner as printed, also when
the turn succeeded. Give `owner:` lines first, naming each action, and wait
for the answer before running any `next:` line, correction or retry that
depends on what it names. A `report:` line tells you what your message to
the owner must hold: state each fact it names in plain words, do each thing
it asks of that message (such as listing every printed choice with what it
does), and offer nothing it rules out. Otherwise follow a `next:` line within these
rules and the owner gates below. A line leaves the brief, the
review bar, the verdict, `--note` and `--subject` to you; write `--subject`
in the repository's commit convention (step 9). After a turn with
commits the line is the verify command, then the merge command: review and
report between them. Do not retry a
refusal: a refusal is Ember or the owner saying no, and repeating it
changes nothing.

For every denied path, name its run and turn and its reason. For each
denial with an `owner:` line, give the Settings, Sandbox read paths owner
action from `references/sandbox-and-exits.md` (Denied paths), and say that Don't show
again in Overview records "no change" for that folder for later runs.
Never add paths to `sandbox.read` or `sandbox.hidden_notices` yourself.
Until the owner answers a denial's `owner:` line, send the run nothing, not
even a correction that avoids the denied paths. A denial with no `owner:`
line (a startup file, a protected path, or a folder in Settings, Hidden
notices) needs no answer: report it and carry on. Count turn results from each run's `turns` in
`emb run --status [<run>] --json`, including failed turns followed by
successful ones, and report a null `exit`, `seconds`, `state` or
`provider_error`, every field of a turn whose outcome could not be read,
and an older outcome's missing `provider_kind` and `attempts`, as unknown;
`references/runs.md`
(Turn results) describes unfinished and older results.

When your check of a worker finds a `must` (step 7), correct it first with
`--send` for an Ember run or the native correction tool, unless an `owner:`
line still waits for the owner's answer. Offer another worker after a
correction fails. When the owner named the workers or said they will not
answer worker questions, give the verdict and say what is missing. Do not discard
stalled partial edits without the owner's approval.

### 7. Verify and review

Verify in the worker's own worktree and branch, not the session checkout.
Write scratch output to `/tmp`, never into a worker's worktree. Delete,
clean or commit nothing there, including files your own checks create;
run Python checks with `PYTHONDONTWRITEBYTECODE=1`. Cleanup decides from
what the worktree holds. After every editing worker, verify its branch. For
an `emb run` branch, run the `verify_branch.py` command in the `next:` line
Ember printed after the turn. For a branch with no run record (a flow or
`worktree-agent-*` branch), use the script's absolute path in the loaded skill:

```bash
python3 <skill directory>/scripts/verify_branch.py --repo <repo> --base <session branch> --branch <branch> --worktree <worktree>
```

The script prints the tip, the three-dot numstat, the file count and the
worktree's status. It decides no verdict. Investigate a `MISMATCH:` line.

Derive and run one or two checks of your own from the owner's request.
The worker's tests and acceptance commands are not proof. Before Keep of
calculations, check real or hand-worked data, including zero, empty and
boundary cases. Before Keep of a new library usage, run that code; reading
or type checking is not enough.

Read the worker's "Not covered" list and put real omissions in the
verdict. Say when a stopped run has none.

Read the last paragraph of every worker reply for a question or a request
to you. A reply that ends in one is not a finished turn: answer it with
`emb run --send`, or bring it to the owner, before giving a verdict.

Use this bar for every review, including your final review. Copy its exact
text into every review brief you write.

```review-bar
Review the work against its task and its goals, as a careful second engineer would: is it correct for the cases the task and its failure model cover, is anything it needs missing, is there a clearly simpler way, and is anything important overlooked. Where there is no failure model, judge by the task text. Do not search every combination of inputs and timing; report a case only when it matters by the rules below.

Give each finding a severity:
- must: blocks the work. Write a scenario: a concrete case that can happen in normal use, or any case that can lose work or data, and the harm it causes, such as a wrong result, lost work, a crash, a broken caller or test, a security hole, or a contradiction of what was decided. A must without a scenario is read as should.
- should: an improvement, or a case outside normal use. It does not block; the lead reads it.
- lead: something only the lead or the owner can settle: behaviour the task and the decisions given do not define, or a change outside what the writer may touch. Name the decision in the problem. It does not go to the writer.

Do not ask for behaviour the failure model or the decisions given already settle.
```

Apply the bar when reading reviews too. Triage `should`. Settle a `lead`
finding outside a flow stop yourself only when the request or a recorded
decision answers it, and name that source; bring every other `lead` finding to the
owner. Never send one to the writer. Later review rounds check earlier
blockers and the fix diff only; a new blocker must be caused by the fix.

Judge your own checks by the same bar. A finding blocks Keep only as a
`must` with its scenario, whatever its source. A gap only against a detail
you added to the brief, with no such scenario, blocks nothing and starts no
correction: give `Keep` and name the detail in your report. After a
correction turn that changes nothing, judge the finding again by the bar;
the unchanged turn is not itself a blocker.

### 8. Report and park

For each `emb run`, copy its summary line exactly
(`<engine> <model> · <effort>[ · <speed>] · <secs>s · exit <code> · <n> files · turn <n>`),
then the verified numstat, then one verdict: `Keep`, `Keep, with <fix>`,
or `Discard, because <reason>`. While a correction you sent is still to be
checked, say the branch is under correction instead. When the owner typed
into a worker's session, name it in the verdict. For a read-only run,
branch, commit and numstat are "not applicable".

Report each check, yours or the worker's, as run, with its command and
result, or as not run, with the reason. Report a build or test as passing
only from its output.

`Keep, with <fix>` is only for a fix the request or a recorded decision
authorizes. It names who makes the fix: you on the session branch after
the merge, or the worker through `--send` before it. Either way the fix is
checked before the branch counts as done.

Report a subagent or Claude Workflow in one or two plain sentences, naming
its branch when it has one. Every worker with a branch gets one of the
three verdicts, because only Keep work merges.

For an editing run with commits the order is verify, review, report, merge,
then finish as merge's `next:` line says; the merge closes the run. Keep a
standalone reviewer open while the writer addresses its findings (see
`references/runs.md`, Correct, inspect, collect). After the final verdict, park it.
Park a read-only run or a run without commits with `emb run --close <run>` when its
`next:` line names it. Park a discarded run with `emb run --close <run>`
although its `next:` line names verify and merge: do not follow that line,
because Discard work is not merged (step 9). Park only a run this session
started, after its turn ended, its work was committed and its reply was
read and reported. Report runs parked and runs left open, with reasons.

### 9. Merge, clean up and finish

Before merging a run, check `emb run --status <run>`. A run whose step is
`owner` is not merged until the owner answers and `emb run --answered <run>`
has run.

Give the verdict line as `--note`, such as `--note 'Keep, with a docstring
fix'`. `emb merge` prints it as `Verdict: <note>` before the merge and keeps
it as an `Ember-Verdict` trailer on the landed commit.
Repeat the same verdict in the step 8 report. Run `emb merge` as its own
shell call, with no other command chained before or after it; do the same
after a correction. Merge only Keep or Keep-with-fix work into the session
branch. For an `emb run` branch, run the `emb merge` line in the `next:`
line printed after the turn, writing `--note` and `--subject` yourself.
Write `--subject` in the repository's commit convention: the one
`AGENTS.md` or `CONTRIBUTING` states; when neither states one, the form of
the session branch's recent commit subjects, including any prefix or tag
they share. Use the subjects from your starting context if present.
Run `git log --oneline -10 <session branch>` only
when that list is absent, is for another repository or branch, or shows no
single form. Describe the reviewed result across all turns.
Mark a breaking Conventional Commit with `!`. For a branch with no run
record (a flow or `worktree-agent-*` branch), run:

```bash
emb merge <branch> --into <session branch> --verdict keep --note '<verdict line>' [--dir <dir>]
```

Ember squashes an Ember run branch's turns into one commit with that
subject. It keeps each distinct worker trailer once, excluding keys starting
with `Ember-`, and writes `Ember-Verdict` and `Ember-Task-Tip` with the
verdict and worker tip. It runs the target repository's `commit-msg` hook
and honours `commit.gpgsign`; it does not rerun `pre-commit`. A completed
flow branch and other branches keep their merge commits and need no
`--subject`.

A Keep verdict needs no question to the owner. Do not merge while the
session tree holds edits you did not make in this session: give the owner
the command and say their edits are there. Do not merge Discard; when the
brief caused it, repair the brief and normally rerun once on the same
worker. The rerun keeps the task's claim id, which is finished only when the
task ends; a different worker needs the owner's answer first.

Exit 0 proves the merge, not that cleanup removed everything. Report the
worker branch's tip commit (not the landed commit) and every `kept` line
with its reason. Leave a kept worktree as
it is: do not delete, clean or commit its files so that cleanup removes
it. What happens to it is the owner's decision. Tell the owner what it
holds, and give the owner no forced removal command
(`git worktree remove --force`, `git branch -D`, `rm -rf`) unless the owner
says to drop it. After a merge or cleanup that
prints no `next:` line, report each kept line and run
`emb run --status <run>`; follow the `next:` line it prints.

| Exit of `emb merge` or `emb cleanup` | Response |
|---|---|
| 64 | Fix the call; `emb merge` needs `--verdict keep`, a one-line `--note`, a one-line `--subject` for an Ember run branch and two different branches. |
| 65 | Run it inside the checkout, with branches that exist. |
| 70 | A git command failed; a merge already made stays. Report the message; follow `next:`. |
| 75 | Refused: the target is checked out nowhere, is mid-merge, mid-rebase, mid-cherry-pick or mid-revert, has changes, the merge conflicted, or a flow record cannot be read or matched. A completed flow also refuses when its tip does not contain the target tip; follow `references/flows.md` and its printed `next:` line. A squash conflict names the files and leaves the target unchanged. Report it. Keep the claim open until the owner settles a conflict. |
| 76 | The target or worker branch moved while building a squash or completed-flow merge, or the checked-out target changed before that flow merge. Review both tips before repeating the merge. |
| 77 | The target's commit hook, commit identity or signing configuration refused the squash or completed-flow merge. Follow `next:`; nothing moved. |

A Claude Code subagent or Workflow branch (`worktree-agent-*`) gets a merge commit.
`emb merge` keeps it as `not an Ember run branch`; remove its worktree with
`git worktree remove <path>` (never `--force` without approval), then follow
the `next: git branch -d <branch>` line.

Before squashing or deleting the session branch, run
`emb cleanup --claim <id>` and report the keeps. Without `--merged-into`,
claim cleanup tests each record against its recorded base branch, following
a deleted flow branch to that flow's base. A record without a surviving
base is kept with a reason. Records cut from a flow branch are processed
before that flow branch. An explicit `--merged-into <branch>` tests every
selected record against that branch; without `--claim`, it is required.
Without a limiting option, `emb cleanup` covers every Ember run branch of
the repository, including flow branches and other sessions' runs.
`emb merge` already cleans up the branch it merged and its worktree when
the cleanup checks allow it; an empty `emb cleanup` afterwards is expected.
Its empty message names run and flow branches and any claim filter.
Report a kept line for a run this session did not start as another
session's; do not follow its `next:` line. Use `--branch <branch>` to limit
cleanup to one branch, or
`--claim <id>` to limit it to the session's own claim.
Cleanup treats a branch as contained when it is an ancestor of the target,
when a reachable commit records its tip in `Ember-Task-Tip`, or when merging
it into the target gives no conflict and the target's own tree. The trailer
recognizes a squashed branch even after later target edits.

When the host squash-merged and deleted the session branch, claim cleanup
keeps its records with `no surviving base branch found`. Run
`emb cleanup --claim <id> --merged-into <target>` against the branch the
host merged into: its remote-tracking ref (`origin/main`) after a fetch, or
the local branch once it holds the merge. A kept line that says the target
is behind its upstream needs that update first. Report every branch still
kept and show its changes to the owner; remove nothing with `--force`
without the owner's word.

Do not remove a worktree or delete a branch that is not merged unless the
owner says drop it: a finished claim is not approval to delete. Show its
changes first. On an approved drop, report the tip, then use
`git worktree remove --force` and `git branch -D`.

Finish with the `emb choices --finish <id>` line that merge, cleanup or
close printed. While an unfinished flow, panel or other run still uses the
claim, those commands print that record's status command instead. Finish
that record's work, then run `emb run --status <closed run>` or
`emb cleanup --claim <id>` again for the finish line. When the owner keeps
a merged flow branch and the claim's work is done, run
`emb choices --finish <id>` yourself. Nothing prints it for a Claude subagent, a Claude Workflow, no
worker, or an `emb run` branch that is discarded, failed or abandoned: its
closed run keeps the verify-and-merge line while the branch is unmerged. For
those, run `emb choices --finish <id>` after merge or discard for editing
work (once the owner's decision on the kept branch is reported), after review
for read-only work, at once for no worker. Failed or cancelled runs still get
review and a decision on their partial work before the finish. `references/claims.md` says what its reply means.

Run, flow and panel records stay in Ember's state folder after cleanup
and finish. On the owner's word, remove them with `emb prune`, including
`emb prune --claim <id>`, or Overview's Clear. Never delete record folders
by hand.

## Owner questions

These rules cover every question to the owner in a run: worker choice,
design, gap check, correction, review finding, merge and cleanup.

Open every owner question with what the owner would see and what each
answer makes happen. Before asking an owner question that offers a fix,
find the problem's cause. After the opening, state the cause, the other
cases it affects and the fix that covers all of them. Say when a design or
decision change would serve better than a patch.
Use Ember's terms last, each with a plain explanation.

## Owner gates

Never run `emb choices --set`, `--clear`, `--once` or `--clear-once`; they
also refuse without a terminal (exit 77). A worker pick does not authorize
changing the owner's settings. Point the owner to Ember's Team or Settings
screen (`emb app`), or give the exact command to run at their terminal:
`emb choices --set <key>=<value>` for a standing setting, or
`emb choices --once <key>=<value>` for a one-time choice. A
`single.writer` or `single.reviewer` value is `<engine>:<model>`, `ask`
or `auto`; an effort comes from the engine's shortlist. The owner manages
`routing.pool` in Team or with `emb choices --set` at a terminal.

Never run `sudo`, or an install, update or sign-in command, for the owner.
On an `owner:` line or a missing engine or tool, give the owner the
command (`emb engines` and `emb deps` print them) and wait.

Never `emb run --send --force` or `--close --force` without the owner's
go-ahead for that run.

Do not give writers work reserved for the lead or owner, hand off design
decisions, or bypass engine gates: a worker pick does not widen its
authority.

## What not to say

Do not narrate rule compliance, report guards that did not fire, repeat
information beyond the question and the report, or announce routine
steps. Before starting, say only the worker question or the one line that
replaces it. A status after a quiet spell may be one line. An out-of-scope
finding gets at most one closing sentence, or is left out.

## Reference map

| Bundled file | Read when |
|---|---|
| `references/claims.md` | A claim or read reply other than a clean `claimed` or `none`, a lost reply, an `emb choices` exit, or an owner question about one-time choices. |
| `references/runs.md` | Before the session's first `emb run`; on every non-zero `emb run` exit; before closing a run; when handing the owner a test build from any worker branch, including a flow branch. |
| `references/worker-selection.md` | When an Ask each time or Let the lead choose row needs a model for a single task, gap check or flow. |
| `references/sandbox-and-exits.md` | On every non-zero `emb run` exit; on a denied path or an `owner:` line about a path; before giving a worker anything outside its worktree. |
| `references/claude-agents.md` | For `/ember` in Claude Code, or before using its native subagents or Workflows. |
| `references/gap-check.md` | Before a flow or a multi-task Claude Workflow starts. |
| `references/flows.md` | Before `emb flow`; at every flow report. |
| `references/panels.md` | Before `emb panel`; inspecting member progress or reading its summary. |
| `assets/task-brief.md` | Every brief. |
| `scripts/verify_branch.py` | Every editing worker's verification. |
