# Ember use survey review

You are one member of a panel reviewing one Ember use survey entry. Other
members answer the same question on their own; the lead compares your
answers. Work read-only: change, create or delete no file.

The entry to review is `{{ENTRY}}`, relative to the repository top.

## Read

1. `dev/survey/README.md`, for the scoring scale and how entries are
   filled.
2. The entry, `{{ENTRY}}`.
3. Every other file in `dev/survey/entries/`, to find points that repeat.
4. `AGENTS.md`, every file in `docs/design/` and every file in
   `docs/decisions/`, for what Ember is, what is decided, and what is not
   planned.
5. Any source file, test or skill file an entry point names, to check it.

## What to do

### 1. List the points

Take every point the entry makes: each friction point, each thing that
worked, each suggested change, each bug and each efficiency. Number them
`P1`, `P2` and so on, in the order the entry gives them. Quote or closely
paraphrase each one, and name its area (the entry's section number and
title, or "Summary").

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
earlier entries (`<entry file name>: <section or quote>`). A point with no
overlap gets no tag.

### 5. Decide on changes

For each point that is not a `note`, decide one outcome:

- `change`: a reasonable change to Ember, its skill, its docs or the survey
  would address it. Say what to change, in which file, command or screen,
  and what it would improve. Keep it to the point's evidence.
- `owner`: a change would conflict with something decided or not planned
  in `docs/design/` or `docs/decisions/`, or needs a decision only the
  owner can make. Name the decision or the conflicting item.
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
