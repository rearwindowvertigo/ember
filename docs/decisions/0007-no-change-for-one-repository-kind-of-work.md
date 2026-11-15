# ADR 0007: No change for one repository's kind of work

- Status: Accepted, 2026-11-15.
- Release: Ember 0.26.1.

## Context

Ember runs in any repository. Reports of friction come from sessions that
each worked in one repository, among them the issues labelled `survey`.

The entry in issue #299 came from a session whose work was layout changes
in a web app. Its review proposed nine changes. Five followed from that
kind of work: a rule to show a rendering with an owner question about a
visual choice, widths and states named in a brief, a flow step that
renders a page and stores images, a recipe for a checkout to render in,
and a lighter check for a wave. The owner declined all five. The two
accepted changes, #300 and #301, concern text in Ember's skill.

No document stated the test the owner applied.

## Decision

1. Ember and its skill are not changed to fit one repository's kind of
   work.
2. A proposed change is judged by the kind of fault its evidence shows.
   A fault in Ember's own behaviour, output or text, which would show in
   any repository, can support a change. A fault that arises from the kind
   of work a repository holds, such as visual layout, a renderer, a long
   test suite or one toolchain, does not.
3. One report is enough evidence for a fault in Ember's own behaviour,
   output or text.
4. The number of reports does not change the kind of a fault.

## Consequences

- A review of a `survey` issue gives a point that arises from the
  repository's kind of work the outcome `repository`. The point stays in
  the review's table and is not a proposed change.
- A proposal made outside a survey meets the same test.
- A session handles its repository's kind of work with its own checks and
  briefs, which the skill already asks it to derive from the owner's
  request.

## Not covered

- Which kinds of work are common enough to be Ember's own concern. The
  owner decides a case that the test does not settle.
- Ember's own desktop app, which is part of this repository and is changed
  as its issues say.
