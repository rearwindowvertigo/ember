---
name: ember
description: >-
  How Claude, as lead and final reviewer, hands coding work to workers
  through Ember and lands the results. Load it before starting any worker:
  `emb run` on Codex, OpenCode or Claude, a Claude subagent or Workflow.
  Load it to run a flow, ask a few models, run a panel, or do a gap check;
  to review, merge or clean up a worker branch (`ember/<engine>/<id>`,
  `ember/flow/<id>`, `worktree-agent-*`); or to handle an `emb`
  exit or a `next:` or `owner:` line. Use it even when the owner only says
  hand this off, have Codex or GPT do it, farm this out, use a workflow, or
  names a model, and for /ember.
---

# Ember

Skill version: 0.14.1 <!-- x-release-please-version -->

## Role

Claude is the lead and final reviewer; spare Claude's work first. Read the
owner's request, split the work and settle design choices yourself. A
worker may read what informs those decisions, or draft, but you decide or
bring the open choice to the owner. Treat worker reports as unverified
until you or a second worker checks them.

The choice of worker is the owner's. You recommend one; the owner's
request, a claimed one-time choice, a standing setting or the owner's live
answer picks it. Every rule in this skill still binds how the picked worker
runs.

The task after `/ember` is `$ARGUMENTS`. That explicit invocation opts into
a Claude Workflow; loading this skill yourself does not. The owner's own
words or saved `multi.runs_as=workflow` also supply opt-in for multi-task work.

Bundled paths (`references/`, `assets/`, `scripts/`) are relative to this
skill folder.

## One run

### 1. Read the ground

Before recommending a worker, run `emb ground --json` once. Read
`machine`, `surface` (`desktop` true is the Claude desktop app),
`worktree_base`, and under `directory`: `git`, `top`, `branch`, `dirty`,
`remote` and `files` (whether `CLAUDE.md` and `AGENTS.md` exist). A null
with its `<field>_reason` is data; do not substitute assumptions. Exit 66
means the folder does not exist: run it with `--dir` on an existing folder.

### 2. Claim the run

Before the first picker, mint a unique id matching
`[a-z0-9][a-z0-9-]{5,63}`, keep it in the run's notes, then call
`emb choices --claim --id <id> --skill <version>`, using the `Skill version:`
from this loaded text on every claim, including retries. Do this for every
run, whether or not you expect to use any answer, including read-only work
and choosing no worker.
Only a claim reply authorizes a one-time answer.

Read `skill` separately from `claim.status`. On `current`, continue. On
`stale`, reload the ember skill before the next step, then follow the
reloaded instructions with the same claim id. On `unknown`, keep the
claim, report the installed-skill problem and the `owner:` line to the
owner, and start no worker until a valid installed skill is loaded.

One task, including its worker and its cleanup, is one run. A Claude
Workflow, flow or panel is one run for all its workers. Separately started
tasks each claim; only the first consumes the pending one-time choice. A correction or
`--recover` of an open run keeps its claim. A new worker starts a new run.

Before a later picker, read `emb choices --json --id <id>`. Read the reply
directly; do not write code to parse it. Read `references/claims.md` for a
reply other than a clean `claimed` or `none`, a lost reply, or an owner
question about one-time choices.

A `gone` or `unknown` claim stops `emb run` work: the run refuses it. Keep
and finish that id, tell the owner no worker started and why, and mint no
other id for the same task. A Claude subagent or no worker may still go
ahead after a live question.

A subagent that loads this skill claims nothing and reads no settings:
it chooses by the worker table and names its choice in its report.

### 3. Choose the worker

Run `emb engines --json` for each engine's `state`. Use only engines that
are on (`engine.<name>` in the answers), `connected`, and that this
surface can run. Do not choose, withhold or swap workers by cost or usage;
Ember reads no usage figures. A provider refusal is a run error, not a
usage reading.

| Worker | Good default |
|---|---|
| No worker | A few lines in one or two files already open. |
| Codex, `emb run --engine codex` | Coding, drafting, tests and research; the default. |
| OpenCode, `emb run --engine opencode` | A model suited to the task from any integration connected in OpenCode; when the owner names it, or as `references/runs.md` allows after a usage limit. |
| Claude CLI, `emb run --engine claude` | A requested Claude CLI run. |
| Claude subagent | Work needing Claude's tools, the browser, connectors, its own workers, or a folder that is not a git checkout. |
| Claude Workflow | Claude-side fan-out of two or more tasks, with opt-in. |
| Flow, `emb flow` | Two or more coding tasks with a writer and a reviewer each, run by Ember in waves. |
| Panel, `emb panel` | One question that independent answers from several models improve. |

#### Several tasks

