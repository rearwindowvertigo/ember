"""Text extraction from worker tool results."""


def output_text(value: object) -> str:
    """Read textual output from strings, content blocks and tool result objects."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(output_text(item) for item in value)
    if isinstance(value, dict):
        return "\n".join(
            output_text(value[key])
            for key in (
                "text",
                "content",
                "output",
                "stdout",
                "stderr",
                "aggregated_output",
                "message",
                "error",
                "result",
            )
            if key in value
        )
    return ""
