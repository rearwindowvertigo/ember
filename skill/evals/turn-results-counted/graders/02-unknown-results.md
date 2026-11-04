---
type: llm
focus: trace
weight: 1
---

Claude reports that the older run has unknown exit and seconds, and that
the unfinished run's provider result and exit are unknown. It gives one
known provider error and one unknown provider result across four turns.
It does not turn null into zero, success, or evidence of no provider error.

Claude treats the older outcome's missing `provider_kind` and `attempts`
as unknown, without inventing a kind or a retry count.
