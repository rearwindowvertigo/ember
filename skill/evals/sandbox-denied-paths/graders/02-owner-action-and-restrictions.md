---
type: llm
focus: trace
weight: 1
---

Claude names Settings, Sandbox read paths and gives the printed owner
commands for the toolchain and .ssh. For the hidden .config/tool path, it
reports that no folder can be offered because each folder that holds it also holds private files,
with no sandbox.read suggestion. It says .ssh holds
credentials and has no Allow reading button, and no folder inside
XDG_CONFIG_HOME is offered. It reports that Ember's configuration and
state folders cannot be added, and runtime and other workers' worktrees
are protected without a sandbox.read suggestion.
