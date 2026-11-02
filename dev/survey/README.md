# Ember use survey

An optional development tool. A Claude session that has used Ember fills in
`survey.md` when the owner asks for it, and the result is saved in
`entries/`. The entries record friction points, efficiencies and how each
session found Ember to use, over time.

Nothing in Ember, its skill, `AGENTS.md`, `CLAUDE.md` or any memory file
refers to this folder. A session fills the survey only when the owner asks.

## Filling in an entry

1. Copy `survey.md` to `entries/<yyyy-mm-dd>-<topic>.md`, where `<topic>` is
   a few lowercase words joined by hyphens naming the session's work. If the
   name is taken, add `-2`, `-3` and so on.
2. Answer from this session only: its commands, exit codes, worker reports,
   owner questions and time spent. Cite them.
3. Answer every question. An area the session did not use gets `not used`
   as its score and the line `Not used in this session.` in place of its
   answers. A question with nothing to report gets `none`.
4. Add a row for the entry to the index below.
5. Commit the entry and the index row on their own branch, with a
   `chore(survey):` subject, and open a pull request for them alone.

Entries are the filling session's own assessment. The rule that committed
text states facts and does not judge does not apply to `dev/survey/`.

Do not put credentials, tokens or the owner's personal data in an entry.

## Scores

| Score | Meaning |
|---|---|
| 5 | Worked first time; nothing to change. |
| 4 | Worked, with minor friction that cost little time. |
| 3 | Worked after a retry, a workaround or an extra question. |
| 2 | Worked only after a substantial detour, or needed the owner to unblock it. |
| 1 | Did not work, or produced a wrong result. |
| not used | The session did not use this area. |

## Index

`n/u` in the table means not used.

| Date | Entry | Ember | Setup | Planning | Running | Review | Panels | Owner | App | Speed | Overall |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-11-02 | [0.2.1 build](entries/2026-11-02-0-2-1-build.md) | 0.2.0 | 4 | 4 | 3 | 4 | n/u | 3 | 2 | 4 | 3 |
