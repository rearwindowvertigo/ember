# Claims and picker recovery

## Read the reply

Each key in `answers` is `{"value", "layer"}`, with `layer` one of
`request`, `one-time`, `setting` or `default`: the first layer that holds
the key wins, and an `ask` in a higher layer wins too.

The menu has four rows. `single.writer` and `single.reviewer` each hold
`value`, `state`, `engine`, `model`, `effort`, `run_as` and `reason`:

- `state` is `set`, `ask`, `invalid`, `engine off`, `not connected` or
  `not shortlisted`, checked in that order.
- `effort` is the effort in the model's shortlist entry; none means none.
- `run_as` is `cli` or `subagent` for a `claude` row, `null` otherwise.
- `reason` is `null` for `set`, and says why otherwise.

`multi.runs_as` holds `value` (`ask`, `flow` or `workflow`), `state`
(`set` or `ask`) and `reason`; it names no worker. `panel.members` holds
`value` (the list), `state` (`empty` or `set`) and `members`, one row per
entry in the worker row states except `ask`. A `set` list can still hold
member rows that are not `set`: those members are left out, and each is
named with its reason. When `panel.members` alone is invalid,
`source_error` is `true` and `menu` is `null`, but the other settings
still resolve.

Use only a `set` row. For any other state, ask live with its state and
reason, among the enabled engines' shortlisted models. A saved value stays
saved when it cannot run; do not repair settings yourself.

`source_error: true` means a stored setting, a claimed copy or the
pending one-time choice is not valid: `menu` is `null`, every key is at
its default, and `errors` names the bad values. Treat every picker as
`ask` and give that as the reason.

## One-time choices

A one-time choice applies to the picker keys (`single.writer`,
`single.reviewer`, `single.writer_as`, `single.reviewer_as`,
`multi.runs_as` and `panel.members`); every other key is standing only. The owner sets it at a terminal with
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

The call and its retries are in SKILL.md step 2.

### Skill version

The claim reply has `skill`, `loaded_skill_version` and
`installed_skill_version`. Ember reads the installed skill link's target
on each claim, including a repeated claim of the same id. SKILL.md step 2
says what to do for each `skill` value:

- `current`: the versions match.
- `stale`: the versions differ, whether the installed version is newer or
  older. Standard error has `WARNING: the loaded ember skill is <x>; the
  installed skill is <y>` and a `next:` line.
- `unknown`: the installed link or its Ember skill is missing or
  unreadable, or its version line is missing, duplicated or malformed.
  `installed_skill_version` is `null`, with a warning and an `owner:`
  repair or reinstall line.

All three outcomes exit 0; they do not change `claim.status`. A malformed
`--skill` exits 64 before claiming and leaves a pending one-time choice
unchanged. Use `x.y.z`, with non-negative integer parts and no leading
zeros except `0` itself. Without `--skill` there are no skill-version
fields. Edits between releases with the same version are not detected.

### Claim status

- `claimed`: this run's copy of the one-time choice. A repeated claim of
  the same id returns the same copy.
- `none`: no one-time choice was pending; use standing settings.
- `gone`: finished or expired. Every key is at its default and
  `--request` values are not used.
- `unknown`: never claimed, or the pending one-time choice was not valid
  and nothing was recorded. Every key is at its default and `--request`
  values are not used.

After `gone` or `unknown`, treat every picker as `ask` for the rest of the
run. Do not fall back to standing settings or read a preview to replace it.
`emb run --claim` refuses such a claim (exit 64); SKILL.md step 2 says what
to do.

A lost reply (exit 75 for a busy database, or no output at all) may still
have recorded the claim. Call again with the same id; never mint a new
one for the same run.

## Run identity

A Claude Workflow shares one claim across its agents. A new worker claims
anew. SKILL.md step 2 says what else is one run.

## Finish

`--finish` records the id `gone`, also when it was not known; `gone` in its
reply means the finish worked. The reply's `was` is the status before the
finish. A failed finish is retried with the same id. SKILL.md step 9 says when
to finish.

## Exit codes

| Exit | Meaning | Response |
|---|---|---|
| 0 | Success | Read the reply. |
| 64 | Usage error, unknown key, invalid value or invalid id | Fix the call: check the id pattern `[a-z0-9][a-z0-9-]{5,63}` and the options. |
| 74 | The settings database cannot be opened or read | Give the owner the `owner:` line. `emb run` cannot start until it is repaired; meanwhile offer only a Claude subagent or no worker. |
| 75 | Stale generation, or the database stayed busy for 5 seconds | Retry the same call once with the same id. If it fails again, tell the owner; `emb run` waits, and only a Claude subagent or no worker can be offered meanwhile. |
| 77 | An owner option without a terminal | An owner option was run; it should not have been (SKILL.md, Owner gates). |
| 78 | The settings database has an unsupported schema | Give the owner the `owner:` line. `emb run` cannot start until it is repaired; meanwhile offer only a Claude subagent or no worker. |

## Owner options

SKILL.md (Owner gates) says what to run and what to give the owner. Standing
settings change in Ember's Team or Settings screen (`emb app`) or at the
owner's terminal; one-time choices only at the terminal.

Ember's configuration and state folders, their descendants and symlink
aliases are refused by Settings and `emb choices`; report that reason.
Other secret locations can be added by the owner by hand, even when
Overview offers no Allow reading button.

The terminal check prevents accidental use; it is not a security boundary
against programs running as the owner, which can open the database.
