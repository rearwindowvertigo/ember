# ADR 0001: A local desktop app on one machine

- Status: Accepted, 2026-11-01.
- Release: Ember 0.1.0.

## Context

Ember lets Claude hand coding work to other AI engines (Codex, OpenCode and
Claude workers) and review and merge the results. The owner needs to see live
work, check that each engine is installed and signed in, keep dependencies
current and change settings. The owner decided the shape of Ember on
2026-11-01; the decisions are listed in design document 0001
(`docs/design/0001-first-release.md`).

## Decision

1. Ember runs on one machine. Workers run where Claude runs. There is no
   network between machines, no network listener and no web page.
2. The owner's screen is a desktop application written in Python with
   PySide6 (Qt). The terminal command is `emb`. The Python package is
   `ember-app`.
3. The app has a tray icon and desktop notifications. It starts at login
   into the tray by default; a Settings switch turns that off.
4. The app may run update, install and sign-in commands after the owner
   confirms each one. It reads the state and settings of Claude Code, Codex
   and OpenCode, and whether Claude Desktop is installed and its version. It
   does not write their configuration. It does not start worker runs.
5. The app learns of run changes by watching run files with inotify, with a
   slow periodic check as a backstop. It does no timed polling while nothing
   changes.
6. Settings, one-time choices and run claims live in one local SQLite
   database. Claude reaches them through `emb choices`; there is no MCP
   server.
7. Sandboxing workers is a setting. Bubblewrap is optional. For Codex the
   owner chooses system bubblewrap, the bubblewrap bundled with Codex, or
   none; for OpenCode, bubblewrap or none. The default is bubblewrap when it
   is installed and none when it is not. The app shows the method in use
   and its last check per engine.
8. Ember reads no seat or plan usage.
9. Ember is packaged as a `.deb` for installers other than the owner.

## Consequences

Work cannot be sent to another machine, and Ember cannot be seen from a
phone. With the sandbox set to none, a worker can read every file the owner
can read, including logins and keys; the app and every run report state
this.
