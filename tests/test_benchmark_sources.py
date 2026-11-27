import io
import json
from urllib.request import Request

import pytest

from ember import benchmark_sources
from ember.benchmark_sources import SOURCES, SourceError


def test_terminal_bench_reads_all_pages_and_uses_reported_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def download(_url: str, options: dict[str, str | int]) -> str:
        page = options["page"]
        return json.dumps(
            {
                "leaderboard": {"package": options["package"], "name": options["name"]},
                "pagination": {"page": page, "total": 2, "total_pages": 2},
                "rows": [
                    {
                        "id": str(page),
                        "metadata": {
                            "model_display": {"label": "Opus 5.5"},
                            "model_org": {"label": "Anthropic"},
                            "agent_display": {"label": "Claude Code"},
                            "reasoning_effort": "max" if page == 1 else None,
                        },
                        "metrics": {
                            "accuracy": 40 if page == 1 else 0,
                            "n_trials": 10,
                            "total_cost_usd": 20,
                            "successes": 4 if page == 1 else 0,
                        },
                    }
                ],
            }
        )

    monkeypatch.setattr(benchmark_sources, "_download", download)
    first, second = benchmark_sources.read(SOURCES["terminal-bench-4.1"])
    assert first["model"] == "Claude Opus 5.5"
    assert first["metrics"] == {"resolution": 40, "attempt_usd": 2, "pass_usd": 5}
    assert second["metrics"] == {"resolution": 0, "attempt_usd": 2}
    assert second["effort"] is None


def test_truncated_csv_at_a_complete_row_is_a_failed_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    partial = b"model,coding,reasoning\nfirst-model,63,75\n"
    full = partial + b"second-model,31,49\n"

    class Reply(io.BytesIO):
        headers = {"Content-Length": str(len(full))}

    monkeypatch.setattr(
        benchmark_sources.urllib.request,
        "urlopen",
        lambda _request, timeout: Reply(partial),
    )
    with pytest.raises(SourceError, match="download ended before its reported length"):
        benchmark_sources.read(SOURCES["livebench-2026-11-06"])


def test_livebench_can_report_zero_without_filling_a_missing_score(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        benchmark_sources,
        "_download",
        lambda _url: "model,coding,reasoning\nexample-model,0,\n",
    )
    [row] = benchmark_sources.read(SOURCES["livebench-2026-11-06"])
    assert row["metrics"] == {"coding": 0}
    assert row["harness"] is None and row["effort"] is None


def test_ironbee_refuses_to_import_a_different_pr_sample(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        benchmark_sources,
        "_download",
        lambda _url: (
            "model,tier,route,repeats,prs,f1\nexample-model,high,test-route,3,30,0.63\n"
        ),
    )
    [row] = benchmark_sources.read(SOURCES["ironbee-frontier-r123"])
    assert row["metrics"] == {"f1": 63}
    with pytest.raises(SourceError, match="unexpected IronBee PR sample"):
        benchmark_sources.read(SOURCES["ironbee-full-r123"])


def test_atlas_refuses_a_new_edition_at_the_same_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        benchmark_sources,
        "_download",
        lambda _url: "<title>SWE Atlas v1.2 results</title>",
    )
    with pytest.raises(SourceError, match="unexpected SWE Atlas edition"):
        benchmark_sources.read(SOURCES["swe-atlas-qna-1.1"])


def test_ionwarp_imports_model_f1_and_skips_finished_products(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    edition = benchmark_sources.IONWARP_EDITION
    monkeypatch.setattr(
        benchmark_sources,
        "_download",
        lambda _url: (
            "family,snapshot_id,kind,model,effort,f1,prs_graded,prs_in_cohort\n"
            f"code-review,{edition},ionwarp_model,z-ai/glm-5.3-flash,low,0.4275,50,50\n"
            f"code-review,{edition},app,,,0.7,50,50\n"
        ),
    )
    [row] = benchmark_sources.read(SOURCES[f"ionwarp-{edition}"])
    assert row["model"] == "glm-5.3-flash"
    assert row["harness"] == "IonWarp Code"
    assert row["metrics"] == {"f1": pytest.approx(42.75)}


def _aa_model(
    name: str, score: float | None, cost: float | None, *, creator: str
) -> dict[str, object]:
    return {
        "id": name,
        "name": name,
        "model_creator": {"name": creator},
        "release_date": "2026-10-17",
        "evaluations": {"artificial_analysis_intelligence_index": score},
        "artificial_analysis_intelligence_index_cost": {
            "total_cost": 100,
            "cost_per_task": None if cost is None else {"total_cost": cost},
        },
        "pricing": {"price_1m_output_tokens": 20},
    }


def test_artificial_analysis_free_pages_use_only_published_index_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = [
        [
            _aa_model(
                "Claude Opus 5.5 (High, Default Fallback)",
                58,
                1.3241,
                creator="Anthropic",
            ),
            _aa_model("GPT-6.2 Sol (Low)", 0, None, creator="OpenAI"),
        ],
        [
            _aa_model(
                "DeepSeek V4.1 Flash 1017 (Non-reasoning)", None, 0, creator="DeepSeek"
            ),
            _aa_model("GPT-6.2 Sol (High, Fast)", 65, 2.4, creator="OpenAI"),
        ],
    ]
    requested: list[str] = []

    class Reply(io.BytesIO):
        headers: dict[str, str] = {}

        def geturl(self) -> str:
            return requested[-1]

    def urlopen(request: Request, *, timeout: float) -> Reply:
        page = len(requested) + 1
        assert request.full_url == f"{benchmark_sources.AA_API_URL}?page={page}"
        assert request.get_method() == "GET"
        assert request.get_header("X-api-key") == "fixture-key"
        assert "X-api-key" not in request.headers  # Never forwarded on a redirect.
        requested.append(request.full_url)
        return Reply(
            json.dumps(
                {
                    "tier": "free",
                    "intelligence_index_version": 5,
                    "pagination": {
                        "page": page,
                        "page_size": 2,
                        "total_pages": 2,
                        "has_more": page == 1,
                    },
                    "data": pages[page - 1],
                }
            ).encode()
        )

    monkeypatch.setattr(benchmark_sources.urllib.request, "urlopen", urlopen)
    edition, [opus, sol, deepseek] = benchmark_sources.read_artificial_analysis(
        "fixture-key"
    )
    assert edition == "5.0"
    assert len(requested) == 2
    assert opus["model"] == "Claude Opus 5.5"
    assert opus["harness"] is None and opus["effort"] == "high"
    assert opus["configuration"] == "Default Fallback"
    assert opus["metrics"] == {"intelligence_index": 58, "task_usd": 1.3241}
    assert sol["metrics"] == {"intelligence_index": 0}
    assert deepseek["model"] == "DeepSeek V4.1 Flash 1017"
    assert deepseek["effort"] == "none" and deepseek["metrics"] == {"task_usd": 0}


def test_artificial_analysis_rejects_a_version_change_between_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def download(url: str, *, api_key: str) -> str:
        page = int(url.rsplit("=", 1)[1])
        return json.dumps(
            {
                "intelligence_index_version": 5 if page == 1 else 5.1,
                "pagination": {
                    "page": page,
                    "page_size": 1,
                    "total_pages": 2,
                    "has_more": page == 1,
                },
                "data": [_aa_model("GPT-6.2 Sol (Low)", 40, 0.8, creator="OpenAI")],
            }
        )

    monkeypatch.setattr(benchmark_sources, "_download", download)
    with pytest.raises(SourceError, match="inconsistent Artificial Analysis pages"):
        benchmark_sources.read_artificial_analysis("fixture-key")
