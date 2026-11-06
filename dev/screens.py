"""Render populated sidebar screens offscreen and report horizontal overflow."""

import argparse
import atexit
import gc
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from unittest.mock import patch
from xml.sax.saxutils import escape

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PySide6.QtGui import QFontInfo
from PySide6.QtWidgets import QApplication, QScrollArea, QScrollBar, QWidget

from ember import choices, deps, paths
from ember.app import state, theme
from ember.app.context import AppContext
from ember.app.screens import KEYS
from ember.app.window import MINIMUM_HEIGHT, MINIMUM_WIDTH, MainWindow
from ember.engines import models, status
from ember.runs import record

SIZES = ((MINIMUM_WIDTH, MINIMUM_HEIGHT), (state.DEFAULT_WIDTH, state.DEFAULT_HEIGHT))
STAMP = 1_800_000_000.0


def setup() -> None:
    """Set offscreen Qt and standalone test fontconfig before QApplication.

    Existing values are kept so pytest can supply its own fontconfig file.
    The generated file and its private cache are removed at process exit.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    if os.environ.get("FONTCONFIG_FILE"):
        return
    folder = Path(tempfile.mkdtemp(prefix="ember-screen-fonts-"))
    atexit.register(shutil.rmtree, folder, ignore_errors=True)
    config = folder / "fonts.conf"
    config.write_text(
        f"""<?xml version="1.0"?>
<fontconfig>
  <dir>/usr/share/fonts</dir>
  <dir>/usr/local/share/fonts</dir>
  <cachedir>{escape(str(folder / "cache"))}</cachedir>
  <alias><family>sans-serif</family>
    <prefer><family>DejaVu Sans</family></prefer></alias>
  <alias><family>Sans Serif</family>
    <prefer><family>DejaVu Sans</family></prefer></alias>
  <alias><family>monospace</family>
    <prefer><family>DejaVu Sans Mono</family></prefer></alias>
