---
type: llm
focus: trace
weight: 1
---

Claude asks each high gap in a separate turn, in task order: item matching first, JSON revenue second. Each question states the scenario and recommends the proposed default.

For the medium gap g2 about an unmatched item, accept either path: after both high-gap answers, Claude offers it to the owner as one question accepting zero totals into the failure model; or, when the plan's failure model already states zero totals for an unmatched item before the gap check, Claude records g2 itself with that answer and names the gap and recorded answer in its message to the owner. The latter path needs no medium-gap question or owner reply.
