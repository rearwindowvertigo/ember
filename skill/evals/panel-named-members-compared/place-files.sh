#!/usr/bin/env bash
set -eu
bash "$(dirname "$0")/../_stubs/install.sh" "$(dirname "$0")"
printf '%s\n' 'Owner review notes.' > review-notes.md
