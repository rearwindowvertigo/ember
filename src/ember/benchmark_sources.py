"""Public benchmark downloads, with a separate result series for each test."""

import csv
import http.client
import io
import json
import math
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import partial
from typing import Any, NotRequired, TypedDict

from ember.web import USER_AGENT

TIMEOUT = 10
MAX_BYTES = 2 * 1024 * 1024
HARBOR_URL = "https://api.harborframework.com/functions/v1/leaderboard-read"
LIVEBENCH_RELEASE = "2026-11-06"
LIVEBENCH_ROOT = f"https://livebench.ai/data/{LIVEBENCH_RELEASE.replace('-', '')}"
ATLAS_VERSION = "1.1"
ATLAS_URL = "https://labs.scale.com/embeds/swe-atlas-index.html"
IRONBEE_ROOT = (
    "https://raw.githubusercontent.com/ironbee-ai/code-review-benchmarks/main/reports"
)
IONWARP_EDITION = "onerow4-code-2026-10-06-r1"
IONWARP_URL = (
    "https://raw.githubusercontent.com/Winding-Labs/ai-code-review-benchmark"
    "/main/data/code-review.csv"
)


class Observation(TypedDict):
    """One published model configuration in one benchmark series."""

    model: str
    harness: str | None
    effort: str | None
    metrics: dict[str, float]
    route: NotRequired[str]
    model_release_date: NotRequired[str]
    updated_at: NotRequired[str]
    trials: NotRequired[int]
    passes: NotRequired[int]
    total_usd: NotRequired[float]


class Metric(TypedDict):
    """The display label and benchmark suite for one field."""

    title: str
    suite: str


@dataclass(frozen=True)
class Source:
    """A supported result series and the individual metrics it publishes."""

    title: str
    edition: str
    url: str
    publisher: str
    metrics: dict[str, Metric]
    read: Callable[[], list[Observation]]
    pricing_basis: str | None = None


class SourceError(ValueError):
    """The download did not contain the expected benchmark results."""


def _download(url: str, data: dict[str, str | int] | None = None) -> str:
    headers = {"User-Agent": USER_AGENT}
    body = None
    if data is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(data).encode()
    request = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(request, timeout=TIMEOUT) as reply:
        length = reply.headers.get("Content-Length")
        content = reply.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        raise SourceError("the download exceeded the size limit")
    if length is not None and len(content) != int(length):
        raise SourceError("the download ended before its reported length")
    return content.decode("utf-8-sig")


def _text(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or not value.isprintable()
        or len(value) > 160
    ):
        raise SourceError(f"invalid {field}")
    return value.strip()


def _number(value: object, field: str, maximum: float = math.inf) -> float:
    if isinstance(value, (int, float, str)) and not isinstance(value, bool):
        try:
            number = float(value)
        except OverflowError, ValueError:
            pass
        else:
            if math.isfinite(number) and 0 <= number <= maximum:
                return number
    raise SourceError(f"invalid {field}")


def _count(value: object, field: str) -> int:
    number = _number(value, field, 1_000_000)
    if not number.is_integer():
        raise SourceError(f"invalid {field}")
    return int(number)


