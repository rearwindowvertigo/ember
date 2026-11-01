# Design 0001: Ember's first release

- Status: Accepted, 2026-11-01
- Target release: 0.1.0
- Created: 2026-11-01
- Decision record: ADR 0001 (Accepted)

Sections F (flows), G (panels) and H2 (gap check) target 0.2.0, except
the review bar (F4), which single runs use in 0.1.0. Every other section
targets 0.1.0. Items inside other sections that are marked 0.2.0 target
0.2.0.

## What Ember does

Claude is the lead. Given a coding task, Claude splits it, picks a worker for
each part, writes each worker a brief, reviews what comes back and merges
what passes. Workers are:

- Codex, through the Codex CLI on the owner's ChatGPT seat;
- OpenCode, through `opencode serve` with OpenRouter models;
- Claude, through the Claude Code CLI;
- Claude subagents and Claude workflows, inside Claude Code itself.

Each editing worker works in its own git worktree and branch, in a tmux
session the owner can watch in a terminal window. Read-only workers review or research without
editing. A flow runs a plan of several tasks with a writer and a read-only
reviewer per task and fix rounds between them. A panel asks several models
the same question and collects their answers.

The owner sees and configures all of this in the desktop app.

## Owner decisions (2026-11-01)

| | Question | Answer |
|---|---|---|
| E1 | Machines | One machine. No remote or phone view. |
| E2 | App toolkit and language | A native app in Python with PySide6 (Qt). |
| E3 | What the app does | Shows work, engines, dependencies and settings; runs commands after a confirm step; sends desktop notifications; has a tray icon. It does not start runs. |
| E4 | Other installed AI tools | Ember reads the state and settings of Claude Code, Codex and OpenCode, and whether Claude Desktop is installed and its version. It does not read Claude Desktop's configuration. It does not drive these tools or write their configuration. |
| E5 | Name and command | Ember; terminal command `emb`; Python package `ember-app`. |
| E6 | Repository | `~/Repos/ember`, remote `github.com/rearwindowvertigo/ember`, private. |
| E7 | Start at login | A Settings switch, on by default, starting into the tray. |
| E8 | Run changes | Found by file watching (inotify) with a slow backstop check. |
| E9 | Sandbox | Optional. Default bubblewrap when installed, none when not. Codex: system bubblewrap, Codex's bundled bubblewrap, or none. OpenCode: bubblewrap or none. Clear controls and as much feedback as each method allows. |
| E10 | Packaging | `.deb` first. |
| E11 | MCP server | None. Claude uses `emb choices`. |
| E12 | Seat usage | Not read or shown. |
| E13 | Scope of 0.1.0 | Settings, engines, single runs, the sandbox, supporting commands, dependencies, the app, the skill and the `.deb`. Flows, panels and the gap check are in 0.2.0. |
| E14 | Licence | MIT. |
| E15 | Worker windows | Each worker runs in a tmux session. The owner picks the terminal that shows it. kitty is not required. |
| E16 | Continuous integration | GitHub Actions on pull requests only. Docs-only pull requests skip the check. A newer push cancels the older run. Release Please runs on `main`. |
| E17 | Python | 3.14 or later. |
| E18 | Unit tests | Stub tools only (N1). |

## A. Repository and tooling

A1. `src/` layout with one package, `ember`. `pyproject.toml` with the
hatchling build backend. Python 3.14 or later. The MIT licence is in
`LICENSE`.

A2. uv manages the environment and lock file. ruff lints and formats.
Pyrefly checks types on `src/` and `tests/`. pytest runs the tests on all
cores with pytest-xdist (`-n auto`), with pytest-qt for the app.

A3. The `Makefile` target `make check` runs lint, format check, type check
and every test. It needs no network after the environment is built.
pre-commit runs ruff lint and format on the changed files at each commit.

A4. GitHub Actions runs `make check` on each pull request to `main`. A pull
request that changes only Markdown files under `docs/` and top-level
Markdown files skips it. A newer push to the same pull request cancels the
older run. No check runs on pushes to `main`.

A5. Releases follow `docs/releasing.md`: Release Please builds the version,
`CHANGELOG.md` and the `v<version>` tag from Conventional Commits, as a
GitHub Action.

## B. Paths

B1. Ember follows the XDG base directories:

| What | Where |
|---|---|
| Settings database | `$XDG_CONFIG_HOME/ember/settings.db` (default `~/.config/ember/`) |
| Run and flow records | `$XDG_STATE_HOME/ember/runs/` (default `~/.local/state/ember/`) |
| Caches (model lists, version reads) | `$XDG_CACHE_HOME/ember/` |
| Runtime files (locks, sockets) | `$XDG_RUNTIME_DIR/ember/` |

B2. The settings database and run records are mode 600 in mode 700 folders,
and every sandboxed worker is denied them.

## C. Settings and choices

