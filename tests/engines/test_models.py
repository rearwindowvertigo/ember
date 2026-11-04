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
    stub = bin_dir / "opencode"
    stub.write_text(OPENCODE_STUB)
    stub.chmod(0o755)
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


OPENCODE_STUB = """#!/bin/sh
case "$*" in
--version) echo '1.19.5';;
models) echo 'openrouter/openai/gpt-4o
openrouter/qwen/qwen3:free
mystery/model';;
'auth list'|'auth list --standalone') echo 'OpenRouter api';;
*) exit 9;;
esac
"""

CATALOGUE = {
    "openrouter": {
        "name": "OpenRouter",
        "models": {
            "openai/gpt-4o": {
                "name": "GPT-4o",
                "limit": {"context": 128000},
                "cost": {"input": 2.5, "output": 10},
                "tool_call": True,
                "reasoning_options": [{"type": "effort", "values": ["low", "high"]}],
            },
            "qwen/qwen3:free": {
                "name": "Qwen Free",
                "limit": {"context": 32000},
                "cost": {"input": 0, "output": 0},
                "tool_call": False,
            },
        },
    },
}


class Fetcher:
    def __init__(self, reply: object = CATALOGUE) -> None:
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
        "cache" if cache_state == "current" else "cli" if engine == "codex" else "cli"
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
def test_opencode_commands(env: dict[str, str], tmp_path: Path, version: str) -> None:
    calls = tmp_path / "opencode-calls"
    stub = Path(env["PATH"]) / "opencode"
    stub.write_text(
        OPENCODE_STUB.replace(
            "#!/bin/sh", f'#!/bin/sh\necho "$*" >> "{calls}"'
        ).replace("1.19.5", version)
    )
    result = models.list_models("opencode", env, fetch=Fetcher(), now=T0)
    assert result.reason is None
    assert calls.read_text().splitlines() == [
        "models",
        "--version",
        "auth list --standalone" if version.startswith("opencode v2") else "auth list",
    ]


def test_openrouter_list(env: dict[str, str]) -> None:
    fetch = Fetcher()
    result = models.list_models("opencode", env, fetch=fetch, now=T0)
    assert fetch.calls == [(models.CATALOGUE_URL, models.TIMEOUT)]
    assert result.source == "cli"
    assert result.fetched_at == T0
    assert result.reason is None
    assert [model.id for model in result.models] == [
        "openrouter/openai/gpt-4o",
        "openrouter/qwen/qwen3:free",
        "mystery/model",
    ]
    gpt, qwen, mystery = result.models
    assert gpt == Model(
        id="openrouter/openai/gpt-4o",
        label="GPT-4o",
        provider="openrouter",
        provider_name="OpenRouter",
        login="api",
        efforts=("low", "high"),
        default_effort=None,
        context_length=128000,
        prompt_price=0.0000025,
        completion_price=0.00001,
        supports_tools=True,
    )
    assert qwen.prompt_price == 0.0
    assert qwen.supports_tools is False
    assert mystery == Model(
        "mystery/model", "mystery/model", provider="mystery", provider_name="mystery"
    )
    cached = models.list_models("opencode", env, fetch=fetch, now=T0 + 1)
    assert cached.source == "cache"
    assert cached.models == result.models
    assert len(fetch.calls) == 1


def test_openrouter_failure_without_cache(env: dict[str, str]) -> None:
    result = models.list_models(
        "opencode", env, fetch=Fetcher(OSError("no network")), now=T0
    )
    assert result.models == ()
    assert result.source == "cli"
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


def test_catalogue_reply_without_providers(env: dict[str, str]) -> None:
    result = models.list_models("opencode", env, fetch=Fetcher({"error": "x"}), now=T0)
    assert result.models == ()
    assert result.reason is not None


def test_model_list_json(env: dict[str, str]) -> None:
    result = models.list_models("opencode", env, fetch=Fetcher(), now=T0)
    data = json.loads(json.dumps(result.to_json()))
    assert set(data) == {"models", "integrations", "source", "fetched_at", "reason"}
    assert data["models"][0]["id"] == "openrouter/openai/gpt-4o"
    assert data["models"][0]["efforts"] == ["low", "high"]
    assert data["models"][0]["supports_tools"] is True


# filter_opencode

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
    assert models.filter_opencode(SAMPLE) == SAMPLE
    assert models.filter_opencode(list(SAMPLE)) == SAMPLE


def test_filter_text_matches_id_or_label_ignoring_case() -> None:
    assert _ids(models.filter_opencode(SAMPLE, text="OPENAI")) == ["openai/gpt-4o"]
    assert _ids(models.filter_opencode(SAMPLE, text="free")) == ["qwen/qwen3:free"]
    assert _ids(models.filter_opencode(SAMPLE, text="mYsT")) == ["mystery/model"]


def test_filter_price_is_inclusive_and_skips_unknown() -> None:
    found = models.filter_opencode(SAMPLE, max_prompt_price=0.0000025)
    assert _ids(found) == ["openai/gpt-4o", "qwen/qwen3:free"]
    assert _ids(models.filter_opencode(SAMPLE, max_prompt_price=0)) == [
        "qwen/qwen3:free"
    ]


def test_filter_context_is_inclusive_and_skips_unknown() -> None:
    assert _ids(models.filter_opencode(SAMPLE, min_context=32000)) == [
        "openai/gpt-4o",
        "qwen/qwen3:free",
    ]
    assert _ids(models.filter_opencode(SAMPLE, min_context=32001)) == ["openai/gpt-4o"]


def test_filter_tools_skips_unknown() -> None:
    assert _ids(models.filter_opencode(SAMPLE, tools=True)) == ["openai/gpt-4o"]
    assert _ids(models.filter_opencode(SAMPLE, tools=False)) == ["qwen/qwen3:free"]


