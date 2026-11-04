import http.client
import json
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from ember.engines import models, status
from ember.engines.models import Model

T0 = 1_800_000_000.0
HOUR = 60 * 60


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    return {
        "HOME": str(home),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "PATH": str(bin_dir),
    }


CODEX_REPLY = {
    "models": [
        {
            "slug": "gpt-5",
            "display_name": "GPT-5",
            "visibility": "list",
            "supported_reasoning_levels": [
                {"effort": "low"},
                {"effort": "high"},
                {"nope": 1},
            ],
            "default_reasoning_level": "high",
        },
        {"slug": "hidden", "visibility": "hide"},
        {"slug": "plain", "visibility": "list"},
        "junk",
    ]
}


def _codex(env: dict[str, str], tmp_path: Path, reply: object = CODEX_REPLY) -> Path:
    data = tmp_path / "codex-models.json"
    data.write_text(json.dumps(reply))
    calls = tmp_path / "codex-calls"
    stub = Path(env["PATH"]) / "codex"
    stub.write_text(
        f'#!/bin/sh\necho "$@" >> {calls}\n'
        f'[ "$1 $2" = "debug models" ] || exit 9\nexec /bin/cat {data}\n'
    )
    stub.chmod(0o755)
    return calls


def _calls(calls: Path) -> int:
    return len(calls.read_text().splitlines()) if calls.exists() else 0


OPENROUTER_REPLY = {
    "data": [
        {
            "id": "openai/gpt-4o",
            "name": "OpenAI: GPT-4o",
            "context_length": 128000,
            "pricing": {"prompt": "0.0000025", "completion": "0.00001"},
            "supported_parameters": ["tools", "temperature"],
            "reasoning": {
                "supported_efforts": ["low", "high"],
                "default_effort": "low",
            },
        },
        {
            "id": "qwen/qwen3:free",
            "name": "Qwen3 (free)",
            "context_length": 32000,
            "pricing": {"prompt": "0", "completion": "0"},
            "supported_parameters": ["temperature"],
        },
        {"id": "mystery/model"},
        {"id": "openai/gpt-4o", "name": "duplicate"},
        {"id": "no-slash"},
        {"id": "bad id/x"},
        {"name": "no id"},
    ]
}


class Fetcher:
    def __init__(self, reply: object = OPENROUTER_REPLY) -> None:
        self.reply = reply
        self.calls: list[tuple[str, float]] = []

    def __call__(self, url: str, timeout: float) -> Any:  # noqa: ANN401
        self.calls.append((url, timeout))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


# claude


def test_claude_models_are_the_cli_aliases(env: dict[str, str]) -> None:
    result = models.list_models("claude", env, now=T0)
    assert result.source == "cli"
    assert result.fetched_at == T0
    assert result.reason is None
    assert [model.id for model in result.models] == ["fable", "opus", "sonnet", "haiku"]
    assert result.models[0].efforts == ("low", "medium", "high", "xhigh", "max")


def test_unknown_engine_raises(env: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="unknown engine"):
        models.list_models("gpt", env)


@pytest.mark.parametrize("engine", ["codex", "opencode"])
@pytest.mark.parametrize("cache_state", ["missing", "current", "expired"])
def test_without_cache_writes_preserves_discovery_and_existing_caches(
    env: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    engine: str,
    cache_state: str,
) -> None:
    calls = _codex(env, tmp_path)
    fetch = Fetcher()
    path = models.cache_path(engine, env)
    if cache_state != "missing":
        models.list_models(engine, env, fetch=fetch, now=T0)
    before = (path.read_bytes(), path.stat().st_mtime_ns) if path.exists() else None

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("read-only lookup attempted a cache write")

    monkeypatch.setattr(status, "save_cache_file", forbidden)
    at = T0 + 1 if cache_state == "current" else T0 + models.MAX_AGE
    with models.without_cache_writes():
        result = models.list_models(engine, env, fetch=fetch, now=at)
    assert result.reason is None
    assert result.models
    assert result.source == (
        "cache"
        if cache_state == "current"
        else "cli"
        if engine == "codex"
        else "openrouter"
    )
    probes = _calls(calls) if engine == "codex" else len(fetch.calls)
    assert probes == (2 if cache_state == "expired" else 1)
    if before is None:
        assert not path.parent.exists()
    else:
        assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_without_cache_writes_restores_policy_after_nested_failure(
    env: dict[str, str],
) -> None:
    fetch = Fetcher()
    with pytest.raises(RuntimeError, match="stop"), models.without_cache_writes():
        with models.without_cache_writes():
            models.list_models("opencode", env, fetch=fetch, now=T0)
        models.list_models("opencode", env, fetch=fetch, now=T0)
        assert not models.cache_path("opencode", env).parent.exists()
        raise RuntimeError("stop")
    models.list_models("opencode", env, fetch=fetch, now=T0)
    assert models.cache_path("opencode", env).exists()
    assert len(fetch.calls) == 3


