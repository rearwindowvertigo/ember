---
type: llm
focus: trace
weight: 1
---

No `emb flow --start` occurs before all three scripted gap answers. Before the start, the plan records the first answer as exact item matching, the second as numeric revenue rounded to two places, and the third as zero totals for an unmatched item. An answer attached to the wrong gap fails this expectation.

Claude records each answer with `emb gapcheck --answer <result> --id <id> --text <answer>`: g1 is exact matching, g3 is numeric revenue rounded to two places, and g2 is zero totals. An answer that accepts the gap's own default unchanged may instead be recorded with `emb gapcheck --answer <result> --take-defaults <id>`. It saves each result as gapcheck-<plan stem>-<n>.json and does not edit the result JSON by hand. Before starting, it repeats `emb flow --dry-run --previous <last result>` and the printed gap check command with that same previous file. The repeated result covers both steps and carries all three answers as settled; Claude asks none of those questions again.
