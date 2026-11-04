"""Installing the `ember` skill for Claude Code as a symbolic link.

The link is `$CLAUDE_CONFIG_DIR/skills/ember` when `CLAUDE_CONFIG_DIR` is set
and not empty, otherwise `~/.claude/skills/ember`. Its target is the skill
source: the source checkout's `skill/` folder when Ember runs from a checkout,
otherwise the packaged `/usr/share/ember/skill`. A folder holds an Ember skill
when its `SKILL.md` has `name: ember` in its front matter.

Ember never removes a folder or a file at the link path; it creates, replaces
and removes only symbolic links.

Every function that reads the environment takes an `env` mapping, and every
function that looks for the source takes the candidate folders, so tests pass
their own.
"""

import os
import re
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import ember
from ember import paths

SKILL_NAME = "ember"
"""The skill's name in its front matter, and the link's name."""

SYSTEM_SKILL = Path("/usr/share/ember/skill")
"""Where the package installs the skill."""

CONFIG_DIR_VARIABLE = "CLAUDE_CONFIG_DIR"
"""The variable naming Claude Code's configuration folder."""

PARENT_MODE = 0o755
"""The mode of each parent folder Ember creates, as Claude Code's own."""

_VERSION = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
_VERSION_LINE = re.compile(
    rf"Skill version: (?P<version>{_VERSION})(?: <!-- x-release-please-version -->)?"
)

EX_OK = 0
EX_NOINPUT = 66
EX_CANTCREAT = 73
EX_TEMPFAIL = 75

INSTALLED = "installed"
MISSING = "missing"
OTHER_LINK = "other link"
BROKEN_LINK = "broken link"
FOLDER = "folder"
FILE = "file"

ALREADY_INSTALLED = "already installed"
REMOVED = "removed"
NOT_INSTALLED = "not installed"


class SkillError(Exception):
    """`emb skill` cannot do what was asked.

    Attributes:
        code: The exit code.
        line: The `next:` or `owner:` line that resolves it.
    """

    def __init__(self, code: int, message: str, line: str) -> None:
        """Keep the exit code, the message and the line.

        Args:
            code: The exit code.
            message: What went wrong.
            line: The `next:` or `owner:` line that resolves it.
        """
        super().__init__(message)
        self.code = code
        self.line = line


@dataclass(frozen=True)
class Status:
    """The state of the link path and the skill source.

    Attributes:
        link: The link path.
        state: `installed`, `missing`, `other link`, `broken link`, `folder`
            or `file`.
        target: The absolute resolved destination of the link; `None` for
            anything that is not a link.
        source: The skill source, or `None` when none is found.
        source_reason: Why no source was found; `None` with a source.
    """

    link: Path
    state: str
    target: Path | None
    source: Path | None
    source_reason: str | None

    def to_json(self) -> dict[str, str | None]:
        """Return the status as `emb skill status --json` prints it.

        Returns:
            The fields, with paths as strings.
        """
        return {
            "link": str(self.link),
            "state": self.state,
            "target": _text(self.target),
            "source": _text(self.source),
            "source_reason": self.source_reason,
        }


@dataclass(frozen=True)
class Result:
    """What `install` or `uninstall` did.

    Attributes:
        link: The link path.
        action: `installed`, `already installed`, `removed` or
            `not installed`.
        target: The link's destination: the source for an install, the
            destination read before removal for an uninstall, `None` when
            nothing was there.
    """

    link: Path
    action: str
    target: Path | None

    def to_json(self) -> dict[str, str | None]:
        """Return the result as `--json` prints it.

        Returns:
            The fields, with paths as strings.
        """
        return {
            "link": str(self.link),
            "action": self.action,
            "target": _text(self.target),
        }

    def line(self) -> str:
        """Return the result's line on standard output.

        Returns:
            `installed: <link> -> <source>`, `already installed: <link> ->
            <source>`, `removed: <link>` or `not installed: <link>`.
        """
        if self.action in (INSTALLED, ALREADY_INSTALLED):
            return f"{self.action}: {self.link} -> {self.target}"
        return f"{self.action}: {self.link}"


