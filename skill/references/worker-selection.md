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
once before choosing. It reads saved data locally. The pool is the owner's
authorization to use an Ember CLI worker. Benchmarks and owner ratings
are evidence for that choice. Check eligibility first: the model is on the
current shortlist, is available at its shortlisted effort, and can do the
job through a connected, enabled engine. Use the model catalogue when you
need to confirm availability or effort. Active model or engine holds
exclude a candidate from automatic selection.

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

In `workers["<engine>:<model>"]`, check that `effort` and `speed` match
the planned start. `matches` and `related` are keyed by benchmark series,
and `sources` identifies each publisher, edition, suite and metric. Compare a numeric value
only with the same metric under the same series key and comparable trial
coverage. Terminal-Bench 2.1 and 4.1 are separate tests, as are IronBee's
50-PR and 10-PR sets. Never combine sources into a capability score or
fill one benchmark's missing value from another. Zero is a result.

Use `related` as background about the tested setup, without treating its
scores or costs as values for the configured worker. Use owner `scores`
and `notes` or relevant prior work as separate evidence when a benchmark
is missing or the status read fails. Do not turn a benchmark score into a
probability of success on this job. Missing evidence alone does not rule
out a model.

Among workers likely to meet the quality bar, prefer the lowest comparable
benchmark cost. Compare dollars per pass (`pass_usd`) when available for
the workers within the same benchmark series. If only dollars per
attempt (`attempt_usd`) are comparable within one series, consider likely
retry or rework as part of judging suitability. When costs are unreported,
known to be out of date, or from different series or setups, choose on
suitability without assuming which worker costs less. Benchmark dollars
describe published inference spend; they do not predict subscription quota
or a bill for the current task. Ignore any legacy `usage` value. Follow
Ember's usual provider retry and fallback rules.

## Apply the policy

- **Ask each time (`ask`):** Recommend the best fit and give the owner a
  meaningful alternative when one is available. Other eligible shortlisted
  models can be offered. Explain the task-specific reason and wait for an
  answer.
- **Let the lead choose (`auto`):** Consider only eligible pool members for
  the automatic choice. Post one line naming the model, effort and why it
  fits before calling the tool that starts it. If benchmark cost helped
  you choose, name the benchmark and unit. If none fits, say why and ask
  for a worker. An eligible model outside the pool needs an explicit choice
  for this job. Never change the pool or settings yourself.

Pass the result explicitly to the command that starts that role, with the
shortlist effort when set. If an OpenCode shortlist entry has no effort,
omit the effort option. Announce a single automatic worker when you pick
it, before preparing its brief. For a flow, use the dry run's worker
lines for the announcement. Name the gap check reviewer before running it.
