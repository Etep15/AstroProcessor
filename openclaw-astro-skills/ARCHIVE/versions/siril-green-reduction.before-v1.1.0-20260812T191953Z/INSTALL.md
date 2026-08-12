# Install siril-green-reduction v1.0.3

Requires the current M16 black-point canonical from `siril-black-point` v1.0.4.
The installer verifies the accepted M16 black-point SHA and manifest contract.

It runs one small real-Siril synthetic self-test, then only a `--plan-only`
check against the real M16 project. It never generates real M16 green-reduction
candidates during installation.

## Mac

```bash
scp ~/Downloads/siril-green-reduction-v1.0.3.zip \
  peter@hawthorne:/home/peter/
```

## Hawthorne

```bash
bash <<'BASH'
package="/home/peter/siril-green-reduction-v1.0.3.zip"
expected_sha256="<PACKAGE_SHA256_FROM_CHAT>"
timestamp="$(date +%Y%m%d-%H%M%S)"
extract_root="/home/peter/siril-green-reduction-v1.0.3-$timestamp"

if [ ! -f "$package" ]; then
    printf 'ERROR: Package not found:\n  %s\n' "$package"
else
    actual="$(sha256sum "$package" | awk '{print $1}')"
    printf 'Expected: %s\nActual:   %s\n' "$expected_sha256" "$actual"
    if [ "$actual" != "$expected_sha256" ]; then
        printf 'ERROR: Checksum mismatch. Nothing installed.\n'
    elif ! mkdir -p "$extract_root"; then
        printf 'ERROR: Could not create extraction directory.\n'
    elif ! unzip -q "$package" -d "$extract_root"; then
        printf 'ERROR: Could not extract package.\n'
    else
        skill="$extract_root/siril-green-reduction-v1.0.3"
        chmod 0755 "$skill/install-skill.sh"
        "$skill/install-skill.sh"
    fi
fi
BASH
```

Success marker:

```text
SIRIL GREEN REDUCTION 1.0.3 INSTALLATION COMPLETE
```
