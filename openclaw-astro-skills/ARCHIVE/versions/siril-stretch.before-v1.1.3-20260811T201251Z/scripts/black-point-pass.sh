#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 3 ]; then
  cat <<'USAGE' >&2
Usage:
  black-point-pass.sh <input_fits> <output_fits> <BP> [siril_apprun]

Example:
  black-point-pass.sh in.fits out.fits 0.0045 /path/to/AppRun
USAGE
  exit 1
fi

input="$1"
output="$2"
BP="$3"
siril_bin="${4:-siril}"

script_file="$(mktemp)"
trap 'rm -f "$script_file"' EXIT

cat > "$script_file" <<EOF2
requires 1.4.0
load $input
linstretch BP=$BP
save $output
close
EOF2

printf 'Prepared BP script: %s\n' "$script_file"
printf 'Input:  %s\nOutput: %s\n' "$input" "$output"
printf 'Params: BP=%s policy=preserve shadow data, no meaningful clipping\n' "$BP"

cat <<NOTE
NOTE:
- The BP shift must move the left edge toward black without clipping meaningful data.
- If your local Siril wrapper syntax differs, adapt the generated command accordingly.
NOTE

"$siril_bin" -s "$script_file"
