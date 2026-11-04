from ember.panels import brief


def test_subject_and_exact_brief() -> None:
    question = "\n\n  First line\nSecond line\n\n"
    assert brief.subject("panel-id", question) == "Panel panel-id: First line"
    assert brief.text("panel-id", question) == (
        "Panel panel-id: First line\n\n"
        "You are one of several models answering the same question on your own.\n"
        "Someone else will compare the answers, so answer fully, in your own words.\n"
        "\nRules for this answer:\n"
        "- Put your whole answer in your final message, in Markdown. "
        "Nothing you write to a file is read.\n"
        "- Create, edit or delete no file, and change nothing in git.\n"
        "- Read the repository in your own working folder. "
        "Use the web only if your tools offer it.\n"
        "- Name your sources: a file path, or a URL you opened.\n"
        "- Say plainly where you are unsure or where the evidence is thin.\n"
        "\nThe question:\n\n\n\n  First line\nSecond line\n"
    )
    assert not any(line.startswith("@") for line in brief.RULES.splitlines())
    assert not brief.subject("panel-id", "@hang").startswith("@")