def _text(path: Path | None) -> str | None:
    return None if path is None else str(path)


def link_path(env: Mapping[str, str] | None = None) -> Path:
    """Return where the skill's link goes.

    Args:
        env: The environment; `None` reads `os.environ`.

    Returns:
        `skills/ember` in `CLAUDE_CONFIG_DIR` when that is set and not empty,
        otherwise `~/.claude/skills/ember`.
    """
    config = (os.environ if env is None else env).get(CONFIG_DIR_VARIABLE, "")
    base = Path(config) if config else paths.home_dir(env) / ".claude"
    return base / "skills" / SKILL_NAME


def checkout_skill() -> Path:
    """Return the `skill/` folder of the source checkout Ember would run from.

    Returns:
        `skill/` two folders above the `ember` package; it exists only when
        Ember runs from a source checkout.
    """
    return Path(ember.__file__).resolve().parents[2] / "skill"


def is_valid_version(version: str) -> bool:
    """Return whether `version` is a release version in `x.y.z` form."""
    return re.fullmatch(_VERSION, version) is not None


def installed_version(env: Mapping[str, str] | None = None) -> str | None:
    """Read the version from the installed skill link's target.

    Args:
        env: The environment; `None` reads `os.environ`.

    Returns:
        The version, or `None` when the link, Ember skill or single version
        line is missing, unreadable or malformed. No source fallback is used.
    """
    link = link_path(env)
    try:
        if not link.is_symlink() or not is_ember_skill(link):
            return None
        text = (link / "SKILL.md").read_text(encoding="utf-8")
    except OSError, UnicodeDecodeError:
        return None
    lines = [line for line in text.splitlines() if line.startswith("Skill version:")]
    if len(lines) != 1:
        return None
    match = _VERSION_LINE.fullmatch(lines[0])
    return None if match is None else match["version"]


def source_candidates() -> tuple[Path, ...]:
    """Return the folders searched for the skill source, in order.

    Returns:
        The checkout's skill, then the packaged one.
    """
    return (checkout_skill(), SYSTEM_SKILL)


def is_ember_skill(folder: Path) -> bool:
    """Return whether a folder holds the Ember skill.

    Args:
        folder: The folder.

    Returns:
        Whether `SKILL.md` in it is a file whose front matter (the lines
        between the first two `---` lines) has `name: ember`.
    """
    try:
        text = (folder / "SKILL.md").read_text(encoding="utf-8")
    except OSError, UnicodeDecodeError:
        return False
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return False
    for line in lines[1:]:
        if line.strip() == "---":
            return False
        key, colon, value = line.partition(":")
        if colon and key.strip() == "name":
            return value.strip().strip("\"'") == SKILL_NAME
    return False


def find_source(candidates: Sequence[Path]) -> Path | None:
    """Return the first candidate that holds the Ember skill.

    Args:
        candidates: The folders to try, in order.

    Returns:
        The candidate as an absolute resolved path, or `None`.
    """
    for candidate in candidates:
        if is_ember_skill(candidate):
            return candidate.resolve()
    return None


def _missing_reason(candidates: Sequence[Path]) -> str:
    tried = ", ".join(str(candidate) for candidate in candidates)
    return f"the skill is missing from {tried}"


def status(env: Mapping[str, str] | None, candidates: Sequence[Path]) -> Status:
    """Read the state of the link path and find the skill source.

    Args:
        env: The environment; `None` reads `os.environ`.
        candidates: The folders to search for the source, in order.

    Returns:
        The status.
    """
    link = link_path(env)
    source = find_source(candidates)
    reason = None if source is not None else _missing_reason(candidates)
    target: Path | None = None
    if link.is_symlink():
        target = Path(os.path.realpath(link))
        if not target.exists():
            state = BROKEN_LINK
        elif source is not None and target == source:
            state = INSTALLED
        else:
            state = OTHER_LINK
    elif link.is_dir():
        state = FOLDER
    elif link.exists():
        state = FILE
    else:
        state = MISSING
    return Status(link, state, target, source, reason)