def test_filters_combine() -> None:
    found = models.filter_opencode(SAMPLE, text="o", min_context=100000, tools=True)
    assert _ids(found) == ["openai/gpt-4o"]


def test_truncated_catalogue_response_reports_reason(
    env: dict[str, str],
) -> None:

    def truncated(url: str, timeout: float) -> dict[str, object]:
        raise http.client.IncompleteRead(b"partial")

    result = models.list_models("opencode", env, fetch=truncated, now=1000.0)
    assert result.models == ()
    assert result.reason is not None
    assert result.reason.startswith("OpenCode model list:")


def test_opencode_tilde_ids_are_kept() -> None:
    found, _ = models.parse_opencode("openrouter/~openai/gpt-sol-latest", {}, ())
    assert [m.id for m in found] == ["openrouter/~openai/gpt-sol-latest"]


def test_all_integrations_and_empty_provider() -> None:
    catalogue = {
        **CATALOGUE,
        "anthropic": {"name": "Anthropic", "models": {}},
        "openai": {"name": "OpenAI", "models": {}},
        "opencode": {"name": "OpenCode", "models": {}},
        "empty": {"name": "Empty", "models": {}},
    }
    found, integrations = models.parse_opencode(
        "anthropic/claude\nopenai/gpt\nopenrouter/openai/gpt-4o\nopencode/free",
        catalogue,
        (
            ("ANTHROPIC", "oauth"),
            ("OpenAI", "oauth"),
            ("OpenRouter", "api"),
            ("Empty", "api"),
        ),
    )
    assert [p.id for p in integrations] == [
        "anthropic",
        "openai",
        "openrouter",
        "opencode",
        "empty",
    ]
    assert found[0].provider_name == "ANTHROPIC"
    assert found[0].login == "oauth"
    assert found[3].provider_name == "OpenCode"
    assert found[3].login is None
    assert models.filter_opencode(found, provider="openai") == (found[1],)
    assert models.filter_opencode(found, max_prompt_price=0) == found[:2]
    assert models.filter_opencode(found, tools=True) == (found[2],)
    assert models.filter_opencode(found, min_context=1) == (found[2],)
    assert models.filter_opencode(found, provider="empty") == ()


def test_ambiguous_and_unmatched_auth_names_stay_unknown() -> None:
    found, integrations = models.parse_opencode(
        "one/m\ntwo/m\nmissing/m",
        {"one": {"name": "Same", "models": {}}, "two": {"name": "Same", "models": {}}},
        (("Same", "oauth"), ("Unknown Name", "oauth")),
    )
    assert all(model.login is None for model in found)
    assert [p.name for p in integrations] == ["Same", "Same", "missing"]


@pytest.mark.parametrize("failure", ["models", "auth", "auth-output", "catalogue"])
@pytest.mark.parametrize("cached", [False, True])
def test_failed_discovery(env: dict[str, str], failure: str, cached: bool) -> None:
    first = (
        models.list_models("opencode", env, fetch=Fetcher(), now=T0) if cached else None
    )
    path = models.cache_path("opencode", env)
    before = path.read_bytes() if cached else None
    script = OPENCODE_STUB
    if failure == "models":
        script = script.replace("models) echo", "models) exit 3; echo")
    if failure == "auth":
        script = script.replace("echo 'OpenRouter api'", "echo 'unavailable'; exit 4")
    if failure == "auth-output":
        script = script.replace("OpenRouter api", "unreadable")
    (Path(env["PATH"]) / "opencode").write_text(script)
    fetch = Fetcher(OSError("offline")) if failure == "catalogue" else Fetcher()
    result = models.list_models("opencode", env, fetch=fetch, now=T0 + 1, refresh=True)
    assert result.reason
    if first is not None:
        assert result.models == first.models
        assert result.integrations == first.integrations
        assert path.read_bytes() == before
    else:
        assert not path.exists()
        if failure.startswith("auth"):
            assert len(result.models) == 3
            assert all(model.login is None for model in result.models)
        else:
            assert result.models == ()


@pytest.mark.parametrize("fails", [False, True])
def test_legacy_cache_refreshes_before_fallback(
    env: dict[str, str], fails: bool
) -> None:
    path = models.cache_path("opencode", env)
    status.save_cache_file(
        path,
        {"fetched_at": T0, "models": [Model("anthropic/claude-x", "Claude").to_json()]},
        env,
    )
    before = path.read_bytes()
    result = models.list_models(
        "opencode",
        env,
        fetch=Fetcher(OSError("offline")) if fails else Fetcher(),
        now=T0 + 1,
    )
    if fails:
        assert result.source == "cache"
        assert result.models[0].id == "openrouter/anthropic/claude-x"
        assert result.models[0].provider_name == "OpenRouter"
        assert result.models[0].login is None
        assert path.read_bytes() == before
    else:
        assert result.source == "cli"
        assert result.models[0].id == "openrouter/openai/gpt-4o"
        assert json.loads(path.read_text())["format_version"] == models.CACHE_VERSION


def test_empty_models_keep_connected_integrations(env: dict[str, str]) -> None:
    (Path(env["PATH"]) / "opencode").write_text(
        '#!/bin/sh\ncase "$*" in models) exit 0;;\n'
        "--version) echo '1.0.0';; *) echo 'OpenRouter api';; esac\n"
    )
    result = models.list_models("opencode", env, fetch=Fetcher(), now=T0)
    assert result.models == ()
    assert result.reason is None
    assert result.integrations == (
        models.Integration("openrouter", "OpenRouter", "api"),
    )
    assert (
        models.list_models("opencode", env, now=T0 + 1).integrations
        == result.integrations
    )
