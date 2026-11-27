# Ember use survey: instructions for the session filling it in

You are a lead agent that used Ember in some repository, and the owner
asked you to take the Ember use survey. These instructions and the survey
template are the only files you read from the Ember repository
(`rearwindowvertigo/ember`). Do not clone it, browse it or read any other
file in it.

## 1. Get the template

Print the survey template from `main`:

```bash
gh api repos/rearwindowvertigo/ember/contents/dev/survey/survey.md -H "Accept: application/vnd.github.raw+json"
```

Fill in a copy of it in a temporary file outside every repository, for
example one made by `mktemp --suffix=.md`.

## 2. Fill the front matter

- `date`: today, `yyyy-mm-dd`.
- `ember_version`: the output of `emb --version` at the session's start,
  one `x.y.z` version. If the session started on an older version than
  `emb --version` prints now, give the one it started on.
- `ember_versions_built`: `[]`, unless the session made an Ember release.
- `main_commit`: the short commit of Ember's `main` now:

  ```bash
  gh api repos/rearwindowvertigo/ember/commits/main --jq '.sha[0:7]'
  ```

- `session_model`: your model id.
- `task`: one line naming the session's work, in general terms.
- `engines` and `workers`: as the template says.
- `scores`: the scale is below.

## 3. Answer

- Answer from this session only: its `emb` commands, exit codes, `next:`,
  `owner:` and `WARNING:` lines, worker reports, owner questions and time
  spent. Cite them.
- Answer every question. An area the session did not use gets `not used`
  as its score and the line `Not used in this session.` in place of its
  answers. A question with nothing to report gets `none`.
- The entry is your own assessment of Ember. Judge freely.
- The entry goes to the Ember repository, not the repository you worked
  in. Describe that repository's work only as far as the survey needs it.
  Include no code from it, no file contents, no client, customer or person
  names, no internal URLs, and no credentials, tokens or personal data.

| Score | Meaning |
|---|---|
| 5 | Worked first time; nothing to change. |
| 4 | Worked, with minor friction that cost little time. |
| 3 | Worked after a retry, a workaround or an extra question. |
| 2 | Worked only after a substantial detour, or needed the owner to unblock it. |
| 1 | Did not work, or produced a wrong result. |
| not used | The session did not use this area. |

## 4. File it

File the filled survey as one issue in the Ember repository, with the
`survey` label. The title is `Survey: <date> <task>`.

```bash
gh issue create --repo rearwindowvertigo/ember --label survey --title 'Survey: <date> <task>' --body-file <filled file>
```

The issue is the survey entry. File one issue for the whole survey, not
one per finding: a panel reviews the entry, and the owner decides which of
its proposed changes become issues. Leave the issue open.

Report the issue's URL to the owner, then delete the temporary file. Do not
open a pull request or change any file in the Ember repository.
