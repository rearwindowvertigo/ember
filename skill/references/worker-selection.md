# Choose a model for the job

Read the row for the current role from the claimed choices. An explicit
model for this job wins, then the resolved row (request, claimed one-time
choice, standing setting, default). An unusable fixed choice still needs
the owner's approval for a substitute. For a `same` gap check or flow row,
use the inherited single-task policy on the current job. Do not reuse a
model the lead previously chose under that policy just because the policy
is the same. Panel members follow `references/panels.md`.

## Match the work

Read `answers["routing.pool"].value` and run `emb benchmarks status --json`
once before choosing. It reads saved data locally. For automatic launches
the owner approves models through the Routing pool. Check that each
candidate is still shortlisted and can do the job through a connected,
enabled engine. Active model or engine holds exclude a candidate from
automatic selection.

In `ask` and `auto`, compare model-and-effort pairs. Confirm the model
advertises the chosen effort with `emb engines --models <engine> --json`
when the current catalogue is needed. The Engines shortlist effort is
the default for fixed Team choices. In the two flexible modes, select any
supported effort for an eligible model. If an OpenCode model offers no
effort setting, leave the effort unset. Use the owner's Codex speed
setting (`fast.codex`); Fast needs separate approval in Engines.

Use the task, plan or diff you already have:

- For a writer, look at LiveBench Coding. Use Terminal-Bench when the
  work requires debugging builds or executing many steps with tools.
  Weigh ambiguity, scope, consequences of a wrong change and how well
  the checks can catch it. Use LiveBench Reasoning for ambiguous or
  cross-cutting work.
- For a code reviewer, look at IronBee and IonWarp F1 for evidence of finding
  defects, keeping each test separate.
  Use SWE Atlas Codebase QnA for repository comprehension and LiveBench
  Reasoning for missed cases. For a gap check, weigh reasoning about
  dependencies and failure cases, and codebase comprehension when the
  plan depends on how the repository works.
- For a flow, choose one writer and one reviewer for the plan. Evaluate each
  role separately against its most demanding work, including the whole-flow
  review when enabled. A task in the flow does not start a new selection.

Use `workers["<engine>:<model>"]` for pool models. When making a
recommendation in `ask`, also use `other_shortlisted_workers`. Read
`matches_by_effort[effort]` for task-specific harness evidence at the
chosen effort. The `matches` field is for the saved shortlist effort.
Check `speed` before using either. `sources` identifies each publisher,
edition, suite and metric. Compare a numeric value only with the same
metric under the same series key and comparable trial coverage.
Terminal-Bench 2.1 and 4.1 are separate tests, as are IronBee's 50-PR and
10-PR sets. Never combine sources into a capability score or fill one
benchmark's missing value from another. Zero is a result.

When present, use `model_reference.efforts[effort]` for an eligible
worker's tested release and effort. The source is the Artificial Analysis
Intelligence Index. A moving Claude alias shows a named test release.
The reference supplies a general baseline; give relevant task-specific
and harness evidence more weight when available. An Index result does
not measure the worker's harness, provider route or Codex speed.

Use `related` as background about the tested setup, without treating its
scores or costs as values for the configured worker. Keep owner `scores`,
`notes` and relevant prior work separate from benchmark results. Do not
turn a benchmark score into a probability of success on this job. Missing
evidence alone does not rule out a model.

Among workers likely to meet the quality bar, prefer the lowest comparable
benchmark cost. Compare dollars per pass (`pass_usd`) when available for
the workers within the same benchmark series. If only dollars per
attempt (`attempt_usd`) are comparable within one series, consider likely
retry or rework as part of judging suitability. For the Index baseline,
compare `task_usd` across comparable model references within one edition.
It is weighted USD per Index task, with no pass or retry adjustment.
When costs are unreported, out of date, or from incomparable series or
setups, choose on suitability without assuming which worker costs less.
Benchmark dollars describe published inference spend; they do not predict
subscription quota or a bill for the current task. Ignore any legacy
`usage` value. Follow Ember's usual provider retry and fallback rules.

## Apply the policy

- **Ask each time (`ask`):** Recommend a shortlisted model and supported
  effort. Give a meaningful alternative when available; it can be another
  effort of the same model. Explain the task-specific reason and wait for
  the owner's choice.
- **Let the lead choose (`auto`):** Pick a suitable supported effort for
  an eligible Routing pool model. If no approved model and effort fits,
  ask the owner and wait. A model outside the pool needs the owner's
  explicit choice for this job. Never change the pool or settings yourself.

Pass the selected model and effort explicitly to the command that starts
the role. If there is no selected effort, omit the effort option.

For an automatic single worker, publish the choice with the dry run's
`--announce` option. Give a task-specific reason, such as “the boundary
tests cover this two-file edit.” When cost helped choose, pass the series
key and cost metric from the same evidence row as `--benchmark-cost
<series>:<metric>`. Copy the complete series key, including any edition.
For example, `--benchmark-cost artificial-analysis-intelligence-index-5.0:task_usd`
posts the Artificial Analysis cost in USD per Index task. Omit the option
if cost did not affect the choice. For a flow, use the dry run's worker
lines for the announcement. Name the gap check reviewer before running it.
