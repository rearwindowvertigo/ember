#!/usr/bin/env bash
set -eu
bash "$(dirname "$0")/../_stubs/install.sh" "$(dirname "$0")"
# A run already exists: start it while setting up, unlogged and uncounted.
EMBER_EVAL_SETUP=1 EMBER_EVAL_DIR="$PWD/.ember-eval" python3 -I .ember-eval/bin/stub.py run \
    --engine codex --dir . --task-file .ember-eval/seed-brief.md \
    --trailer 'Co-Authored-By: Claude <noreply@anthropic.com>' --claim report-since-01 > /dev/null
