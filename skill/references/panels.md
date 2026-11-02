# Panels

Use a panel when the owner asks for several models' views, or independent
answers improve a design choice, review or gap check. It is not a routine
research step. A panel has one question, one claim, and no branch or verdict.

## Members

Pass the owner's named models as repeatable
`--member <engine>:<model>[@<effort>]` options. An OpenCode model is the
bare OpenRouter id, such as `opencode:deepseek/deepseek-v4.1-flash-1017`;
Ember adds the provider itself, so never prefix it with `openrouter/`. Without named models, use
saved `panel.members` by omitting `--member`. If none are saved, ask the
owner in plain text and pass the answer as explicit members. Never set
`panel.members` yourself. A panel accepts one to eight distinct
engine/model pairs; different efforts do not make a repeated pair valid.

Engines must be on and connected. Saved members that cannot run are left
out with their reasons; include each exclusion in the report. Exit 64
with the members' `owner:` line goes to the owner for a corrected choice.
Do not repeat an unchanged refusal. Claude members run through the CLI;
do not add a subagent the owner did not pick.

Before starting, post one visible line to the owner naming the members
and their efforts and that OpenCode members spend OpenRouter credit,
whatever the dry run returns. Add `--dry-run` to the
start command to resolve members and efforts without starting any run.

## Question and start

Write the question alone in a session-scratchpad file: what to answer,
files to read, and the shape and length of the answer. For a review,
include the skill's review bar exactly. For a gap check by a panel, use
the text `emb gapcheck --plan <file> --brief` prints. Members must not
write files; their answers belong in their final messages.

Run as a background Bash command, unpiped, and take its exit code:

```bash
emb panel --question-file <file> --dir <repo> --claim <id> [--member W]...
```

Keep the panel id from the first line with the claim id. Read standard
error, including warnings and action lines, even at exit 0. Follow `next:`
within the skill's rules; bring `owner:` to the owner and wait.

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

Report one line per member from the output: engine, model, effort, status,
seconds, exit and bytes. Then give the comparison and answer. Name every
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
Report open runs and their reasons. Finish the claim after the report with
`emb choices --finish <id>`, using the claim id, not the panel id.
