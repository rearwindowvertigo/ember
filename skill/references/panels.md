# Panels

Use a panel when the owner asks for several models' views, or independent
answers improve a design choice, review or gap check. It is not a routine
research step. A panel has one question, one claim, and no branch or verdict.

Complete the skill-version check at the claim (SKILL.md step 2) before
choosing members.

## Members

Pass the owner's named models as repeatable
`--member <engine>:<model>[@<effort>]` options. An OpenCode model is the
full `<integration>/<model>` id from `emb engines --models opencode --json`,
such as `opencode:openrouter/deepseek/deepseek-v4.1-flash-1017` or
`opencode:anthropic/claude-sonnet-4`. Ember uses the id unchanged.
Without named models, use
saved `panel.members` by omitting `--member`. If none are saved, ask the
owner in plain text and pass the answer as explicit members. A panel accepts one to eight distinct
engine/model pairs; different efforts do not make a repeated pair valid.

Engines must be on and connected. Saved members that cannot run are left
out with their reasons; include each exclusion in the report. Exit 64
with the members' `owner:` line goes to the owner for a corrected choice.
Do not repeat an unchanged refusal. Claude members run through the CLI;
do not add a subagent the owner did not pick.

Before starting, post one visible line to the owner naming the members
and their efforts, whatever the dry run returns. Add `--dry-run` to the
start command to resolve members and efforts without starting any run.

## Question and start

Write the question alone in a session-scratchpad file: what to answer,
files to read, and the shape and length of the answer. For a review,
include the skill's review bar exactly. For a gap check by a panel, use
the text `emb gapcheck --plan <file> --brief` prints, adding
`--previous <last result>` on every repeat and using that same result when
parsing the reply with `emb gapcheck --reply`. Members must not
edit the reviewed source; their answers belong in their final messages.
Tell them to read the repository in their own working folder, using relative
paths. A panel pins one source `HEAD` for all members and retries; each
member gets its own detached worktree there (`references/runs.md`). Members
have no branch and Ember never commits their changes. Uncommitted edits and
later source-checkout changes are absent.

Start the panel:

```bash
emb panel --question-file <file> --dir <repo> --claim <id> [--member W]...
```

Keep the panel id from the first line with the claim id. Each member's
completion line arrives on standard error when it ends, while other members
may still be running; `--json` keeps standard output as one final JSON
object. Follow printed lines as SKILL.md step 6 says.

## Progress

Use `emb panel --status <id> --json` to inspect a running panel. Its
`members` array contains each member's status, elapsed `seconds` and
`last_screen_change_at` from its current run. The timestamp is epoch
seconds; null means no recorded activity, including older run records
without the field. Preserve null as unknown and zero as zero. Elapsed
time includes the current collection interval and accumulates across
recovery; a retry's replacement run starts its own timing.

`emb panel --status --json` lists panels with integer `member_count` and
`running_members`, not `members`. Each running-member entry has `n`,
`seconds` and `last_screen_change_at`; no running members gives `[]`.
Read one panel's status for its full member records. Text status shows
elapsed seconds and `last screen change <epoch or ->`; the list shows
these lines for `starting` and `running` members and members with denied
paths.

Member diagnostics and status include `denied_paths`, retained across
attempts, with `path`, `reason`, `allow_path`, `run` and `turn`. Panel status
also gives their union with the `member` number. Empty lists mean no
recorded denials. Panels have no denial event stream. Report every denied
path with its member, run, turn and reason, including for answered members.
Startup-file denials and older records are handled as
`references/runs.md` (Denied paths) says. Report printed owner actions and hidden-path
restrictions as `references/runs.md` (Denied paths) describes.

## Read and compare

After exit 0 or 83, read the whole `summary.md` at the printed `summary`
path before writing the comparison or answer. It holds every available
reply, including partial replies from failed members. Read it after an
interruption too if the owner chooses to use the available answers.

Compare in five parts, every part every time; an empty part says
"none", and with two answers part 3 says "none beyond part 4":

1. What all or most agree on.
2. Where they disagree.
3. What only some cover.
4. What only one says.
5. What none address.

Then answer the question. Agreement is evidence, not proof. Check the
claims your answer depends on yourself and say which you checked.

Report one line per member from standard error or status: engine, model,
effort, status, seconds, exit and bytes. Then give the comparison and answer. Name every
member that is not `answered`, including failed, stalled, timed-out and
interrupted members, every left-out member and reason, and every member
that changed paths even if it answered. Preserve zero counts as zero.

## Unfinished members

On exit 83 or 74 with unfinished members, offer
`emb panel --recover <id>` to collect late answers, or reading the summary
now. Recovery starts no new member and cannot collect a member without a
saved run id; follow the status action when no such id is known. Run
recovery in the background and read the updated summary before comparing.
Exit 75 on recovery: follow `next:`.

Member turns may still run after the panel stops waiting. Close one with
`emb run --close <run> --force` only on the owner's go-ahead for that run.
Report open runs and their reasons. Closing a member removes its clean
detached worktree. `emb panel --close <id>` closes open current and retained
runs without force and retries cleanup of closed or failed runs' detached
worktrees. Worktrees whose only new files are git-ignored are removed.
Tracked changes and untracked non-ignored files keep the worktree and its
recorded path with a
warning naming `git worktree remove --force <path>`, including after a
forced session close. Report it and leave discard to the owner's
instruction. Finish the claim after the report as SKILL.md
step 9 says, with the claim id, not the panel id.
