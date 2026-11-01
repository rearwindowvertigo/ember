import pytest

from ember import __version__
from ember.cli import main


def test_version_prints_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out == f"emb {__version__}\n"


def test_no_command_prints_help_to_stderr_and_exits_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "usage: emb" in captured.err


def test_help_exits_0(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])
    assert exit_info.value.code == 0
    assert "usage: emb" in capsys.readouterr().out
