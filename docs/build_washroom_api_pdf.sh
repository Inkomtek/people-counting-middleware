#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
# Which docs/<name>.md to build (default: the internal draft) and an optional page footer text
# (default: the one in washroom-api-pdf-style.tex). The sensor team's guide:
#   sh docs/build_washroom_api_pdf.sh Dokumentasi_API_Washroom_Tim_Sensor "PENGIRIMAN DATA SENSOR"
name=${1:-Dokumentasi_API_Washroom}
footer_tex="$repo_root/docs/.footer.tex"
trap 'rm -f "$footer_tex"' EXIT
if [ -n "${2:-}" ]; then
  printf '\\newcommand{\\WashroomFooter}{%s}\n' "$2" > "$footer_tex"
else
  : > "$footer_tex"
fi

docker run --rm \
  -v "$repo_root/docs:/data" \
  pandoc/latex:latest \
  /data/$name.md \
  -o "/data/$name.pdf" \
  --pdf-engine=xelatex \
  --include-in-header=/data/.footer.tex \
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