def _records(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise SourceError("expected a list of result rows")
    return value


def _csv_rows(url: str, *fields: str) -> Iterator[dict[str, str]]:
    rows = csv.DictReader(io.StringIO(_download(url)), strict=True)
    headers = rows.fieldnames or []
    if len(set(headers)) != len(headers) or not set(fields) <= set(headers):
        raise SourceError("unexpected CSV columns")
    for row in rows:
        if None in row or any(row[field] is None for field in fields):
            raise SourceError("incomplete CSV row")
        yield row


def _model(label: object, vendor: str) -> str:
    model = _text(label, "model name")
    if vendor.casefold() == "anthropic" and re.match(
        r"(?i)^(?:fable|opus|sonnet|haiku)\s+\d", model
    ):
        return f"Claude {model}"
    return model


def _terminal_rows(package: str, board: str) -> Iterator[dict[str, Any]]:
    seen: set[str] = set()
    total = pages = 0
    for page in range(1, 21):
        reply = json.loads(
            _download(
                HARBOR_URL,
                {"package": package, "name": board, "page": page, "page_size": 100},
            )
        )
        metadata = reply["leaderboard"]
        if (metadata["package"], metadata["name"]) != (package, board):
            raise SourceError("unexpected Terminal-Bench leaderboard")
        pagination = reply["pagination"]
        if page == 1:
            total = _count(pagination["total"], "leaderboard size")
            pages = _count(pagination["total_pages"], "leaderboard page count")
        if (
            not 1 <= pages <= 20
            or pagination["page"] != page
            or pagination["total"] != total
            or pagination["total_pages"] != pages
        ):
            raise SourceError("the Terminal-Bench leaderboard changed during download")
        for row in _records(reply["rows"]):
            row_id = _text(row["id"], "leaderboard row id")
            if row_id in seen:
                raise SourceError("duplicate Terminal-Bench leaderboard row")
            seen.add(row_id)
            yield row
        if page == pages:
            if len(seen) != total:
                raise SourceError("incomplete Terminal-Bench leaderboard")
            break


def _terminal(package: str, board: str) -> list[Observation]:
    rows = []
    for row in _terminal_rows(package, board):
        metadata, stats = row["metadata"], row["metrics"]
        trials = _count(stats["n_trials"], "trial count")
        if not trials:
            continue
        effort = metadata.get("reasoning_effort")
        metrics = {"resolution": _number(stats["accuracy"], "resolution rate", 100)}
        result = Observation(
            model=_model(
                metadata["model_display"]["label"], metadata["model_org"]["label"]
            ),
            harness=_text(metadata["agent_display"]["label"], "agent name"),
            effort=None if effort in (None, "") else _text(effort, "reasoning effort"),
            metrics=metrics,
            trials=trials,
        )
        if model_date := metadata.get("release_date") or metadata.get("date"):
            result["model_release_date"] = _text(model_date, "model release date")
        if row.get("updated_at") is not None:
            result["updated_at"] = _text(row["updated_at"], "row update time")
        if stats.get("total_cost_usd") is not None:
            total = _number(stats["total_cost_usd"], "inference cost")
            result["total_usd"] = total
            metrics["attempt_usd"] = total / trials
        if stats.get("successes") is not None:
            passes = _count(stats["successes"], "pass count")
            if passes > trials:
                raise SourceError("pass count exceeds trial count")
            result["passes"] = passes
            if passes and "total_usd" in result:
                metrics["pass_usd"] = result["total_usd"] / passes
        rows.append(result)
    return rows


def _livebench() -> list[Observation]:
    results = []
    for row in _csv_rows(f"{LIVEBENCH_ROOT}/table.csv", "model", "coding", "reasoning"):
        metrics = {
            field: _number(row[field], field, 100)
            for field in ("coding", "reasoning")
            if row[field].strip().casefold() not in ("", "n/a")
        }
        if metrics:
            results.append(
                Observation(
                    model=_text(row["model"], "model id"),
                    harness=None,
                    effort=None,
                    metrics=metrics,
                )
            )
    return results


def _livebench_cost() -> list[Observation]:
    results = []
    for row in _csv_rows(f"{LIVEBENCH_ROOT}/cost.csv", "model", "coding", "nq_coding"):
        if row["coding"].strip().casefold() in ("", "n/a"):
            continue
        trials = _count(row["nq_coding"], "Coding task count")
        if trials:
            total = _number(row["coding"], "Coding inference cost")
            results.append(
                Observation(
                    model=_text(row["model"], "model id"),
                    harness=None,
                    effort=None,
                    metrics={"attempt_usd": total / trials},
                    trials=trials,
                    total_usd=total,
                )
            )
    return results


def _atlas() -> list[Observation]:
    page = _download(ATLAS_URL)
    if f"<title>SWE Atlas v{ATLAS_VERSION} results</title>" not in page:
        raise SourceError("unexpected SWE Atlas edition")
    match = re.search(
        r'<script\b(?=[^>]*\bid="lb-data")(?=[^>]*\btype="application/json")'
        r"[^>]*>(.*?)</script>",
        page,
        re.DOTALL,
    )
    if match is None:
        raise SourceError("SWE Atlas did not publish its results block")
    data = json.loads(match[1])
    if data["tracks"]["qna"] != ["Codebase QnA", 124]:
        raise SourceError("unexpected SWE Atlas QnA task set")
    return [
        Observation(
            model=_model(row["name"], row["provider"]),
            harness=_text(row["agent"], "agent name"),
            effort=_text(row["effort"], "reasoning effort"),
            metrics={"qna": _number(row["qna"]["x"], "QnA score", 100)},
            trials=124,
        )
        for row in _records(data["models"])
        if row["qna"] is not None
    ]


def _ironbee(report: str, attempts: int) -> list[Observation]:
    results = []
    for row in _csv_rows(
        f"{IRONBEE_ROOT}/{report}/configs.csv",
        "model",
        "tier",
        "route",
        "repeats",
        "prs",
        "f1",
    ):
        if _count(row["prs"], "PR count") != attempts or row["repeats"] != "3":
            raise SourceError("unexpected IronBee PR sample")
        results.append(
            Observation(
                model=_text(row["model"], "model id"),
                harness="IronBee review pipeline",
                effort=_text(row["tier"], "effort tier"),
                metrics={"f1": 100 * _number(row["f1"], "F1", 1)},
                route=_text(row["route"], "provider route"),
                trials=attempts,
            )
        )
    return results


def _ionwarp() -> list[Observation]:
    results = []
    for row in _csv_rows(
        IONWARP_URL,
        "family",
        "snapshot_id",
        "kind",
        "model",
        "effort",
        "f1",
        "prs_graded",
        "prs_in_cohort",
    ):
        if row["family"] != "code-review" or row["snapshot_id"] != IONWARP_EDITION:
            raise SourceError("unexpected IonWarp review edition")
        if row["kind"] != "ionwarp_model":
            continue
        if row["prs_graded"] != "50" or row["prs_in_cohort"] != "50":
            raise SourceError("unexpected IonWarp PR sample")
        _, separator, model = row["model"].partition("/")
        if not separator:
            raise SourceError("invalid IonWarp model id")
        results.append(
            Observation(
                model=_text(model, "model id"),
                harness="IonWarp Code",
                effort=_text(row["effort"], "effort"),
                metrics={"f1": 100 * _number(row["f1"], "F1", 1)},
                trials=50,
            )
        )
    return results


_TERMINAL_METRICS = {
    "resolution": Metric(title="Resolved %", suite="Terminal tasks"),
    "attempt_usd": Metric(title="USD / attempt", suite="Terminal tasks"),
    "pass_usd": Metric(title="USD / pass", suite="Terminal tasks"),
}

SOURCES = {
    "terminal-bench-4.1": Source(
        "Terminal-Bench 4.1",
        "4.1",
        "https://www.tbench.ai/?version=4.1",
        "Terminal-Bench",
        _TERMINAL_METRICS,
        partial(_terminal, "terminal-bench/terminal-bench", "4-1-0"),
        "Publisher-reported inference spend across trials",
    ),
    "terminal-bench-2.1": Source(
        "Terminal-Bench 2.1",
        "2.1",
        "https://www.tbench.ai/?version=2.1",
        "Terminal-Bench",
        _TERMINAL_METRICS,
        partial(_terminal, "terminal-bench/terminal-bench-2-1", "main"),
        "Publisher-reported inference spend across trials",
    ),
    f"livebench-{LIVEBENCH_RELEASE}": Source(
        f"LiveBench 2.0 ({LIVEBENCH_RELEASE})",
        LIVEBENCH_RELEASE,
        f"{LIVEBENCH_ROOT}/table.csv",
        "LiveBench",
        {
            "coding": Metric(title="Coding", suite="Coding"),
            "reasoning": Metric(title="Reasoning", suite="Reasoning"),
        },
        _livebench,
    ),
    f"livebench-coding-cost-{LIVEBENCH_RELEASE}": Source(
        f"LiveBench 2.0 Coding cost ({LIVEBENCH_RELEASE})",
        LIVEBENCH_RELEASE,
        f"{LIVEBENCH_ROOT}/cost.csv",
        "LiveBench",
        {"attempt_usd": Metric(title="USD / attempt", suite="Coding")},
        _livebench_cost,
        "Publisher-reported Coding inference spend at API rates",
    ),
    "swe-atlas-qna-1.1": Source(
        "SWE Atlas Codebase QnA v1.1",
        ATLAS_VERSION,
        "https://labs.scale.com/leaderboard/swe-atlas?tab=qna",
        "Scale AI",
        {"qna": Metric(title="QnA score", suite="Codebase QnA")},
        _atlas,
    ),
    "ironbee-full-r123": Source(
        "IronBee Code Review (50 PRs)",
        "full-r123",
        f"{IRONBEE_ROOT}/full-r123/configs.csv",
        "IronBee",
        {"f1": Metric(title="F1 (0–100)", suite="Code review, 50 PRs")},
        partial(_ironbee, "full-r123", 150),
    ),
    "ironbee-frontier-r123": Source(
        "IronBee Code Review (10 PRs)",
        "frontier-r123",
        f"{IRONBEE_ROOT}/frontier-r123/configs.csv",
        "IronBee",
        {"f1": Metric(title="F1 (0–100)", suite="Code review, 10 PRs")},
        partial(_ironbee, "frontier-r123", 30),
    ),
    f"ionwarp-{IONWARP_EDITION}": Source(
        "IonWarp Code Review (2026-10-06)",
        IONWARP_EDITION,
        IONWARP_URL,
        "IonWarp",
        {"f1": Metric(title="F1 (0–100)", suite="Code review, 50 PRs")},
        _ionwarp,
    ),
}


def read(source: Source) -> list[Observation]:
    """Read a complete series or raise a source-specific error."""
    try:
        rows = source.read()
    except urllib.error.HTTPError as error:
        raise SourceError(f"HTTP {error.code}") from error
    except (OSError, http.client.HTTPException) as error:
        raise SourceError("the download failed") from error
    except SourceError:
        raise
    except (
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        IndexError,
        RecursionError,
        csv.Error,
    ) as error:
        raise SourceError("unexpected result format") from error
    if not rows:
        raise SourceError("the download had no results")
    return rows
