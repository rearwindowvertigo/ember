"""Lead recommendations, benchmark sources and shortlisted worker evidence."""

from contextlib import suppress
from typing import TYPE_CHECKING, cast

from PySide6.QtCore import QFileSystemWatcher, QPointF, QRectF, Qt
from PySide6.QtGui import QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QSpacerItem,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

from ember import benchmark_recommendations, benchmarks, choices
from ember.app import icons, theme, widgets
from ember.app.screens.common import (
    FittingRow,
    TooltipRow,
    align_controls,
    chain_tab_order,
    clear_layout,
    engine_name,
    label,
    screen_header,
    scrolled,
    sentence,
    titled_card,
)
from ember.benchmark_recommendations import RecommendationPoint, RecommendationReport
from ember.benchmark_sources import (
    AA_KEY_URL,
    AA_TERMS_URL,
    AA_URL,
    SOURCES,
    Observation,
)
from ember.engines import models, status

if TYPE_CHECKING:
    from ember.app.context import AppContext

MARGIN = choices.BENCHMARK_SCORE_MARGIN
APPS = choices.BENCHMARK_LEAD_APPS
ONE_PER_MODEL = choices.BENCHMARK_ONE_PER_MODEL


def _plain_label(text: str = "", role: str | None = None) -> QLabel:
    result = label(text, role)
    result.setTextFormat(Qt.TextFormat.PlainText)
    return result


def _set_line(line: QLabel, text: str) -> None:
    line.setText(text)
    line.setVisible(bool(text))


def _section_title(text: str) -> QLabel:
    heading = _plain_label(text)
    heading.setFont(theme.font("heading"))
    return heading


def _number(value: float) -> str:
    return f"{value:.12g}" if value else "0"


def _badge_position(point: QPointF, plot: QRectF, occupied: list[QPointF]) -> QPointF:
    bounds = plot.adjusted(10, 10, -10, -10)
    for dx, dy in (
        (0, -22),
        (32, -22),
        (-32, -22),
        (32, 0),
        (-32, 0),
        (0, 32),
        (32, 32),
        (-32, 32),
    ):
        candidate = QPointF(
            max(bounds.left(), min(bounds.right(), point.x() + dx)),
            max(bounds.top(), min(bounds.bottom(), point.y() + dy)),
        )
        if all(
            abs(candidate.x() - other.x()) >= 20 or abs(candidate.y() - other.y()) >= 20
            for other in occupied
        ):
            return candidate
    return point


