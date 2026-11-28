#!/usr/bin/env bash
# Generate eval fixtures from the checkout, with paths relative to evals/.
set -eu
mode=$1
out=${2:-}
kit=$(cd "$(dirname "$0")" && pwd)
top=$(cd "$kit/../../.." && pwd)
if [ -z "$out" ]; then
    out=$(mktemp -d "${TMPDIR:-/tmp}/ember-eval-fixtures.XXXXXX")
fi
out=$(realpath -m "$out")
case "$out/" in
    "$top/skill/evals/"*)
        echo "generate.sh: output must be outside skill/evals" >&2
        exit 64 ;;
esac
mkdir -p "$out"
cd "$top"
if [ "$mode" = all ] || [ "$mode" = help ]; then
    mkdir -p "$out/_stubs/replies"
    for command in "" choices engines benchmarks "benchmarks recommend" run gapcheck flow panel ground merge cleanup deps app skill "skill install" "skill uninstall" "skill status"; do
        echo "==> emb${command:+ $command} --help <=="
        if ! COLUMNS=80 NO_COLOR=1 FORCE_COLOR=0 uv run --frozen emb $command --help; then
            echo "generate.sh: help generator (emb $command --help) failed" >&2
            exit 1
        fi
    done > "$out/_stubs/replies/help.out"
fi
if [ "$mode" = all ] || [ "$mode" = replies ]; then
    if ! uv run --frozen python skill/evals/_stubs/make_replies.py --output-dir "$out"; then
        echo "generate.sh: make_replies.py failed" >&2
        exit 1
    fi
fi
printf '%s\n' "$out"
