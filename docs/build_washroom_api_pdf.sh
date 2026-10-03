#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

docker run --rm \
  -v "$repo_root/docs:/data" \
  pandoc/latex:latest \
  /data/Dokumentasi_API_Washroom.md \
  -o /data/Dokumentasi_API_Washroom.pdf \
  --pdf-engine=xelatex \
  --include-in-header=/data/washroom-api-pdf-style.tex \
  --listings \
  --toc \
  --toc-depth=2 \
  --shift-heading-level-by=-1 \
  -V date="$(date +%Y-%m-%d)" \
  -V geometry:a4paper \
  -V geometry:margin=2cm \
  -V colorlinks=true \
  -V linkcolor=WashroomTeal \
  -V urlcolor=WashroomTeal \
  -V toccolor=WashroomTeal \
  -V toc-title="Daftar Isi"