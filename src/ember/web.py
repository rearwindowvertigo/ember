"""What Ember sends with requests to outside websites."""

from importlib.metadata import PackageNotFoundError, version


def _user_agent() -> str:
    try:
        return f"ember/{version('ember-app')}"
    except PackageNotFoundError:
        return "ember"


USER_AGENT = _user_agent()
"""The `User-Agent` of requests to outside websites: `ember/<version>`."""
