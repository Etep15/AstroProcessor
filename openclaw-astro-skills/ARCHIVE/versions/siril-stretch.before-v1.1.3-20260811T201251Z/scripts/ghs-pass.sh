#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 7 ]; then
  cat <<'USAGE' >&2
Usage:
  ghs-pass.sh <input_fits> <output_fits> <D> <B> <SP> <LP> <HP> [siril_apprun]

Example:
  ghs-pass.sh in.fits out.fits 5.75 2.5 0.19 0.0 0.97 /path/to/AppRun
USAGE
  exit 1
fi

input="$1"
output="$2"
D="$3"
B="$4"
SP="$5"
LP="$6"
HP="$7"
siril_bin="${8:-siril}"

script_file="$(mktemp)"
trap 'rm -f "$script_file"' EXIT

cat > "$script_file" <<EOF2
requires 1.4.0
load $input
ghs D=$D B=$B SP=$SP LP=$LP HP=$HP colour=0 channel=0 inverse=0 type=0
save $output
close
EOF2

printf 'Prepared GHS script: %s\n' "$script_file"
printf 'Input:  %s\nOutput: %s\n' "$input" "$output"
printf 'Params: D=%s B=%s SP=%s LP=%s HP=%s mode=rgbblend/even (documented policy)\n' "$D" "$B" "$SP" "$LP" "$HP"

cat <<NOTE
NOTE:
- Ensure the Siril command used for production honors the skill policy:
  RGB Blend clipping and even-weighted luminance color handling.
- If your local Siril wrapper syntax differs, adapt the generated command accordingly.
- This helper intentionally exposes parameters rather than hard-coding a single recipe.
NOTE

"$siril_bin" -s "$script_file"
