from ember.engines.diagnostics import output_text


def test_text_blocks_keep_outputs_without_tool_request_inputs() -> None:
    assert (
        output_text(
            {
                "input": {"command": "printf EACCES"},
                "output": [
                    {"type": "text", "text": "cat: ../tools/file: Permission denied"}
                ],
            }
        )
        == "cat: ../tools/file: Permission denied"
    )