C1. Standing settings: the worker menu (which engine, model and effort writes
single tasks, which reviews; in 0.2.0 also how multi-task work runs and
panel members),
per-engine on or off, model shortlists with efforts, worker network access,
sandbox method per engine, the terminal that shows worker sessions, start at
login and notification switches.

C2. One-time choices: the owner sets an answer for the next run only, at the
terminal (`emb choices --once`). A run claims it with a unique id
(`emb choices --claim --id <id>`) and releases the claim with `--finish`.

C3. `emb choices` reads settings and resolves each picker key in this order:
the owner's request, a claimed one-time choice, the standing setting, then
`ask`. Claude never writes settings; only the owner does, in the app or at
the terminal.

C4. Writes check a generation number, so a save from a stale screen is
refused and the screen reloads.

## D. Engines

D1. One adapter per engine (`codex`, `opencode`, `claude`) reports:
installed, version, signed in, models, and whether it is connected (every
requirement met). A requirement that is missing names its fix command.

D2. Sign-in state is read when the app starts, when the Engines screen opens,
on Check now, and after a run fails with a sign-in error. It is not read on a
timer.

D3. Model lists: Codex and Claude models from their CLIs; OpenRouter models
from OpenRouter's model list, with filters, cached.

## E. Single worker runs

E1. `emb run --engine <codex|opencode|claude> --dir <repo> --task-file
<brief>` starts one worker. Its first output line is `run <id>`.

E2. An editing run gets its own worktree and branch, `ember/<engine>/<id>`,
cut from the current branch, and commits with the given trailer. A
`--read-only` run edits nothing.

E3. The run runs in its own tmux session, on a tmux server private to Ember
(socket in `$XDG_RUNTIME_DIR/ember/`). Ember opens a window of the owner's
terminal attached to that session. The setting `terminal` holds the command
that opens it; unset means `xdg-terminal-exec`. Closing the window leaves
the run going. Commands: `--send` (a correction into the same session,
through tmux `send-keys`), `--screen` (tmux `capture-pane`), `--interrupt`,
`--attach` (open a new window on the session), `--close`, `--recover`,
`--status <id> --json`. Each non-zero exit prints a `next:` or `owner:` line.

E4. Workers never push, merge, rebase, delete branches or touch `main`.

## F. Flows

Target release: 0.2.0.

F1. `emb flow --plan <file>` runs a JSON plan of tasks in waves by their
`after` dependencies. Each task has a writer in an editing worktree and a
read-only reviewer, up to two fix rounds, and a declared list of files.

F2. Accepted tasks merge into the flow's run branch, `ember/flow/<id>`. The
plan's check command runs after each wave's merges.

F3. A task that fails, stays unresolved or conflicts stops at a lead stop.
Claude reports it to the owner with every available choice: send a
correction, accept a reviewed commit, start a fresh review, use another
worker, or drop the task.

F4. Reviews use one written review bar with three severities: must, should
and lead.

F5. `--dry-run`, `--start` in the background, `--watch --json` and
`--status`.

## G. Panels

Target release: 0.2.0.

G1. `emb panel` sends one prompt to the configured members (OpenRouter,
Codex and Claude models) and collects each answer and a summary file.

## H. Supporting commands

H1. `emb ground --json`: the machine, the Claude surface, the repository,
branch, dirty state and remote, read before a worker is chosen.

H2. `emb gapcheck`: a read-only worker reads a flow plan against its failure
model before the flow starts and reports gaps. Target release: 0.2.0.

H3. `emb merge <branch> --into <branch> --verdict keep`: merges a reviewed
worker branch, then removes its worktree and branch.

H4. `emb cleanup --merged-into <branch>`: removes merged worker branches,
their worktrees and tmux sessions, and keeps unmerged work.

## I. Sandbox

I1. Settings `sandbox.codex` (`bubblewrap`, `codex-bundled`, `none`) and
`sandbox.opencode` (`bubblewrap`, `none`). Unset means `bubblewrap` when
`bwrap` is on PATH and `none` when it is not.

I2. With a sandbox, each worker gets a deny list built at launch: every
entry in the home folder except the worktree, the repository's git folder,
`~/.local/bin`, `~/.local/lib`, git's config and the paths in the
`sandbox.read` setting; and all of `$XDG_RUNTIME_DIR`, which holds Ember's
tmux socket. Git writes are blocked. Codex receives the list as a
permission profile in `-c` options and applies it with its own `bwrap`
(I4). Ember runs OpenCode inside `bwrap` with the same list. Codex gets
network only when `worker.network` is on; OpenCode keeps network for
OpenRouter.

I3. With `none`, each run prints a `WARNING:` line, its record notes the
method, and the app shows a notice on Overview and Engines.

