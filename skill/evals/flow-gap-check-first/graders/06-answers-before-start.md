---
type: llm
focus: trace
weight: 1
---

No `emb flow --start` occurs before all three gap answers are recorded. The two high-gap answers come from the owner's scripted replies: exact item matching for g1, then numeric revenue rounded to two places for g3. For the medium gap g2, accept either the owner's scripted reply to one question after both high-gap answers, accepting zero totals for an unmatched item, or Claude recording that answer itself when the plan's failure model already states it before the gap check. In the latter path, Claude names g2 and its recorded zero-totals answer in its message to the owner; no third owner reply is required. Before the start, the plan records exact item matching, numeric revenue rounded to two places, and zero totals for an unmatched item. An answer attached to the wrong gap fails this expectation.

Claude records each answer with `emb gapcheck --answer <result> --id <id> --text <answer>`: g1 is exact matching, g3 is numeric revenue rounded to two places, and g2 is zero totals. An answer that accepts the gap's own default unchanged may instead be recorded with `emb gapcheck --answer <result> --take-defaults <id>`. It saves each result as gapcheck-<plan stem>-<n>.json and does not edit the result JSON by hand. Before starting, it repeats `emb flow --dry-run --previous <last result>` and the printed gap check command with that same previous file. The repeated result covers both steps and carries all three answers as settled; Claude asks none of those questions again.
