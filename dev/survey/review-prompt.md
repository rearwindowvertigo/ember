# Ember use survey review

You are one member of a panel reviewing one Ember use survey entry. Other
members answer the same question on their own; the lead compares your
answers. Work read-only: change, create or delete no file.

The entry to review is the survey issue saved at `{{ENTRY}}`.

## Read

1. `dev/survey/README.md` and `dev/survey/agent.md`, for the scoring
   scale and how entries are filled.
2. The entry, `{{ENTRY}}`.
3. `{{POINTS}}`, the lead's numbered list of the entry's points (`P1` to
   `Pn`), each with its area and a one-line quote or close paraphrase.
4. Every other file in `{{EARLIER}}` (earlier survey issues, named
   `issue-<number>.md`) and in `dev/survey/entries/` (entries from before
   survey issues), to find points that repeat.
5. `AGENTS.md`, every file in `docs/design/` and every file in
   `docs/decisions/`, for what Ember is, what is decided, and what is not
   planned. `docs/design/` is history up to 0.5.0.
6. `{{DECISIONS}}`, the decisions recorded as "Decision (owner, ...)" and
   "Decision (lead, ...)" comments on the repository's GitHub issues, one
   JSON object per issue. Where it and `docs/` disagree, the later decision
   holds.
7. Any source file, test or skill file an entry point names, to check it.

## What to do

### 1. Use the lead's points

Use every point in `{{POINTS}}`, keeping its number from `P1` to `Pn`.
Check the entry for missed points: each friction point, thing that worked,
suggested change, bug and efficiency. Append any missed points as `Pn+1`
onwards, marking each as `added` in the Point column. Quote or closely
paraphrase each added point, and name its area (the entry's section number
and title, or "Summary"). Do not renumber the lead's points. Added numbers
are local to your answer; the lead compares them by member and local
number, then assigns consolidated numbers with a mapping back to each
member's additions.

### 2. Check each point

For each point, check its evidence against the repository: the files,
commands, exit codes or behaviour it names. Say whether the evidence holds
(`confirmed`), cannot be checked from the repository (`unverifiable`), or
is contradicted by what you found (`contradicted`). Cite the file and line,
or say what you could not reach.

### 3. Rank each point

Give each point one severity:

| Severity | Meaning |
|---|---|
| blocker | Work failed, was lost or was wrong, or a safety rule was broken. Matches an area score of 1. |
| major | A substantial detour, a retry the owner had to unblock, or a wrong instruction to the owner. Matches an area score of 2. |
| minor | Small friction that cost little time. Matches an area score of 3 or 4. |
| note | A positive or neutral point; no change follows from it. Matches an area score of 5. |

### 4. Tag overlaps

Group points that are about the same thing, whether positive, negative or
neutral, under one tag. A tag is a few lowercase words joined by hyphens,
such as `tray-menu` or `gap-check-premise`. A point can carry more than one
tag. For each tag, list the points in this entry and the matching points in
earlier entries (`#<issue number>` or `<entry file name>`, then
`: <section or quote>`). A point with no overlap gets no tag.

### 5. Decide on changes

For each point that is not a `note`, decide one outcome:

- `change`: a reasonable change to Ember, its skill, its docs or the survey
  would address it. Say what to change, in which file, command or screen,
  and what it would improve. Keep it to the point's evidence.
- `owner`: a change would conflict with something decided or not planned
  in `docs/design/`, `docs/decisions/` or the issue decisions, or needs a
  decision only the owner can make. Name the decision or the conflicting item.
- `done`: the repository already holds a change that addresses it. Name it.
- `none`: no change is reasonable, for example because the point is
  outside Ember's control (another program's behaviour) or one-off. Say
  why.

## Answer

Answer in Markdown, in this shape and nothing else:

```
## Points

| Point | Area | Summary | Evidence | Severity | Tags | Outcome |
|---|---|---|---|---|---|---|
| P1 | 3. Running and following work | <one line> | confirmed: <file:line or source> | major | tray-menu | change |

## Tags

- `<tag>`: P1, P3; <earlier entry>: <section or quote>

## Changes

- P1 (`change`): <what to change>, in <file, command or screen>; <what it improves>.
- P4 (`owner`): <the decision needed>, <the conflicting item if any>.
- P6 (`done`): <the change already in the repository>.
- P7 (`none`): <why not>.

## Not covered

<What you could not read or check, or "none".>
```

Every point appears once in the table and, unless it is a `note`, once
under Changes. Keep each table cell to one line.
