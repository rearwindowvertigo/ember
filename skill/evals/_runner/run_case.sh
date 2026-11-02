#!/usr/bin/env bash
# Run one skill eval case under bubblewrap:
#   run_case.sh <case dir> <skill dir> <out dir> [prompt override]
# The root is read-only except the case's work folder, ~/.claude,
# ~/.claude.json and a private /tmp. The skill is mounted read-only at
# ~/.claude/skills/ember, the stub emb over every emb on PATH and first on
# PATH, and Ember's config, state, cache and runtime folders are blanked.
# After a turn whose final result text contains a question mark, the next
# reply in the case's owner-replies.md is sent by resuming the session, until
# the replies run out or a turn ends with no question. A prompt override sends
# no replies. The prompt is prompt.md's body after the settings block, from
# its first non-blank line.
set -eu
[ $# -ge 3 ] || { echo "usage: run_case.sh <case dir> <skill dir> <out dir> [prompt override]" >&2; exit 64; }
case_dir=$(readlink -f "$1"); skill=$(readlink -f "$2"); out=$3; override=${4:-}
H=$HOME
command -v bwrap > /dev/null || { echo "run_case.sh: bwrap (bubblewrap) is not on PATH" >&2; exit 69; }
claude_bin=$(readlink -f "$H/.local/bin/claude") || true
[ -n "$claude_bin" ] && [ -x "$claude_bin" ] || { echo "run_case.sh: no claude at $H/.local/bin/claude" >&2; exit 69; }
mkdir -p "$out"; out=$(readlink -f "$out")
work="$out/work"; rm -rf "$work"; mkdir -p "$work"
(cd "$work" && bash "$case_dir/place-files.sh") > "$out/scaffold.log" 2>&1
eval_dir="$work/.ember-eval"
if [ -n "$override" ]; then prompt=$override
else  # the body after the settings block, from its first non-blank line, so a leading /ember is the first character
    prompt=$(awk 'BEGIN{n=0; body=0} n<2 && /^---$/{n++; next} n>=2 && (body || NF){body=1; print}' "$case_dir/prompt.md")
fi
tools=$(sed -n 's/^allowed_tools: *\[\(.*\)\]/\1/p' "$case_dir/prompt.md" | tr -d ' ')
[ -n "$tools" ] || tools="Read,Glob,Grep,Skill,Bash"
extra=()
# Ember's own folders, each where it exists, as empty folders.
for d in "$H/.config/ember" "$H/.local/state/ember" "$H/.cache/ember" "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/ember"; do
    [ -d "$d" ] && extra+=(--tmpfs "$d")
done
# The stub over every emb that exists, so a full path reaches it too.
declare -A seen=()
while IFS= read -r found; do
    [ -n "$found" ] && [ -e "$found" ] || continue
    [ -n "${seen[$found]:-}" ] && continue
    seen[$found]=1
    extra+=(--ro-bind "$eval_dir/bin/emb" "$found")
done < <({ type -ap emb || true; echo /usr/bin/emb; echo "$H/.local/bin/emb"; })
# Claude Code's skills folder as an empty tmpfs, so the mount point for the
# skill is made there and not in the owner's folder.
mkdir -p "$H/.claude/skills"
# The cases, graders and stubs stay out of the session's view of the skill.
hide_evals=()
[ -d "$skill/evals" ] && hide_evals=(--tmpfs "$H/.claude/skills/ember/evals")
# One private /tmp for every turn, so files Claude writes there survive a
# resumed turn.
mkdir -p "$out/tmp"
printf '%s\n' "$prompt" > "$out/prompt.txt"
start=$(date +%s)
run_claude() {  # run_claude <out file> <claude args...>
    local file=$1; shift
    bwrap --ro-bind / / --dev /dev --proc /proc --bind "$out/tmp" /tmp \
        --bind "$work" "$work" \
        --bind "$H/.claude" "$H/.claude" --bind "$H/.claude.json" "$H/.claude.json" \
        --tmpfs "$H/.claude/skills" \
        --ro-bind "$skill" "$H/.claude/skills/ember" \
        "${hide_evals[@]}" \
        --unsetenv CLAUDE_CONFIG_DIR \
        "${extra[@]}" \
        --setenv EMBER_EVAL_DIR "$eval_dir" \
        --setenv PATH "$eval_dir/bin:/usr/local/bin:/usr/bin:/bin" \
        --chdir "$work" --die-with-parent \
        "$claude_bin" -p "$@" --model opus --output-format stream-json --verbose \
            --allowedTools "$tools" --permission-mode acceptEdits \
        >> "$file" 2>> "$out/stderr.txt" < /dev/null
}
last_turn() {  # prints the session id, then Q when the last result text contains a question mark, else -
    python3 -I -c '
import json, sys
sid = res = ""
for line in open(sys.argv[1], encoding="utf-8"):
    try:
        d = json.loads(line)
    except ValueError:
        continue
    if isinstance(d, dict) and d.get("type") == "result":
        sid = d.get("session_id") or ""
        res = d.get("result") or ""
print(sid)
print("Q" if "?" in res else "-")' "$out/trace.jsonl"
}
set +e
: > "$out/trace.jsonl"
: > "$out/owner.log"
run_claude "$out/trace.jsonl" "$prompt"
code=$?
replies="$case_dir/owner-replies.md"
if [ -z "$override" ] && [ -f "$replies" ]; then
    n=1
    while :; do
        info=$(last_turn)
        sid=$(printf '%s\n' "$info" | sed -n 1p); q=$(printf '%s\n' "$info" | sed -n 2p)
        [ "$q" = Q ] && [ -n "$sid" ] || break
        reply=$(grep -E '^[0-9]+\. ' "$replies" | sed -n "${n}p" | sed 's/^[0-9]*\. [^:]*: //')
        [ -n "$reply" ] || break
        echo "owner reply $n: $reply" >> "$out/owner.log"
        run_claude "$out/trace.jsonl" --resume "$sid" "$reply"
        code=$?
        n=$((n + 1))
    done
fi
set -e
end=$(date +%s)
cp "$eval_dir/calls.log" "$out/calls.log" 2>/dev/null || true
printf '{"exit": %d, "seconds": %d}\n' "$code" "$((end - start))" > "$out/run.json"
echo "exit $code in $((end - start))s; calls $(wc -l < "$out/calls.log" 2>/dev/null || echo 0)"
