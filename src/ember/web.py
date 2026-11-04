"""What Ember sends with every HTTP request."""

from importlib.metadata import PackageNotFoundError, version


def _user_agent() -> str:
    try:
        return f"ember/{version('ember-app')}"
    except PackageNotFoundError:
        return "ember"


USER_AGENT = _user_agent()
"""The `User-Agent` of every HTTP request Ember makes: `ember/<version>`."""
