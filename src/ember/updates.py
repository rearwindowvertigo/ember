"""Read release versions from Ember's apt repository."""

import os
import re
from collections.abc import Mapping

from ember import deps

PACKAGES_URL = (
    "https://rearwindowvertigo.github.io/ember/apt/dists/stable/main/"
    "binary-amd64/Packages"
)
"""The Packages index for Ember's stable amd64 releases."""

_STANZA_BREAK = re.compile(r"\n(?:[ \t]*\n)+")
_FIELDS = re.compile(r"^(package|version):([^\n]*)", re.IGNORECASE | re.MULTILINE)


def parse_packages(
    text: str,
    package: str = "ember",
    *,
    env: Mapping[str, str] | None = None,
    deadline: float = deps.TIMEOUT,
) -> str:
    """Return the highest version of `package` in a Packages index.

    Args:
        text: The index in Debian control-file format.
        package: The exact binary package name to match.
        env: The environment for `dpkg`; `None` uses `os.environ`.
        deadline: Seconds each `dpkg` command may run.

    Returns:
        The highest Version field in Debian version order.

    Raises:
        deps.ReadError: The package's version is absent, invalid or unreadable.
    """
    env = os.environ if env is None else env
    newest = None
    for stanza in _STANZA_BREAK.split(text.replace("\r\n", "\n")):
        fields = {
            match[1].lower(): match[2].strip() for match in _FIELDS.finditer(stanza)
        }
        if fields.get("package") != package:
            continue
        version = fields.get("version")
        if not version:
            raise deps.ReadError(f"Packages stanza for {package} has no Version")
        deps.validate_debian_version(version, env, deadline)
        if newest is None or deps.debian_older(newest, version, env, deadline):
            newest = version
    if newest is None:
        raise deps.ReadError(f"Packages index contains no stanza for {package}")
    return newest


def newest_version(
    env: Mapping[str, str] | None = None,
    *,
    url: str = PACKAGES_URL,
    package: str = "ember",
    fetch: deps.Fetch | None = None,
    deadline: float = deps.TIMEOUT,
) -> str:
    """Read the highest version of a package from an apt Packages index.

    Args:
        env: The environment for `dpkg`; `None` uses `os.environ`.
        url: The index URL, by default Ember's stable amd64 index.
        package: The binary package whose version is read.
        fetch: The text fetcher; `None` uses the dependency reader's fetcher.
        deadline: Seconds each `dpkg` command may run.

    Returns:
        The highest Version field in Debian version order.

    Raises:
        deps.ReadError: The index could not be fetched or used.
    """
    text = deps.fetch_version_text(url, deps.fetch_text if fetch is None else fetch)
    try:
        return parse_packages(text, package, env=env, deadline=deadline)
    except deps.ReadError as error:
        raise deps.ReadError(f"{url}: {error}") from None
