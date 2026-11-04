---
name: ember-survey
description: Take the Ember use survey for this session and file it as an issue in the Ember repository. Run only when the owner types /ember-survey.
disable-model-invocation: true
---

Print the survey instructions from the Ember repository's `main` and follow
them:

```bash
gh api repos/rearwindowvertigo/ember/contents/dev/survey/agent.md -H "Accept: application/vnd.github.raw+json"
```

Read no other file from that repository unless those instructions name it.
