import pytest

from ember import deps, updates


@pytest.mark.parametrize("newest_first", [False, True])
@pytest.mark.parametrize(
    ("older", "newer"),
    [
        ("0.9.0", "0.10.0"),
        ("0.33.0", "0.33.0-1"),
        ("0.33.0~rc1", "0.33.0"),
        ("99.0.0-1", "1:0.33.0-1"),
    ],
)
def test_parse_packages_uses_debian_order(
    older: str, newer: str, newest_first: bool
) -> None:
    first, last = (newer, older) if newest_first else (older, newer)
    index = (
        f"Version: {first}\r\n"
        "package: ember\r\n"
        "Description: Ember\r\n"
        " Version: 99:1.0\r\n"
        "\r\n"
        "Package: ember-skill\r\n"
        "Version: 99:2.0\r\n"
        " \t\r\n"
        f"Package:\tember\r\nVersion: {last}"
    )
    assert updates.parse_packages(index) == newer


@pytest.mark.parametrize(
    ("index", "error"),
    [
        ("", "contains no stanza for ember"),
        ("Package: ember-skill\nVersion: 0.33.0\n", "contains no stanza for ember"),
        ("Package: ember\n\nVersion: 0.33.0\n", "has no Version"),
        ("Package: ember\nVersion: \t\n", "has no Version"),
        ("Package: ember\nVersion: invalid\n", "invalid Debian version"),
    ],
)
def test_parse_packages_reports_unusable_index(index: str, error: str) -> None:
    with pytest.raises(deps.ReadError, match=error):
        updates.parse_packages(index)


def test_newest_version_uses_the_dependency_fetcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, float]] = []

    def fetch(url: str, timeout: float) -> str:
        calls.append((url, timeout))
        return "Package: ember\nVersion: 0.33.0-1\n"

    monkeypatch.setattr(deps, "fetch_text", fetch)
    assert updates.newest_version() == "0.33.0-1"
    assert calls == [(updates.PACKAGES_URL, deps.TIMEOUT)]
