---
type: llm
focus: trace
weight: 1
---

Before the flow starts, `emb gapcheck` runs with `--engine codex --model gpt-6-astra --effort high`.

The JSON result is saved as gapcheck-<plan stem>-<n>.json. Claude records the low gap's default with `emb gapcheck --answer <result> --id g1 --text <default>` or `emb gapcheck --answer <result> --take-defaults g1` before starting, without asking the absent owner or editing JSON by hand. Any repeat passes that result with `--previous` to both the dry run and the gap check.