For two or more tasks needing workers, read `menu["multi.runs_as"].value`:
`flow` runs a flow; `workflow` runs a Claude Workflow and supplies opt-in.
`ask` asks the owner, offering a flow or separate single runs, and a Claude
Workflow only with opt-in. The owner's words win over the row.
Read `references/flows.md` before a flow and `references/panels.md` before a
panel.

For research or search, recommend Codex with `--read-only`, and a Claude
subagent as the alternative when it needs the browser or connectors.

A small edit (a few lines in one or two files already open) defaults to no
worker even when `single.writer` is `set`; say so in one line. The run still
claims, and finishes at once. Work a CLI
engine cannot do (connectors, the browser, Claude's tools) fails that
engine's gate: recommend a Claude subagent with the reason, whatever the
row says. Editing workers need a git checkout; work in a folder that is not
one goes to a read-only subagent, or you do it. Otherwise a `set` row, a
claimed choice or the owner's request binds, and departing from it needs
the owner's consent.

Use `single.writer` for edits and `single.reviewer` for read-only work.
For a `claude` row, `run_as` picks `cli` (`emb run --engine claude`) or
`subagent`. Take engine, model and effort from a `set` row. For `emb run`,
once the brief is written, run `emb run --dry-run` with the start's exact
options, including its claim, model, effort and read-only options when
given. On success, copy its standard-output line as the visible announce
line right before the start, for example:
`Team, Single tasks Writer: codex gpt-6.1-sol · high (single.writer)`.
A dry-run refusal starts no worker. For a Claude subagent, name the row,
model and effort yourself.

An explicit worker or model in the owner's request wins over the menu,
within the gates. Use its named model; otherwise use the job's row when it
holds the same engine, or ask among that engine's shortlisted models. Say
model and effort once, with any difference from Team; for `emb run`, the
dry-run line says it. Do not change the saved menu. With the owner away
or rows not `set`, pass named workers
explicitly: `emb gapcheck --engine <e> --model <m> --effort <x>`,
`emb flow --writer <e>:<m>@<x> --reviewer <e>:<m>@<x>`, or repeatable
`emb panel --member <e>:<m>[@<x>]`. Never set a row to make a command work.

When a row is not `set` and no worker was named, ask with AskUserQuestion.
Include its `state` and `reason`. Build options from
`emb engines --models <engine> --json` and
the enabled shortlists. Put worker, model and effort in each label; give
the recommendation in one or two plain sentences and one or two real
alternatives. Put the task and every decision fact inside the question:
text before the picker is hidden. Without the picker tool (print mode),
ask the same question in plain text, as one direct question ending in a
question mark, and stop until the owner answers.

Resolve two or more separately started tasks one at a time, numbered
`Task N of M`. A same-worker correction, recovery or retry needs no new
question; a replacement worker or model does. After a capacity error, offer
only the same-model retry; see `references/runs.md`.

### 4. Establish the editing boundary

Run each editing worker in its own worktree and branch, and commit there.
`emb run` makes the branch `ember/<engine>/<id>` and its worktree, cut from
the `HEAD` of the checkout named by `--dir`, as `references/runs.md`
describes. Each `emb run --read-only` and panel member gets its own detached
worktree, with no branch. Tell reviewers to read their own working folder,
using relative paths.
An editing subagent or Workflow agent uses
`isolation: "worktree"`; Claude Code bases it on `worktree.baseRef`:
`head` is the current `HEAD`, `fresh` the remote default branch. Say so
when it is `fresh`. A worker's base must hold what its task needs: when
the base is `fresh` and the task needs session commits or an earlier
merged branch, use `emb run` instead, or ask the owner.

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

Pass the harness attribution trailer to every `emb run` start with
`--trailer`; give editing Claude agents the same trailer in their git
line. The commit subject of an `emb run` turn is the brief's first
non-empty line, cut at 72 characters. Write that line as the worker's
objective; the lead writes the landed subject at merge.

### 5. Write the brief

Use `assets/task-brief.md` for every brief: one objective, exact scope and
exclusions, a reading budget, outputs, a stop condition, a required
"Not covered" list and the right git line. Editing workers report branch,
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
folder hidden except the paths `references/runs.md` lists. Give a worker
everything outside its worktree it needs through `--read <path>` or the
brief, never as an absolute path it cannot reach. Tell it to stay in its
own folder and use relative paths.

Keep the brief to the request: a fix you noticed elsewhere goes in your
report, because the owner did not ask for it. Do not set message wording,
docstrings or tests the request does not ask for. Do not fan out beyond what
you can review.

Before a flow or a Claude Workflow of two or more tasks starts, follow the
gap check in `references/gap-check.md`.

### 6. Start and follow the run

Read `references/runs.md` before the session's first `emb run`, and again
on every non-zero exit. Read `references/claude-agents.md` before a
subagent or Workflow spawn.

Run every `emb run` start, `--send` and `--recover`, and every
`emb flow --start` and `--watch`, `emb gapcheck` and `emb panel`, as a
background Bash command, unpiped, and take the exit code: a pipe hides it.
Pass the claim with `--claim <id>`.

| Purpose | Command |
|---|---|
| Preview and announce | Add `--dry-run` to the start's exact options; copy its line before starting |
| Start | `emb run --engine <engine> --dir <repo> --task-file <brief> --trailer '<trailer>' --claim <id>` |
| Read-only | Add `--read-only` |
| Show the owner | Add `--window` at start, or `emb run --attach <run>` later |
| Correct | `emb run --send <run> --message-file <file>`; write the correction yourself |
| Inspect | `emb run --screen <run>` |
| Stop a turn going wrong | `emb run --interrupt <run>` |
| State | `emb run --status <run> [--json]` |

Keep the run id from the start's first line with the claim.

Ember prints one `next:` or `owner:` line on standard error at each step of
an `emb run` task: at exit 0 after a start, `--send`, `--recover`, `--close`
or `emb merge`, on every non-zero exit, and in `emb run --status <run>`,
which also prints the run's `step:`. The line is the next command with its arguments
filled in. Read standard error after every run, also at exit 0. An
`owner:` line comes first: give it to the owner, naming the action, and
wait for the answer before running any `next:` line, correction or retry
that depends on what it names. Otherwise follow a `next:` line within these
rules and the owner gates below. A line leaves the brief, the
review bar, the verdict, `--note` and `--subject` to you. After a turn with
commits the line is the verify command, then the merge command: review and
report between them. Do not retry a
refusal: a refusal is Ember or the owner saying no, and repeating it
changes nothing.

Report every `WARNING:` line. For every denied path, name its run and turn
and its reason, and give the Settings, Sandbox read paths owner action from
`references/runs.md` (Denied paths). Never add paths to `sandbox.read`
yourself. A denied path counts as an `owner:` line, also when Ember prints
none for it: until the owner answers, send the run nothing, not even a
correction that avoids the denied paths. Count turn results from each run's `turns` in
`emb run --status [<run>] --json`, including failed turns followed by
successful ones, and report unknown fields as unknown; `references/runs.md`
(Turn results) describes unfinished and older results.

When your check of a worker fails, correct it first with `--send` or
SendMessage, unless an `owner:` line or a denied path still waits for the
owner's answer; offer another worker after a correction fails. When the
owner named the workers or said they will not answer worker questions,
do not offer one: give the verdict and say what is missing. Do not discard
stalled partial edits without the owner's approval.

### 7. Verify and review

Verify in the worker's own worktree and branch, not the session checkout.
Write scratch output to `/tmp`, never into a worker's worktree. Delete,
clean or commit nothing there, including files your own checks create;
run Python checks with `PYTHONDONTWRITEBYTECODE=1`. Cleanup decides from
what the worktree holds. After every editing worker, verify its branch. For
an `emb run` branch, run the `verify_branch.py` command in the `next:` line
Ember printed after the turn. For a branch with no run record (a flow
or `worktree-agent-*` branch), run:

```bash
python3 scripts/verify_branch.py --repo <repo> --base <session branch> --branch <branch> --worktree <worktree>
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

### 8. Report and park

For each `emb run`, copy its summary line exactly
(`<engine> <model> · <effort> · <secs>s · exit <code> · <n> files · turn <n>`),
then the verified numstat, then one verdict: `Keep`, `Keep, with <fix>`,
or `Discard, because <reason>`. While a correction you sent is still to be
checked, say the branch is under correction instead. When the owner typed
into a worker's session, name it in the verdict. For a read-only run,
branch, commit and numstat are "not applicable".

`Keep, with <fix>` names who makes the fix: you on the session branch
after the merge, or the worker through `--send` before it. Either way the
fix is checked before the branch counts as done.

Report a subagent or Claude Workflow in one or two plain sentences, naming
its branch when it has one. Every worker with a branch gets one of the
three verdicts, because only Keep work merges.

For an editing run with commits the order is verify, review, report, merge,
then finish as merge's `next:` line says; the merge closes the run. Park a
read-only run or a run without commits with `emb run --close <run>` when its
`next:` line names it. Park a discarded run with `emb run --close <run>`
although its `next:` line names verify and merge: do not follow that line,
because Discard work is not merged (step 9). Park only a run this session
started, after its turn ended, its work was committed and its reply was
read and reported. Report runs parked and runs left open, with reasons.

### 9. Merge, clean up and finish

Give the verdict line as `--note`, such as `--note 'Keep, with a docstring
fix'`. `emb merge` prints it as `Verdict: <note>` before the merge and keeps
it as an `Ember-Verdict` trailer on the landed commit.
Repeat the same verdict in the step 8 report. Run `emb merge` as its own
Bash call, with no other command chained before or after it; do the same
after a correction. Merge only Keep or Keep-with-fix work into the session
branch. For an `emb run` branch, run the `emb merge` line in the `next:`
line printed after the turn, writing `--note` and `--subject` yourself. Write
`--subject` in the repository's commit convention, read from `AGENTS.md`,
`CONTRIBUTING` or `git log`. Describe the reviewed result across all turns.
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
it. What happens to it is the owner's decision. After a merge or cleanup that
prints no `next:` line, report each kept line and run
`emb run --status <run>`; follow the `next:` line it prints.

| Exit of `emb merge` or `emb cleanup` | Response |
|---|---|
| 64 | Fix the call; `emb merge` needs `--verdict keep`, a one-line `--note`, a one-line `--subject` for an Ember run branch and two different branches. |
| 65 | Run it inside the checkout, with branches that exist. |
| 70 | A git command failed; a merge already made stays. Report the message; follow `next:`. |
| 75 | Refused: the target is checked out nowhere, is mid-merge, mid-rebase, mid-cherry-pick or mid-revert, has changes, or the merge conflicted. A squash conflict names the files and leaves the target unchanged. Report it; do not retry or force it. After a conflict, ask the owner: a new run cut from the updated session branch redoes the work, or the owner resolves it. Keep the claim open until then. |
| 76 | The target or worker branch moved while building the squash. Review both tips before repeating the merge. |
| 77 | The target's `commit-msg` hook, commit identity or signing configuration refused the squash. Follow `next:`; nothing moved. |

A subagent or Workflow branch (`worktree-agent-*`) gets a merge commit.
`emb merge` keeps it as `not an Ember run branch`; remove its worktree with
`git worktree remove <path>` (never `--force` without approval), then follow
the `next: git branch -d <branch>` line.

Before squashing or deleting the session branch, run
`emb cleanup --merged-into <session branch>` and report the keeps.
Without a limiting option, `emb cleanup` covers every Ember run branch of
the repository, including other sessions' runs. Report a kept line for a
run this session did not start as another session's; do not follow its
`next:` line. Use `--branch <branch>` to limit cleanup to one branch, or
`--claim <id>` to limit it to the session's own claim.
Cleanup treats a branch as contained when it is an ancestor of the target,
when a reachable commit records its tip in `Ember-Task-Tip`, or when merging
it into the target gives no conflict and the target's own tree. The trailer
recognizes a squashed branch even after later target edits.

Do not remove a worktree or delete a branch that is not merged unless the
owner says drop it: a finished claim is not approval to delete. Show its
changes first. On an approved drop, report the tip, then use
`git worktree remove --force` and `git branch -D`.

Finish with the `emb choices --finish <id>` line that merge, cleanup or
close printed. Nothing prints it for a Claude subagent, a Claude Workflow, no
worker, or an `emb run` branch that is discarded, failed or abandoned: its
closed run keeps the verify-and-merge line while the branch is unmerged. For
those, run `emb choices --finish <id>` after merge or discard for editing
work (once the owner's decision on the kept branch is reported), after review
for read-only work, at once for no worker. Failed or cancelled runs still get
review and a decision on their partial work before the finish. `references/claims.md` says what its reply means.

## Owner gates

Never run `emb choices --set`, `--clear`, `--once` or `--clear-once`; they
also refuse without a terminal (exit 77). A worker pick does not authorize
changing the owner's settings. Point the owner to Ember's Team or Settings
screen (`emb app`), or give the exact command to run at their terminal:
`emb choices --set <key>=<value>` for a standing setting, or
`emb choices --once <key>=<value>` for a one-time choice. A
`single.writer` or `single.reviewer` value is `<engine>:<model>` or `ask`,
with no effort; an effort comes from the engine's shortlist.

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
| `references/runs.md` | Before the session's first `emb run`; on every non-zero `emb run` exit; before closing a run. |
| `references/claude-agents.md` | Before a Claude subagent or Claude Workflow spawn. |
| `references/gap-check.md` | Before a flow or a multi-task Claude Workflow starts. |
| `references/flows.md` | Before `emb flow`; at every flow report. |
| `references/panels.md` | Before `emb panel`; inspecting member progress or reading its summary. |
| `assets/task-brief.md` | Every brief. |
| `scripts/verify_branch.py` | Every editing worker's verification. |
