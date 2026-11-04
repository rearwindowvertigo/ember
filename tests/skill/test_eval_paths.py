from pathlib import Path


def test_eval_files_use_worktree_placeholders(generated_eval_fixtures: Path) -> None:
    root = Path(__file__).resolve().parents[2] / "skill" / "evals"
    found = [
        str(path.relative_to(folder))
        for folder in (root, generated_eval_fixtures)
        for path in sorted(folder.rglob("*"))
        if path.is_file()
        and "__pycache__" not in path.parts
        and b".ember/worktrees" in path.read_bytes()
    ]
    assert found == [], f"use a stub worktree placeholder in {found}"
