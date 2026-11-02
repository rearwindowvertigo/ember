---
date: yyyy-mm-dd
ember_version: x.y.z
session_model: <the filling session's model id>
task: <one line naming the session's work>
engines: [<engines used>]
workers:
  - <engine:model@effort and its role, one per line>
scores:
  setup: <1-5 or not used>
  planning: <1-5 or not used>
  running: <1-5 or not used>
  review: <1-5 or not used>
  panels: <1-5 or not used>
  owner: <1-5 or not used>
  app: <1-5 or not used>
  speed: <1-5 or not used>
  overall: <1-5>
---

# Ember use survey: <task>

Score each area with the scale in `README.md`. An area the session did not
use gets `not used` and the line `Not used in this session.` in place of
its answers. A question with nothing to report gets `none`.

Every area asks the same five questions:

- **What happened:** the commands run, exit codes and outcomes, in order.
- **What worked:** steps that went as the skill or Ember said they would.
- **Friction:** each point where work slowed, failed, was retried or needed
  a workaround, with its evidence and the time it cost.
- **Skill and references:** where the skill's text or its references
  helped, were unclear, were missing something or misled.
- **Suggested change:** what to change, where (file, command or screen),
  and what it would improve.

## 1. Setup and choosing workers

`emb ground`, `emb choices` claims and reads, `emb engines`, Menu rows and
the worker question.

Score:

- What happened:
- What worked:
- Friction:
- Skill and references:
- Suggested change:

## 2. Planning and gap checks

Briefs, flow plans, `emb flow --dry-run`, the gap check before a flow and
the owner's answers to its gaps.

Score:

- What happened:
- What worked:
- Friction:
- Skill and references:
- Suggested change:

## 3. Running and following work

`emb run` and `emb flow`, `--watch`, `--send`, `--recover`, lead stops,
`next:` and `owner:` lines, `WARNING:` lines, non-zero exits and recovery.

Score:

- What happened:
- What worked:
- Friction:
- Skill and references:
- Suggested change:

## 4. Review, merge and cleanup

`verify_branch.py`, reviewer findings and the review bar, verdicts,
`emb merge`, `emb cleanup`, `emb run --close` and `emb choices --finish`.

Score:

- What happened:
- What worked:
- Friction:
- Skill and references:
- Suggested change:

## 5. Panels and stand-alone gap checks

`emb panel` and `emb gapcheck` run outside a flow.

Score:

- What happened:
- What worked:
- Friction:
- Skill and references:
- Suggested change:

## 6. Owner interaction

Questions put to the owner: how many, which were unclear or needed a
follow-up, decisions that had to come back to the owner, and owner
corrections.

Score:

- What happened:
- What worked:
- Friction:
- Skill and references:
- Suggested change:

## 7. Desktop app

Only what the session itself opened, ran or inspected in Ember's app.

Score:

- What happened:
- What worked:
- Friction:
- Skill and references:
- Suggested change:

## 8. Speed and waiting

Wall time of each run, flow and check; time spent waiting on workers,
checks or the owner; steps repeated.

Score:

- What happened:
- What worked:
- Friction:
- Skill and references:
- Suggested change:

## Summary

- **Top friction points:** ranked, most costly first, each naming its area.
- **Efficiencies found:** steps that saved time, or could save it if made
  the default.
- **Bugs found:** each with what was run, what happened and what was
  expected.
- **Overall score:** and one or two sentences on why.
