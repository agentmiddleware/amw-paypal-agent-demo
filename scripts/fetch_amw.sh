#!/usr/bin/env sh
# Fetch the AMW gateway source at the pinned commit into .amw/ (gitignored).
# AMW's server package is not pip-installable at this commit, so the demo and
# tests load it from this checkout. To use your own checkout instead, skip this
# script and set AMW_SOURCE_DIR.
set -eu
AMW_REF=b082579d4e15e21bab5a3bc65a10464817927ded
DEST="$(cd "$(dirname "$0")/.." && pwd)/.amw/agent-middleware-api"

if [ ! -d "$DEST/.git" ]; then
  git clone --quiet https://github.com/agentmiddleware/agent-middleware-api "$DEST"
fi
git -C "$DEST" checkout --quiet "$AMW_REF"
echo "AMW gateway source: $DEST at $(git -C "$DEST" rev-parse --short HEAD)"
