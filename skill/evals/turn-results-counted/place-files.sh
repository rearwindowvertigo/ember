#!/usr/bin/env bash
set -eu
bash "$(dirname "$0")/../_stubs/install.sh" "$(dirname "$0")"
python3 - <<'PYTHON'
import json
import os
from pathlib import Path

path = Path(".ember-eval/state.json")
data = Path(os.environ["XDG_DATA_HOME"])
values = {
    "{{DIR}}": str(Path.cwd()),
    "{{WORKTREES}}": str(data / "ember/worktrees"),
}
text = path.read_text()
for key, value in values.items():
    text = text.replace(key, json.dumps(value)[1:-1])
path.write_text(text)
PYTHON
