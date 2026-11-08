# ADR 0002: Worktrees outside every repository

- Status: Accepted, 2026-11-04.
- Release: Ember 0.13.0.
- Issue: #93.

## Context

Tools such as `uv`, `ruff` and `pytest` search the parent folders of their
working folder for a project file such as `pyproject.toml`. From a worktree
inside a repository's checkout, that search reaches the checkout's own files.
In a sandboxed run, the sandbox denies those files and the tool exits with
`Permission denied`; without a sandbox, the tool reads the checkout's files in
place of the worktree's.

A worktree's path was computed in four places, and prune found a main
checkout from the shape of a worktree path.

## Decision

This decision covers the worktrees Ember creates for runs and flows.

1. Every run and flow worktree Ember creates is at `worktrees/<id>` in its data
   folder, `$XDG_DATA_HOME/ember`, with `~/.local/share` as the default base.
   One function, `ember.paths.worktree_path`, gives the path from the id and
   the environment, with symlinks resolved. The `worktrees` folder and its
   Ember parent are mode 700. A symlinked Ember data folder or worktrees
   folder is accepted when its resolved target is a folder owned by the
   current user with mode 700. Symlinks are resolved before the checks.
2. Before it creates a worktree, Ember refuses a worktree folder inside any
   Git working tree, whichever repository it belongs to, with exit 69. It
   also refuses a folder that is a checkout of the source repository, as
   `git worktree list` lists them, or its common git folder, or lies inside
   one. The message names the
   folder, the repository folder and the environment variable that placed it
   there (`XDG_DATA_HOME`, or `HOME` when that is unset).
3. A run record stores the worktree's resolved path, the main checkout and
   the resolved common git folder at start. A flow record stores the same
   three paths. The flow record and the
   flow branch are made with the same path, resolved once.
4. Merge, cleanup and prune take a worktree from its run or flow record and
   never from the shape of a path. A branch with no record is matched through
   `git worktree list` of its repository: the worktree holding the branch in
   a folder named by its id, other than the main checkout.
   A recorded common git folder identifies the repository even when the
   source checkout has been removed or replaced. Records without the new
   fields use their source checkout and worktree as before.
5. A sandboxed run is refused with exit 69 when any kept path (the worktree,
   the common git folder, a `sandbox.read` or `--read` path, or another path
   the sandbox keeps) is one of Ember's configuration or state folders or
   lies inside one, given or with symlinks resolved. The message names the
   kept path and the folder. Ember walks its worktrees folder and denies
   every entry except the worker's own worktree by name, in both sandboxes,
   including through symlinked read paths. A kept path inside that folder
   outside the worker's own worktree is refused the same way. The parent
   worktrees folder is never denied around a kept worktree. The folder is
   created before every sandbox starts, including a read-only worker's.
6. Ember does not add `.ember/` to `.git/info/exclude`.
7. Worktrees at `.ember/worktrees/<id>` in a main checkout, from records made
   before this decision, are removed by merge, cleanup and prune from the
   paths their records name, and by cleanup through `git worktree list` when
   they have no record.
8. The skill eval runner gives each case a private empty folder for every
   XDG base (configuration, state, data, cache and runtime) and blanks the
   owner's Ember folder in each base.
9. A sandboxed worker has the workers' cache folder writable outside its
   worktree and the engine's own folders:
   `$XDG_CACHE_HOME/ember/worker` (default `~/.cache/ember/worker`), mode 700.
   It lies outside Ember's configuration and state folders, is a kept path
   checked as in item 5, is created when a sandboxed run starts (exit 69 when
   it cannot be), and is writable in the Codex profile and in `bwrap`, for
   read-only runs too. It is shared by every sandboxed run and kept between
   runs. Every sandboxed Codex run, editing or read-only, also gets a
   temporary folder at `/tmp/ember-<6 hex>`, using the six-hex suffix of the
   run id. It has mode 700, is a kept path checked as in item 5, and is
   created before the session starts. Start exits 69 naming the folder if
   any entry already exists there, including a symlink, or creation fails.
   The Codex profile makes it writable and sets `TMPDIR` to it through
   `shell_environment_policy.set`. The shared `/tmp` stays writable.
   Codex walks `/tmp` and denies every other entry matching `ember-<6 hex>`
   by name. Ember removes the folder when the worktree is removed or the run
   is closed, and when prune removes the run record. An unscoped prune sweeps
   matching temporary folders no record names only when owned by the current
   user, not symlinks, and every record could be read. It also sweeps
   unrecorded entries in `$XDG_CACHE_HOME/ember/tmp` from 0.20.0 under the
   same record-readability rule. Removal never follows symlinks out of the
   folder. With `sandbox.codex` off, Ember creates no temporary folder and
   sets no `TMPDIR`. Bubblewrap keeps its private empty `/tmp` and gets no run
   temporary folder.
