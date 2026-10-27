#!/usr/bin/env bash
# Render the Quarto site into analysis_quarto/docs/ and commit the build, because
# .github/workflows/pages.yml publishes exactly that committed folder (CI cannot
# rebuild it: eprtr_enhanced.csv, LAU/ and the raster folders are local-only).
#
# Usage:  ./publish.sh ["commit message"]
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/.." && pwd)"
msg="${1:-render site}"

cd "$here"
quarto render          # whole project: index.html, about.html, assets/map, ...

cd "$repo"
git add -A analysis_quarto/docs
if git diff --cached --quiet; then
  echo "analysis_quarto/docs is unchanged - nothing to commit."
  exit 0
fi
git commit -m "$msg"
echo
echo "Committed the build. Publish it with:  git push origin $(git rev-parse --abbrev-ref HEAD)"
