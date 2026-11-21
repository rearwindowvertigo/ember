---
type: llm
focus: trace
weight: 1
---

After the first `emb run` start exits 73, Claude closes the failed run with
`emb run --close` and starts a new run with `--model gpt-6.1-sol-mini` and
`--fallback-from` naming the failed run, as the printed `next:` line says.
It does both without first asking the owner which worker to use or
whether to go on.
