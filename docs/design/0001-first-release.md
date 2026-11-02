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
- OpenCode, through its interactive view with its built-in server, with
  OpenRouter models;
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
| E3 | What the app does | Shows work, engines, dependencies and settings; runs commands after a confirm step; has a tray icon. It does not start runs. |
| E4 | Other installed AI tools | Ember reads the state and settings of Claude Code, Codex and OpenCode, and whether Claude Desktop is installed and its version. It does not read Claude Desktop's configuration. It does not drive these tools or write their configuration. |
| E5 | Name and command | Ember; terminal command `emb`; Python package `ember-app`. |
| E6 | Repository | `~/Repos/ember`, remote `github.com/rearwindowvertigo/ember`, private. |
| E7 | Start at login | A Settings switch, on by default, starting into the tray. |
| E8 | Run changes | Found by file watching (inotify) with a slow backstop check. |
| E9 | Sandbox | Optional, one on/off switch per engine. Each engine uses its own operating-system sandbox where it has one that works, and Ember's bubblewrap where it does not: Codex its own sandbox, configured by Ember; OpenCode and Claude Ember's bubblewrap around the engine. On by default when the engine's sandbox can work, off when it cannot. Clear controls and as much feedback as each sandbox allows. |
| E10 | Packaging | `.deb` first. |
| E11 | MCP server | None. Claude uses `emb choices`. |
| E12 | Seat usage | Not read or shown. |
| E13 | Scope of 0.1.0 | Settings, engines, single runs, the sandbox, supporting commands, dependencies, the app, the skill and the `.deb`. Flows, panels and the gap check are in 0.2.0. |
| E14 | Licence | MIT. |
| E15 | Worker windows | Each worker runs in a tmux session. The owner picks the terminal that shows it. kitty is not required. |
| E16 | Continuous integration | GitHub Actions on pull requests only. Docs-only pull requests skip the check. A newer push cancels the older run. Release Please runs on `main`. |
| E17 | Python | 3.14 or later. |
| E18 | Unit tests | Stub tools only (N1). |
| E19 | App look and layout | A sidebar with five screens, flat cards, pill controls, the desktop's fonts and Lucide icons (J1, J2, J8). |
| E20 | Closing the window | Hides Ember to the tray (J1). |
| E21 | Run view | A read-only mirror of a live worker's screen and Open terminal; no interrupt or close from the app (J2). |
| E22 | Tray | Idle and live states, no attention state; a menu with the live runs (J3). |
| E23 | One-time choices | Not shown in the app (J2). |
| E24 | Engine logos | Each engine card shows its maker's logo (J2). |
| E25 | Mark | Three coals (J11). |
| E26 | Notifications | None. |
| E27 | Keyboard | Ctrl+1 to Ctrl+5 open the screens, Ctrl+W hides to the tray, Ctrl+Q quits; every control is reachable with Tab. |
| E28 | Claude Desktop | A tile on the Dependencies screen: installed and its version, or not installed. |

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
| Run, flow and panel records | `$XDG_STATE_HOME/ember/runs/` (default `~/.local/state/ember/`) |
| Caches (model lists, version reads) | `$XDG_CACHE_HOME/ember/` |
| Runtime files (locks, sockets) | `$XDG_RUNTIME_DIR/ember/` |

B2. The settings database and run records are mode 600 in mode 700 folders,
and every sandboxed worker is denied them.

## C. Settings and choices

C1. Standing settings: the worker menu (which engine, model and effort writes
single tasks, which reviews; in 0.2.0 also how multi-task work runs
(`multi.runs_as`: ask, a flow or a Claude Workflow) and panel members
(`panel.members`)),
per-engine on or off, model shortlists with efforts, worker network access,
a sandbox switch per engine, the terminal that shows worker sessions and
start at login.

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

D3. Model lists: Codex models from `codex debug models`; Claude models are
the aliases the Claude CLI's `--model` option accepts (`fable`, `opus`,
`sonnet`, `haiku`), as the CLI has no model-list command; OpenRouter models
from OpenRouter's model list, with filters. Codex and OpenRouter lists are
cached.

