#!/usr/bin/env bash
set -eu
bash "$(dirname "$0")/../_stubs/install.sh" "$(dirname "$0")"
cp -R "$(dirname "$0")/../claim-and-menu-writer/files/.ember-eval/replies/run-start.files" .ember-eval/replies/
