# ADR 0010: Codex speed tier for each model

- Status: Accepted, 2026-11-21.
- Release: Ember 0.27.0.

## Context

Codex has a Fast service tier. Its model catalog,
`~/.codex/models_cache.json`, gives each model the tier `priority`, named
"Fast", and a `default_service_tier`. On 2026-11-21 the catalog set that
default to `priority` for `gpt-6.2-astra` and `gpt-6.1-terra` and to null
for the other models. The catalog describes the tier as more speed, and
for some models as increased usage.

Ember passed Codex the model and the effort and nothing about the tier.
Codex then applied the model's default. A worker on `gpt-6.2-astra` showed
`fast` in its session footer (#316). No line, record or screen of Ember
named the tier.

Codex 0.171.0 has the configuration key `service_tier`, whose value `fast`
selects the tier, and the feature `features.fast_mode`, on by default,
which enables the Fast tier of the active model. A start with
`-c features.fast_mode=false` on `gpt-6.2-astra` showed no tier in the
footer. A start with `-c service_tier="fast"` on `gpt-6.1-sol` showed
`fast`.

## Decision

1. The owner sets the tier for each Codex model. `fast.codex` is the list
   of the Codex models that run on the Fast tier.
2. A Codex model that is not in the list runs on the standard tier,
   whatever default the catalog gives it.
3. Ember passes `-c features.fast_mode=false` for the standard tier and
   `-c service_tier="fast"` for the Fast tier.
4. A run's tier is set at its start, saved on its record as `speed` and
   kept for the run.
5. Ember names the speed wherever it names a Codex worker with its effort:
   the announce lines of a run and of a flow, the turn summary line, the
   run's status, the model list, the gap check result, the panel preview
   and panel member lines, the Engines screen and the Overview.
6. The Speed control is on shortlisted models. A model that the owner
   takes off the shortlist on the Engines screen leaves `fast.codex` too.

## Options not chosen

- One setting for all Codex workers.
- A setting on each Team row.
- Codex's own default for a model the owner has not set.

## Consequences

- A model whose catalog default is the Fast tier runs on the standard tier
  until the owner sets it to Fast.
- A run record written before 0.27.0 has no `speed`. Its command and lines
  are as before.
- Flow status lines name workers as `<engine>:<model>@<effort>` and do not
  name the speed. The record of each run of a flow holds it.
- An announce line or preview names the speed its model has when the line
  is printed. A run takes the speed its model has when the run starts.
- The two options were checked on Codex 0.171.0. Another Codex version can
  name or apply the tier differently.
