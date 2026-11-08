# ADR 0006: Worker XDG folders and lasting denial answers

- Status: Accepted, 2026-11-08.
- Release: Ember 0.21.0.
- Amends: ADR 0003.

## Context

A Codex worker's commands keep the owner's `HOME` and XDG base variables.
The sandbox hides the folders those variables name, and Ember redirects only
caches. Google Chrome 157's crash reporter keeps its database in
`$XDG_CONFIG_HOME/google-chrome` even when Chrome is given its own profile
folder. Under the Codex profile that folder is denied, and Chrome exits
with `chrome_crashpad_handler: --database is required`. Ember's bubblewrap
masks a denied folder with an empty writable one, so OpenCode and Claude
workers do not fail this way when the folder exists.

The `google-chrome` wrapper script touches `mimeapps.list` in the data
folder. Ember recorded that denial of `~/.local/share/applications`, which
does not stop Chrome, and could not record the crash reporter's, which
prints no path.

Since issue #183, every denial with an `owner:` line holds a run started
by `emb run` until the owner answers, also when the turn finished and did
not need the path. Flow and panel runs never hold. The owner's answer
lasted only for that run unless the owner added the folder to
`sandbox.read`. Overview's Don't show again was kept in the app's state
file, which neither `emb` nor the lead reads.

Detection matched denial diagnostics anywhere in a worker's output. A
worker that read a file quoting a denial had that denial recorded as its
own.

## Decision

1. The commands a sandboxed Codex run executes get `XDG_CONFIG_HOME`,
   `XDG_DATA_HOME` and `XDG_STATE_HOME` set to `config`, `data` and `state`
   in the run's temporary folder, and `XDG_DATA_DIRS` without entries in
   the home folder. `HOME` and the Codex process's own environment are
   unchanged. Each kept path inside the owner's matching base gets a
   symlink at the same relative place, so git's configuration and the
   owner's read paths stay reachable through the new bases. The folders
   are created before each launch and go with the temporary folder.
2. `sandbox.hidden_notices` is a standing setting: the resolved folders
   whose denials the owner has decided need no answer. Overview's Don't
   show again adds a folder and Settings, Hidden notices' Show again removes
   it. A denial inside one of these folders keeps its `WARNING:` line and
   `denied_paths` entry, prints no `owner:` line and records no owner wait.
   Adding a folder ends a pending wait for each denial whose resolved path
   lies inside it, as adding the offered folder to `sandbox.read` does,
   including a denial with no offered folder. Folders are compared after
   resolving symlinks, in `emb` and in Overview. A folder with no decision
   still holds the run the first time, as #183 decided.
3. A denial diagnostic that Ember gave the worker is not a denial. Lines
   matching the denial patterns and naming an absolute or `~/` path are
   collected from the run's brief, its turn messages, the files under its
   `--read` paths and its worktree's tracked files. An output line that
   contains one of them, after collapsing whitespace and ignoring case, is
   skipped.

ADR 0003's rules on hiding the home folder, offers, secret and protected
locations and the absence of built-in folder lists are unchanged. Its
statement that hidden folders persist in the app's state file is replaced
by point 2.

## Consequences

- Chrome and other tools that create per-user configuration, data or
  state start under the Codex profile without a read grant. What they
  write there lasts for the run only.
- A tool that reads the owner's configuration through an XDG base finds
  only kept and granted paths, as before.
- The owner answers a folder once, in Overview or Settings, and later runs
  that report it do not stop for an answer.
- A real denial whose diagnostic matches a line in the brief, a turn
  message, a read file or a tracked file is not recorded.

## Not covered

- Ember's bubblewrap still gives OpenCode and Claude workers the owner's
  XDG variables. A tool that must create a folder that does not exist in
  the read-only home fails there with `Read-only file system`, which
  detection does not match.
- `XDG_RUNTIME_DIR`, `DBUS_SESSION_BUS_ADDRESS` and `SSH_AUTH_SOCK` still
  name the hidden runtime folder, so their `protected` denials continue.
- A tool that fails without printing the denied path is still not detected.
