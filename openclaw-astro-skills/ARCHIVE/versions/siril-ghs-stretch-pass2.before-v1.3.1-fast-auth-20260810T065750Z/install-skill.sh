#!/usr/bin/env bash

workspace="/home/peter/.openclaw/workspace/agents/codewarrior"
target="$workspace/skills/siril-ghs-stretch-pass2"
astro_python="$workspace/AstroProcessor/.venv/bin/python"
siril_app="/home/peter/.openclaw/runtime/siril-processor/toolchain/.toolchain/siril/1.4.4/squashfs-root/AppRun"

package_dir="$(cd "$(dirname "$0")" && pwd)"
helper="$package_dir/scripts/ghs_pass2.py"
contract="$package_dir/scripts/contract_self_test.py"
probe="$package_dir/scripts/m16_regression_probe.py"

timestamp="$(date +%Y%m%d-%H%M%S)"
staging="$workspace/skills/.siril-ghs-stretch-pass2-v1.0.0-$timestamp"
backup_root="/home/peter/.openclaw/backups/codewarrior-skills/siril-ghs-stretch-pass2"
backup_dir="$backup_root/before-v1.0.0-$timestamp"

printf '\n===== Install siril-ghs-stretch-pass2 1.0.0 =====\n'

for path in "$astro_python" "$siril_app" "$helper" "$contract" "$probe" "$package_dir/SKILL.md"; do
    if [ ! -e "$path" ]; then
        printf 'ERROR: Required path missing:\n  %s\n' "$path"
        exit 1
    fi
done

siril_output="$(APPDIR="$(dirname "$siril_app")" "$siril_app" siril-cli --version 2>&1)"
printf 'Siril version output: %s\n' "$siril_output"
case "$siril_output" in
  *"1.4.4"*) ;;
  *) printf 'ERROR: Expected Siril 1.4.4.\n'; exit 1 ;;
esac

printf '\n===== Stage new pass-2 skill =====\n'
mkdir -p "$staging" || exit 1
cp -a -- "$package_dir/." "$staging/" || exit 1

printf '\n===== Validate staged helper =====\n'
"$astro_python" -m py_compile "$staging/scripts/ghs_pass2.py" || exit 1
version="$("$astro_python" "$staging/scripts/ghs_pass2.py" --version 2>/dev/null)"
printf 'Staged helper version: %s\n' "$version"
if [ "$version" != "1.0.0" ]; then
    printf 'ERROR: Expected staged helper 1.0.0.\n'
    exit 1
fi

printf '\n===== Real-Siril synthetic self-test =====\n'
if ! "$astro_python" "$staging/scripts/ghs_pass2.py" self-test --timeout 1800; then
    printf '\nINSTALLATION NOT COMPLETED\n'
    printf 'Synthetic self-test failed. Existing skills and M16 were unchanged.\n'
    printf 'Staging remains at:\n  %s\n' "$staging"
    exit 1
fi

printf '\n===== Upstream pass-1 contract regression =====\n'
if ! "$astro_python" "$contract" --helper "$staging/scripts/ghs_pass2.py"; then
    printf '\nINSTALLATION NOT COMPLETED\n'
    printf 'Pass-1 contract test failed. Existing skills and M16 were unchanged.\n'
    exit 1
fi

printf '\n===== Non-destructive real-M16 pass-2 regression probe =====\n'
if ! "$astro_python" "$probe" \
    --helper "$staging/scripts/ghs_pass2.py" \
    --workspace "$workspace" \
    --timeout 1800
then
    printf '\nINSTALLATION NOT COMPLETED\n'
    printf 'Real M16 pass-2 probe failed. Existing skills and canonical M16 data were unchanged.\n'
    printf 'Staging/probe evidence remains preserved.\n'
    printf 'Staging:\n  %s\n' "$staging"
    exit 1
fi

if [ -e "$target" ]; then
    printf '\n===== Preserve existing pass-2 skill =====\n'
    mkdir -p "$backup_dir" || exit 1
    mv -- "$target" "$backup_dir/skill" || exit 1
fi

printf '\n===== Publish tested pass-2 skill =====\n'
if ! mv -- "$staging" "$target"; then
    printf 'ERROR: Could not publish pass-2 skill.\n'
    if [ ! -e "$target" ] && [ -e "$backup_dir/skill" ]; then
        mv -- "$backup_dir/skill" "$target"
        printf 'Previous pass-2 skill restored.\n'
    fi
    exit 1
fi

installed="$("$astro_python" "$target/scripts/ghs_pass2.py" --version 2>/dev/null)"
printf 'Installed helper version: %s\n' "$installed"
if [ "$installed" != "1.0.0" ]; then
    printf 'ERROR: Final installed version check failed.\n'
    exit 1
fi

printf '\nSIRIL GHS PASS2 1.0.0 INSTALLATION COMPLETE\n'
printf 'Installed skill:\n  %s\n' "$target"
printf 'The existing siril-ghs-stretch pass-1 skill was not modified.\n'
printf 'Canonical M16 processing/ghs-pass1 and processing/ghs-pass2 were not changed by installation.\n'