## E. Single worker runs

E1. `emb run --engine <codex|opencode|claude> --dir <repo> --task-file
<brief>` starts one worker. Its first output line is `run <id>`.
`--trailer <line>`, repeatable, adds a trailer line to each commit the
worker makes; there is no default trailer.
Ember pastes the whole brief into the worker's session as its first
message, under a first line stating that Claude, the lead, wrote it. The
worker's session starts in its worktree, or in the repository for a
`--read-only` run. `--read <path>`, repeatable, lets that run read the path,
read-only, in addition to the `sandbox.read` setting.

E2. An editing run gets its own worktree and branch, `ember/<engine>/<id>`,
cut from the current branch, and commits with the trailers given by
`--trailer`. A `--read-only` run edits nothing.

E3. The run runs in its own tmux session, on a tmux server private to Ember
(socket in `$XDG_RUNTIME_DIR/ember/`). The session runs with no window.
`emb run --window` at start, or `--attach` later, opens a window of the
owner's terminal on the session; the setting `terminal` holds the command
that opens it; unset means `xdg-terminal-exec`. Closing a window leaves the
run going. Commands: `--send` (a correction into the same session,
through tmux `send-keys`), `--screen` (tmux `capture-pane`), `--interrupt`,
`--attach` (open a new window on the session), `--close`, `--recover`,
`--status <id> --json`. Each non-zero exit prints a `next:` or `owner:` line.

E4. Workers never push, merge, rebase, delete branches or touch `main`.

E5. A run's record stores at start who provides its sandbox (`codex`,
`bubblewrap` or `none`), whether `worker.network` is on (`network`), and its
read paths: the `sandbox.read` setting followed by the `--read` paths. Every
turn of the run uses the stored values.

E6. The owner can type into a worker's session through a terminal window.
When a session holds messages Ember did not send, or its history cannot be
checked, `--send` and `--close` refuse with exit 75 and a line saying that
Claude asks the owner; `--force` goes on, and `--send --force` records the
messages then in the session as accepted. Codex's driver compares the
session's user messages with the messages Ember pasted, OpenCode's driver
the ids of the session's user messages with the ids Ember sent, and a Claude
session takes no typed messages.

## F. Flows

Target release: 0.2.0.

F1. `emb flow --plan <file> --start` runs a JSON plan of tasks in waves by
their `after` dependencies. Each task has a writer in an editing worktree
and a read-only reviewer, two fix rounds by default (zero to three), and a
declared list of files. `--writer` and `--reviewer`, the plan, or the menu
rows name the workers.

F2. Accepted tasks merge into the flow's run branch, `ember/flow/<id>`. The
plan's check command runs after each wave's merges.

F3. A task that fails, stays unresolved or conflicts stops at a lead stop.
Claude reports it to the owner with every available choice: send a
correction, accept a reviewed commit, start a fresh review, use another
worker, or drop the task. Claude answers with `emb flow --lead`.

F4. Reviews use one written review bar with three severities: must, should
and lead.

F5. `--dry-run` prints the waves and the gap check command. `--start`
starts a runner in its own tmux session. `--watch --json` waits for a lead
stop or the end, `--status` reads flow records, `--lead` queues an answer,
and `--resume` starts another runner attempt.

## G. Panels

Target release: 0.2.0.

G1. `emb panel --question-file <file>` asks each member the same question
in a read-only `emb run` run, in parallel, and writes `summary.md` with
every answer.

G2. Members come from repeatable `--member <engine>:<model>[@<effort>]`
options or `panel.members`: at most eight, using Codex, OpenCode or Claude.

G3. A member that fails, stalls or passes the deadline is reported and the
other members' answers are kept. `--recover` collects late members.

## H. Supporting commands

H1. `emb ground --json`: the machine, the Claude surface, the repository,
branch, dirty state and remote, read before a worker is chosen.

H2. `emb gapcheck --plan <file>`: a read-only run of the `single.reviewer`
row, or of the worker given with `--engine`, `--model` and `--effort`, reads
a plan against its failure model before the plan's work starts and reports,
for each step, the behaviours the plan leaves undefined or contradictory,
each with a harm and a proposed default. `--brief` prints its brief and
`--reply` reads a reply from another worker, such as a Claude subagent.
Target release: 0.2.0.

