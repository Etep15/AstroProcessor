# Install `siril-ghs-stretch-pass2` 1.0.0

This is a **new sibling skill**. It does not modify the installed
`siril-ghs-stretch` pass-1 skill.

## Mac upload

```bash
scp ~/Downloads/siril-ghs-stretch-pass2-v1.0.0.zip \
  peter@hawthorne:/home/peter/
```

## Hawthorne install

Use the SHA-256 supplied in ChatGPT:

```bash
bash <<'BASH'
package="/home/peter/siril-ghs-stretch-pass2-v1.0.0.zip"
expected_sha256="<PACKAGE_SHA256_FROM_CHAT>"
timestamp="$(date +%Y%m%d-%H%M%S)"
extract_root="/home/peter/siril-ghs-pass2-v1.0.0-$timestamp"

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
        skill="$extract_root/siril-ghs-stretch-pass2-v1.0.0"
        chmod 0755 "$skill/install-skill.sh"
        "$skill/install-skill.sh"
    fi
fi
BASH
```

The installer runs:
- Python syntax/version checks;
- real-Siril synthetic self-test;
- upstream pass-1 contract regression;
- a non-destructive real M16 stride-4 pass-2 probe.

It will not install unless the real M16 probe produces a balanced,
publication-eligible result.

Success marker:

```text
SIRIL GHS PASS2 1.0.0 INSTALLATION COMPLETE
```
