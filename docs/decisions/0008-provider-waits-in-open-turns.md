# ADR 0008: Provider waits in open turns

- Status: Accepted, 2026-11-21.
- Release: Ember 0.27.0.

## Context

Ember read a turn's state from two signals: the engine's record of the
turn, and whether the session's screen changed. A provider failure was
classified only when it ended the turn and was written to that record
(#103). Any screen change counted as progress.

Codex 0.171.0 retries a request the provider refused or dropped inside the
open turn. It shows the retry on its status line and writes it to its log,
not to the rollout, and the turn stays open. Its retry count starts again
after each resend that connects. The status line holds a timer that changes
every second.

In the turn recorded in #309 the provider refused five requests for lack of
capacity in 30 minutes. Ember showed the run as running with no provider
kind, printed no warning, applied neither its capacity retry nor exit 73,
and did not stall.

The Claude and OpenCode drivers also classify a provider error only from an
ended turn. Flows, panels and gap checks wait for turns through the same
loop.

## Decision

1. A driver can report that an open turn waits for the provider, with a
   provider kind and the engine's message. The core applies one set of
   rules to such a wait for every engine and every command that waits for
   a turn.
2. A wait is saved in the turn outcome and printed as a `WARNING:` line.
   `emb run --status`, `emb flow --status` and the Overview show it.
3. A `capacity` wait ends the turn at once. The core interrupts the turn
   and applies the capacity rule of #103: a resend after
   `provider.capacity_wait` seconds, up to `provider.capacity_retries`
   times, then exit 73.
4. Any other wait ends the turn once it has lasted the stall time without
   a break, at exit 73 with the wait's kind.
5. A driver gives the core the screen text without the timer of its wait.
   The stall count and `last_screen_change_at` compare that text.
6. The Codex driver reads the wait from its `Reconnecting...` status line
   on the screen the wait loop already captures.

## Options not chosen

- Codex keeps its own retry schedule for capacity, and Ember ends the turn
  at notice number `provider.capacity_retries` plus one. The capacity rule
  would then differ by where the error appears, and
  `provider.capacity_wait` would not apply.
- Codex's log, `logs_2.sqlite`, as the source. It gives the error text and
  the length of Codex's wait by thread id. It was 68 MB with an 18 MB
  write-ahead log on 2026-11-21, above the 64 MB limit of the driver's
  database reader.
- `stream_max_retries` set for Codex's built-in provider. The Codex
  0.171.0 binary holds the text "Built-in providers cannot be overridden".
- Showing the wait and leaving the turn open.

## Consequences

- A change of the wording or place of Codex's status line stops the
  detection. Ember then treats the turn as it did before this decision.
- A resend after a capacity wait pastes the turn's prompt again into the
  same session, as a resend after a capacity error does. Edits the
  interrupted attempt made stay in the worktree, and the turn commits once
  after its last attempt.
- The saved wait is read from a file. When the waiting command is killed,
  the wait is shown until the next command that waits for the turn.
- The Claude and OpenCode drivers report no wait. A wait inside an open
  turn of those engines is not detected.
- A model that makes no progress under a moving work timer is not
  detected on any engine.
- A start that cannot reach the provider is not covered (#310).
