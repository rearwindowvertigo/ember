#!/usr/bin/env bash
# Set up a case's working directory (the current directory) for one eval run.
# Each case's place-files.sh runs this after copying the case's files/ here.
#
# - copies the fixture repository (fixture/) where the case gave no file of
#   the same path;
# - installs bin/emb and stub.py in .ember-eval/bin/ and the default replies
#   in .ember-eval/replies/, for each route the case gave no reply for;
# - makes the directory a git repository on main with one commit, an origin
#   remote that no git command can reach (core.sshCommand false), and checks
#   out the session branch;
# - writes .ember-eval/env.sh, which puts the stub first on PATH.
#
# .ember-eval/case.env, from the case's files/, may set REMOTE_URL and
# SESSION_BRANCH.
set -eu

kit="$(cd "$(dirname "$0")" && pwd)"
work="$(pwd)"
eval_dir="$work/.ember-eval"
REMOTE_URL="git@github.com:example/order-tools.git"
SESSION_BRANCH="claude/order-tools-work"
mkdir -p "$eval_dir"
if [ -f "$eval_dir/case.env" ]; then
    # shellcheck disable=SC1091
    . "$eval_dir/case.env"
fi

copy_missing() {
    local source="$1" target="$2" file
    (cd "$source" && find . -type f -print0) | while IFS= read -r -d '' file; do
        if [ ! -e "$target/$file" ]; then
            mkdir -p "$(dirname "$target/$file")"
            cp "$source/$file" "$target/$file"
        fi
    done
}

copy_missing "$kit/fixture" "$work"
mkdir -p "$eval_dir/bin" "$eval_dir/replies"
cp "$kit/bin/emb" "$kit/stub.py" "$eval_dir/bin/"
chmod +x "$eval_dir/bin/emb"
# A route the case gives any reply for takes none of the default's parts.
case_routes=" "
for entry in "$eval_dir"/replies/*; do
    [ -e "$entry" ] || continue
    name="$(basename "$entry")"
    case_routes="$case_routes${name%%.*} "
done
for file in "$kit"/replies/*; do
    name="$(basename "$file")"
    case "$case_routes" in
        *" ${name%%.*} "*) ;;
        *) cp -R "$file" "$eval_dir/replies/" ;;
    esac
done
: > "$eval_dir/calls.log"
printf 'export EMBER_EVAL_DIR="%s"\nexport PATH="%s/bin:$PATH"\n' "$eval_dir" "$eval_dir" > "$eval_dir/env.sh"

if [ ! -d "$work/.git" ]; then
    git init -q -b main "$work"
    git -C "$work" config user.name "Eval Owner"
    git -C "$work" config user.email "owner@example.invalid"
    git -C "$work" config commit.gpgsign false
    git -C "$work" config core.sshCommand false
    printf '.ember-eval/\n' >> "$work/.git/info/exclude"
    git -C "$work" remote add origin "$REMOTE_URL"
    git -C "$work" add -A
    git -C "$work" commit -q -m "Add the order report tool and its tests"
    git -C "$work" checkout -q -b "$SESSION_BRANCH"
fi
