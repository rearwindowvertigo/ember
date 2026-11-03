# Ember use survey

An optional development tool. A Claude session that has used Ember fills in
`survey.md` when the owner asks for it, and the result is saved in
`entries/`. The entries record friction points, efficiencies and how each
session found Ember to use, over time.

Nothing in Ember, its skill, `AGENTS.md`, `CLAUDE.md` or any memory file
refers to this folder. A session fills the survey only when the owner asks.
`review-prompt.md` is the question a panel answers to review an entry.

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

## Reviewing an entry

A review runs once for each new entry, when the owner asks for it.

1. Copy `review-prompt.md` to a scratch file and replace `{{ENTRY}}` with
   the entry's path, such as `dev/survey/entries/2026-11-02-0-2-1-build.md`.
2. Run it as an Ember panel on this checkout, with the scratch file as the
   question: `emb panel --question-file <scratch file> --dir <repository top>`.
   The members are the saved `panel.members` unless the owner names others.
3. Compare the members' answers in the panel's `summary.md`, point by
   point, and write `reviews/<entry file name>` with:
   - the panel id and its members, with any member that failed;
   - one table of every point with the severity, tags and outcome most
     members gave, and each disagreement with each member's answer;
   - the tags, merged where members named the same overlap differently;
   - the proposed changes, each with the members that proposed it.
4. Commit the review on its own branch, with a `chore(survey):` subject,
   and open a pull request for it alone.

A proposed change in a review is not a decision. It enters a design
document only when the owner accepts it.

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
| 2026-11-03 | [0.3.0 to 0.5.0 builds](entries/2026-11-03-0-3-0-to-0-5-0-builds.md) | 0.2.1-0.5.0 | 4 | 3 | 2 | 3 | 4 | 3 | 3 | 3 | 3 |