I4. Codex 0.164.0 (traced on 2026-11-01) ships its own `bwrap` at
`codex-resources/bwrap` inside its platform package. It looks for `bwrap` in
its own folders, then in each `PATH` folder in order, and runs the first one
found with `--help`. When that succeeds it uses that `bwrap`. When no `bwrap`
is found, or `--help` fails, it uses the bundled one. No setting or feature
flag selects the bundled one; `codex features list` shows
`use_linux_sandbox_bwrap` as removed and `use_legacy_landlock` as
deprecated.

- `codex-bundled`: Ember puts a folder first on the worker's `PATH` holding
  a `bwrap` symlink to Codex's `codex-resources/bwrap`. With it, every
  `bwrap` that Codex ran was the bundled one.
- `bubblewrap`: Ember leaves `PATH` as it is, and Codex uses the first
  `bwrap` on it.

The sandbox check (I5) records which `bwrap` Codex ran.

I5. The Engines screen shows per engine: the method in use, whether `bwrap`
was found and its version, the last sandbox check and its result, and known
blocking causes when detected (Ubuntu's `bwrap-userns-restrict` AppArmor
profile; a Codex install under a path the sandbox does not mount; a
`sandbox_mode` key in a Codex `config.toml`, which makes Codex ignore the
permission profile). Ember refuses to start a sandboxed Codex run while
that key is present. The check runs on Check now and when the method
changes.

## J. Desktop app

J1. Started by `emb app` or the app menu entry. One instance at a time; a
second start brings the window forward.

J2. Screens:

| Screen | Holds |
|---|---|
| Overview | Live runs; the last twenty finished runs. In 0.2.0 also flows with their tasks and steps |
| Menu | The worker menu (C1) |
| Engines | One card per engine: logo, status, Use switch, sandbox method and checks; open for models, efforts and shortlists; OpenCode's OpenRouter model browser and filters; setup steps with copyable commands when not connected |
| Dependencies | One tile per component, grouped (engines and apps, system tools, Ember's libraries): installed and newest version, status, and the update or install command |
| Settings | Start at login, notifications, worker network, sandbox read paths, terminal, theme |

J3. Tray icon: shows whether any run is live; its menu opens the window and
quits. The tray uses Qt's `QSystemTrayIcon`. GNOME shows tray icons
through an extension; the owner's Ubuntu 26.04 has
`ubuntu-appindicators@ubuntu.com` installed.

J4. Notifications: a run finishes, a run fails. In 0.2.0 also: a flow
finishes, a flow reaches a lead stop. Each has a Settings switch.

J5. Commands: an update, install or sign-in command shows a Run button. Run
opens a dialog naming the exact command; on confirm, the app runs it in a
window of the owner's terminal so its output and any password prompt stay
with the owner.

J6. Run records are watched with Qt's `QFileSystemWatcher` (inotify), with a
check every sixty seconds as a backstop. While the window is hidden, the app
reads only what the tray and notifications need.

J7. Theme follows the desktop's light or dark setting, with an override in
Settings.

## K. Dependencies

K1. A component list in `src/ember/data/components.toml`: id, label, group,
how the installed version is read (apt, npm, a command, Python package),
where the newest version is read, and its update and install commands.
The list includes tmux, bubblewrap and git.

K2. Status per component: current, update available, missing, check failed.
Installed versions are read at start and on Check now; newest versions at
most every six hours, cached.

## L. Skill

L1. An `ember` skill for Claude Code in `skill/`: `SKILL.md` and references
for claims, wrappers and Claude agents; references for flows, panels and
gap checks are added in 0.2.0. It covers grounding, claiming, choosing a worker, the editing boundary, briefs,
following a run, verification against the review bar, reporting, merging
and cleanup.

L2. Eval cases in `skill/evals/` run against stub engines and check the
skill's rules.

L3. `emb skill install` links the skill into `~/.claude/skills/ember`.

## M. Packaging

M1. A `.deb` built by `packaging/deb/`, installing the package, the `emb`
command, the desktop entry, icons and the autostart entry template.

M2. The `.deb` carries its own Python environment under `/usr/lib/ember/`,
built from uv's lock file, with PySide6 and Ember's other libraries from
PyPI. It runs on the system's Python 3.14. It depends on `python3` (3.14 or
later), `tmux` and `git`, and recommends `bubblewrap`.

M3. `ember-app` was free on PyPI on 2026-11-01. It is checked again before
the first release.

## N. Tests

N1. Unit tests per module with stub `codex`, `opencode`, `claude`, `tmux`,
`bwrap` and `git` tools on PATH. Unit tests run no real engine.

N2. App tests with pytest-qt on Qt's offscreen platform.

N3. Skill eval cases run outside `make check`, because they use a real
Claude session.

## O. Build order

1. Repository tooling (A), paths (B), settings and choices (C).
2. Engines (D) and single runs (E), with the sandbox (I).
3. Supporting commands (H, without H2).
4. Dependencies (K) and the desktop app (J).
5. Skill (L), packaging (M). 0.1.0 is released.
6. For 0.2.0: gap check (H2), flows (F), panels (G).

Each step passes `make check` on its own and lands as its own pull request.