10. Every sandboxed worker gets `XDG_CACHE_HOME` set to the workers' cache
    folder, and the variables below set to folders inside it, replacing any value the owner
    set. Codex receives them as `shell_environment_policy.set` options;
    `bwrap` receives them as `--setenv`. OpenCode and Claude Code run inside
    `bwrap`, so their own caches move into the folder too, and Ember no
    longer keeps their caches in `$XDG_CACHE_HOME` writable.

    | Variable | Folder | Tool |
    |---|---|---|
    | `UV_CACHE_DIR` | `uv` | uv, when the owner sets the variable |
    | `UV_PYTHON_INSTALL_DIR` | `uv-python` | uv's Python installations; the sandbox hides `~/.local/share/uv/python`, and uv exits 2 when it cannot read it |
    | `PIP_CACHE_DIR` | `pip` | pip, when the owner sets the variable |
    | `npm_config_cache` | `npm` | npm, which uses `~/.npm` otherwise |
    | `GOMODCACHE` | `go/mod` | Go modules, which use `~/go/pkg/mod` otherwise |

    Ember does not rewrite `CARGO_HOME` or `GRADLE_USER_HOME`: these tool
    homes hold configuration and credentials as well as caches.
    `UV_PYTHON_INSTALL_DIR` is included because uv downloads Python there
    on demand.

11. A run's tmux session runs `launch.sh` in the run folder, mode 600, which
    holds the engine's command. The deny list names each entry of the home
    folder's walked folders, so its length grows with the home folder. With
    the workers' cache folder kept, it also names each entry of
    `$XDG_CACHE_HOME` and of Ember's cache folder, and run
    `codex-20261104-055515-481e1f` exited 71 with `tmux set-option failed:
    command too long`.
12. Prune removes a worktree named by a removed record when no open run uses
    it, it is inside Ember's worktrees folder, and its regular, non-symlink
    `.git` file is valid and names a git folder that no longer exists.
    An unscoped prune also sweeps folders there that no record names,
    including flow task paths, only when every record could be read. A
    folder's age is its modification time. A scoped prune never runs this
    sweep. A dry run lists the same candidates. Other folders are kept and
    reported with the reason; folders outside Ember's worktrees folder are
    never removed as orphans.

13. Run worktree removal uses `ember.runs.core.worktree_removal_reason` under
    the run command and record locks after reloading the record. Closed and
    failed runs qualify only when their session has ended, no turn is sent,
    and no writing turn is collected but uncommitted. A sent turn includes
    uncertain retry delivery. Close, panel close, cleanup and prune keep a
    refused worktree and report the reason. Forced close ends the session
    without the command lock, then attempts removal under both run locks.
    A busy command lock keeps the worktree with a warning.
14. Every Git worktree add, list, remove and prune holds
    `ember.paths.worktree_lock`, keyed by the resolved common git folder.
    The lock file is
    `$XDG_RUNTIME_DIR/ember/locks/worktrees-<sha256>.lock`. Flow or panel
    locks precede run command locks, then run record locks, then the repository
    worktree lock. No caller acquires a run, flow or panel lock while holding
    the repository worktree lock. The repository lock is not reentrant.

## Consequences

On 0.12.0, run `codex-20261104-052529-8b299b` showed `uv run --frozen ruff
--version` exiting 2 with `Failed to initialize cache at ~/.cache/uv` in a
sandboxed worktree. With items 9 to 11, the same command exits 0 and prints
`ruff 0.17.3` in a sandboxed Codex run (`codex-20261104-055627-b1a2fa`) and
a sandboxed Claude run under `bwrap` (`claude-20261104-055703-6eef77`), with
no cache variable set by the lead.

A worker on a run without network starts with an empty workers' cache folder
the first time, so a tool that must download a package fails until a run
with network has filled the folder. A tool whose cache is not in the table
and does not follow `XDG_CACHE_HOME` still finds its home folder hidden.
uv's hard links from the cache into a worktree fall back to copies, because
the two are separate mounts in the sandbox.

A tool that searches parent folders from a worktree passes through the data
folder and the home folder. A file the sandbox denies there, such as
`~/pyproject.toml`, still returns `Permission denied` in place of "not found".

Prune uses a run's or flow's recorded main checkout, or its recorded common git
folder when the main checkout is gone. Older run records use `repo`, then
the checkouts their parent flow or panel records name, then
`git worktree list` run in their worktree.

## Not covered

Claude Code's own subagent and Workflow worktrees are outside this decision's
scope.