def test_without_cache_writes_leaves_other_threads_policy_unchanged(
    env: dict[str, str],
) -> None:
    path = models.cache_path("opencode", env)
    with models.without_cache_writes(), ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(
            models.list_models, "opencode", env, fetch=Fetcher(), now=T0
        ).result()
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        models.list_models("opencode", env, fetch=Fetcher(), refresh=True, now=T0 + 1)
        assert (path.read_bytes(), path.stat().st_mtime_ns) == before


# codex


def test_codex_list_from_cli_is_cached(env: dict[str, str], tmp_path: Path) -> None:
    calls = _codex(env, tmp_path)
    result = models.list_models("codex", env, now=T0)
    assert result.source == "cli"
    assert result.fetched_at == T0
    assert result.reason is None
    assert result.models == (
        Model("gpt-5", "GPT-5", ("low", "high"), "high"),
        Model("plain", "plain"),
    )
    path = models.cache_path("codex", env)
    assert path.name == "models-codex.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    cached = models.list_models("codex", env, now=T0 + 6 * HOUR - 1)
    assert cached.source == "cache"
    assert cached.models == result.models
    assert cached.fetched_at == T0
    assert _calls(calls) == 1


def test_codex_list_is_read_again_after_six_hours(
    env: dict[str, str], tmp_path: Path
) -> None:
    calls = _codex(env, tmp_path)
    models.list_models("codex", env, now=T0)
    result = models.list_models("codex", env, now=T0 + 6 * HOUR)
    assert result.source == "cli"
    assert result.fetched_at == T0 + 6 * HOUR
    assert _calls(calls) == 2


def test_codex_refresh_reads_again(env: dict[str, str], tmp_path: Path) -> None:
    calls = _codex(env, tmp_path)
    models.list_models("codex", env, now=T0)
    result = models.list_models("codex", env, refresh=True, now=T0 + 1)
    assert result.source == "cli"
    assert _calls(calls) == 2


def test_cache_from_the_future_is_read_again(
    env: dict[str, str], tmp_path: Path
) -> None:
    calls = _codex(env, tmp_path)
    models.list_models("codex", env, now=T0)
    models.list_models("codex", env, now=T0 - 10)
    assert _calls(calls) == 2


def test_codex_failure_falls_back_to_cache(env: dict[str, str], tmp_path: Path) -> None:
    _codex(env, tmp_path)
    first = models.list_models("codex", env, now=T0)
    (Path(env["PATH"]) / "codex").write_text("#!/bin/sh\necho 'boom'\nexit 4\n")
    result = models.list_models("codex", env, now=T0 + 7 * HOUR)
    assert result.source == "cache"
    assert result.models == first.models
    assert result.fetched_at == T0
    assert result.reason is not None
    assert "boom" in result.reason
    path = models.cache_path("codex", env)
    assert json.loads(path.read_text())["fetched_at"] == T0


def test_codex_stderr_is_kept_out_of_the_list(
    env: dict[str, str], tmp_path: Path
) -> None:
    data = tmp_path / "codex-models.json"
    data.write_text(json.dumps(CODEX_REPLY))
    stub = Path(env["PATH"]) / "codex"
    stub.write_text(
        f"#!/bin/sh\necho 'WARNING: config key deprecated' >&2\nexec /bin/cat {data}\n"
    )
    stub.chmod(0o755)
    result = models.list_models("codex", env, now=T0)
    assert result.reason is None
    assert result.source == "cli"
    assert [model.id for model in result.models] == [
        model.id for model in models.parse_codex(json.dumps(CODEX_REPLY))
    ]
    assert result.models


