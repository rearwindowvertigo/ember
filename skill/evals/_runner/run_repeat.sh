#!/usr/bin/env bash
# Run one skill eval case several times, each run in its own out dir:
#   run_repeat.sh [--runs <n>] [--jobs <n>] [--base <git ref>] <case dir> <out dir>
# Each run is run_case.sh with this checkout's skill/ folder, in
# <out dir>/skill/run-<i>. With --base, the skill/ folder at that ref is
# extracted to <out dir>/base-skill/skill and run the same number of times in
# <out dir>/base/run-<i>. Both sides use this checkout's case files, stubs and
# runner. --runs is 1 to 50 (default 3); --jobs is how many runs go at once,
# 1 to 8 (default 1). Prints one line per finished run:
# <side> run-<i> <run_case.sh's summary line>.
set -eu -o pipefail
usage="usage: run_repeat.sh [--runs <n>] [--jobs <n>] [--base <git ref>] <case dir> <out dir>"
fail() { echo "run_repeat.sh: $2" >&2; exit "$1"; }
runs=3; jobs=1; base=""
while [ $# -gt 0 ]; do
    case $1 in
        --runs|--jobs|--base)
            [ $# -ge 2 ] || fail 64 "$1 needs a value"
            case $1 in
                --runs) runs=$2 ;;
                --jobs) jobs=$2 ;;
                --base) base=$2 ;;
            esac
            shift 2 ;;
        -h|--help) echo "$usage"; exit 0 ;;
        -*) echo "$usage" >&2; exit 64 ;;
        *) break ;;
    esac
done
[ $# -eq 2 ] || { echo "$usage" >&2; exit 64; }
[[ $runs =~ ^[0-9]+$ ]] && [ "$runs" -ge 1 ] && [ "$runs" -le 50 ] \
    || fail 64 "--runs must be a whole number from 1 to 50"
[[ $jobs =~ ^[0-9]+$ ]] && [ "$jobs" -ge 1 ] && [ "$jobs" -le 8 ] \
    || fail 64 "--jobs must be a whole number from 1 to 8"
[ -f "$1/prompt.md" ] || fail 66 "no case at $1: prompt.md is missing"
case_dir=$(readlink -f "$1")
here=$(cd "$(dirname "$0")" && pwd)
runner="$here/run_case.sh"
skill=$(readlink -f "$here/../..")
mkdir -p "$2"; out=$(readlink -f "$2")

sides=("skill:$skill")
if [ -n "$base" ]; then
    repo=$(git -C "$skill" rev-parse --show-toplevel)
    git -C "$repo" rev-parse --verify --quiet "$base^{commit}" > /dev/null \
        || fail 64 "--base $base is not a commit in $repo"
    rm -rf "$out/base-skill"; mkdir -p "$out/base-skill"
    git -C "$repo" archive "$base" skill | tar -x -C "$out/base-skill"
    [ -f "$out/base-skill/skill/SKILL.md" ] || fail 66 "$base has no skill/SKILL.md"
    sides+=("base:$out/base-skill/skill")
fi

status=$(mktemp -d "${TMPDIR:-/tmp}/run-repeat.XXXXXX")
trap 'rm -rf "$status"' EXIT
run_one() {  # run_one <side> <skill dir> <i>
    local dir="$out/$1/run-$3" line code=0
    line=$(bash "$runner" "$case_dir" "$2" "$dir" 2> "$status/$1-$3.err" | tail -n 1) || code=$?
    if [ "$code" -eq 0 ]; then
        echo "$1 run-$3 $line"
    else
        echo "$1 run-$3 failed with exit $code: $(tail -n 1 "$status/$1-$3.err")" >&2
        echo "$code" > "$status/$1-$3.failed"
    fi
}
for i in $(seq 1 "$runs"); do
    for pair in "${sides[@]}"; do
        while [ "$(jobs -rp | wc -l)" -ge "$jobs" ]; do wait -n || true; done
        run_one "${pair%%:*}" "${pair#*:}" "$i" &
    done
done
wait
shopt -s nullglob
failed=("$status"/*.failed)
[ ${#failed[@]} -eq 0 ] || exit "$(head -n 1 "${failed[0]}")"
