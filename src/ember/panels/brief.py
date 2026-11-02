"""Build the shared question and rules for every panel member."""

RULES = (
    "You are one of several models answering the same question on your own.\n"
    "Someone else will compare the answers, so answer fully, in your own words.\n"
    "\n"
    "Rules for this answer:\n"
    "- Put your whole answer in your final message, in Markdown. "
    "Nothing you write to a file is read.\n"
    "- Create, edit or delete no file, and change nothing in git.\n"
    "- You may read the files in this checkout. "
    "Use the web only if your tools offer it.\n"
    "- Name your sources: a file path, or a URL you opened.\n"
    "- Say plainly where you are unsure or where the evidence is thin.\n"
)


def subject(panel_id: str, question: str) -> str:
    """Name the panel and the first non-empty question line."""
    first = next((line.strip() for line in question.splitlines() if line.strip()), "")
    return f"Panel {panel_id}: {first}"


def text(panel_id: str, question: str) -> str:
    """Return the subject, rules and original question with one final newline."""
    return f"{subject(panel_id, question)}\n\n{RULES}\nThe question:\n\n" + (
        question.rstrip("\n") + "\n"
    )