class RecommendationChart(QWidget):
    """Plot the shared report's costs, scores, frontier, and recommendations."""

    def __init__(self) -> None:
        """Set up an initially empty chart."""
        super().__init__()
        self.setMinimumHeight(252)
        self.setAccessibleName("Intelligence Index score by USD per Index task")
        self.report: RecommendationReport | None = None
        theme.signals.changed.connect(self.update)

    def set_report(self, report: RecommendationReport) -> None:
        """Show the exact scope and recommendation order in the report."""
        self.report = report
        self.setAccessibleDescription(
            "The chart numbers match the recommendations below."
            if report["recommendations"]
            else report["summary"]
        )
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: ARG002, N802
        """Draw the plot with the current theme."""
        report = self.report
        if report is None or not report["points"]:
            return
        best, cutoff = report["best_score"], report["minimum_score"]
        assert best is not None and cutoff is not None
        cost_max = max(point["task_usd"] for point in report["points"]) or 1.0
        low = min(cutoff, min(point["score"] for point in report["points"]))
        padding = (best - low) * 0.08 or max(abs(best) * 0.05, 1.0)
        score_min, score_max = low - padding, best + padding

        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setFont(theme.font("caption"))
            metrics = painter.fontMetrics()
            height = metrics.height()
            ticks = range(5)
            scores = [
                f"{score_min + (score_max - score_min) * (tick / 4):.4g}"
                for tick in ticks
            ]
            costs = [f"{cost_max * (tick / 4):.4g}" for tick in ticks]
            gutter = theme.SPACE[1]
            left = max(metrics.horizontalAdvance(tick) for tick in scores) + gutter
            right = max(gutter, metrics.horizontalAdvance(costs[-1]) / 2)
            plot = QRectF(self.rect()).adjusted(
                left, height + gutter, -right, -2 * height - gutter
            )
            if plot.width() <= 20 or plot.height() <= 20:
                return

            def y(score: float) -> float:
                return (
                    plot.bottom()
                    - (score - score_min) / (score_max - score_min) * plot.height()
                )

            def position(point: RecommendationPoint) -> QPointF:
                return QPointF(
                    plot.left() + point["task_usd"] / cost_max * plot.width(),
                    y(point["score"]),
                )

            cutoff_y = y(cutoff)
            painter.fillRect(
                QRectF(plot.left(), plot.top(), plot.width(), cutoff_y - plot.top()),
                theme.color("bg-accent"),
            )
            grid = theme.color("border")
            ink = theme.color("text-secondary")
            for tick in ticks:
                x = plot.left() + plot.width() * (tick / 4)
                level = plot.bottom() - plot.height() * (tick / 4)
                painter.setPen(grid)
                painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
                painter.drawLine(
                    QPointF(plot.left(), level), QPointF(plot.right(), level)
                )
                painter.setPen(ink)
                painter.drawText(
                    QPointF(
                        left - gutter - metrics.horizontalAdvance(scores[tick]),
                        level + metrics.ascent() / 2,
                    ),
                    scores[tick],
                )
                painter.drawText(
                    QPointF(
                        x - metrics.horizontalAdvance(costs[tick]) / 2,
                        plot.bottom() + height,
                    ),
                    costs[tick],
                )
            painter.setPen(ink)
            painter.drawText(QPointF(0, metrics.ascent()), "Index score")
            caption = "USD per Index task"
            painter.drawText(
                QPointF(
                    plot.center().x() - metrics.horizontalAdvance(caption) / 2,
                    self.height() - metrics.descent(),
                ),
                caption,
            )

            accent = theme.color("text-accent")
            painter.setPen(QPen(accent, 1, Qt.PenStyle.DotLine))
            painter.drawLine(
                QPointF(plot.left(), cutoff_y), QPointF(plot.right(), cutoff_y)
            )
            painter.setPen(QPen(accent, 2))
            frontier = [position(point) for point in report["frontier"]]
            for start, end in zip(frontier, frontier[1:], strict=False):
                painter.drawLine(start, end)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(theme.color("text-muted"))
            for point in report["points"]:
                painter.drawEllipse(position(point), 3.5, 3.5)
            painter.setBrush(accent)
            for point in frontier:
                painter.drawEllipse(point, 4, 4)

            badges: list[QPointF] = []
            for index, point in enumerate(report["recommendations"], 1):
                anchor = position(point)
                badge = _badge_position(anchor, plot, badges)
                badges.append(badge)
                painter.setPen(accent)
                painter.drawLine(anchor, badge)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawEllipse(badge, 9, 9)
                painter.setPen(theme.color("bg-card"))
                painter.drawText(
                    QRectF(badge.x() - 9, badge.y() - 9, 18, 18),
                    Qt.AlignmentFlag.AlignCenter,
                    str(index),
                )
        finally:
            painter.end()


def _fill_table(layout: QGridLayout, points: list[RecommendationPoint]) -> None:
    clear_layout(layout)
    for column, title in enumerate(
        ("Release / effort", "Score", "Gap to best", "USD / Index task")
    ):
        heading = _plain_label(title, "secondary")
        if column:
            heading.setAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(heading, 0, column)

    row = 1
    for index, point in enumerate(points, 1):
        release = _plain_label(f"{index}. {point['model']}")
        release.setFont(theme.font("label"))
        measured = [point["effort"], point["configuration"], point["release_date"]]
        layout.addWidget(release, row, 0)
        layout.addWidget(
            _plain_label(" · ".join(part for part in measured if part), "secondary"),
            row + 1,
            0,
            1,
            4,
        )
        for column, metric in enumerate(("score", "gap", "task_usd"), 1):
            value = _plain_label(_number(point[metric]))
            value.setAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            layout.addWidget(value, row, column)
        row += 2
        for route in point["routes"]:
            alias = " (family selector)" if route["is_alias"] else ""
            app = engine_name(route["app"])
            text = f"{app} · {route['model_id']} @ {route['effort']}{alias}"
            route_line = _plain_label(text, "secondary")
            route_line.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            layout.addWidget(route_line, row, 0, 1, 4)
            row += 1
        layout.addItem(QSpacerItem(0, theme.SPACE[1]), row, 0)
        row += 1


