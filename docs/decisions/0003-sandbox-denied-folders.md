# ADR 0003: Sandbox denied folders

- Status: Accepted, 2026-11-03. Amended by ADR 0006, 2026-11-08.
- Release: Ember 0.13.0.
- Issue: #106.

## Decision

The sandbox hides the home folder except its kept paths. Ember does not
expose the home folder read-only or keep built-in toolchain folder lists.
The owner grants additional read access through `sandbox.read`.

At launch, each run records the hidden paths in effect, including their
given and resolved names. Detection checks each attempt's output and
screen against that inventory for a denied-access diagnostic naming a
path. A turn keeps the union of denials across capacity attempts,
deduplicated by resolved hidden path, with one warning per path.
Each `denied_paths` entry keeps the matched hidden `path`, its `reason`
(`hidden`, `startup file`, `secret location` or `protected`) and
`allow_path` or null.

The offered folder is the deepest folder holding the denied path that
contains no secret or protected location. `allow_path` is built from the
path that matched the hidden entry, using the resolved target when a
symlink reaches a hidden path. It must contain the denied path after
resolving symlinks. Given and resolved paths are both checked. No folder
inside `$XDG_CONFIG_HOME` is offered. Ember keeps an explicit, tested
inventory of credential locations, including native,
Flatpak and Snap browser profiles. Secret locations have no Allow reading
button. Other workers' worktrees and `$XDG_RUNTIME_DIR` are protected,
with no Allow reading button or `sandbox.read` suggestion.

Overview groups notices by resolved folder. Each notice shows the report
count, distinct flows, panels and standalone runs, the first report date
and the latest report time in local time. Show runs starts collapsed and
lists one report per resolved folder, run and turn, newest first, naming
the kind, claim when present, flow or panel when present, run and turn.
Times are the turn outcome files' modification times; missing times show
as `time unknown` and sort last. Expansion is kept across refreshes.
Before read-path coverage checks and merging, a recorded `allow_path` must
equal, contain or lie inside the recorded hidden path after resolving
symlinks; invalid offers are treated as no offer.
Allow reading offers only `allow_path` and requires confirmation that
every future worker can read the folder and none can change it. Confirming
adds it to `sandbox.read`. Dismiss hides the notice until a later run or
turn reports the path again. Don't show again hides notices for the
resolved folder for every later report until Show again in Settings,
Hidden notices. Show again removes both the permanent hiding and any
Dismiss entry for that folder. Dismissals and hidden folders persist in
the app's state file. Run status, flow status and `WARNING:` lines still
report denials; the sidebar count counts only live runs, flows and panels.

The owner can add secret and configuration folders by hand in Settings,
Sandbox read paths, except Ember's configuration and state folders.
Settings and `emb choices` refuse those folders, their descendants and
symlink aliases with the reason. Permission changes apply to newly
started runs; existing runs retain their launch paths.

Flow task status and events retain denied paths with their run and turn.
Panel member diagnostics and status retain denied paths across attempts;
panels have no denial event stream.

## Not covered

- A tool that fails without printing the denied path is not detected.
- A diagnostic that names the denied path relative to its working directory
  is not detected.