def test_codex_failure_reason_reads_stderr(env: dict[str, str], tmp_path: Path) -> None:
    stub = Path(env["PATH"]) / "codex"
    stub.write_text("#!/bin/sh\necho 'partial'\necho 'not signed in' >&2\nexit 3\n")
    stub.chmod(0o755)
    result = models.list_models("codex", env, now=T0)
    assert result.models == ()
    assert result.reason == "codex debug models exited 3: not signed in"


def test_codex_not_installed_without_cache(env: dict[str, str]) -> None:
    result = models.list_models("codex", env, now=T0)
    assert result.models == ()
    assert result.source == "cli"
    assert result.fetched_at is None
    assert result.reason is not None
    assert "not installed" in result.reason
    assert not models.cache_path("codex", env).exists()


def test_codex_unexpected_output_is_a_reason(
    env: dict[str, str], tmp_path: Path
) -> None:
    _codex(env, tmp_path, reply={"something": "else"})
    result = models.list_models("codex", env, now=T0)
    assert result.models == ()
    assert result.reason is not None
    assert "something" in result.reason
    assert not models.cache_path("codex", env).exists()


@pytest.mark.parametrize(
    "content",
    [
        "{broken",
        "[]",
        '{"fetched_at": "x", "models": []}',
        '{"fetched_at": 1.0, "models": [{"id": "x"}]}',
        '{"fetched_at": 1.0, "models": [{"id": "x", "label": "x", "efforts": [1]}]}',
    ],
)
def test_invalid_cache_is_absent(env: dict[str, str], content: str) -> None:
    path = models.cache_path("codex", env)
    path.parent.mkdir(parents=True)
    path.write_text(content)
    result = models.list_models("codex", env, now=T0)
    assert result.models == ()
    assert result.source == "cli"
    assert result.fetched_at is None


# opencode


@pytest.mark.parametrize("version", ["1.19.5", "opencode v2.2.5"])
def test_openrouter_list_does_not_run_opencode(
    env: dict[str, str], tmp_path: Path, version: str
) -> None:
    """Read models from OpenRouter without invoking either OpenCode version."""
    calls = tmp_path / "opencode-calls"
    stub = Path(env["PATH"]) / "opencode"
    stub.write_text(
        f'#!/bin/sh\necho "$*" >> "{calls}"\n'
        f'if [ "$*" = "--version" ]; then echo "{version}"; else exit 9; fi\n'
    )
    stub.chmod(0o755)
    fetch = Fetcher()
    result = models.list_models("opencode", env, fetch=fetch, now=T0)
    assert result.source == "openrouter"
    assert result.reason is None
    assert result.models
    assert fetch.calls == [(models.OPENROUTER_URL, models.TIMEOUT)]
    assert not calls.exists()


def test_openrouter_list(env: dict[str, str]) -> None:
    fetch = Fetcher()
    result = models.list_models("opencode", env, fetch=fetch, now=T0)
    assert fetch.calls == [(models.OPENROUTER_URL, models.TIMEOUT)]
    assert result.source == "openrouter"
    assert result.fetched_at == T0
    assert result.reason is None
    assert [model.id for model in result.models] == [
        "openai/gpt-4o",
        "qwen/qwen3:free",
        "mystery/model",
    ]
    gpt, qwen, mystery = result.models
    assert gpt == Model(
        id="openai/gpt-4o",
        label="OpenAI: GPT-4o",
        efforts=("low", "high"),
        default_effort="low",
        context_length=128000,
        prompt_price=0.0000025,
        completion_price=0.00001,
        supports_tools=True,
    )
    assert qwen.prompt_price == 0.0
    assert qwen.supports_tools is False
    assert mystery == Model("mystery/model", "mystery/model")
    cached = models.list_models("opencode", env, fetch=fetch, now=T0 + 1)
    assert cached.source == "cache"
    assert cached.models == result.models
    assert len(fetch.calls) == 1


def test_openrouter_failure_without_cache(env: dict[str, str]) -> None:
    result = models.list_models(
        "opencode", env, fetch=Fetcher(OSError("no network")), now=T0
    )
    assert result.models == ()
    assert result.source == "openrouter"
    assert result.fetched_at is None
    assert result.reason is not None
    assert "no network" in result.reason
    assert not models.cache_path("opencode", env).exists()