def _observation_line(
    source: benchmarks.SourceStatus, observation: Observation
) -> QLabel:
    metrics = [
        f"{metric['title']} {_number(observation['metrics'][key])}"
        for key, metric in source["metrics"].items()
        if key in observation["metrics"]
    ]
    setup = (
        observation["model"],
        observation["effort"] or "Unknown effort",
        observation["harness"] or "Unknown harness",
        observation.get("configuration"),
    )
    line = _plain_label(
        " · ".join((source["title"], *metrics))
        + "\n"
        + " · ".join(part for part in setup if part),
        "secondary",
    )
    line.setToolTip(
        "\n".join(
            filter(
                None,
                (
                    source["url"],
                    source["publisher"],
                    benchmarks.source_line(source, show_edition=True),
                    source["pricing_basis"],
                ),
            )
        )
    )
    return line


def _worker_title(name: str, worker: benchmarks.WorkerEvidence) -> QLabel:
    engine, _, model = name.partition(":")
    title = f"{engine_name(engine)} · {model}"
    if effort := worker["effort"]:
        title += f" · Preferred effort {effort}"
    heading = _plain_label(title)
    heading.setFont(theme.font("label"))
    return heading


class BenchmarksScreen(QWidget):
    """The saved lead settings, benchmark measurements, and explicit refresh."""

    TITLE = "Benchmarks"

    def __init__(self, context: AppContext) -> None:
        """Build the Benchmarks page and read its saved inputs in the background."""
        super().__init__()
        self.context = context
        self._confirmed = self._context_settings()
        self._pending: dict[str, tuple[object, object]] = {}
        self._connected: list[str] = []
        self._rank_current = False
        self._rank_failed = False
        self._worker_report: benchmarks.Report | None = None
        self._reading = False
        self._read_again = False
        self._refreshing = False
        self._saving_key = False
        self._watcher = QFileSystemWatcher(self)
        self._watcher.directoryChanged.connect(self._watch_changed)
        self._watcher.fileChanged.connect(self._watch_changed)

        layout = scrolled(self)
        self.refresh_button = widgets.button("Refresh benchmarks", size="sm")
        self.refresh_button.clicked.connect(self._refresh)
        screen_header(
            layout,
            self.TITLE,
            "Lead recommendations and worker benchmark evidence.",
            trailing=(self.refresh_button,),
        )
        self.refresh_status = _plain_label("", "secondary")
        layout.addWidget(self.refresh_status)
        self.refresh_status.hide()

        layout.addWidget(self._lead_card())
        layout.addWidget(self._analysis_card())
        public_card, public_layout = titled_card("Public sources")
        self.public_status = _plain_label("Reading saved source statuses…", "secondary")
        public_layout.addWidget(self.public_status)
        self.public_sources_body = QVBoxLayout()
        public_layout.addLayout(self.public_sources_body)
        layout.addWidget(public_card)
        layout.addWidget(self._worker_card())
        layout.addStretch(1)

        align_controls(self._lead_rows)
        order: list[QWidget] = [self.refresh_button]
        for row in self._lead_rows:
            order.append(row.info)
            if row.control is self._app_checklist:
                order.extend(self.lead_apps.values())
            else:
                order.append(row.control)
        order.extend(
            (
                self.open_engines,
                self.aa_key,
                self.aa_remove,
                self.aa_matrix,
                self.related,
            )
        )
        chain_tab_order(order)

        context.settings_changed.connect(self._settings_changed)
        context.engines_changed.connect(self._reload)
        self._draw_lead_controls()
        self._draw_rank()
        self._draw_index()
        self._reload()

    def _lead_card(self) -> QWidget:
        card, layout = titled_card("Lead recommendations")
        self.margin = widgets.Picker()
        for value in choices.BENCHMARK_SCORE_MARGINS:
            self.margin.add_option(value, value)
        self.margin.setAccessibleName("Points below the best score")
        self.margin.activated.connect(self._pick_margin)

        self._app_checklist = QWidget()
        apps_layout = QVBoxLayout(self._app_checklist)
        apps_layout.setContentsMargins(0, 0, 0, 0)
        apps_layout.setSpacing(theme.SPACE[0])
        self.lead_apps = {"all": QCheckBox("All connected apps")}
        self.lead_apps.update(
            {app: QCheckBox(engine_name(app)) for app in status.ENGINES}
        )
        for app, box in self.lead_apps.items():
            box.clicked.connect(lambda checked, app=app: self._pick_app(app, checked))
            apps_layout.addWidget(box)

        self.one_per_model = widgets.Switch()
        self.one_per_model.setAccessibleName("One result per model")
        self.one_per_model.clicked.connect(
            lambda checked: self._save_setting(
                ONE_PER_MODEL, "on" if checked else "off"
            )
        )
        self._lead_rows = (
            TooltipRow(
                "Points below the best score",
                "The cutoff is this many Index points below the best comparable score.",
                self.margin,
            ),
            TooltipRow(
                "Lead apps",
                "All includes apps connected later. Clear an app to save a fixed list.",
                self._app_checklist,
            ),
            TooltipRow(
                "One result per model",
                "Keep the lowest-cost qualifying effort for each tested release.",
                self.one_per_model,
            ),
        )
        for row in self._lead_rows:
            layout.addWidget(row)

        self.rank_status = _plain_label("", "secondary")
        layout.addWidget(self.rank_status)
        self.open_engines = widgets.button("Open Engines", size="sm")
        self.open_engines.clicked.connect(lambda: self.context.show_screen("engines"))
        layout.addWidget(self.open_engines, 0, Qt.AlignmentFlag.AlignLeft)

        self._result = QWidget()
        result = QVBoxLayout(self._result)
        result.setContentsMargins(0, 0, 0, 0)
        result.setSpacing(theme.SPACE[2])
        self.chart = RecommendationChart()
        result.addWidget(self.chart)
        result.addWidget(
            _plain_label(
                "The line connects the cost and score frontier. "
                "Shading shows scores at or above the cutoff.",
                "secondary",
            )
        )
        self.recommendation_table = QGridLayout()
        self.recommendation_table.setHorizontalSpacing(theme.SPACE[3])
        self.recommendation_table.setVerticalSpacing(theme.SPACE[0])
        self.recommendation_table.setColumnStretch(0, 1)
        result.addLayout(self.recommendation_table)
        result.addWidget(
            _plain_label(
                "USD per Index task is the publisher's weighted API inference cost. "
                "Subscription and job charges are separate.",
                "secondary",
            )
        )
        layout.addWidget(self._result)
        return card

    def _analysis_card(self) -> QWidget:
        card, layout = titled_card("Artificial Analysis")
        self.aa_status = _plain_label("", "secondary")
        layout.addWidget(self.aa_status)
        self.aa_key = widgets.button("Add API key", size="sm")
        self.aa_key.setAccessibleName("Set Artificial Analysis API key")
        self.aa_key.clicked.connect(self._enter_key)
        self.aa_remove = widgets.button("Remove key", size="sm")
        self.aa_remove.setAccessibleName("Remove Artificial Analysis API key")
        self.aa_remove.clicked.connect(lambda: self._save_key(None))
        self.aa_matrix = widgets.button("View effort matrix", size="sm")
        self.aa_matrix.clicked.connect(self._show_aa_matrix)
        actions = FittingRow((self.aa_key, self.aa_remove, self.aa_matrix))
        actions.box.addStretch(1)
        layout.addWidget(actions)
        return card

    def _worker_card(self) -> QWidget:
        card, layout = titled_card("Worker evidence")
        self.benchmark_summary = _plain_label(
            "Reading saved worker evidence…", "secondary"
        )
        layout.addWidget(self.benchmark_summary)
        self._worker_details = QVBoxLayout()
        self._worker_details.setSpacing(theme.SPACE[1])
        layout.addLayout(self._worker_details)

        self.related = widgets.button("Related results", "ghost", size="sm")
        self.related.setCheckable(True)
        self.related.toggled.connect(self._toggle_related)
        self.related.hide()
        layout.addWidget(self.related, 0, Qt.AlignmentFlag.AlignLeft)
        self._related_results = QWidget()
        self._related_layout = QVBoxLayout(self._related_results)
        self._related_layout.setContentsMargins(theme.SPACE[2], 0, 0, 0)
        self._related_layout.setSpacing(theme.SPACE[1])
        layout.addWidget(self._related_results)
        self._toggle_related(False)
        theme.signals.changed.connect(self._draw_related_icon)
        return card

    def shown(self) -> None:
        """Read saved inputs again when the window shows this screen."""
        self._reload()

    def _context_settings(self) -> dict[str, object]:
        return {
            key: self.context.setting(key)
            if self.context.settings_ready()
            else choices.KEYS[key].default(self.context.env)
            for key in (MARGIN, APPS, ONE_PER_MODEL)
        }

    def _settings_changed(self) -> None:
        confirmed = self._context_settings()
        if confirmed != self._confirmed:
            self._confirmed = confirmed
            self._rank_current = False
            self._draw_lead_controls()
            self._draw_rank()
        self._reload()

    def _lead_value(self, key: str) -> object:
        pending = self._pending.get(key)
        return self._confirmed[key] if pending is None else pending[1]

    def _draw_lead_controls(self) -> None:
        self.margin.set_value(self._lead_value(MARGIN))
        self.one_per_model.setChecked(self._lead_value(ONE_PER_MODEL) == "on")
        selected = cast(list[str], self._lead_value(APPS))
        all_apps = selected == ["all"]
        self.lead_apps["all"].setChecked(all_apps)
        for app in status.ENGINES:
            box = self.lead_apps[app]
            available = app in self._connected
            included = app in selected
            name = engine_name(app)
            box.setText(name if available else f"{name} (unavailable)")
            box.setChecked(available if all_apps else included)
            if not available and not included and box.hasFocus():
                self.lead_apps["all"].setFocus(Qt.FocusReason.OtherFocusReason)
            box.setVisible(available or included)

    def _pick_margin(self, _index: int) -> None:
        if (value := self.margin.value()) is not None:
            self._save_setting(MARGIN, value)

    def _pick_app(self, app: str, checked: bool) -> None:
        selected = cast(list[str], self._lead_value(APPS))
        if app == "all":
            self._save_setting(APPS, ["all"] if checked else list(self._connected))
            return
        apps = set(self._connected if selected == ["all"] else selected)
        if checked:
            apps.add(app)
        else:
            apps.discard(app)
        self._save_setting(APPS, [name for name in status.ENGINES if name in apps])

    def _save_setting(self, key: str, value: object) -> None:
        if self._lead_value(key) == value:
            return
        token = object()
        self._pending[key] = (token, value)
        self._draw_lead_controls()
        self._draw_rank()
        self.context.save_settings(
            {key: value},
            result=lambda result: self._setting_saved(key, token, result),
        )

    def _setting_saved(self, key: str, token: object, result: object) -> None:
        if not isValid(self):
            return
        pending = self._pending.get(key)
        if pending is not None and pending[0] is token:
            del self._pending[key]
        if isinstance(result, Exception):
            self._rank_current = False
            self._reload()
        self._draw_lead_controls()
        self._draw_rank()

    def _watch_changed(self, path: str) -> None:
        if path in self._watcher.files():
            self._watcher.removePath(path)
        self._reload()

    def _watch_saved_inputs(self) -> None:
        env = self.context.env
        files = (
            benchmarks.cache_path(env),
            status.cache_path(env),
            *(models.cache_path(app, env) for app in status.ENGINES),
        )
        folders = {path.parent for path in files}
        folders.add(benchmarks.artificial_analysis_directory(env))
        # Keep a directory watch through atomic file replacements and creation.
        targets = {
            str(next(p for p in (folder, *folder.parents) if p.is_dir()))
            for folder in folders
        }
        targets.update(str(path) for path in files if path.is_file())
        watched = set(self._watcher.files() + self._watcher.directories())
        if added := targets - watched:
            self._watcher.addPaths(sorted(added))
        if obsolete := watched - targets:
            self._watcher.removePaths(sorted(obsolete))

    def _reload(self) -> None:
        self._watch_saved_inputs()
        if self._reading:
            self._read_again = True
            return
        self._reading = True
        self.context.tasks.run(
            self._read_saved, self._loaded, lambda _error: self._loaded(None)
        )

    def _read_saved(self) -> tuple[RecommendationReport, benchmarks.Report | None]:
        env = self.context.env
        rank = benchmark_recommendations.read(env)
        saved = choices.read(env=env)
        workers = None
        if not saved["source_error"]:
            with suppress(OSError):
                workers = benchmarks.cached(saved["answers"], env)
        return rank, workers

    def _loaded(
        self, saved: tuple[RecommendationReport, benchmarks.Report | None] | None
    ) -> None:
        if not isValid(self):
            return
        self._reading = False
        if self._read_again:
            self._read_again = False
            self._reload()
            return
        self._rank_failed = saved is None
        self._rank_current = saved is not None
        workers = None
        if saved is not None:
            rank, workers = saved
            self._confirmed = {
                MARGIN: str(rank["score_margin"]),
                APPS: rank["selected_apps"],
                ONE_PER_MODEL: "on" if rank["one_per_model"] else "off",
            }
            self._connected = rank["connected_apps"]
            if self.chart.report != rank:
                self.chart.set_report(rank)
                _fill_table(self.recommendation_table, rank["recommendations"])
            self._draw_index()
        if workers is None:
            self.benchmark_summary.setText("Could not read saved worker evidence.")
            _set_line(self.public_status, "Could not read saved source statuses.")
        else:
            self.benchmark_summary.setText(workers["summary"])
            self.public_status.hide()
            if workers != self._worker_report:
                self._worker_report = workers
                self._draw_workers(workers)
        self._draw_lead_controls()
        self._draw_rank()

    def _draw_rank(self) -> None:
        rank = self.chart.report
        show_result = False
        open_engines = False
        if self._pending:
            text = "Saving lead settings…"
        elif self._rank_failed:
            text = "Could not read saved lead recommendations."
        elif not self._rank_current or rank is None:
            text = "Reading saved lead recommendations…"
        else:
            best, cutoff = rank["best_score"], rank["minimum_score"]
            show_result = best is not None and cutoff is not None
            text = (
                f"Best comparable score {_number(best)} · Cutoff {_number(cutoff)}"
                if best is not None and cutoff is not None
                else rank["summary"]
            )
            missing = rank["missing_model_caches"]
            if missing:
                names = ", ".join(engine_name(app) for app in missing)
                detail = f"Refresh model catalogues in Engines for {names}."
                text = (
                    detail
                    if rank["status"] == "missing_model_cache"
                    else f"{text}\n{detail}"
                )
            open_engines = bool(missing) or rank["status"] in (
                "missing_engine_cache",
                "no_connected_apps",
                "no_comparable_pairs",
            )
        self.rank_status.setText(text)
        self._result.setVisible(show_result)
        self.open_engines.setVisible(open_engines)

    def _draw_index(self) -> None:
        rank = self.chart.report
        source = rank["source"] if rank is not None else None
        if source is None:
            text = "Intelligence Index downloads are disabled."
        elif source["result"] == "not_checked":
            text = (
                "API key saved. Refresh benchmarks to download the Intelligence Index."
            )
        else:
            text = benchmarks.source_line(source, show_edition=True)
        self.aa_status.setText(text)
        self.aa_key.setText("Add API key" if source is None else "Replace API key")
        self.aa_remove.setVisible(source is not None)
        self.aa_matrix.setVisible(source is not None)
        self._update_actions()

    def _draw_workers(self, report: benchmarks.Report) -> None:
        clear_layout(self.public_sources_body)
        for key, source in report["sources"].items():
            if key in SOURCES:
                self.public_sources_body.addWidget(
                    _plain_label(
                        benchmarks.source_line(source, show_edition=True),
                        "secondary",
                    )
                )
        clear_layout(self._worker_details)
        clear_layout(self._related_layout)
        shortlist = report["workers"] | report["other_shortlisted_workers"]
        if shortlist:
            self._worker_details.addWidget(_section_title("Exact matches"))
        for name, worker in shortlist.items():
            self._worker_details.addWidget(_worker_title(name, worker))
            if not worker["shortlisted"]:
                self._worker_details.addWidget(
                    _plain_label("This model is off its worker shortlist.", "secondary")
                )
            elif not worker["matches"]:
                self._worker_details.addWidget(
                    _plain_label("No matching benchmark for this setup.", "secondary")
                )
            else:
                for key, observation in worker["matches"].items():
                    source = report["sources"][key]
                    self._worker_details.addWidget(
                        _observation_line(source, observation)
                    )
                    if missing := worker["missing"].get(key):
                        metrics = ", ".join(
                            source["metrics"][metric]["title"] for metric in missing
                        )
                        self._worker_details.addWidget(
                            _plain_label(
                                f"Missing measurements for {metrics}.", "secondary"
                            )
                        )
                if not benchmarks.has_cost(worker):
                    self._worker_details.addWidget(
                        _plain_label(
                            "No matching benchmark cost for this setup.", "secondary"
                        )
                    )
            if worker["related"]:
                self._related_layout.addWidget(_worker_title(name, worker))
                for key, related in worker["related"].items():
                    self._related_layout.addWidget(
                        _observation_line(
                            report["sources"][key], related["observation"]
                        )
                    )
                    self._related_layout.addWidget(
                        _plain_label(f"{sentence(related['reason'])}.", "secondary")
                    )

        references = [
            (name, worker, reference)
            for name, worker in shortlist.items()
            if (reference := worker.get("model_reference")) is not None
        ]
        if references:
            self._worker_details.addWidget(
                _section_title("Artificial Analysis model reference")
            )
        for name, worker, reference in references:
            self._worker_details.addWidget(_worker_title(name, worker))
            effort = worker["effort"] or ""
            if (observation := reference["efforts"].get(effort)) is not None:
                source = report["sources"][reference["source"]]
                self._worker_details.addWidget(_observation_line(source, observation))
            else:
                missing = (
                    "No Index measurement for the preferred effort."
                    if effort
                    else "Set a preferred effort in Engines to view a reference here."
                )
                if release := reference["model"]:
                    missing = f"{release} · {missing}"
                self._worker_details.addWidget(_plain_label(missing, "secondary"))
        has_related = bool(self._related_layout.count())
        self.related.setVisible(has_related)
        if not has_related:
            self.related.setChecked(False)
        self._toggle_related(self.related.isChecked())
        self._update_actions()

    def _toggle_related(self, shown: bool) -> None:
        self._related_results.setVisible(shown)
        self.related.setAccessibleDescription("Expanded" if shown else "Collapsed")
        self._draw_related_icon()

    def _draw_related_icon(self) -> None:
        name = "chevron-down" if self.related.isChecked() else "chevron-right"
        widgets.set_icon(
            self.related, icons.icon(name, theme.color("text-secondary"), 16)
        )

    def _update_actions(self) -> None:
        available = not (self._refreshing or self._saving_key)
        for button in (self.refresh_button, self.aa_key, self.aa_remove):
            button.setEnabled(available)
        report = self._worker_report
        shortlist = (
            (report["workers"] | report["other_shortlisted_workers"]) if report else {}
        )
        self.aa_matrix.setEnabled(
            available
            and any("model_reference" in worker for worker in shortlist.values())
        )

    def _refresh(self) -> None:
        if self._refreshing or self._saving_key:
            return
        self._refreshing = True
        _set_line(self.refresh_status, "Refreshing benchmarks…")
        self._update_actions()
        self.context.tasks.run(
            lambda: benchmarks.refresh(self.context.env),
            self._refreshed,
            lambda error: self._refresh_ended(f"Benchmark refresh failed. {error}"),
        )

    def _refreshed(self, report: benchmarks.Report) -> None:
        match report["status"]:
            case "ok":
                text = "Benchmark refresh complete."
            case "partial":
                text = (
                    "Benchmark refresh finished with errors. See the source statuses."
                )
            case "retained":
                text = report["summary"]
            case "empty_pool":
                text = (
                    "Add an Artificial Analysis API key or shortlist worker models "
                    "in Engines to refresh benchmarks."
                )
            case _:
                text = (
                    "Benchmark sources were unavailable. See the source statuses below."
                )
        self._refresh_ended(text)

    def _refresh_ended(self, text: str) -> None:
        if not isValid(self):
            return
        self._refreshing = False
        _set_line(self.refresh_status, text)
        self._update_actions()
        self._reload()

    def _enter_key(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Artificial Analysis API key")
        dialog.setMinimumWidth(440)
        layout = QVBoxLayout(dialog)
        margin = theme.SPACE[5]
        layout.setContentsMargins(margin, margin, margin, margin)
        layout.addWidget(label("Use your own free API key for local benchmark data."))
        links = label(
            f'<a href="{AA_KEY_URL}">Get a free API key</a> · '
            f'<a href="{AA_TERMS_URL}">Data Platform Terms</a>'
        )
        links.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        links.setOpenExternalLinks(True)
        layout.addWidget(links)
        key = QLineEdit()
        key.setAccessibleName("Artificial Analysis API key")
        key.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(key)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = widgets.button("Cancel")
        save = widgets.button("Save", "primary")
        cancel.clicked.connect(dialog.reject)
        save.clicked.connect(dialog.accept)
        save.setEnabled(False)
        key.textChanged.connect(lambda text: save.setEnabled(bool(text.strip())))
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout.addLayout(buttons)
        key.setFocus()
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._save_key(key.text())
        key.clear()
        dialog.deleteLater()

    def _save_key(self, key: str | None) -> None:
        if self._saving_key or self._refreshing:
            return
        self._saving_key = True
        self._update_actions()
        self.context.tasks.run(
            lambda: benchmarks.configure_artificial_analysis(key, self.context.env),
            lambda _result: self._key_saved(),
            self._key_saved,
        )

    def _key_saved(self, error: Exception | None = None) -> None:
        if not isValid(self):
            return
        self._saving_key = False
        if error is not None:
            self.context.error.emit(
                f"Could not save the Artificial Analysis key. {error}"
            )
        self._update_actions()
        self._reload()

    def _show_aa_matrix(self) -> None:
        report = self._worker_report
        if report is None:
            return
        shortlist = report["workers"] | report["other_shortlisted_workers"]
        references = [
            (name, worker["effort"], reference)
            for name, worker in shortlist.items()
            if (reference := worker.get("model_reference")) is not None
        ]
        if not references:
            return
        source = report["sources"][references[0][2]["source"]]
        dialog = QDialog(self)
        dialog.setWindowTitle("Artificial Analysis effort matrix")
        dialog.resize(830, 550)
        layout = QVBoxLayout(dialog)
        margin = theme.SPACE[5]
        layout.setContentsMargins(margin, margin, margin, margin)
        subtitle = label(
            f'<a href="{AA_URL}">Artificial Analysis Intelligence Index</a>'
            f" · Edition {source['edition']}"
        )
        subtitle.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        subtitle.setOpenExternalLinks(True)
        layout.addWidget(subtitle)
        layout.addWidget(
            label(
                "Model-level references. Unavailable results are blank. "
                "Fixed Team choices use the preferred effort. Automatic choices "
                "use models in the Routing pool.",
                "secondary",
            )
        )
        if any(not ref["effort_catalog_available"] for _, _, ref in references):
            layout.addWidget(
                label("Some effort lists are missing. Refresh model lists in Engines.")
            )
        headings = (
            "Shortlisted model",
            "Tested release",
            "Effort",
            "Index score",
            "USD / task",
        )
        table = QTableWidget(0, len(headings))
        table.setAccessibleName("Model references by release and effort")
        table.setHorizontalHeaderLabels(headings)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.verticalHeader().hide()
        header = table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for name, preferred, reference in references:
            for effort, observation in (reference["efforts"] or {"": None}).items():
                metrics = {} if observation is None else observation["metrics"]
                score = metrics.get("intelligence_index")
                cost = metrics.get("task_usd")
                release = reference["model"] or ""
                if observation and (configuration := observation.get("configuration")):
                    release += f" · {configuration}"
                cells = (
                    name,
                    release,
                    f"{effort} (preferred)" if effort == preferred else effort,
                    "" if score is None else f"{score:g}",
                    "" if cost is None else f"{cost:.4f}",
                )
                index = table.rowCount()
                table.insertRow(index)
                for column, text in enumerate(cells):
                    item = QTableWidgetItem(text)
                    item.setToolTip(text)
                    table.setItem(index, column, item)
        layout.addWidget(table)
        close = widgets.button("Close", "secondary")
        close.clicked.connect(dialog.accept)
        layout.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)
        dialog.exec()
        dialog.deleteLater()
