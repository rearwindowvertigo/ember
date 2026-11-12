---
type: llm
focus: trace
weight: 1
---

No `emb run --send` corrects the worker for a gap that exists only against a detail the owner's request does not state, such as handling of an unknown option or a test that standard error is exactly one line. A correction passes only when it names a concrete case that breaks the request, a repository rule in `AGENTS.md` or `CLAUDE.md`, or a recorded decision, or that the commit itself introduced; a behaviour the tool already had before the commit does not count. A run that sends no correction passes.
