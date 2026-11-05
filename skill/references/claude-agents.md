# Claude agents

## Subagents

Use Explore only for search fan-out that returns locations. Use
general-purpose for other tasks. Explore has no Write or Edit, refuses
file creation through Bash, and receives neither `CLAUDE.md` nor the git
status snapshot. Explore and Plan return no agentId, so neither can be
corrected or resumed.

Do not set a model on spawn unless the owner named one or a `set` Claude
row with run-as `subagent` supplies it; otherwise use Claude Code's
default.

To narrow tools or turns, use an existing agent definition in
`~/.claude/agents/`; otherwise the agent inherits this session's tools and
permissions.

Subagents have no AskUserQuestion and no Workflow tool. They receive
`CLAUDE.md` at every level and a git status snapshot (not Explore or
Plan), but not the owner's output style or memory. Put in the prompt only
the repository rules the task needs.

For editing, use `isolation: "worktree"`, tell the agent to commit with
the supplied trailer, and have it return branch, worktree and tip. Its
worktree is `.claude/worktrees/agent-<agentId>` on the branch
`worktree-agent-<agentId>`, based on `worktree.baseRef` (`head` or
`fresh`; see the skill's editing boundary). Do not isolate a read-only
agent. Read-only output belongs in the final message, with a length cap
set in the prompt; other results go to a session-scratchpad file whose
path the agent returns.

Correct a running or finished agent with SendMessage addressed by its
agentId. Respawn Explore or Plan, which have no agentId, and respawn when
the original prompt itself was wrong. After an API error, retry once it
clears; treat earlier output as partial.

Progress is in
`~/.claude/projects/<slug>/<session>/subagents/agent-<agentId>.jsonl`.
For Explore or Plan, read the newest `agent-*.jsonl` made after the spawn.

The runtime allows 20 concurrent subagents and three levels of nesting.

## Claude Workflow

A Workflow needs the opt-in SKILL.md (Role) describes. It is one run and one
claim, whatever its agent count.

Do not set a model or effort on `agent()`: agents use this session's.
Give each editing agent `isolation: 'worktree'` and a schema that returns
branch, worktree and tip; return those from the script too. Do not
isolate a read-only agent only for reading.

Read results from the Workflow tool's result, or single returns from the
run's `journal.jsonl`; the background task's output file is not the
result. For a large task, run a small slice first.

Run at most the owner's Max agents at once (`workflow.agents` in
`emb choices --json` answers: 4, 8 or 16) at the same time. The runtime
allows 16 concurrent agents, 4,096 items per call and 1,000 agents per run. Scripts cannot read files or `import()`; `Date.now()` and
`Math.random()` throw, so the run can be replayed. A stopped run relaunches
with cached results up to the first agent that changed or failed; that
agent and every later one run again.

Verify, review and merge every editing result as the skill says, one
branch at a time.

## Deadlines

These apply to subagents and Claude Workflows; `emb run` has its own stall
detection.

Use the prompt's implied runtime plus half again, or ten minutes when none
is implied. Start one background Bash `sleep <seconds>` and keep its task
id beside the worker.

When it ends, check the subagent's transcript: a growing transcript means
wait; a stopped one without a result means stalled. For a Workflow agent,
follow the owner's When an agent runs late (`workflow.late`): `ask` asks
the owner whether to wait or stop; `wait` starts one more timer of the same
length and asks the owner when it ends; `stop` stops the agent. When the
check says wait for a subagent, start one more timer of the same length,
and ask the owner when it ends. Stop a
stalled agent before respawning it narrower or doing the work yourself.

As soon as the worker reports, stop its timer with TaskStop by that id,
before reading the result. If a timer reports after the worker finished,
do nothing and say nothing.
