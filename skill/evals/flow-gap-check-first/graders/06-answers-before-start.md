---
type: llm
focus: trace
weight: 1
---

No `emb flow --start` occurs before all three scripted gap answers. Before the start, the plan records the first answer as exact item matching, the second as numeric revenue rounded to two places, and the third as zero totals for an unmatched item. An answer attached to the wrong gap fails this expectation.
