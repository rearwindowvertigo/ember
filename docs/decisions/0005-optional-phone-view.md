# ADR 0005: An optional phone view over Tailscale

- Status: Proposed, 2026-11-07.
- Release: none.

## Context

ADR 0001 gives Ember no network listener and no web page, and its
consequences state that Ember cannot be seen from a phone. The owner
watches Claude sessions on this machine from a phone. The Overview screen
of the desktop app is visible only at the machine.

## Decision

1. Phone view is an optional module, off by default. No other part of
   Ember depends on it. Its code is in its own package, and a test fails
   when code outside that package imports it. This decision amends point 1
   of ADR 0001 for this module only.
2. The desktop app serves the page while the module is on. The page is
   unavailable while the app is not running.
3. The app listens only on this machine's Tailscale address, over plain
   HTTP. Tailscale encrypts the traffic between devices. Ember reads
   Tailscale's state and changes none of its settings.
4. The app answers only requests from devices signed in as the same
   Tailscale user as this machine. It checks each request's device through
   Tailscale's local API.
5. The page shows the Overview screen's Live and Finished sections, read
   from the same record models as the desktop app, with the same order,
   status chips and rules. It adds no states or rules of its own. It has the
   Overview's view-only controls: expanding a row, `Show all`, and the Type,
   Status and Engine filters. It has no control that changes state, such as
   Clear. Its layout fits a phone screen and follows the device's light or
   dark mode. The app sends updates to the page as server-sent events when
   its run file watch reports a change.
6. A Phone view section on the Settings screen holds the switch, tooltips
   for each control, and a setup checklist: Tailscale installed, Tailscale
   signed in, page running, and phone steps. The phone steps are installing
   the Tailscale app on the phone, signing in as the same user and scanning
   a QR code that holds only the page address.
7. The section shows the running state and page address. The tray and the
   sidebar do not change while the module works. A problem, such as
   Tailscale signed out, the port taken or no Tailscale address, shows in
   the section with its fix and adds to the count pill on the sidebar's
   Settings row. Nothing shows while the module is off.
8. Tailscale is a row on the Dependencies screen only while the module is
   on. When Tailscale is missing, the confirmed install command opens in
   the owner's terminal: it adds Tailscale's signing key and apt repository,
   then runs `sudo apt install tailscale`. Updates use
   `sudo apt install --only-upgrade tailscale`. The checklist's sign-in step
   opens Tailscale's sign-in. The `.deb` does not depend on Tailscale.
9. Switching the module off stops the listener. Tailscale stays installed
   and signed in, and the section states how to remove it.
10. The `emb` command has no Phone view command.

## Consequences

With the module off, Ember behaves as ADR 0001 describes. With it on,
Ember has one network listener, on the Tailscale address, and the Overview
can be seen from any device signed in as the same Tailscale user. Safari
marks the page as not secure because it is served over plain HTTP.