</fontconfig>
""",
        encoding="utf-8",
    )
    os.environ["FONTCONFIG_FILE"] = str(config)


def environment(root: Path) -> dict[str, str]:
    """Create a private HOME, XDG folders and an empty executable search path."""
    folders = {
        "HOME": "home",
        "XDG_CONFIG_HOME": "config",
        "XDG_STATE_HOME": "state",
        "XDG_CACHE_HOME": "cache",
        "XDG_DATA_HOME": "data",
        "XDG_RUNTIME_DIR": "runtime",
        "TMPDIR": "tmp",
        "PATH": "bin",
    }
    env = {key: str(root / name) for key, name in folders.items()}
    for folder in env.values():
        Path(folder).mkdir(parents=True, mode=0o700, exist_ok=True)
    return env


def _name(widget: QWidget) -> str:
    """Identify a widget in a finding without depending on memory addresses."""
    label = widget.objectName() or widget.accessibleName()
    return f"{type(widget).__name__}({label})" if label else type(widget).__name__


def find_overflow(widget: QWidget) -> list[str]:
    """Report visible horizontal scroll bars and descendants past the right edge.

    Each descendant is mapped to its enclosing scroll area's viewport, or
    the supplied screen when outside a scroll area. A right edge at least
    one pixel beyond that boundary is reported, even when clipped. Hidden
    descendants and vertical overflow are ignored.
    """
    findings: list[str] = []
    for child in widget.findChildren(QWidget):
        if not child.isVisible() or child.isWindow():
            continue
        if (
            isinstance(child, QScrollBar)
            and child.orientation() == Qt.Orientation.Horizontal
            and child.maximum() > 0
        ):
            findings.append(
                f"{_name(child)}: horizontal scroll bar max={child.maximum()}"
            )
        boundary = widget
        parent = child.parentWidget()
        while parent is not None and parent is not widget:
            if isinstance(parent, QScrollArea):
                content = parent.widget()
                if content is not None and (
                    child is content or content.isAncestorOf(child)
                ):
                    boundary = parent.viewport()
                    break
            parent = parent.parentWidget()
        right = child.mapTo(boundary, QPoint(child.width(), 0)).x()
        excess = right - boundary.width()
        if excess >= 1:
            findings.append(
                f"{_name(child)}: right edge exceeds viewport by {excess} px"
            )
    return findings


def _seed(env: Mapping[str, str]) -> None:
    """Write fixed settings and live and finished runs into the private folders."""
    choices.set_settings(
        {
            "shortlist.codex": ["gpt-5@high"],
            "shortlist.claude": ["sonnet@high"],
            "shortlist.opencode": ["openrouter/example@high"],
            "single.writer": "codex:gpt-5",
            "single.reviewer": "claude:sonnet",
            "panel.members": ["codex:gpt-5", "claude:sonnet"],
            "worker.network": "on",
            "sandbox.codex": "on",
            "sandbox.claude": "on",
            "sandbox.opencode": "on",
        },
        env=env,
    )
    state.save(state.AppState(finished_open=True), env)
    for index, run_status in enumerate((record.WAITING, record.RUNNING, record.CLOSED)):
        run_id = f"codex-20270115-12000{index}-aaaaa{index}"
        run = record.Run(
            id=run_id,
            engine="codex",
            model="gpt-5",
            effort="high",
            read_only=False,
            repo="/workspace/example",
            worktree=f"/workspace/example/{run_id}",
            branch=f"ember/{run_id}",
            base="a" * 40,
            sandbox="bubblewrap",
            network=False,
            read=(),
            read_effective=(),
            trailers=(),
            claim=None,
            status=run_status,
            turn=1,
            turn_state=record.SENT,
            reply=None,
            expected_head=None,
            started_at=STAMP,
            updated_at=STAMP + 60,
            ended_at=STAMP + 60 if run_status == record.CLOSED else None,
            last_exit=0 if run_status == record.CLOSED else None,
            last_reason=None,
            commits=(),
            subject="Render the app screens",
            folder=paths.runs_dir(env) / run_id,
            env=env,
        )
        record.make_run_dir(run.id, env)
        record.save(run)


def _engines(env: Mapping[str, str]) -> dict[str, status.EngineStatus]:
    """Return connected fixture engines without probing installed commands."""
    return {
        engine: status.EngineStatus(
            engine=engine,
            state=status.CONNECTED,
            reason=None,
            installed=True,
            binary=str(Path(env["PATH"]) / engine),
            version="1.0.0",
            signed_in=True,
            connected=True,
            requirements=(status.Requirement(f"{engine} CLI", True, None),),
            checked_at=STAMP,
        )
        for engine in status.ENGINES
    }


def _models(
    engine: str, env: Mapping[str, str] | None = None, *, refresh: bool = False
) -> models.ModelList:
    """Supply a fixed populated model list without commands or network reads."""
    name = {"codex": "gpt-5", "claude": "sonnet", "opencode": "openrouter/example"}
    return models.ModelList(
        (models.Model(name[engine], name[engine], ("high",), "high"),),
        models.CACHE,
        STAMP,
        None,
    )


def _dependencies() -> deps.DepsReport:
    """Populate every dependency group from the shipped component definitions."""
    components = tuple(
        deps.Component(
            id=spec.id,
            label=spec.label,
            group=spec.group,
            status=deps.CURRENT,
            installed="1.0.0",
            newest="1.0.0",
            error=None,
            command=None,
            command_kind=None,
            note=None,
        )
        for spec in deps.load_components()
    )
    stamps = {item.id: STAMP for item in components}
    return deps.DepsReport(components, stamps, stamps)


def _settle(app: QApplication, context: AppContext) -> None:
    """Wait for data callbacks, deferred deletes and queued layout passes."""
    deadline = time.monotonic() + 10
    quiet = 0
    while quiet < 8:
        app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        quiet = quiet + 1 if context.tasks.pending() == 0 else 0
        if time.monotonic() > deadline:
            raise TimeoutError("screen data did not finish loading in 10 seconds")
        if context.tasks.pending():
            context.tasks.wait(10)


def _prepare_output(out: Path) -> None:
    """Replace an output directory outside the checkout, rejecting unsafe roots."""
    repo = Path(__file__).resolve().parents[1]
    resolved = out.resolve()
    if (
        out.is_symlink()
        or resolved == Path(resolved.anchor)
        or resolved == Path(tempfile.gettempdir())
        or resolved == Path.home()
        or resolved == repo
        or repo in resolved.parents
        or resolved in repo.parents
    ):
        raise ValueError("--out must be a dedicated folder outside the repository")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)


def render(app: QApplication, out: Path, env: Mapping[str, str]) -> list[str]:
    """Render every populated sidebar screen at both sizes in both themes.

    Args:
        app: An existing offscreen QApplication using the pinned fonts.
        out: A dedicated output folder outside the checkout, replaced on entry.
        env: Private folders created by environment(), never the user's folders.

    Returns:
        One string per overflow, prefixed with its image filename.
    """
    families = QFontInfo(app.font()).family(), QFontInfo(theme.font("code")).family()
    if families != ("DejaVu Sans", "DejaVu Sans Mono"):
        raise RuntimeError(f"Install fonts-dejavu-core; Qt resolved {families!r}")
    _prepare_output(out)
    findings: list[str] = []
    with (
        patch.dict(os.environ, env),
        patch.object(tempfile, "tempdir", env["TMPDIR"]),
        patch.object(status, "read", return_value=_engines(env)),
        patch.object(models, "list_models", side_effect=_models),
        patch.object(deps, "read", return_value=_dependencies()),
    ):
        _seed(env)
        context = AppContext(env)
        window: MainWindow | None = None
        try:
            context.reload_settings()
            context.refresh_engines()
            context.refresh_deps(check_now=False)
            context.runs.reload()
            context.flows.set_active(True)
            _settle(app, context)
            if (
                not context.settings_ready()
                or context.settings().get("errors")
                or context.runs.error()
                or len(context.runs.runs()) != 3
                or not context.flows.loaded()
                or context.flows.error()
            ):
                raise RuntimeError("could not load the screen fixtures")
            theme.apply(app, theme.MODES[0])
            window = MainWindow(context, has_tray=False, quit_app=lambda: None)
            window.show()
            for mode in theme.MODES:
                theme.apply(app, mode)
                for width, height in SIZES:
                    window.resize(width, height)
                    for key in KEYS:
                        window.show_screen(key)
                        _settle(app, context)
                        filename = f"{key}-{width}x{height}-{mode}.png"
                        screen = window.screen_widget(key)
                        assert screen is not None
                        findings.extend(
                            f"{filename}: {finding}"
                            for finding in find_overflow(screen)
                        )
                        if window.width() != width or window.height() != height:
                            findings.append(
                                f"{filename}: window grew to "
                                f"{window.width()}x{window.height()}"
                            )
                        if not window.grab().save(str(out / filename)):
                            raise OSError(f"could not save {out / filename}")
        finally:
            context.runs.stop()
            context.flows.set_active(False)
            _settle(app, context)
            if window is not None:
                window.hide()
                window.deleteLater()
            context.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            gc.collect()
    return findings


def main() -> int:
    """Parse --out and return 0 when clean, 1 for overflow, or 2 for errors."""
    parser = argparse.ArgumentParser(description=__doc__)
    cache = os.environ.get("XDG_CACHE_HOME")
    default = Path(cache) / "ember/screens" if cache else Path("/tmp/ember-screens")
    parser.add_argument(
        "--out", type=Path, default=default, help="output folder (replaced)"
    )
    args = parser.parse_args()
    try:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        setup()
        with tempfile.TemporaryDirectory(prefix="ember-screens-") as temporary:
            env = environment(Path(temporary))
            with patch.dict(os.environ, env):
                app = QApplication([])
                findings = render(app, args.out, env)
        for finding in findings:
            print(finding)
        print(f"Saved {len(KEYS) * len(SIZES) * len(theme.MODES)} images to {args.out}")
        return 1 if findings else 0
    except Exception as error:
        print(f"screens: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
