# Offscreen screens

Run `make screens` after `uv sync`. Qt renders the populated Overview, Team,
Engines, Dependencies and Settings screens with the app's style sheet, in light
and dark themes, at 640 × 480 and 1100 × 720. The run produces 20 PNG images.
It needs no display or network access.

Install `fonts-dejavu-core`. The command uses the test fontconfig aliases:
DejaVu Sans for interface text and DejaVu Sans Mono for code. It checks Qt's
resolved families and exits with an error if either font is substituted.
Fontconfig reads only the system font directories and a private cache.

Images go to `$XDG_CACHE_HOME/ember/screens` when `XDG_CACHE_HOME` is set, or
`/tmp/ember-screens` otherwise. Choose another dedicated folder with
`make screens OUT=/tmp/ember-review` or
`uv run --frozen python dev/screens.py --out /tmp/ember-review`.
The output folder is replaced on each run. Repository paths and symbolic-link
output folders are rejected. Files are named
`<screen>-<width>x<height>-<theme>.png`; Team's screen key is `menu`.

The command builds `AppContext` and `MainWindow` directly, with temporary HOME
and XDG folders, an empty stub PATH and fixed settings, run records, engine
status, model lists and dependency data. It does not start App, the tray,
socket or autostart wiring. It ignores saved window geometry. Fixture writes
and generated theme assets stay in the temporary environment, which is removed
after the command; the temporary fontconfig is removed at process exit.

Each finding is printed on its own line with the image filename and widget
class and name when available. A visible horizontal scroll bar with a positive
maximum is reported. A visible descendant whose right edge extends at least
one pixel beyond its enclosing scroll viewport is reported, including clipped
children. Outside a scroll area, the screen itself is the boundary. Hidden
widgets and vertical overflow are ignored. A window that grows beyond the
requested size is also reported. Exit codes are 0 for no findings, 1 for
findings and 2 for errors.

`tests/dev/test_screens.py` checks a planted overflow, scrollbar detection,
exit codes and a full render of the fixture in all sizes and themes. This test
runs in CI through pytest; `check.yml` does not run `make screens` separately.

Offscreen images cannot show the desktop's compositor, fractional scale or
desktop fonts. A test build on the desktop checks those. The renderer covers
each sidebar screen in its populated state, at the top of its scroll area;
it does not render dialogs, open menus, empty, loading or error states, or
exercise controls. These images are not pixel comparison tests.
