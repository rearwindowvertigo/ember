"""Find sources a plan cites by reference, and the read files that supply them.

A sandboxed worker reads only its worktree, the repository's git folder and
its read paths; it has no credentials for an issue tracker. A plan that
cites a source by reference, such as `#93`, `owner/repo#93` or a URL, is
read without that source unless a read file supplies it. These checks let
`emb flow --dry-run` and `emb gapcheck` warn about such citations, and let
briefs name the read files a worker should read.

Issue keys such as `ABC-123` are not detected: the pattern also matches
ordinary words such as `UTF-8`, `SHA-256` and `ISO-8601`.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PATTERN = re.compile(
    r"https?://[^\s<>()\[\]{}`'\"]*[^\s<>()\[\]{}`'\".,;:!?]"
    r"|(?<![\w/#&.-])(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#\d+\b"
)
"""A URL, `#<n>` or `<owner>/<repo>#<n>`; an HTML entity such as `&#39;` is not one."""

HEAD_LINES = 20
"""How many first lines of a read file are searched for a reference."""


@dataclass(frozen=True)
class Citation:
    """One reference in one place of a plan."""

    where: str
    """The part of the plan that cites it, such as `task a` or `the context`."""

    reference: str
    """The reference as written."""


def find(where: str, text: str) -> list[Citation]:
    """Return each distinct reference in a text, in order of first appearance.

    Args:
        where: The part of the plan the text is.
        text: The text.

    Returns:
        One citation per distinct reference; empty when there is none.
    """
    seen: dict[str, None] = {}
    for match in PATTERN.finditer(text):
        seen.setdefault(match.group(0))
    return [Citation(where, reference) for reference in seen]


def standing(settings: Mapping[str, Any] | None) -> tuple[str, ...]:
    """Return the `sandbox.read` setting from a choices reply, or nothing.

    Every run reads these paths besides its own, so they supply citations
    and are listed as sources too.

    Args:
        settings: A reply of `choices.read`; `None` when it could not be read.

    Returns:
        The standing read paths, empty when the reply holds none.
    """
    try:
        value = (settings or {})["answers"]["sandbox.read"]["value"]
    except KeyError, TypeError:
        return ()
    return tuple(str(item) for item in value) if isinstance(value, list) else ()


def source_files(read: Iterable[str]) -> list[Path]:
    """Return the readable files a run's read paths give, in order.

    A read path that is a file counts as itself; a folder counts as the
    files directly inside it, sorted by name. Missing paths are left out.

    Args:
        read: The read paths.

    Returns:
        The files, without duplicates.
    """
    result: dict[Path, None] = {}
    for item in read:
        path = Path(item)
        try:
            if path.is_file():
                result.setdefault(path)
            elif path.is_dir():
                for child in sorted(path.iterdir()):
                    if child.is_file():
                        result.setdefault(child)
        except OSError:
            continue
    return list(result)


def _head(path: Path) -> str:
    """Return a file's first lines, or an empty string when it cannot be read."""
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            return "".join(
                line for _, line in zip(range(HEAD_LINES), stream, strict=False)
            )
    except OSError:
        return ""


def _number(reference: str) -> str | None:
    """Return the issue number of a `#<n>` or `<owner>/<repo>#<n>` reference."""
    if reference.startswith(("http://", "https://")):
        return None
    return reference.rpartition("#")[2]


def supplies(path: Path, reference: str) -> bool:
    """Return whether a read file supplies a reference.

    It does when its first lines hold the same whole reference, so `#930`
    does not supply `#93` and a longer URL does not supply its prefix, or,
    for an issue reference, when the file name holds the issue number as its
    own token, such as `issue-93.md` for `#93`.

    Args:
        path: A read file.
        reference: The reference as written.

    Returns:
        Whether the file names the reference.
    """
    if reference in {match.group(0) for match in PATTERN.finditer(_head(path))}:
        return True
    number = _number(reference)
    if number is None:
        return False
    return number in re.split(r"[^0-9]+", path.name)


def uncovered(citations: Iterable[Citation], files: Sequence[Path]) -> list[Citation]:
    """Return the citations no read file supplies, one per reference.

    Args:
        citations: Citations in plan order.
        files: The read files, from `source_files`.

    Returns:
        For each reference no file supplies, its first citation.
    """
    result: dict[str, Citation] = {}
    for citation in citations:
        if citation.reference in result:
            continue
        if not any(supplies(path, citation.reference) for path in files):
            result[citation.reference] = citation
    return list(result.values())


def warning(citation: Citation) -> str:
    """Return the warning text for a citation no read path supplies."""
    return f"{citation.where} cites {citation.reference}, which no read path supplies"


def sources_section(files: Sequence[Path]) -> str | None:
    """Return a brief section naming the read files, or `None` without any.

    Args:
        files: The read files, from `source_files`.

    Returns:
        A `## Sources to read` section listing each file's path.
    """
    if not files:
        return None
    listing = "\n".join(f"- {path}" for path in files)
    return (
        "## Sources to read\n\n"
        "The lead supplied these files for the sources this work cites. "
        "Read them before you start; they are outside your working folder "
        "and readable as given.\n\n" + listing
    )


def append_sources(brief: str, read: Iterable[str]) -> str:
    """Append a run's read paths unless its brief already names sources.

    Args:
        brief: The supplied brief, which may already have a sources section.
        read: The effective read paths, including standing paths and folders.

    Returns:
        The brief with each distinct absolute read path listed, or unchanged
        when there are no paths or a `## Sources to read` heading exists.
    """
    if re.search(r"(?m)^## Sources to read[ \t]*\r?$", brief):
        return brief
    paths = list(dict.fromkeys(Path(item).resolve() for item in read))
    section = sources_section(paths)
    return f"{brief.rstrip()}\n\n{section}\n" if section else brief
