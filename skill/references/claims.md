# Claims and picker recovery

## Read the reply

Read the reply of `emb choices` directly; do not write a parser or helper.
Each key in `answers` is `{"value", "layer"}`, with `layer` one of
`request`, `one-time`, `setting` or `default`: the first layer that holds
the key wins, and an `ask` in a higher layer wins too.

The menu has two rows, `single.writer` and `single.reviewer`. Each holds
`value`, `state`, `engine`, `model`, `effort`, `run_as` and `reason`:

- `state` is `set`, `ask`, `invalid`, `engine off`, `not connected` or
  `not shortlisted`, checked in that order.
- `effort` is the effort in the model's shortlist entry; none means none.
- `run_as` is `cli` or `subagent` for a `claude` row, `null` otherwise.
- `reason` is `null` for `set`, and says why otherwise.

Use only a `set` row. For any other state, ask live with its state and
reason, among the enabled engines' shortlisted models. A saved value stays
saved when it cannot run; do not repair settings yourself.

`source_error: true` means a stored setting, a claimed copy or the
pending one-time choice is not valid: `menu` is `null`, every key is at
its default, and `errors` names the bad values. Treat every picker as
`ask` and give that as the reason.

## One-time choices

A one-time choice applies to the picker keys (`single.writer`,
`single.reviewer`, `single.writer_as`, `single.reviewer_as`); every other
key is standing only. The owner sets it at a terminal with
`emb choices --once <key>=<value>`. It expires 24 hours after it is set.
A claimed copy reads `gone` 7 days after the claim.

A read without `--id` is a preview: it shows the pending one-time choice
but authorizes no one-time answer. Only the claim's own reply, or a read
with `--id <id>`, does.

A claimed answer that cannot apply (an engine off or not connected, a
gate it fails, a worker this surface cannot run) is still used up. Name
the value, why it cannot apply, and that it is used up, in the live
question that replaces it.

## Claim outcomes

Keep the id in the run's notes before calling
`emb choices --claim --id <id>`.

- `claimed`: this run's copy of the one-time choice. A repeated claim of
  the same id returns the same copy.
- `none`: no one-time choice was pending; use standing settings.
- `gone`: finished or expired. Every key is at its default and
  `--request` values are not used.
- `unknown`: never claimed, or the pending one-time choice was not valid
  and nothing was recorded. Every key is at its default and `--request`
  values are not used.

After `gone` or `unknown`, treat every picker as `ask` for the rest of the
run. Do not fall back to standing settings, mint another id to recover the
answer, or read a preview to replace it. `emb run --claim` refuses such a
claim (exit 64): keep and finish the id, tell the owner no worker started
and why, and mint no other id for the same task. A Claude subagent or no
worker may still go ahead after a live question.

A lost reply (exit 75 for a busy database, or no output at all) may still
have recorded the claim. Call again with the same id; never mint a new
one for the same run.

Before each later picker, read `emb choices --json --id <id>`.

## Run identity

A Claude Workflow shares one claim across its agents. Separately started
tasks claim independently and are resolved in order; only the first
consumes the pending one-time choice. A correction or `--recover` of an
open run keeps its claim. A new worker claims anew.

## Finish

Finish with `emb choices --finish <id>`: after merge or discard for
editing work, after review for read-only work, at once for no worker.
For failed or cancelled work, review the partial results and settle merge
or discard before the finish. `--finish` records the id `gone`, also when
it was not known. A failed finish is retried with the same id.

## Exit codes

| Exit | Meaning | Response |
|---|---|---|
| 0 | Success | Read the reply. |
| 64 | Usage error, unknown key, invalid value or invalid id | Fix the call: check the id pattern `[a-z0-9][a-z0-9-]{5,63}` and the options. |
| 74 | The settings database cannot be opened or read | Give the owner the `owner:` line; ask the pickers live meanwhile. |
| 75 | Stale generation, or the database stayed busy for 5 seconds | Retry the same call once with the same id. If it fails again, ask the pickers live and tell the owner. |
| 77 | An owner option without a terminal | An owner option was run; it should not have been. Give the owner the command instead. |
| 78 | The settings database has an unsupported schema | Give the owner the `owner:` line; ask the pickers live meanwhile. |

## Owner options

Do not run `--set`, `--clear`, `--once` or `--clear-once`. Standing
settings change in Ember's Menu or Settings screen (`emb app`) or at the
owner's terminal; one-time choices only at the terminal. Give the owner
the exact command: `emb choices --set <key>=<value>` or
`emb choices --once <key>=<value>`.

The terminal check prevents accidental use; it is not a security boundary
against programs running as the owner, which can open the database.