def test_openrouter_failure_with_cache(env: dict[str, str]) -> None:
    first = models.list_models("opencode", env, fetch=Fetcher(), now=T0)
    result = models.list_models(
        "opencode", env, refresh=True, fetch=Fetcher(OSError("no network")), now=T0
    )
    assert result.source == "cache"
    assert result.models == first.models
    assert result.fetched_at == T0
    assert result.reason is not None


def test_openrouter_reply_without_list(env: dict[str, str]) -> None:
    result = models.list_models("opencode", env, fetch=Fetcher({"error": "x"}), now=T0)
    assert result.models == ()
    assert result.reason is not None


def test_model_list_json(env: dict[str, str]) -> None:
    result = models.list_models("opencode", env, fetch=Fetcher(), now=T0)
    data = json.loads(json.dumps(result.to_json()))
    assert set(data) == {"models", "source", "fetched_at", "reason"}
    assert data["models"][0]["id"] == "openai/gpt-4o"
    assert data["models"][0]["efforts"] == ["low", "high"]
    assert data["models"][0]["supports_tools"] is True


@pytest.mark.parametrize(
    ("value", "expected"),
    [("0.000001", 0.000001), (0, 0.0), ("-1", None), ("x", None), (None, None)],
)
def test_price(value: object, expected: float | None) -> None:
    assert models.price(value) == expected


# filter_openrouter

SAMPLE = (
    Model(
        "openai/gpt-4o",
        "GPT-4o",
        context_length=128000,
        prompt_price=0.0000025,
        supports_tools=True,
    ),
    Model(
        "qwen/qwen3:free",
        "Qwen Free",
        context_length=32000,
        prompt_price=0.0,
        supports_tools=False,
    ),
    Model("mystery/model", "Mystery"),
)


def _ids(found: tuple[Model, ...]) -> list[str]:
    return [model.id for model in found]


def test_filter_without_filters_keeps_all() -> None:
    assert models.filter_openrouter(SAMPLE) == SAMPLE
    assert models.filter_openrouter(list(SAMPLE)) == SAMPLE


def test_filter_text_matches_id_or_label_ignoring_case() -> None:
    assert _ids(models.filter_openrouter(SAMPLE, text="OPENAI")) == ["openai/gpt-4o"]
    assert _ids(models.filter_openrouter(SAMPLE, text="free")) == ["qwen/qwen3:free"]
    assert _ids(models.filter_openrouter(SAMPLE, text="mYsT")) == ["mystery/model"]


def test_filter_price_is_inclusive_and_skips_unknown() -> None:
    found = models.filter_openrouter(SAMPLE, max_prompt_price=0.0000025)
    assert _ids(found) == ["openai/gpt-4o", "qwen/qwen3:free"]
    assert _ids(models.filter_openrouter(SAMPLE, max_prompt_price=0)) == [
        "qwen/qwen3:free"
    ]


def test_filter_context_is_inclusive_and_skips_unknown() -> None:
    assert _ids(models.filter_openrouter(SAMPLE, min_context=32000)) == [
        "openai/gpt-4o",
        "qwen/qwen3:free",
    ]
    assert _ids(models.filter_openrouter(SAMPLE, min_context=32001)) == [
        "openai/gpt-4o"
    ]


def test_filter_tools_skips_unknown() -> None:
    assert _ids(models.filter_openrouter(SAMPLE, tools=True)) == ["openai/gpt-4o"]
    assert _ids(models.filter_openrouter(SAMPLE, tools=False)) == ["qwen/qwen3:free"]


def test_filters_combine() -> None:
    found = models.filter_openrouter(SAMPLE, text="o", min_context=100000, tools=True)
    assert _ids(found) == ["openai/gpt-4o"]


def test_truncated_openrouter_response_falls_back_to_cache_reason(
    tmp_path: Path,
) -> None:
    env = {"HOME": str(tmp_path), "PATH": str(tmp_path)}

    def truncated(url: str, timeout: float) -> dict[str, object]:
        raise http.client.IncompleteRead(b"partial")

    result = models.list_models("opencode", env, fetch=truncated, now=1000.0)
    assert result.models == ()
    assert result.reason is not None
    assert result.reason.startswith("OpenRouter model list:")


def test_openrouter_tilde_ids_are_kept() -> None:
    reply = {"data": [{"id": "~openai/gpt-sol-latest", "name": "GPT Sol latest"}]}
    assert [m.id for m in models.parse_openrouter(reply)] == ["~openai/gpt-sol-latest"]
