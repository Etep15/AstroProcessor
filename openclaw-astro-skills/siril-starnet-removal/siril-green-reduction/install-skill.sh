#!/usr/bin/env bash
workspace="/home/peter/.openclaw/workspace/agents/codewarrior"
target="$workspace/skills/siril-green-reduction"
astro_python="$workspace/AstroProcessor/.venv/bin/python"
siril_app="/home/peter/.openclaw/runtime/siril-processor/toolchain/.toolchain/siril/1.4.4/squashfs-root/AppRun"
package_dir="$(cd "$(dirname "$0")" && pwd)"
new_helper="$package_dir/scripts/green_reduction.py"
new_wrapper="$package_dir/bin/green-reduction"
expected_new_sha="d05455f08c99551c0ed88140380d7054863e075b9dbdc06661b207412b702307"
expected_black_point_sha="86f274e7315869a5b67442f6cdec0e9f4d78e2003d40fd533510ad2c4aaeca18"
timestamp="$(date +%Y%m%d-%H%M%S)"
staging="$workspace/skills/.siril-green-reduction-v1.0.3-$timestamp"
backup_root="/home/peter/.openclaw/backups/codewarrior-skills/siril-green-reduction"
backup_dir="$backup_root/before-v1.0.3-$timestamp"
project="$workspace/Projects/M16 July 2026"
black_fit="$project/processing/black-point/SHO-starless-black-point.fit"
black_manifest="$project/processing/black-point/black-point-manifest.json"

printf '\n===== Install Siril green-reduction 1.0.3 =====\n'
for path in "$astro_python" "$siril_app" "$new_helper" "$new_wrapper" "$package_dir/SKILL.md" "$package_dir/scripts/api_surface_regression_test.py" "$package_dir/scripts/selection_policy_regression_test.py" "$black_fit" "$black_manifest"; do
  if [ ! -e "$path" ]; then printf 'ERROR: Required path missing:\n  %s\n' "$path"; exit 1; fi
done
black_sha_before="$(sha256sum "$black_fit" | awk '{print $1}')"
black_manifest_sha_before="$(sha256sum "$black_manifest" | awk '{print $1}')"
if [ "$black_sha_before" != "$expected_black_point_sha" ]; then printf 'ERROR: Current M16 black-point canonical SHA is not the accepted v1.0.4 baseline.\nExpected: %s\nActual:   %s\n' "$expected_black_point_sha" "$black_sha_before"; exit 1; fi

"$astro_python" - "$black_manifest" <<'PYCHK'
import json, sys
m=json.load(open(sys.argv[1],encoding="utf-8"))
required=[(m.get("helper_version")=="1.0.4","helper_version 1.0.4"),(m.get("status")=="ready","status ready"),(m.get("next_stage")=="siril-green-reduction","next_stage siril-green-reduction"),(m.get("green_reduction_processing_permitted") is True,"green reduction permitted"),(m.get("visual_review_completed") is True,"visual review complete"),(m.get("quality_assessment",{}).get("satisfactory") is True,"quality satisfactory"),(m.get("selection_policy",{}).get("version")=="1.0.4","selection policy 1.0.4")]
failed=[label for ok,label in required if not ok]
if failed: raise SystemExit("ERROR: Black-point upstream contract failed: "+", ".join(failed))
print("PASS: real M16 black-point v1.0.4 upstream contract")
PYCHK
if [ $? -ne 0 ]; then exit 1; fi

printf '\n===== Stage green-reduction v1.0.3 =====\n'
mkdir -p "$staging/scripts" "$staging/bin" || exit 1
cp -- "$package_dir/SKILL.md" "$staging/SKILL.md" || exit 1
cp -- "$package_dir/DESIGN.md" "$staging/DESIGN.md" || exit 1
cp -- "$package_dir/TEST-PROMPT.txt" "$staging/TEST-PROMPT.txt" || exit 1
cp -- "$package_dir/ONE-LINE-PROMPT.txt" "$staging/ONE-LINE-PROMPT.txt" || exit 1
cp -- "$new_helper" "$staging/scripts/green_reduction.py" || exit 1
cp -- "$package_dir/scripts/api_surface_regression_test.py" "$staging/scripts/" || exit 1
cp -- "$package_dir/scripts/selection_policy_regression_test.py" "$staging/scripts/" || exit 1
cp -- "$new_wrapper" "$staging/bin/green-reduction" || exit 1
chmod 0755 "$staging/scripts/"*.py "$staging/bin/green-reduction"