H3. `emb merge <branch> --into <branch> --verdict keep`: merges a reviewed
worker branch, then removes its worktree and branch.

H4. `emb cleanup --merged-into <branch>`: removes merged worker branches,
their worktrees and tmux sessions, and keeps unmerged work.

## I. Sandbox

I1. Settings `sandbox.codex`, `sandbox.opencode` and `sandbox.claude`, each
`on` or `off`. Unset means `on` when the engine's sandbox can work (Codex: a
`bwrap` on `PATH` or Codex's bundled `bwrap`; OpenCode and Claude: a `bwrap`
on `PATH`) and `off` otherwise. Who provides each engine's sandbox:

| Engine | Provider | How |
|---|---|---|
| Codex | `codex` | Codex's own sandbox, configured by Ember (I2, I4) |
| OpenCode | `bubblewrap` | Ember's `bwrap` around the whole program |
| Claude | `bubblewrap` | Ember's `bwrap` around each turn's `claude -p` |

Claude Code's own sandbox covers only its shell commands and does not start
on the owner's machine ("nested userns is capability-restricted"), so Claude
runs in Ember's `bwrap`. A switch that is `on` while no working `bwrap` is
found refuses the run with exit 69. Codex with `off` runs with
`--sandbox danger-full-access`.

I2. With a sandbox, each worker gets a deny list built at launch: every
entry in the home folder except the worktree, the repository's git folder,
`~/.local/bin`, `~/.local/lib`, git's config, the engine's own folders and
the run's read paths (the `sandbox.read` setting and the run's `--read`
paths); and all of `$XDG_RUNTIME_DIR`, which holds Ember's tmux socket, and
Ember's own configuration and state folders. The worktree is writable, or
read-only for a read-only run. Git writes are blocked. Codex receives the
list as a permission profile in `-c` options and applies it with a `bwrap`
(I4). Ember runs OpenCode, and each Claude turn, inside `bwrap` with the
same list; the program in a Claude run's pane that starts each turn runs
outside the sandbox. Codex gets network only when `worker.network` is on;
Codex's web search stays on. OpenCode keeps network for OpenRouter. Claude
keeps network for its API, and `worker.network` controls only its web
tools.

I3. A run's record names who provides its sandbox: `codex`, `bubblewrap` or
`none`. With `none`, each run of any engine prints a `WARNING:` line, and
the app shows a notice on Overview and Engines.

I4. Codex 0.164.0 (traced on 2026-11-01) ships its own `bwrap` at
`codex-resources/bwrap` inside its platform package. It looks for `bwrap` in
its own folders, then in each `PATH` folder in order, and runs the first one
found with `--help`. When that succeeds it uses that `bwrap`. When no `bwrap`
is found, or `--help` fails, it uses the bundled one. No setting or feature
flag selects the bundled one; `codex features list` shows
`use_linux_sandbox_bwrap` as removed and `use_legacy_landlock` as
deprecated.

Ember chooses the `bwrap` without a setting:

- With a `bwrap` on `PATH`, Ember leaves `PATH` as it is, and Codex uses
  the first `bwrap` on it.
- Without one, Ember puts a folder first on the worker's `PATH` holding a
  `bwrap` symlink to Codex's `codex-resources/bwrap`. With it, every `bwrap`
  that Codex ran was the bundled one.

The sandbox check (I5) records which `bwrap` Codex ran.

I5. The Engines screen shows per engine: the switch and who provides the
sandbox, whether `bwrap` was found and its version, the last sandbox check
and its result, and known blocking causes when detected (Ubuntu's
`bwrap-userns-restrict` AppArmor profile; a Codex install under a path the
sandbox does not mount; a `sandbox_mode` key in a Codex `config.toml`,
which makes Codex ignore the permission profile). Ember refuses to start a
sandboxed Codex run while that key is present. The check runs on Check now
and when the switch changes.

## J. Desktop app

