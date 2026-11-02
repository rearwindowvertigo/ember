Check this plan against its failure model before it starts

## Objective

Find gaps in the plan below: cases the plan's tasks do not cover that its
failure model says must be handled, contradictions between tasks, files
shared between tasks that run in parallel, a missing order between tasks
that depend on each other, and missing checks. Do not redesign the plan
and do not decide missing behaviour; name each gap.

Harm:
- high: work or data can be lost, or a wrong result is likely in normal
  use.
- medium: writers would likely build different things that need rework.
- low: easily changed later.

## Plan

<paste the plan here>

## Output

Your final message only, at most 60 lines:

- One line per gap, by step: `step <n>: <gap>`, then
  `harm: high | medium | low` and one sentence of scenario.
- `ranked:` every gap, from high to low harm.
- `uncovered steps:` the steps you did not check, or `none`.

Write "no gaps" only after naming each step you checked.

## Limits

Read-only. Do not edit, commit, stage or stash. Read only the files the
plan names.

## Closing report

State what you checked. List anything you could not check under
"Not covered".