printf '\n===== Compile / version / API =====\n'
"$astro_python" -m py_compile "$staging/scripts/green_reduction.py" || exit 1
staged_version="$("$astro_python" "$staging/scripts/green_reduction.py" --version 2>/dev/null)"
staged_sha="$(sha256sum "$staging/scripts/green_reduction.py" | awk '{print $1}')"
printf 'Staged helper version: %s\nStaged helper SHA-256: %s\n' "$staged_version" "$staged_sha"
if [ "$staged_version" != "1.0.3" ] || [ "$staged_sha" != "$expected_new_sha" ]; then printf 'ERROR: staged helper validation failed.\n'; exit 1; fi
"$astro_python" "$staging/scripts/api_surface_regression_test.py" --helper "$staging/scripts/green_reduction.py" || exit 1
"$astro_python" "$staging/scripts/selection_policy_regression_test.py" --helper "$staging/scripts/green_reduction.py" || exit 1
"$astro_python" "$staging/scripts/review_contract_regression_test.py" --helper "$staging/scripts/green_reduction.py" || exit 1
"$astro_python" "$staging/scripts/note_contract_regression_test.py" --helper "$staging/scripts/green_reduction.py" || exit 1

printf '\n===== Real-Siril synthetic self-test =====\n'
"$astro_python" "$staging/scripts/green_reduction.py" self-test --timeout 1800 || exit 1

printf '
===== Non-destructive real M16 state check =====
'
stage_json="$("$astro_python" "$staging/scripts/green_reduction.py" stage-status --project "M16 July 2026")"
printf '%s
' "$stage_json"

if printf '%s
' "$stage_json" | grep -q '"status": "missing"'; then
    plan_json="$("$astro_python" "$staging/scripts/green_reduction.py" advance --project "M16 July 2026" --plan-only)"
    printf '%s
' "$plan_json"
    if ! printf '%s
' "$plan_json" | grep -q '"status": "would_generate_candidates"'; then
        printf 'ERROR: Missing M16 green stage did not produce the expected plan-only result.
'
        exit 1
    fi
    if ! printf '%s
' "$plan_json" | grep -q '"manual_baseline_amount": 0.15'; then
        printf 'ERROR: M16 plan-only result lost manual 0.15 baseline.
'
        exit 1
    fi
elif printf '%s
' "$stage_json" | grep -q '"status": "ready"'; then
    if ! printf '%s
' "$stage_json" | grep -q '"next_stage": "siril-saturation"'; then
        printf 'ERROR: Completed M16 green stage has unexpected next stage.
'
        exit 1
    fi
    if ! printf '%s
' "$stage_json" | grep -q '"saturation_processing_permitted": true'; then
        printf 'ERROR: Completed M16 green stage no longer permits saturation.
'
        exit 1
    fi
    plan_json="$("$astro_python" "$staging/scripts/green_reduction.py" advance --project "M16 July 2026" --plan-only)"
    printf '%s
' "$plan_json"
    if ! printf '%s
' "$plan_json" | grep -q '"status": "confirmation_required"'; then
        printf 'ERROR: Completed M16 green stage did not require fresh-run confirmation.
'
        exit 1
    fi
else
    printf 'ERROR: Real M16 green stage is neither safely missing nor safely complete.
'
    exit 1
fi

printf '\n===== Confirm real M16 black-point evidence unchanged =====\n'
black_sha_after="$(sha256sum "$black_fit" | awk '{print $1}')"
black_manifest_sha_after="$(sha256sum "$black_manifest" | awk '{print $1}')"
printf 'Black-point FITS before: %s\nafter: %s\n' "$black_sha_before" "$black_sha_after"
printf 'Black-point manifest before: %s\nafter: %s\n' "$black_manifest_sha_before" "$black_manifest_sha_after"
if [ "$black_sha_before" != "$black_sha_after" ] || [ "$black_manifest_sha_before" != "$black_manifest_sha_after" ]; then printf 'ERROR: Installation testing modified real M16 upstream evidence.\n'; exit 1; fi

if [ -e "$target" ]; then printf '\n===== Preserve existing green-reduction skill =====\n'; mkdir -p "$backup_dir" || exit 1; mv -- "$target" "$backup_dir/skill" || exit 1; fi
printf '\n===== Publish tested green-reduction v1.0.3 skill =====\n'
if ! mv -- "$staging" "$target"; then printf 'ERROR: Could not publish green-reduction skill.\n'; if [ ! -e "$target" ] && [ -e "$backup_dir/skill" ]; then mv -- "$backup_dir/skill" "$target"; printf 'Previous skill restored.\n'; fi; exit 1; fi
installed_version="$("$target/bin/green-reduction" --version 2>/dev/null)"; installed_sha="$(sha256sum "$target/scripts/green_reduction.py" | awk '{print $1}')"
printf 'Installed version through wrapper: %s\nInstalled helper SHA-256: %s\n' "$installed_version" "$installed_sha"
if [ "$installed_version" != "1.0.3" ] || [ "$installed_sha" != "$expected_new_sha" ]; then printf 'ERROR: final installed helper verification failed.\n'; exit 1; fi
printf '\nSIRIL GREEN REDUCTION 1.0.3 INSTALLATION COMPLETE\n'
printf 'Real M16 black-point canonical remains unchanged.\n'
