# Ember use survey

An optional development tool. A Claude session that has used Ember, in any
repository, fills in `survey.md` when the owner asks for it and files the
result as one issue in this repository with the `survey` label. That issue
is the entry: it records friction points, efficiencies and how the session
found Ember to use. A panel reviews it, and the owner decides which of the
review's proposed changes become issues.

Nothing in Ember, its skill, `AGENTS.md`, `CLAUDE.md` or any memory file
refers to this folder. A session fills the survey only when the owner asks.

| File | Holds |
|---|---|
| `agent.md` | The instructions a session follows to fill in and file the survey |
| `survey.md` | The survey template |
| `ember-survey/SKILL.md` | A skill that fetches `agent.md` and follows it |
| `review-prompt.md` | The question a panel answers to review an entry |
| `entries/`, `reviews/` | Entries and reviews from before survey issues, kept as history |

`agent.md` carries its own copy of the score scale below; a change to one
is made to both.

## Taking the survey

A session in any repository, this one included, takes the survey by
fetching `agent.md` from `main` with `gh` and following it. It reads no
other file of this repository except `survey.md`.

`ember-survey/SKILL.md` is a Claude Code skill that only fetches
`agent.md` from `main` and follows it. It runs when the owner types
`/ember-survey`, never on its own (`disable-model-invocation: true`). It
holds no survey text, so it does not change when the survey does. To
install it on a machine, with `gh` signed in to an account that can read
this repository (the file is written only when the download succeeds):

```bash
mkdir -p ~/.claude/skills/ember-survey && gh api repos/rearwindowvertigo/ember/contents/dev/survey/ember-survey/SKILL.md -H "Accept: application/vnd.github.raw+json" > /tmp/ember-survey-SKILL.md && mv /tmp/ember-survey-SKILL.md ~/.claude/skills/ember-survey/SKILL.md
```

Without the skill, the owner asks the session to take the Ember use survey
by running this command and following what it prints:

```bash
gh api repos/rearwindowvertigo/ember/contents/dev/survey/agent.md -H "Accept: application/vnd.github.raw+json"
```

Entries are the filling session's own assessment. The rule that committed
text states facts and does not judge does not apply to survey entries.

## Reviewing an entry

A review runs once for each open `survey` issue, when the owner asks for
it, in a session in this repository. Panel members cannot reach GitHub, so
the session gives them the issues and decisions as files outside the
checkout.

1. Save every `survey` issue's body, open and closed, to a scratch folder
   outside the checkout, one file per issue named `issue-<number>.md`:
   `gh issue list --label survey --state all --limit 1000 --json number,body`.
2. Write the decisions recorded on GitHub issues to a scratch file outside
   the checkout:
   `gh issue list --state all --limit 1000 --json number,title,comments --jq '.[] | {number, title, decisions: [.comments[].body | select(startswith("Decision"))]} | select(.decisions | length > 0)' > <decisions file>`.
3. Copy `review-prompt.md` to a scratch file. Replace `{{ENTRY}}` with the
   absolute path of the reviewed issue's file, `{{EARLIER}}` with the
   scratch folder's absolute path, and `{{DECISIONS}}` with the decisions
   file's absolute path.
4. Claim the run (`emb choices --claim --id <id> --skill <version>`), then
   run the question as an Ember panel on this checkout:
   `emb panel --question-file <scratch file> --dir <repository top> --read <scratch folder> --read <decisions file> --claim <id>`.
   The members are the saved `panel.members` unless the owner names others.
5. Compare the members' answers in the panel's `summary.md`, point by
   point, and post the review as one comment on the survey issue, starting
   `Survey review`, with:
   - the panel id and its members, with any member that failed;
   - one table of every point with the severity, tags and outcome most
     members gave, and each disagreement with each member's answer;
   - the tags, merged where members named the same overlap differently;
   - the proposed changes, each with the members that proposed it.
6. Put the proposed changes to the owner. File each one the owner accepts
   as its own issue, with the labels and milestone `AGENTS.md` describes
   and a line naming the survey issue. Then close the survey issue with a
   comment listing those issues, or saying none was accepted.

A proposed change in a review is not a decision. It becomes an issue only
when the owner accepts it.

## Scores

| Score | Meaning |
|---|---|
| 5 | Worked first time; nothing to change. |
| 4 | Worked, with minor friction that cost little time. |
| 3 | Worked after a retry, a workaround or an extra question. |
| 2 | Worked only after a substantial detour, or needed the owner to unblock it. |
| 1 | Did not work, or produced a wrong result. |
| not used | The session did not use this area. |

Each entry's scores are in its front matter. To compare them across
entries, read the `scores` of every `survey` issue:
`gh issue list --label survey --state all --limit 1000 --json number,title,body`.

## Index of earlier entries

The entries made before survey issues, in `entries/`. `n/u` means not
used. Ember is the entry's `ember_version`, the version at the session's
start.

| Date | Entry | Ember | Setup | Planning | Running | Review | Panels | Owner | App | Speed | Overall |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-11-02 | [0.2.1 build](entries/2026-11-02-0-2-1-build.md) | 0.2.0 | 4 | 4 | 3 | 4 | n/u | 3 | 2 | 4 | 3 |
| 2026-11-03 | [0.3.0 to 0.5.0 builds](entries/2026-11-03-0-3-0-to-0-5-0-builds.md) | 0.2.1 | 4 | 3 | 2 | 3 | 4 | 3 | 3 | 3 | 3 |
| 2026-11-03 | [0.6.0 to 0.12.0 builds](entries/2026-11-03-0-6-0-to-0-12-0-builds.md) | 0.5.0 | 4 | 3 | 3 | 4 | 3 | 3 | 3 | 3 | 3 |