J1. Started by `emb app` or the app menu entry. One instance at a time; a
second start brings the window forward. The window opens at 1100 by 720
pixels, is never smaller than 900 by 600, and keeps its size between starts.
Closing the window hides Ember to the tray; Quit Ember is in the tray menu.
When no tray is available, closing the window quits.

J2. Screens, chosen from a sidebar on the left. The sidebar shows the Ember
mark and name, the five screens with an icon each, and the version. Overview
shows the number of live runs and Dependencies the number of updates and
missing items, each only when above zero.

| Screen | Holds |
|---|---|
| Overview | Live runs; the last twenty finished runs. In 0.2.0 also flows with their tasks and steps |
| Menu | The worker menu (C1) |
| Engines | One card per engine: logo, status, Use switch, sandbox switch, provider and checks; open for models, efforts and shortlists; OpenCode's OpenRouter model browser and filters; setup steps with copyable commands when not connected |
| Dependencies | One tile per component, grouped (engines and apps, system tools, Ember's libraries): installed and newest version, status, and the update or install command; Claude Desktop installed and its version |
| Settings | General: start at login, theme. Workers: worker network, terminal, sandbox read paths |

- Overview lists as live each run whose status is `starting`, `running` or
  `waiting`, with the badge Starting, Running or Waiting for Claude, and as
  finished each run whose status is `closed` or `failed`, with the badge
  Closed or Failed. A row shows the engine, model, effort, repository,
  branch, and the turn or the subject, with times.
- A live row has Open terminal, which does what `emb run --attach <id>`
  does. Expanded, a live row shows a read-only mirror of the worker's
  screen, updated when the worker writes output and never on a timer. The
  mirror follows the session through tmux control mode on Ember's tmux
  server: a client attaches with `-C attach-session -f
  read-only,ignore-size` and never sets a size. After each `%output`
  notification for the pane, it reads the pane with `capture-pane -p -e`
  through the same client and redraws from that text, with at most one
  capture in flight. tmux is the terminal emulator; the app parses only the
  SGR sequences in the captured text. Expanded, a finished row shows its run
  record; no screen is kept.
- The app does not interrupt, close or send to a run.
- While an engine's sandbox method is `none`, Overview and Engines show a
  warning naming the engine (I3).
- Menu holds the Writer and Reviewer pickers and, for a Claude model, how it
  runs (CLI or subagent). Each picker lists Ask each time, then the
  shortlisted models under their engine. Engines that are off are left out.
  One-time choices (C2) are not shown in the app.
- Engines shows three cards side by side, each with the engine maker's logo.
  Models opens that engine's model list below the cards, with a switch per
  model for the shortlist and an effort picker.
- Settings has two groups: General (start at login, theme) and Workers
  (worker network, terminal, sandbox read paths). Each change saves at
  once. Terminal is picked from the terminals Ember finds, with System
  default first and Other command last. Read paths are added with the
  desktop's folder chooser.
- Keyboard: Ctrl+1 to Ctrl+5 open Overview, Menu, Engines, Dependencies
  and Settings; Ctrl+W hides the window to the tray, or quits when there is
  no tray; Ctrl+Q quits. Every control is reachable with Tab, in reading
  order, with a visible focus ring.

J3. Tray icon: shows whether any run is live, in two states, idle and a run
live. The tray uses Qt's `QSystemTrayIcon`. GNOME shows tray icons through
an extension; the owner's Ubuntu 26.04 has `ubuntu-appindicators@ubuntu.com`
installed. The tray menu holds a status line ("2 runs live" or "No runs
live"), one line per live run ("<Engine> · <repository> · <time>", which
opens a terminal on that run), Open Ember and Quit Ember. The tray always
uses the icons for a dark top bar (`on-dark`): Ubuntu 26.04's Yaru shell
themes draw the top bar `#131313` in light and dark mode.

J4. Ember sends no desktop notifications.

J5. Commands: an update, install or sign-in command shows a Run button. Run
opens a dialog titled "Run this command?" naming the exact command, with
Cancel and Run in terminal. Cancel is the default button. On confirm, the
app runs the command in a window of the owner's terminal, opened with the
`terminal` setting, so its output and any password prompt stay with the
owner. After the command ends, the window shows its exit code and stays
open until Enter is pressed.

J6. Run records are watched with Qt's `QFileSystemWatcher` (inotify), with a
check every sixty seconds as a backstop. While the window is hidden, the app
reads only what the tray needs, besides the reads at start and the engine
status read after a run fails with a sign-in error; screens do not redraw.

J7. Theme follows the desktop's light or dark setting, with an override in
Settings (System, Light, Dark).

J8. Look: gray surfaces, black or white primary buttons, pill-shaped
controls 36 pixels tall with 14-pixel text, and blue only for what is on,
selected or focused. Cards are flat with a hairline border; nothing has a
shadow. The app draws this with a Qt style sheet; the switch, spinner and
screen mirror are painted by the app. Text uses the desktop's interface and
monospace fonts. Icons are Lucide icons (ISC licence). Settings use
switches, segmented controls and pickers; the only text fields are the
OpenRouter model search and Other command.

J9. Screen mirror colors: the mirror follows the app's theme. Sixteen
terminal colors are set per theme, each at least 5.8:1 against the mirror
background. Other colors a worker sets are lightened or darkened to 4.5:1;
background colors a worker sets are dropped, except reverse video.

J10. Empty, loading and error states: an empty list shows a title and one
line, with no button. A read in progress shows a spinner and what is being
read, after 300 milliseconds. An error says what happened and what to do,
with Try again where a retry can help. A save refused as stale (C4) reloads
the screen and says so.

J11. The Ember mark is three coals on a dark rounded tile: an orange coal
(`#f76707`) above two dark ones. The source files are in
`docs/design/assets/0001/`: the scalable app icon, a simpler drawing for 32
pixels and below, and the tray icons for idle and a run live, each for a
dark and a light top bar.

## K. Dependencies

K1. A component list in `src/ember/data/components.toml`: id, label, group,
how the installed version is read (an apt package, a command's output, or a
Python package), where the newest version is read (apt, a JSON or text URL,
or PyPI), and its update and install commands. The groups are `apps`
(engines and apps), `system` (system tools) and `libraries` (Ember's
libraries).

| id | Group | Installed read | Newest read | Update | Install |
|---|---|---|---|---|---|
| `codex` | apps | `codex --version` | `https://registry.npmjs.org/@openai/codex/latest`, `version` | `codex update` | `npm install -g @openai/codex` |
| `opencode` | apps | `opencode --version` | `https://registry.npmjs.org/opencode-ai/latest`, `version` | `opencode upgrade` | `curl -fsSL https://opencode.ai/install \| bash` |
| `claude` | apps | `claude --version` | `https://downloads.claude.ai/claude-code-releases/latest`, the whole body | `claude update` | `curl -fsSL https://claude.ai/install.sh \| bash` |
| `claude-desktop` | apps | apt `claude-desktop` | apt `claude-desktop` | `sudo apt install --only-upgrade claude-desktop` | None |
| `tmux` | system | apt `tmux` | apt `tmux` | `sudo apt install --only-upgrade tmux` | `sudo apt install tmux` |
| `bubblewrap` | system | apt `bubblewrap` | apt `bubblewrap` | `sudo apt install --only-upgrade bubblewrap` | `sudo apt install bubblewrap` |
| `git` | system | apt `git` | apt `git` | `sudo apt install --only-upgrade git` | `sudo apt install git` |
| `python3` | system | apt `python3` | apt `python3` | `sudo apt install --only-upgrade python3` | `sudo apt install python3` |
| `xdg-terminal-exec` | system | apt `xdg-terminal-exec` | apt `xdg-terminal-exec` | `sudo apt install --only-upgrade xdg-terminal-exec` | `sudo apt install xdg-terminal-exec` |
| `pyside6` | libraries | Python package `PySide6-Essentials` | PyPI `PySide6-Essentials` | None | None |

A component with no command for its state shows a note instead: Claude
Desktop missing shows "Not installed."; PySide6 with an update shows
"Updates with the next Ember release." and missing shows "Reinstall Ember."

K2. Status per component: current, update available, missing, check failed.
Installed versions are read at start and on Check now. Newest versions are
cached, and an ordinary read reads them again only when the cached read is
six hours old or more; an explicit refresh (`emb deps --refresh`) reads
them at once.

## L. Skill

L1. An `ember` skill for Claude Code in `skill/`: `SKILL.md` and references
for claims, runs and Claude agents; references for flows, panels and gap
checks are added in 0.2.0. It covers grounding, claiming, choosing a worker,
the editing boundary, briefs, following a run, verification against the
review bar, reporting, merging and cleanup. Until `emb gapcheck` (H2), the
plan check before a Claude Workflow runs `assets/plan-check-brief.md` with
the `single.reviewer` row.

L2. Eval cases in `skill/evals/` run in a real Claude session against a stub
`emb` command and check the skill's rules. The cases include flows, panels
and the gap check, with stub replies for `emb gapcheck`, `emb flow` and
`emb panel`.

L3. `emb skill install` links the skill into Claude Code's skills folder:
`$CLAUDE_CONFIG_DIR/skills/ember` when `CLAUDE_CONFIG_DIR` is set and not
empty, else `~/.claude/skills/ember`. The link's target is the checkout's
`skill/` when `emb` runs from a source checkout, else
`/usr/share/ember/skill`. `emb skill uninstall` removes the link and
`emb skill status` reports it. The package does not create the link.

L4. `scripts/verify_branch.py` in the skill prints a branch's tip, its
numstat against the session branch and its worktree's status. It decides no
verdict.

## M. Packaging

M1. A `.deb` built by `packaging/deb/build_deb.py` (`make deb`), with no
maintainer scripts:

| Path | Holds |
|---|---|
| `/usr/bin/emb` | `exec /usr/lib/ember/bin/python -I -m ember.cli "$@"`, mode 755 |
| `/usr/lib/ember/` | A virtual environment on `/usr/bin/python3.14` |
| `/usr/lib/ember/lib/python3.14/site-packages/ember/` | Ember |
| `/usr/share/ember/skill/` | The skill, `skill/` without `evals/` |
| `/usr/share/applications/io.github.rearwindowvertigo.Ember.desktop` | The desktop entry |
| `/usr/share/icons/hicolor/scalable/apps/io.github.rearwindowvertigo.Ember.svg` | The icon |
| `/usr/share/doc/ember/copyright` | The copyright file |
| `/usr/share/doc/ember/changelog.gz` | `CHANGELOG.md`, when present |

The package installs no autostart file; the app writes the user's own
autostart entry while `app.autostart` is on.

M2. The `.deb`'s Python environment is built from uv's lock file, with
PySide6-Essentials and Ember's other libraries from PyPI. It runs on
`/usr/bin/python3.14`. The package depends on `python3.14`, `git`, `tmux`
and the libraries Qt loads, and recommends `bubblewrap` and
`xdg-terminal-exec`.

M3. `ember-app` was free on PyPI on 2026-11-01 and on 2026-11-02.

M4. When Release Please creates a release, the `deb` job in
`.github/workflows/release-please.yml` builds the `.deb` from the release
tag on `ubuntu-26.04` and attaches it to the GitHub release. A manual run of
the workflow with a tag rebuilds and replaces it.

## N. Tests

N1. Unit tests per module with stub `codex`, `opencode`, `claude`, `tmux`,
`bwrap` and `git` tools on PATH. Unit tests run no real engine.

N2. App tests with pytest-qt on Qt's offscreen platform.

N3. Skill eval cases run outside `make check`, because they use a real
Claude session against a stub `emb` command.

## O. Build order

1. Repository tooling (A), paths (B), settings and choices (C).
2. Engines (D) and single runs (E), with the sandbox (I).
3. Supporting commands (H, without H2).
4. Dependencies (K) and the desktop app (J).
5. Skill (L), packaging (M). 0.1.0 is released.
6. For 0.2.0: gap check (H2), flows (F), panels (G).

Each step passes `make check` on its own and lands as its own pull request.