def _not_a_link(current: Status) -> SkillError:
    kind = "a folder" if current.state == FOLDER else "a file"
    return SkillError(
        EX_TEMPFAIL,
        f"{current.link} is {kind}, not a link; Ember does not remove it",
        f"owner: move or remove {current.link}, then emb skill install",
    )


def _make_parents(folder: Path) -> None:
    """Create `folder` and its missing parents at mode 755."""
    missing = [folder, *folder.parents]
    missing = [item for item in missing if not item.exists()]
    for item in reversed(missing):
        try:
            item.mkdir(mode=PARENT_MODE)
        except FileExistsError:
            continue
        item.chmod(PARENT_MODE)


def install(
    env: Mapping[str, str] | None,
    candidates: Sequence[Path],
    *,
    force: bool = False,
) -> Result:
    """Link the skill into Claude Code's skills folder.

    A missing or broken link is created or replaced. A link to another
    folder is replaced only with `force`. A folder or file is never touched.

    Args:
        env: The environment; `None` reads `os.environ`.
        candidates: The folders to search for the source, in order.
        force: Replace a link to another folder.

    Returns:
        What was done.

    Raises:
        SkillError: No source is found (66), the link cannot be created
            (73), or something else is at the link path (75).
    """
    current = status(env, candidates)
    source = current.source
    if source is None:
        raise SkillError(
            EX_NOINPUT,
            "no skill source found",
            f"owner: reinstall Ember; {current.source_reason}",
        )
    link = current.link
    if current.state == INSTALLED:
        return Result(link, ALREADY_INSTALLED, source)
    if current.state in (FOLDER, FILE):
        raise _not_a_link(current)
    if current.state == OTHER_LINK and not force:
        raise SkillError(
            EX_TEMPFAIL,
            f"{link} links to {current.target}, not to {source}",
            "next: emb skill install --force",
        )
    folder = link.parent
    temporary = folder / f".{SKILL_NAME}.{secrets.token_hex(4)}.tmp"
    try:
        _make_parents(folder)
        os.symlink(source, temporary)
        try:
            os.replace(temporary, link)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise
    except OSError as error:
        detail = error.strerror or str(error)
        raise SkillError(
            EX_CANTCREAT,
            f"cannot create the link {link}: {detail}",
            f"owner: check {folder}: {detail}",
        ) from error
    return Result(link, INSTALLED, source)


def uninstall(env: Mapping[str, str] | None, candidates: Sequence[Path]) -> Result:
    """Remove the skill's link.

    A link is removed when it is installed, broken, or points to a folder
    that holds the Ember skill. No source is needed.

    Args:
        env: The environment; `None` reads `os.environ`.
        candidates: The folders to search for the source, in order.

    Returns:
        What was done.

    Raises:
        SkillError: The link cannot be removed (73), or something other than
            an Ember skill link is at the link path (75).
    """
    current = status(env, candidates)
    link = current.link
    if current.state == MISSING:
        return Result(link, NOT_INSTALLED, None)
    if current.state in (FOLDER, FILE):
        raise _not_a_link(current)
    target = current.target
    if current.state == OTHER_LINK and (target is None or not is_ember_skill(target)):
        raise SkillError(
            EX_TEMPFAIL,
            f"{link} links to {target}, which is not an Ember skill",
            f"owner: move or remove {link}, then emb skill install",
        )
    try:
        link.unlink()
    except OSError as error:
        detail = error.strerror or str(error)
        raise SkillError(
            EX_CANTCREAT,
            f"cannot remove the link {link}: {detail}",
            f"owner: check {link.parent}: {detail}",
        ) from error
    return Result(link, REMOVED, target)


def status_lines(current: Status) -> list[str]:
    """Return the text lines of `emb skill status`.

    Args:
        current: The status.

    Returns:
        One `<field>: <value>` line each for `link`, `state`, `target` and
        `source`.
    """
    source = (
        str(current.source)
        if current.source is not None
        else f"null ({current.source_reason})"
    )
    return [
        f"link: {current.link}",
        f"state: {current.state}",
        f"target: {_text(current.target) or 'null'}",
        f"source: {source}",
    ]
