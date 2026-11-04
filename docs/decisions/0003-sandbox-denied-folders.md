# ADR 0003: Sandbox denied folders

- Status: Accepted, 2026-11-03.
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
(`hidden`, `secret location` or `protected`) and `allow_path` or null.

The offered folder is the deepest folder holding the denied path that
contains no secret or protected location. Given and resolved paths are
both checked. No folder inside `$XDG_CONFIG_HOME` is offered. Ember keeps
an explicit, tested inventory of credential locations, including native,
Flatpak and Snap browser profiles. Secret locations have no Allow reading
button. Other workers' worktrees and `$XDG_RUNTIME_DIR` are protected,
with no Allow reading button or `sandbox.read` suggestion.

Overview names the path and the run, flow or panel that reported it.
Allow reading offers only `allow_path` and requires confirmation that
every future worker can read the folder and none can change it. Confirming
adds it to `sandbox.read`. Dismiss hides the notice until a later run or
turn reports the path again; dismissals persist in the app's state file.

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
