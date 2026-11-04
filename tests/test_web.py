from importlib.metadata import version

from ember import web


def test_user_agent_names_ember_and_its_version() -> None:
    assert f"ember/{version('ember-app')}" == web.USER_AGENT
