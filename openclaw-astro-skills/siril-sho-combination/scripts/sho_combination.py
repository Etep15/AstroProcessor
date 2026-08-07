#!/usr/bin/env python3
"""Deterministic Siril SHO combination from validated mono-denoise outputs."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

VERSION = "1.1.1"
REQUIRED_SIRIL_VERSION = "1.4.4"
REQUIRED_MONO_DENOISE_HELPER_VERSION = "1.0.3"
UPSTREAM_STAGE = "siril-mono-linear-denoise"
CURRENT_STAGE = "siril-sho-combination"
NEXT_STAGE = "siril-background-neutralization"
MIGRATABLE_HELPER_VERSION = "1.1.0"
DEFAULT_TIMEOUT_SECONDS = 900
MAPPING_TOLERANCE = 1.0e-6
SIRIL_ROOT = Path("/home/peter/.openclaw/runtime/siril-processor/toolchain/.toolchain/siril/1.4.4/squashfs-root")
SIRIL_APPRUN = SIRIL_ROOT / "AppRun"
CHANNEL_ORDER = ("SII", "Ha", "OIII")
ROLE_FILENAMES = {"SII": "R_SII.fit", "Ha": "G_Ha.fit", "OIII": "B_OIII.fit"}
STABLE_OUTPUT_NAME = "SHO-linear.fit"
STABLE_MANIFEST_NAME = "sho-combination-manifest.json"

class ShoCombinationError(RuntimeError):
    pass

@dataclass(frozen=True)
class FitsEvidence:
    path: str
    size: int
    sha256: str
    width: int
    height: int
    channels: int
    bitpix: int
    dtype: str
    finite_fraction: float
    minimum: float
    maximum: float
    median: float
    exposure_seconds: float | None
    stack_count: int | None
    live_time_seconds: float | None
    filter_header: str | None

def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()

def unique_id() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + f"-p{os.getpid()}"

def derive_workspace() -> Path:
    resolved = Path(__file__).resolve()
    for parent in resolved.parents:
        if parent.name == "skills":
            return parent.parent
    raise ShoCombinationError(f"Cannot derive owning workspace from helper path: {resolved}")

def project_paths(workspace: Path, project_name: str) -> dict[str, Path]:
    project = workspace / "Projects" / project_name
    mono = project / "processing" / "mono-linear-denoise"
    sho = project / "processing" / "sho"
    return {
        "workspace": workspace,
        "project": project,
        "mono": mono,
        "mono_manifest": mono / "mono-linear-denoise-manifest.json",
        "sho": sho,
        "stable_output": sho / STABLE_OUTPUT_NAME,
        "stable_manifest": sho / STABLE_MANIFEST_NAME,
        "runs": project / ".siril-sho-combination",
    }

def expected_input_path(paths: dict[str, Path], name: str) -> Path:
    return paths["mono"] / f"denoised_{name}.fit"

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def json_dump_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f".{path.name}.{unique_id()}.partial")
    partial.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(partial, path)

def inspect_fits(path: Path, *, expected_channels: int | None = None) -> FitsEvidence:
    try:
        import numpy as np
        from astropy.io import fits
    except Exception as exc:
        raise ShoCombinationError("Astropy and NumPy are required.") from exc
    if not path.is_file():
        raise ShoCombinationError(f"FITS file is missing: {path}")
    with fits.open(path, memmap=False, do_not_scale_image_data=False) as hdul:
        data = hdul[0].data
        header = hdul[0].header.copy()
        if data is None:
            raise ShoCombinationError(f"Primary FITS image has no data: {path}")
        array = np.asarray(data)
    if array.ndim == 2:
        height, width = array.shape
        channels = 1
    elif array.ndim == 3 and array.shape[0] in {1, 3, 4}:
        channels, height, width = array.shape
    else:
        raise ShoCombinationError(f"Unsupported FITS shape {array.shape}: {path}")
    if expected_channels is not None and channels != expected_channels:
        raise ShoCombinationError(f"Expected {expected_channels} channel(s), found {channels}: {path}")
    if array.dtype.kind != "f" or array.dtype.itemsize != 4:
        raise ShoCombinationError(f"Expected 32-bit floating-point FITS, found {array.dtype}: {path}")
    finite = np.isfinite(array)
    if not finite.any():
        raise ShoCombinationError(f"FITS image contains no finite pixels: {path}")
    values = array[finite]
    minimum, maximum, median = float(np.min(values)), float(np.max(values)), float(np.median(values))
    if maximum <= minimum:
        raise ShoCombinationError(f"FITS image is constant or blank: {path}")
    def opt_float(key: str) -> float | None:
        try:
            return float(header[key]) if key in header else None
        except Exception:
            return None
    def opt_int(key: str) -> int | None:
        try:
            return int(header[key]) if key in header else None
        except Exception:
            return None
    return FitsEvidence(
        path=str(path), size=path.stat().st_size, sha256=sha256_file(path),
        width=int(width), height=int(height), channels=int(channels),
        bitpix=int(header.get("BITPIX", 0)), dtype=str(array.dtype),
        finite_fraction=float(finite.mean()), minimum=minimum, maximum=maximum,
        median=median, exposure_seconds=opt_float("EXPTIME"),
        stack_count=opt_int("STACKCNT"), live_time_seconds=opt_float("LIVETIME"),
        filter_header=str(header["FILTER"]) if "FILTER" in header else None,
    )

def read_fits_array(path: Path):
    import numpy as np
    from astropy.io import fits
    with fits.open(path, memmap=False, do_not_scale_image_data=False) as hdul:
        return np.asarray(hdul[0].data, dtype=np.float64)

def load_mono_manifest(paths: dict[str, Path]) -> tuple[dict[str, Any], str]:
    path = paths["mono_manifest"]
    if not path.is_file():
        raise ShoCombinationError(f"Ready mono-linear-denoise manifest is missing: {path}")
    manifest_hash = sha256_file(path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("project") != paths["project"].name:
        raise ShoCombinationError("Mono-linear-denoise manifest project does not match.")
    if Path(str(manifest.get("project_path", ""))).resolve() != paths["project"].resolve():
        raise ShoCombinationError("Mono-linear-denoise manifest project path does not match.")
    if manifest.get("status") != "ready":
        raise ShoCombinationError("Mono-linear-denoise manifest status is not ready.")
    if manifest.get("helper_version") != REQUIRED_MONO_DENOISE_HELPER_VERSION:
        raise ShoCombinationError(
            f"Expected mono-linear-denoise helper {REQUIRED_MONO_DENOISE_HELPER_VERSION}; "
            f"manifest reports {manifest.get('helper_version')!r}."
        )
    if manifest.get("visual_review_completed") is not True:
        raise ShoCombinationError("Mono-linear-denoise visual review is not complete.")
    if manifest.get("sho_combination_permitted") is not True:
        raise ShoCombinationError("Mono-linear-denoise manifest does not permit SHO combination.")
    order = manifest.get("stage_order", {})
    if order.get("current") != "siril-mono-linear-denoise" or order.get("downstream") != "siril-sho-combination":
        raise ShoCombinationError("Mono-linear-denoise stage order is incompatible.")
    review = manifest.get("visual_review", {})
    review_path = Path(str(review.get("record_path", "")))
    expected_review = paths["mono"] / "visual-review-record.json"
    if not review_path.is_file() or review_path.resolve() != expected_review.resolve():
        raise ShoCombinationError("Mono-linear-denoise visual review record is missing or unexpected.")
    if not review.get("record_sha256") or sha256_file(review_path) != review.get("record_sha256"):
        raise ShoCombinationError("Mono-linear-denoise visual review record checksum does not match.")
    return manifest, manifest_hash

def validate_mono_inputs(paths: dict[str, Path], manifest: dict[str, Any]) -> dict[str, FitsEvidence]:
    outputs = manifest.get("outputs", {})
    evidence: dict[str, FitsEvidence] = {}
    for name in ("Ha", "SII", "OIII"):
        record = outputs.get(name)
        if not isinstance(record, dict):
            raise ShoCombinationError(f"Mono-linear-denoise manifest has no {name} output record.")
        path = Path(str(record.get("path", ""))).resolve()
        if path != expected_input_path(paths, name).resolve():
            raise ShoCombinationError(f"{name} path is not the canonical mono-linear-denoise output.")
        current = inspect_fits(path, expected_channels=1)
        if current.bitpix != -32 or current.finite_fraction != 1.0:
            raise ShoCombinationError(f"{name} input is not a finite BITPIX -32 mono FITS.")
        if current.sha256 != record.get("sha256"):
            raise ShoCombinationError(f"{name} checksum no longer matches the mono-linear-denoise manifest.")
        for key in ("width", "height", "channels", "bitpix"):
            if record.get(key) is not None and getattr(current, key) != record.get(key):
                raise ShoCombinationError(f"{name} {key} differs from the manifest.")
        evidence[name] = current
    if len({(x.width, x.height, x.channels) for x in evidence.values()}) != 1:
        raise ShoCombinationError("Mono-linear-denoise input dimensions do not match.")
    return evidence

def copy_verified(source: Path, destination: Path, expected_hash: str) -> None:
    if destination.exists():
        raise ShoCombinationError(f"Refusing to overwrite existing path: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(f".{destination.name}.{unique_id()}.partial")
    shutil.copy2(source, partial)
    if sha256_file(partial) != expected_hash:
        raise ShoCombinationError(f"Checksum changed while copying {source}; partial preserved at {partial}")
    os.replace(partial, destination)

def siril_version() -> str:
    if not SIRIL_APPRUN.is_file() or not os.access(SIRIL_APPRUN, os.X_OK):
        raise ShoCombinationError(f"Siril AppRun is missing or not executable: {SIRIL_APPRUN}")
    env = os.environ.copy()
    env["APPDIR"] = str(SIRIL_ROOT)
    completed = subprocess.run([str(SIRIL_APPRUN), "siril-cli", "--version"], env=env, capture_output=True, text=True, timeout=60, check=False)
    output = (completed.stdout + "\n" + completed.stderr).strip()
    if completed.returncode != 0 or REQUIRED_SIRIL_VERSION not in output:
        raise ShoCombinationError(f"Expected Siril {REQUIRED_SIRIL_VERSION}; received: {output}")
    return output

def composition_script() -> str:
    return "\n".join([
        f"requires {REQUIRED_SIRIL_VERSION}", "setext fit",
        'rgbcomp "R_SII.fit" "G_Ha.fit" "B_OIII.fit" -out=SHO_linear.fit -nosum',
        "close", "",
    ])

def preview_script(input_path: Path, stem: Path) -> str:
    return "\n".join([
        f"requires {REQUIRED_SIRIL_VERSION}", f'load "{input_path}"',
        "autostretch -linked", f'savepng "{stem}"', "close", "",
    ])

def run_siril(*, workdir: Path, script: Path, stdout_path: Path, stderr_path: Path, timeout_seconds: int) -> dict[str, Any]:
    command = [str(SIRIL_APPRUN), "siril-cli", "--directory", str(workdir), "--script", str(script)]
    env = os.environ.copy(); env["APPDIR"] = str(SIRIL_ROOT)
    started = time.monotonic()
    try:
        completed = subprocess.run(command, cwd=workdir, env=env, capture_output=True, text=True, timeout=timeout_seconds, check=False)
        stdout, stderr, exit_status, timed_out = completed.stdout, completed.stderr, completed.returncode, False
    except subprocess.TimeoutExpired as exc:
        stdout, stderr, exit_status, timed_out = exc.stdout or "", exc.stderr or "", None, True
        if isinstance(stdout, bytes): stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes): stderr = stderr.decode("utf-8", errors="replace")
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    combined = (stdout + "\n" + stderr).lower()
    fatal = [m for m in ("script execution failed", "cannot open", "could not open", "fatal error") if m in combined]
    return {
        "command": command,
        "display_command": f'env APPDIR="{SIRIL_ROOT}" "{SIRIL_APPRUN}" siril-cli --directory "{workdir}" --script "{script}"',
        "exit_status": exit_status, "timed_out": timed_out, "timeout_seconds": timeout_seconds,
        "duration_seconds": round(time.monotonic() - started, 3),
        "stdout_log": str(stdout_path), "stderr_log": str(stderr_path), "fatal_log_markers": fatal,
    }

def validate_mapping(output_path: Path, inputs: dict[str, Path]) -> dict[str, Any]:
    import numpy as np
    output = read_fits_array(output_path)
    if output.ndim != 3 or output.shape[0] != 3:
        raise ShoCombinationError(f"Expected RGB FITS shape (3,H,W), found {output.shape}.")
    expected = {"red_from_SII": read_fits_array(inputs["SII"]), "green_from_Ha": read_fits_array(inputs["Ha"]), "blue_from_OIII": read_fits_array(inputs["OIII"])}
    planes = {"red_from_SII": output[0], "green_from_Ha": output[1], "blue_from_OIII": output[2]}
    results: dict[str, Any] = {}
    for key in expected:
        difference = np.abs(planes[key] - expected[key])
        passed = bool(np.allclose(planes[key], expected[key], rtol=MAPPING_TOLERANCE, atol=MAPPING_TOLERANCE, equal_nan=False))
        results[key] = {
            "maximum_absolute_difference": float(np.max(difference)),
            "mean_absolute_difference": float(np.mean(difference)),
            "tolerance": MAPPING_TOLERANCE, "passed": passed,
        }
        if not passed:
            raise ShoCombinationError(f"SHO mapping failed for {key}.")
    return results

def canonical_state(paths: dict[str, Path]) -> dict[str, Any]:
    if not paths["stable_manifest"].is_file():
        return {
            "status": "not_run",
            "background_neutralization_permitted": False,
            "star_removal_permitted": False,
        }
    try:
        manifest = json.loads(
            paths["stable_manifest"].read_text(encoding="utf-8")
        )
    except Exception as exc:
        return {
            "status": "blocked",
            "error": str(exc),
            "background_neutralization_permitted": False,
            "star_removal_permitted": False,
        }

    helper_version = manifest.get("helper_version")
    upstream_stage = manifest.get("upstream_stage")
    order = manifest.get("stage_order", {})

    if (
        helper_version == MIGRATABLE_HELPER_VERSION
        and upstream_stage == UPSTREAM_STAGE
    ):
        return {
            "status": "obsolete",
            "manifest_helper_version": helper_version,
            "required_helper_version": VERSION,
            "upstream_stage": upstream_stage,
            "reason": (
                "The SHO image is valid, but its helper-1.1.0 manifest "
                "incorrectly permits StarNet directly. Run migrate-contract "
                "to preserve the image and correct only the pipeline contract."
            ),
            "contract_migration_available": True,
            "background_neutralization_permitted": False,
            "star_removal_permitted": False,
        }

    if helper_version != VERSION or upstream_stage != UPSTREAM_STAGE:
        return {
            "status": "obsolete",
            "manifest_helper_version": helper_version,
            "required_helper_version": VERSION,
            "upstream_stage": upstream_stage,
            "reason": (
                "Existing SHO output does not satisfy the current "
                "mono-linear-denoise upstream and pipeline-stage contract."
            ),
            "contract_migration_available": False,
            "background_neutralization_permitted": False,
            "star_removal_permitted": False,
        }

    contract_valid = (
        order.get("upstream") == UPSTREAM_STAGE
        and order.get("current") == CURRENT_STAGE
        and order.get("downstream") == NEXT_STAGE
        and manifest.get("background_neutralization_permitted") is True
        and manifest.get("star_removal_permitted") is False
    )
    if not contract_valid:
        return {
            "status": "blocked",
            "manifest_helper_version": helper_version,
            "reason": "The SHO manifest has an invalid downstream contract.",
            "contract_migration_available": False,
            "background_neutralization_permitted": False,
            "star_removal_permitted": False,
        }

    return {
        "status": manifest.get("status", "blocked"),
        "manifest_helper_version": helper_version,
        "upstream_stage": upstream_stage,
        "next_stage": NEXT_STAGE,
        "contract_migration_available": False,
        "background_neutralization_permitted": bool(
            manifest.get("background_neutralization_permitted")
        ),
        "star_removal_permitted": False,
    }


def compact_upstream_summary(
    manifest: dict[str, Any],
    manifest_hash: str,
) -> dict[str, Any]:
    outputs = manifest.get("outputs", {})
    return {
        "manifest_path": manifest.get(
            "stable_paths", {}
        ).get("manifest"),
        "manifest_sha256": manifest_hash,
        "helper_version": manifest.get("helper_version"),
        "status": manifest.get("status"),
        "visual_review_completed": manifest.get(
            "visual_review_completed"
        ),
        "sho_combination_permitted": manifest.get(
            "sho_combination_permitted"
        ),
        "selected_candidates": manifest.get("selected_candidates"),
        "denoise_applied": {
            name: outputs.get(name, {}).get("denoise_applied")
            for name in ("Ha", "SII", "OIII")
        },
    }


def status_project(
    project_name: str,
    workspace: Path,
) -> dict[str, Any]:
    paths = project_paths(workspace, project_name)
    state = canonical_state(paths)
    if state["status"] in {"not_run", "obsolete", "blocked"}:
        output = None
        if paths["stable_output"].is_file():
            try:
                output = asdict(
                    inspect_fits(
                        paths["stable_output"],
                        expected_channels=3,
                    )
                )
            except Exception:
                output = None
        return {
            **state,
            "helper_version": VERSION,
            "project": str(paths["project"]),
            "manifest": str(paths["stable_manifest"]),
            "output": output,
        }

    errors: list[str] = []
    inputs: dict[str, FitsEvidence] = {}
    output_record: dict[str, Any] | None = None
    mapping: dict[str, Any] | None = None
    upstream_summary: dict[str, Any] | None = None

    try:
        manifest = json.loads(
            paths["stable_manifest"].read_text(encoding="utf-8")
        )
        order = manifest.get("stage_order", {})
        if order != {
            "upstream": UPSTREAM_STAGE,
            "current": CURRENT_STAGE,
            "downstream": NEXT_STAGE,
        }:
            errors.append("SHO stage_order is invalid.")
        if manifest.get("background_neutralization_permitted") is not True:
            errors.append(
                "SHO manifest does not permit background neutralization."
            )
        if manifest.get("star_removal_permitted") is not False:
            errors.append(
                "SHO manifest incorrectly permits star removal."
            )

        mono_manifest, mono_hash = load_mono_manifest(paths)
        upstream_summary = compact_upstream_summary(
            mono_manifest,
            mono_hash,
        )
        if mono_hash != manifest.get(
            "mono_linear_denoise_manifest_sha256"
        ):
            errors.append(
                "Mono-linear-denoise manifest checksum changed."
            )

        inputs = validate_mono_inputs(paths, mono_manifest)
        for name, current in inputs.items():
            record = manifest.get("inputs", {}).get(name, {})
            if (
                current.sha256 != record.get("sha256")
                or Path(current.path).resolve()
                != Path(str(record.get("path", ""))).resolve()
            ):
                errors.append(f"SHO input changed for {name}.")

        output = inspect_fits(
            paths["stable_output"],
            expected_channels=3,
        )
        output_record = asdict(output)
        recorded = manifest.get("output", {})
        if output.sha256 != recorded.get("sha256"):
            errors.append("SHO output checksum changed.")
        if output.bitpix != -32 or output.finite_fraction != 1.0:
            errors.append("SHO output format or finiteness changed.")
        if not errors:
            mapping = validate_mapping(
                paths["stable_output"],
                {
                    name: Path(item.path)
                    for name, item in inputs.items()
                },
            )
    except Exception as exc:
        errors.append(str(exc))

    ready = (
        not errors
        and manifest.get("status") == "ready"
        and manifest.get(
            "background_neutralization_permitted"
        ) is True
        and manifest.get("star_removal_permitted") is False
    )
    return {
        "status": "ready" if ready else "blocked",
        "helper_version": VERSION,
        "project": str(paths["project"]),
        "manifest": str(paths["stable_manifest"]),
        "next_stage": NEXT_STAGE,
        "upstream_summary": upstream_summary,
        "inputs": {
            name: asdict(item)
            for name, item in inputs.items()
        },
        "output": output_record,
        "mapping_validation": mapping,
        "errors": errors,
        "background_neutralization_permitted": ready,
        "star_removal_permitted": False,
    }


def migrate_contract(
    project_name: str,
    workspace: Path,
) -> dict[str, Any]:
    paths = project_paths(workspace, project_name)
    if not paths["stable_manifest"].is_file():
        raise ShoCombinationError(
            f"SHO manifest is missing: {paths['stable_manifest']}"
        )
    if not paths["stable_output"].is_file():
        raise ShoCombinationError(
            f"SHO output is missing: {paths['stable_output']}"
        )

    old_manifest_bytes = paths["stable_manifest"].read_bytes()
    old_manifest_hash = hashlib.sha256(
        old_manifest_bytes
    ).hexdigest()
    old_manifest = json.loads(
        old_manifest_bytes.decode("utf-8")
    )

    if old_manifest.get("helper_version") != MIGRATABLE_HELPER_VERSION:
        raise ShoCombinationError(
            "Contract migration requires a canonical helper-1.1.0 "
            "SHO manifest."
        )
    if old_manifest.get("upstream_stage") != UPSTREAM_STAGE:
        raise ShoCombinationError(
            "The existing SHO result is not based on "
            "siril-mono-linear-denoise and cannot be contract-migrated."
        )

    mono_manifest, mono_hash = load_mono_manifest(paths)
    if mono_hash != old_manifest.get(
        "mono_linear_denoise_manifest_sha256"
    ):
        raise ShoCombinationError(
            "Mono-linear-denoise manifest checksum differs from the "
            "existing SHO manifest."
        )
    inputs = validate_mono_inputs(paths, mono_manifest)

    output = inspect_fits(
        paths["stable_output"],
        expected_channels=3,
    )
    recorded_output = old_manifest.get("output", {})
    if output.sha256 != recorded_output.get("sha256"):
        raise ShoCombinationError(
            "SHO output checksum differs from the existing manifest."
        )
    if output.bitpix != -32 or output.finite_fraction != 1.0:
        raise ShoCombinationError(
            "SHO output is not finite BITPIX -32 data."
        )
    mapping = validate_mapping(
        paths["stable_output"],
        {
            name: Path(item.path)
            for name, item in inputs.items()
        },
    )

    migration_root = (
        paths["runs"]
        / f"contract-migration-{unique_id()}"
    )
    migration_root.mkdir(parents=True, exist_ok=False)
    preserved_manifest = (
        migration_root
        / "previous-sho-combination-manifest-v1.1.0.json"
    )
    preserved_manifest.write_bytes(old_manifest_bytes)

    migrated = json.loads(json.dumps(old_manifest))
    migrated["helper_version"] = VERSION
    migrated["stage_order"] = {
        "upstream": UPSTREAM_STAGE,
        "current": CURRENT_STAGE,
        "downstream": NEXT_STAGE,
    }
    migrated["background_neutralization_permitted"] = True
    migrated["star_removal_permitted"] = False
    migrated["contract_migrated_from_helper_version"] = (
        MIGRATABLE_HELPER_VERSION
    )
    migrated["contract_migrated_at"] = utc_now()
    migrated["contract_migration_attempt"] = str(migration_root)
    migrated["previous_manifest_preserved_at"] = str(
        preserved_manifest
    )
    migrated["mapping_validation"] = mapping

    output_hash_before = output.sha256
    json_dump_atomic(paths["stable_manifest"], migrated)
    output_hash_after = sha256_file(paths["stable_output"])
    if output_hash_after != output_hash_before:
        paths["stable_manifest"].write_bytes(old_manifest_bytes)
        raise ShoCombinationError(
            "SHO output changed during manifest-only migration; "
            "the previous manifest was restored."
        )

    checked = status_project(project_name, workspace)
    if checked.get("status") != "ready":
        paths["stable_manifest"].write_bytes(old_manifest_bytes)
        raise ShoCombinationError(
            "Post-migration status verification failed; the previous "
            f"manifest was restored. Evidence: {checked}"
        )

    result = {
        "status": "ready",
        "helper_version": VERSION,
        "project": project_name,
        "migration_root": str(migration_root),
        "output_recomputed": False,
        "output_path": str(paths["stable_output"]),
        "output_sha256_before": output_hash_before,
        "output_sha256_after": output_hash_after,
        "previous_manifest": str(preserved_manifest),
        "previous_manifest_sha256": old_manifest_hash,
        "new_manifest": str(paths["stable_manifest"]),
        "new_manifest_sha256": sha256_file(
            paths["stable_manifest"]
        ),
        "next_stage": NEXT_STAGE,
        "background_neutralization_permitted": True,
        "star_removal_permitted": False,
        "post_migration_status_verified": True,
    }
    json_dump_atomic(
        migration_root / "contract-migration-result.json",
        result,
    )
    return result

def run_combination(project_name: str, workspace: Path, timeout_seconds: int, fresh_run: bool) -> dict[str, Any]:
    paths = project_paths(workspace, project_name)
    if not paths["project"].is_dir():
        raise ShoCombinationError(f"Project does not exist: {paths['project']}")
    existing = paths["sho"].exists()
    if existing and not fresh_run:
        raise ShoCombinationError(f"Canonical SHO directory exists: {paths['sho']}. Use --fresh-run to preserve it.")
    if existing and not paths["sho"].is_dir():
        raise ShoCombinationError(f"Canonical SHO path is not a directory: {paths['sho']}")
    existing_state = canonical_state(paths)
    siril_output = siril_version()
    mono_manifest, mono_hash = load_mono_manifest(paths)
    input_evidence = validate_mono_inputs(paths, mono_manifest)
    attempt = paths["runs"] / unique_id()
    work, logs, previews, staging = attempt / "work", attempt / "logs", attempt / "previews", attempt / "publish-staging"
    for d in (work, logs, previews, staging): d.mkdir(parents=True, exist_ok=False)
    copies: dict[str, Path] = {}
    for name in CHANNEL_ORDER:
        destination = work / ROLE_FILENAMES[name]
        copy_verified(Path(input_evidence[name].path), destination, input_evidence[name].sha256)
        copies[name] = destination
    script = attempt / "combine-sho.ssf"
    script.write_text(composition_script(), encoding="utf-8")
    run_record = run_siril(workdir=work, script=script, stdout_path=logs / "stdout.log", stderr_path=logs / "stderr.log", timeout_seconds=timeout_seconds)
    if run_record["timed_out"] or run_record["exit_status"] != 0 or run_record["fatal_log_markers"]:
        raise ShoCombinationError(f"Siril SHO combination failed; attempt preserved at {attempt}")
    attempt_output = work / "SHO_linear.fit"
    output = inspect_fits(attempt_output, expected_channels=3)
    if output.bitpix != -32 or output.finite_fraction != 1.0:
        raise ShoCombinationError("SHO output is not finite BITPIX -32 data.")
    if (output.width, output.height) != (input_evidence["Ha"].width, input_evidence["Ha"].height):
        raise ShoCombinationError("SHO output dimensions do not match inputs.")
    mapping = validate_mapping(attempt_output, copies)
    if sha256_file(paths["mono_manifest"]) != mono_hash:
        raise ShoCombinationError("Mono-linear-denoise manifest changed during composition.")
    for name, item in input_evidence.items():
        if sha256_file(Path(item.path)) != item.sha256:
            raise ShoCombinationError(f"{name} input changed during composition.")
    preview_stem = previews / "SHO-linear-autostretch-preview"
    preview_ssf = attempt / "preview.ssf"
    preview_ssf.write_text(preview_script(attempt_output, preview_stem), encoding="utf-8")
    preview_run = run_siril(workdir=attempt, script=preview_ssf, stdout_path=logs / "preview-stdout.log", stderr_path=logs / "preview-stderr.log", timeout_seconds=min(timeout_seconds, 300))
    preview_path = preview_stem.with_suffix(".png")
    if preview_run["timed_out"] or preview_run["exit_status"] != 0 or preview_run["fatal_log_markers"] or not preview_path.is_file():
        raise ShoCombinationError(f"Preview generation failed; attempt preserved at {attempt}")
    staged_output = staging / STABLE_OUTPUT_NAME
    copy_verified(attempt_output, staged_output, output.sha256)
    staged_evidence = inspect_fits(staged_output, expected_channels=3)
    final_output = asdict(staged_evidence); final_output["path"] = str(paths["stable_output"])
    previous = attempt / "previous-processing-sho" if existing else None
    manifest = {
        "schema_version": 2, "helper_version": VERSION, "created_at": utc_now(), "status": "ready",
        "project": project_name, "project_path": str(paths["project"]),
        "stage_order": {"upstream": UPSTREAM_STAGE, "current": CURRENT_STAGE, "downstream": NEXT_STAGE},
        "upstream_stage": UPSTREAM_STAGE,
        "mono_linear_denoise_manifest": str(paths["mono_manifest"]),
        "mono_linear_denoise_manifest_sha256": mono_hash,
        "mono_linear_denoise_helper_version": mono_manifest.get("helper_version"),
        "mono_linear_denoise_visual_review_completed": mono_manifest.get("visual_review_completed"),
        "mono_linear_denoise_selected_candidates": mono_manifest.get("selected_candidates"),
        "mono_linear_denoise_denoise_applied": {name: mono_manifest.get("outputs", {}).get(name, {}).get("denoise_applied") for name in ("Ha", "SII", "OIII")},
        "mapping": {"red": "SII", "green": "Ha", "blue": "OIII"},
        "method": {"siril_command": "rgbcomp", "brightness_matching": False, "normalization": False, "permanent_stretch": False, "sum_exposure_headers": False},
        "inputs": {name: asdict(item) for name, item in input_evidence.items()},
        "output": final_output, "mapping_validation": mapping, "attempt": str(attempt),
        "script": str(script), "script_sha256": sha256_file(script), "siril_run": run_record,
        "preview": {"path": str(preview_path), "sha256": sha256_file(preview_path), "display_only": True, "linked_autostretch": True},
        "preview_script": str(preview_ssf), "preview_script_sha256": sha256_file(preview_ssf), "preview_run": preview_run,
        "previous_processing_sho_preserved_at": str(previous) if previous else None,
        "publication_method": "validate mono-linear-denoise inputs, compose fixed SHO mapping, preserve existing canonical directory, atomically publish",
        "siril_version_output": siril_output, "background_neutralization_permitted": True, "star_removal_permitted": False,
    }
    json_dump_atomic(staging / STABLE_MANIFEST_NAME, manifest)
    moved = False
    try:
        if existing:
            if previous.exists():
                raise ShoCombinationError(f"Preservation destination exists: {previous}")
            paths["sho"].rename(previous); moved = True
        staging.rename(paths["sho"])
    except Exception:
        if moved and not paths["sho"].exists(): previous.rename(paths["sho"])
        raise
    checked = status_project(project_name, workspace)
    if checked.get("status") != "ready":
        raise ShoCombinationError(f"Post-publication status verification failed: {checked}")
    result = {
        "status": "ready", "helper_version": VERSION, "project": project_name, "project_path": str(paths["project"]),
        "attempt": str(attempt), "existing_canonical_state_at_start": existing_state,
        "mono_linear_denoise_manifest": str(paths["mono_manifest"]), "mono_linear_denoise_manifest_sha256": mono_hash,
        "inputs": {name: {**asdict(item), "denoise_applied": mono_manifest.get("outputs", {}).get(name, {}).get("denoise_applied"), "selected_modulation": mono_manifest.get("outputs", {}).get(name, {}).get("selected_modulation")} for name, item in input_evidence.items()},
        "mapping": manifest["mapping"], "mapping_validation": mapping, "output": final_output,
        "preview": manifest["preview"], "stable_manifest": str(paths["stable_manifest"]),
        "previous_processing_sho_preserved_at": manifest["previous_processing_sho_preserved_at"],
        "next_stage": NEXT_STAGE, "background_neutralization_permitted": True, "star_removal_permitted": False, "post_publication_status_verified": True,
    }
    json_dump_atomic(attempt / "sho-combination-result.json", result)
    return result

def write_synthetic_mono(path: Path, *, name: str, base: float, phase: float) -> None:
    import numpy as np
    from astropy.io import fits
    yy, xx = np.mgrid[0:128, 0:160]
    image = (base + 0.025*np.sin(xx/17.0 + phase) + 0.015*np.cos(yy/19.0 - phase) + 0.08*np.exp(-(((xx-82.0)/25.0)**2 + ((yy-61.0)/20.0)**2))).astype(np.float32)
    header = fits.Header(); header["FILTER"] = name; header["EXPTIME"] = 300.0; header["STACKCNT"] = 8; header["LIVETIME"] = 2400.0
    fits.PrimaryHDU(data=image, header=header).writeto(path, overwrite=False, output_verify="fix")

def self_test(timeout_seconds: int) -> dict[str, Any]:
    real_workspace = derive_workspace()
    root = real_workspace / ".skill-self-tests" / "siril-sho-combination" / unique_id()
    workspace = root / "workspace"; project_name = "Synthetic SHO Combination 1.1.1"
    project = workspace / "Projects" / project_name; mono = project / "processing" / "mono-linear-denoise"
    mono.mkdir(parents=True, exist_ok=False)
    outputs: dict[str, Any] = {}
    for name, base, phase in (("Ha", 0.21, 0.3), ("SII", 0.11, 1.1), ("OIII", 0.31, 2.2)):
        path = mono / f"denoised_{name}.fit"; write_synthetic_mono(path, name=name, base=base, phase=phase)
        record = asdict(inspect_fits(path, expected_channels=1)); record.update({"denoise_applied": False, "selected_modulation": 0.0}); outputs[name] = record
    review = mono / "visual-review-record.json"; json_dump_atomic(review, {"schema_version": 2, "project": project_name, "visual_review_completed": True})
    manifest = mono / "mono-linear-denoise-manifest.json"
    json_dump_atomic(manifest, {
        "schema_version": 2, "helper_version": REQUIRED_MONO_DENOISE_HELPER_VERSION, "created_at": utc_now(), "status": "ready",
        "project": project_name, "project_path": str(project),
        "stage_order": {"upstream": "siril-mono-background-cleanup", "current": "siril-mono-linear-denoise", "downstream": "siril-sho-combination"},
        "selected_candidates": {name: "candidate-00" for name in ("Ha", "SII", "OIII")}, "outputs": outputs,
        "visual_review": {"record_path": str(review), "record_sha256": sha256_file(review), "all_contact_previews_inspected": True},
        "visual_review_completed": True, "sho_combination_permitted": True,
    })
    result = run_combination(
        project_name,
        workspace,
        timeout_seconds,
        False,
    )
    checked = status_project(project_name, workspace)
    if (
        checked.get("status") != "ready"
        or not all(
            item.get("passed")
            for item in result["mapping_validation"].values()
        )
        or checked.get("background_neutralization_permitted") is not True
        or checked.get("star_removal_permitted") is not False
    ):
        raise ShoCombinationError(
            f"Synthetic self-test failed: {checked}"
        )

    synthetic_paths = project_paths(workspace, project_name)
    output_hash_before_migration_test = sha256_file(
        synthetic_paths["stable_output"]
    )
    old_contract = json.loads(
        synthetic_paths["stable_manifest"].read_text(
            encoding="utf-8"
        )
    )
    old_contract["helper_version"] = MIGRATABLE_HELPER_VERSION
    old_contract["stage_order"]["downstream"] = (
        "siril-starnet-removal"
    )
    old_contract.pop(
        "background_neutralization_permitted",
        None,
    )
    old_contract["star_removal_permitted"] = True
    json_dump_atomic(
        synthetic_paths["stable_manifest"],
        old_contract,
    )
    obsolete = status_project(project_name, workspace)
    if (
        obsolete.get("status") != "obsolete"
        or obsolete.get("contract_migration_available") is not True
    ):
        raise ShoCombinationError(
            f"Synthetic migration precondition failed: {obsolete}"
        )
    migration = migrate_contract(project_name, workspace)
    migrated_status = status_project(project_name, workspace)
    if (
        migration.get("output_recomputed") is not False
        or migration.get("output_sha256_before")
        != output_hash_before_migration_test
        or migration.get("output_sha256_after")
        != output_hash_before_migration_test
        or migrated_status.get("status") != "ready"
        or migrated_status.get(
            "background_neutralization_permitted"
        ) is not True
        or migrated_status.get("star_removal_permitted") is not False
    ):
        raise ShoCombinationError(
            "Synthetic contract-migration self-test failed."
        )
    return {
        "status": "success", "helper_version": VERSION, "self_test_directory": str(root),
        "synthetic_mono_manifest": str(manifest), "synthetic_mono_manifest_sha256": sha256_file(manifest),
        "output": result["output"], "mapping_validation": result["mapping_validation"], "preview": result["preview"],
        "final_status": migrated_status["status"], "background_neutralization_permitted": migrated_status["background_neutralization_permitted"], "star_removal_permitted": migrated_status["star_removal_permitted"], "contract_migration": migration,
    }

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Combine validated mono-linear-denoise outputs into linear SHO.")
    parser.add_argument("--version", action="version", version=VERSION)
    subs = parser.add_subparsers(dest="command")
    p = subs.add_parser("self-test"); p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    p = subs.add_parser("run"); p.add_argument("--project", required=True); p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS); p.add_argument("--fresh-run", action="store_true")
    p = subs.add_parser("migrate-contract"); p.add_argument("--project", required=True)
    p = subs.add_parser("status"); p.add_argument("--project", required=True)
    return parser

def main() -> int:
    parser = build_parser(); args = parser.parse_args()
    if args.command is None: parser.print_help(); return 2
    workspace = derive_workspace()
    try:
        if args.command == "self-test":
            payload = self_test(args.timeout)
        elif args.command == "run":
            payload = run_combination(
                args.project,
                workspace,
                args.timeout,
                args.fresh_run,
            )
        elif args.command == "migrate-contract":
            payload = migrate_contract(
                args.project,
                workspace,
            )
        else:
            payload = status_project(
                args.project,
                workspace,
            )
    except Exception as exc:
        print(json.dumps({"status": "blocked", "helper_version": VERSION, "error": str(exc)}, indent=2, sort_keys=True)); return 2
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload.get("status") in {"success", "ready", "not_run", "obsolete"} else 2

if __name__ == "__main__":
    raise SystemExit(main())
