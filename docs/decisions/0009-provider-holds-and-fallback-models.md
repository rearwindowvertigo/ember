# ADR 0009: Provider holds and fallback models

- Status: Accepted, 2026-11-21.
- Release: Ember 0.27.0.

## Context

Since 0.13.0 the model and effort of a run never changed after a provider
error (#103). A capacity error was retried on the same model and then
ended the run at exit 73. A usage limit or an outage ended it at once, and
a flow then halted new runs on that engine until the lead answered.

Ember kept no shared record of a provider's state. Each job learned that a
model or an engine failed by failing on it, and what it learned ended with
that run. A flow's provider halt was kept in that one flow and had no end
time.

On 2026-11-21 a flow's reviewer model was at capacity for 30 minutes
(#309). The owner changed the Team Reviewer row by hand and the lead
answered each stopped task again. Five starts met the same account-service
timeout within six minutes (#310), a second flow started a writer while
the first flow's starts were failing, and the lead started a probe run by
hand to learn whether the model was back.

The owner asked for fallback models set ahead of time, and for a return to
the first choice that is set by the owner and not managed by the lead
(#312).

## Decision

1. `provider.fallback` is one ordered list of `<engine>:<model>` entries
   for every job.
2. A provider error that ends a run at exit 73 starts a hold in one record
   for the machine. `capacity` holds the model. `usage_limit` and `outage`
   hold the engine.
3. `provider.hold_minutes` sets how long a hold lasts: 0, 5, 15, 30 or 60
   minutes, 15 by default. With 0 no hold is recorded and every job starts
   on the first choice.
4. A start on a held worker goes to the fallback entry that applies. With
   no such entry it starts on the held worker. Panel members start as
   named.
5. The entry that applies is the first one that is usable, not held and
   not a worker that failed on the job. An entry on the engine of a worker
   that failed with `usage_limit` or `outage` is passed over. A review
   does not take a model that wrote the work.
6. A capacity error is first retried on the same model within
   `provider.capacity_retries`. The fallback follows when the retries are
   used up.
7. In a flow and in a gap check the start is repeated on the fallback
   worker without a question. The task's saved worker does not change. A
   single `emb run` prints the fallback start as the `next:` line of its
   first turn, and the skill tells the lead to run it.
8. A writer that takes over continues on the failed run's branch.
9. A hold ends at its time, after a successful turn on the worker, or when
   the owner releases it. The next start after a hold uses the first
   choice. Ember starts no run of its own to test a model.
10. A fallback start names the run it replaces. The workers of that run and
    of the runs it replaced are not used again for the job, with or
    without holds.
11. A turn whose resend is uncertain is not repeated on another worker.

With an empty list the behaviour of #103 stays, and holds are still
recorded and shown.

## Options not chosen

- One list for each Team group or for each row.
- A fallback for `capacity` only, or to any entry after every kind.
- A fallback at the first capacity error, without Ember's own retries.
- A fallback writer that starts again from the base.
- A probe run that Ember starts by itself when a hold's time is up.
- No shared record, with every job trying the first choice.

## Consequences

- A hold passes over a worker that may have recovered before the hold's
  time. The owner sets the time and can release a hold.
- A correction or fix sent to a running writer is not moved to another
  worker. A provider error there stops the task as before.
- An outage is not retried on the same model.
- The review of a continued task covers work by more than one model.
- The rule of #103 that the model never changes holds only while the
  fallback list is empty.
