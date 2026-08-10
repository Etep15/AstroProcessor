#!/usr/bin/env python3
"""Canonical StarNet workflow for validated SHO projects.

This helper deliberately separates two different products:

* a linear starless FITS image, obtained by inverse-transforming the temporary
  nonlinear StarNet result back to the original linear domain; and
* StarNet's native ``-m`` starmask, kept in the nonlinear 16-bit TIFF domain
  that StarNet actually produced.

The starmask is not treated as ``original - starless`` and is not required
(or expected) to reconstruct the source by linear addition.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import math
import os
import shutil
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

VERSION = "1.5.2"
ADAPTIVE_REQUIRED_VERSION = "1.2.0"
BACKEND_REQUIRED_VERSION = "1.0.4"
DEFAULT_TIMEOUT_SECONDS = 7200
MAX_RETRIES_LIMIT = 3
REQUIRED_BACKGROUND_HELPER_VERSION = "1.1.0"
UPSTREAM_STAGE = "siril-background-neutralization"
CURRENT_STAGE = "siril-starnet-removal"
NEXT_STAGE = "siril-ghs-stretch-pass1"
REVIEW_SCHEMA_VERSION = 1
SIRIL_SAVE_PATH_LIMIT_BYTES = 255
STARNET_GENERATED_BASENAMES = (
    "SHO_starless_stretched.fit",
    "starnetmask_SHO_input_stretched.fit",
    "starnetdescreen_SHO_input_stretched.fit",
)

REFERENCE_SOURCE_SHA256 = (
    "09f8a7f04817da13cb46e46b68ec922a02baac85cb18b41a3c35d12818b0a7a2"
)
REFERENCE_MASK_SHA256 = (
    "fc0434ba2c36d8d3e96104e6f7d24dd45431f39c7c5045f97f3d0fd75140f7b7"
)

# The first profile approximates the old processed image's ~0.15 median and
# x1/no-upsample history. Three retries are permitted, never more.
NATIVE_CONFIGS = (
    {
        "label": "baseline-target-0.15-x1",
        "target_background": 0.15,
        "upsample": False,
        "stride": 256,
        "protect_highlights": True,
    },
    {
        "label": "retry-1-target-0.10-x1",
        "target_background": 0.10,
        "upsample": False,
        "stride": 256,
        "protect_highlights": True,
    },
    {
        "label": "retry-2-target-0.06-x1",
        "target_background": 0.06,
        "upsample": False,
        "stride": 256,
        "protect_highlights": True,
    },
    {
        "label": "retry-3-target-0.10-x2",
        "target_background": 0.10,
        "upsample": True,
        "stride": 256,
        "protect_highlights": True,
    },
)

NATIVE_MASK_THRESHOLDS = {
    "minimum_quantized_16bit_fraction": 0.995,
    "minimum_exact_zero_fraction": 0.40,
    "maximum_negative_fraction": 0.0,
    "minimum_nonzero_fraction": 0.001,
}

# Native StarNet -m masks are expected to have an exactly-zero or almost-zero
# background. Ratio gates are applied only when the denominator is large
# enough to be stable relative to the measured stellar signal.
NATIVE_RATIO_BACKGROUND_FRACTION_OF_STAR_CORE = 1.0e-4

SCRIPT_DIR = Path(__file__).resolve().parent
ADAPTIVE_PATH = SCRIPT_DIR / "adaptive_starnet_removal.py"
BACKEND_PATH = SCRIPT_DIR / "starnet_removal.py"


class NativeStarMaskError(RuntimeError):
    pass


def _load_module(path: Path, name: str):
    if not path.is_file():
        raise NativeStarMaskError(f"Required helper is missing: {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise NativeStarMaskError(f"Cannot load helper: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


adaptive = _load_module(ADAPTIVE_PATH, "siril_starnet_adaptive_v120")
base = _load_module(BACKEND_PATH, "siril_starnet_backend_v104")

if getattr(adaptive, "VERSION", None) != ADAPTIVE_REQUIRED_VERSION:
    raise NativeStarMaskError(
        f"Native helper requires adaptive {ADAPTIVE_REQUIRED_VERSION}; "
        f"found {getattr(adaptive, 'VERSION', None)}"
    )
if getattr(base, "VERSION", None) != BACKEND_REQUIRED_VERSION:
    raise NativeStarMaskError(
        f"Native helper requires backend {BACKEND_REQUIRED_VERSION}; "
        f"found {getattr(base, 'VERSION', None)}"
    )


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def project_paths(workspace: Path, project_name: str) -> dict[str, Path]:
    project = workspace / "Projects" / project_name
    processing = project / "processing"
    stable = processing / "starnet"
    return {
        "project": project,
        "processing": processing,
        "source": (
            processing
            / "background-neutralization"
            / "SHO-linear-neutralized.fit"
        ),
        "background_manifest": (
            processing
            / "background-neutralization"
            / "background-neutralization-manifest.json"
        ),
        "runs": project / ".siril-starnet",
        "reference_runs": project / ".siril-starnet-reference",
        "stable": stable,
        "stable_starless": stable / "SHO-starless-linear.fit",
        "stable_mask": stable / "SHO-starmask.fit",
        "stable_unscreen": stable / "SHO-stars-unscreen.fit",
        "stable_manifest": stable / "starnet-manifest.json",
        "stable_review": stable / "visual-review-record.json",
        "stable_source_preview": (
            stable / "SHO-linear-neutralized-before-linked.png"
        ),
        "stable_starless_preview": (
            stable / "SHO-starless-linear-linked.png"
        ),
        "stable_mask_linked_preview": (
            stable / "SHO-starmask-linked.png"
        ),
        "stable_mask_unlinked_preview": (
            stable / "SHO-starmask-unlinked.png"
        ),
        "stable_unscreen_preview": (
            stable / "SHO-stars-unscreen-linked.png"
        ),
        "legacy_starnet_native": processing / "starnet-native",
    }


def load_background_manifest(
    paths: dict[str, Path],
) -> tuple[dict[str, Any], str]:
    manifest_path = paths["background_manifest"]
    if not manifest_path.is_file():
        raise NativeStarMaskError(
            f"Background-neutralization manifest is missing: "
            f"{manifest_path}"
        )
    manifest_hash = base.sha256_file(manifest_path)
    try:
        manifest = json.loads(
            manifest_path.read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise NativeStarMaskError(
            f"Cannot read background-neutralization manifest: {exc}"
        ) from exc

    if manifest.get("project") != paths["project"].name:
        raise NativeStarMaskError(
            "Background-neutralization manifest project does not match."
        )
    if (
        Path(str(manifest.get("project_path", ""))).resolve()
        != paths["project"].resolve()
    ):
        raise NativeStarMaskError(
            "Background-neutralization project path does not match."
        )
    if manifest.get("status") != "ready":
        raise NativeStarMaskError(
            "Background-neutralization status is not ready."
        )
    if (
        manifest.get("helper_version")
        != REQUIRED_BACKGROUND_HELPER_VERSION
    ):
        raise NativeStarMaskError(
            "Expected background-neutralization helper "
            f"{REQUIRED_BACKGROUND_HELPER_VERSION}; found "
            f"{manifest.get('helper_version')!r}."
        )
    if manifest.get("visual_review_completed") is not True:
        raise NativeStarMaskError(
            "Background-neutralization visual review is incomplete."
        )
    if manifest.get("star_removal_permitted") is not True:
        raise NativeStarMaskError(
            "Background-neutralization does not permit StarNet."
        )
    if manifest.get("stage_order") != {
        "upstream": "siril-sho-combination",
        "current": UPSTREAM_STAGE,
        "downstream": CURRENT_STAGE,
    }:
        raise NativeStarMaskError(
            "Background-neutralization stage order is invalid."
        )
    return manifest, manifest_hash


def validate_background_input(
    paths: dict[str, Path],
    manifest: dict[str, Any],
):
    output = manifest.get("output", {})
    recorded_path = Path(str(output.get("path", ""))).resolve()
    if recorded_path != paths["source"].resolve():
        raise NativeStarMaskError(
            "Background-neutralization manifest does not reference the "
            "canonical neutralized FITS."
        )
    evidence = base.inspect_fits(
        paths["source"],
        expected_channels=3,
        require_float32=True,
    )
    if evidence.bitpix != -32 or evidence.finite_fraction != 1.0:
        raise NativeStarMaskError(
            "StarNet input must be finite BITPIX -32 RGB data."
        )
    if evidence.sha256 != output.get("sha256"):
        raise NativeStarMaskError(
            "StarNet input checksum does not match the upstream manifest."
        )
    for key in ("width", "height", "channels", "bitpix"):
        expected = output.get(key)
        if expected is not None and getattr(evidence, key) != expected:
            raise NativeStarMaskError(
                f"StarNet input {key} differs from the upstream manifest."
            )
    return evidence


def _source_preview_script(
    source_path: Path,
    preview_stem: Path,
) -> str:
    return "\n".join(
        (
            f"requires {base.MINIMUM_SIRIL_VERSION}",
            f'load "{source_path}"',
            "autostretch -linked",
            f'savepng "{preview_stem}"',
            "close",
            "",
        )
    )


def create_source_preview(
    source_path: Path,
    run_root: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    preview_dir = run_root / "source-preview"
    logs = preview_dir / "logs"
    preview_dir.mkdir(parents=True, exist_ok=False)
    logs.mkdir()
    preview_stem = preview_dir / "SHO-linear-neutralized-before-linked"
    script = preview_dir / "source-preview.ssf"
    script.write_text(
        _source_preview_script(source_path, preview_stem),
        encoding="utf-8",
    )
    run = base.run_siril(
        workdir=preview_dir,
        script=script,
        stdout_path=logs / "stdout.log",
        stderr_path=logs / "stderr.log",
        timeout_seconds=min(timeout_seconds, 600),
    )
    run.pop("combined_log_text", None)
    preview = preview_stem.with_suffix(".png")
    if (
        run["timed_out"]
        or run["exit_status"] != 0
        or run["fatal_log_markers"]
        or not preview.is_file()
    ):
        raise NativeStarMaskError(
            f"Source preview generation failed; evidence at {preview_dir}"
        )
    return {
        "path": str(preview),
        "sha256": base.sha256_file(preview),
        "script": str(script),
        "script_sha256": base.sha256_file(script),
        "run": run,
    }



def _find_single_prefixed_fits(directory: Path, prefix: str) -> Path | None:
    suffixes = (".fit", ".fits", ".fts", ".fit.fz", ".fits.fz", ".fts.fz")
    matches = sorted(
        path
        for path in directory.iterdir()
        if path.is_file()
        and path.name.lower().startswith(prefix.lower())
        and path.name.lower().endswith(suffixes)
    )
    return matches[0] if len(matches) == 1 else None


def _native_siril_script(
    executable: Path,
    *,
    source_filename: str,
    saved_starless_stem: str,
    stride: int,
    upsample: bool,
    protect_highlights: bool,
) -> str:
    executable_text = str(executable)
    if '"' in executable_text or "\n" in executable_text:
        raise NativeStarMaskError(f"Unsafe StarNet executable path: {executable}")
    upsample_option = "--upsample" if upsample else "--no-upsample"
    highlight_option = (
        "--protect-highlights"
        if protect_highlights
        else "--disable-highlights-protection"
    )
    return "\n".join(
        [
            f"requires {base.MINIMUM_SIRIL_VERSION}",
            "setext fit",
            f'load "{source_filename}"',
            (
                f'pyscript StarNet.py --exe "{executable_text}" '
                f"--no-linear --stride {int(stride)} {upsample_option} "
                f"{highlight_option} --masks starnet-mask,starnet-unscreen"
            ),
            f"save {saved_starless_stem} -chksum",
            "close",
            "",
        ]
    )


def _copy_fits_with_history(
    source: Path,
    destination: Path,
    *,
    filter_value: str,
    history_lines: list[str],
):
    from astropy.io import fits
    import numpy as np

    if destination.exists():
        raise NativeStarMaskError(f"Refusing to overwrite: {destination}")
    with fits.open(source, memmap=False, do_not_scale_image_data=False) as hdul:
        data = np.asarray(hdul[0].data, dtype=np.float32)
        header = hdul[0].header.copy()
    header["FILTER"] = filter_value
    for line in history_lines:
        header["HISTORY"] = line
    fits.PrimaryHDU(data=data, header=header).writeto(
        destination, overwrite=False, checksum=True
    )
    return base.inspect_fits(destination, expected_channels=3, require_float32=True)


def starmask_semantics(path: Path) -> dict[str, Any]:
    import numpy as np
    from astropy.io import fits

    with fits.open(path, memmap=False, do_not_scale_image_data=False) as hdul:
        data = np.asarray(hdul[0].data, dtype=np.float32)
        filter_value = str(hdul[0].header.get("FILTER", ""))
    if data.ndim != 3 or data.shape[0] != 3:
        raise NativeStarMaskError(f"Starmask must be RGB; found {data.shape}")

    finite = np.isfinite(data)
    finite_fraction = float(np.mean(finite))
    safe = np.nan_to_num(data, nan=0.0, posinf=1.0, neginf=0.0)
    clipped = np.clip(safe, 0.0, 1.0)
    quantized = np.rint(clipped * 65535.0) / 65535.0
    quantization_error = np.abs(clipped - quantized)

    record = {
        "finite_fraction": finite_fraction,
        "minimum": float(np.min(safe)),
        "maximum": float(np.max(safe)),
        "mean": float(np.mean(safe)),
        "median": float(np.median(safe)),
        "negative_fraction": float(np.mean(safe < 0.0)),
        "exact_zero_fraction": float(np.mean(safe == 0.0)),
        "nonzero_fraction": float(np.mean(safe != 0.0)),
        "quantized_16bit_fraction": float(
            np.mean(quantization_error <= 1.0e-8)
        ),
        "maximum_16bit_quantization_error": float(np.max(quantization_error)),
        "filter_header": filter_value,
        "thresholds": dict(NATIVE_MASK_THRESHOLDS),
    }

    failed = []
    if finite_fraction != 1.0:
        failed.append({"metric": "finite_fraction", "required": 1.0, "value": finite_fraction})
    if record["negative_fraction"] > NATIVE_MASK_THRESHOLDS["maximum_negative_fraction"]:
        failed.append(
            {
                "metric": "negative_fraction",
                "maximum_allowed": NATIVE_MASK_THRESHOLDS["maximum_negative_fraction"],
                "value": record["negative_fraction"],
            }
        )
    if record["quantized_16bit_fraction"] < NATIVE_MASK_THRESHOLDS["minimum_quantized_16bit_fraction"]:
        failed.append(
            {
                "metric": "quantized_16bit_fraction",
                "minimum_required": NATIVE_MASK_THRESHOLDS["minimum_quantized_16bit_fraction"],
                "value": record["quantized_16bit_fraction"],
            }
        )
    if record["exact_zero_fraction"] < NATIVE_MASK_THRESHOLDS["minimum_exact_zero_fraction"]:
        failed.append(
            {
                "metric": "exact_zero_fraction",
                "minimum_required": NATIVE_MASK_THRESHOLDS["minimum_exact_zero_fraction"],
                "value": record["exact_zero_fraction"],
            }
        )
    if record["nonzero_fraction"] < NATIVE_MASK_THRESHOLDS["minimum_nonzero_fraction"]:
        failed.append(
            {
                "metric": "nonzero_fraction",
                "minimum_required": NATIVE_MASK_THRESHOLDS["minimum_nonzero_fraction"],
                "value": record["nonzero_fraction"],
            }
        )
    record["failed_checks"] = failed
    record["satisfactory"] = not failed
    record["status"] = "satisfactory" if not failed else "needs_retry"
    return record


def starmask_diffuse_gate(
    diffuse: dict[str, Any],
) -> dict[str, Any]:
    """Apply stable diffuse-structure gates to a sparse StarNet starmask."""
    metrics = diffuse["metrics"]
    thresholds = diffuse["thresholds"]
    failed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    def check_max(metric: str, value: float, maximum: float) -> None:
        if value > maximum:
            failed.append(
                {
                    "metric": metric,
                    "value": float(value),
                    "maximum_allowed": float(maximum),
                }
            )

    luma = metrics["luma_detail"]
    luma_floor = max(
        1.0e-12,
        float(luma["star_core_clip_value"])
        * NATIVE_RATIO_BACKGROUND_FRACTION_OF_STAR_CORE,
    )
    if float(luma["background_median"]) >= luma_floor:
        check_max(
            "luma_nebula_background_ratio",
            float(metrics["luma_nebula_background_ratio"]),
            float(thresholds["luma_nebula_background_ratio"]),
        )
    else:
        skipped.append(
            {
                "metric": "luma_nebula_background_ratio",
                "reason": "near-zero starmask background makes the ratio unstable",
                "background_median": float(luma["background_median"]),
                "minimum_stable_background": float(luma_floor),
                "diagnostic_value": float(
                    metrics["luma_nebula_background_ratio"]
                ),
            }
        )

    for channel_name, detail in metrics["per_channel"].items():
        floor = max(
            1.0e-12,
            float(detail["star_core_clip_value"])
            * NATIVE_RATIO_BACKGROUND_FRACTION_OF_STAR_CORE,
        )
        if float(detail["background_median"]) >= floor:
            check_max(
                f"{channel_name}_nebula_background_ratio",
                float(detail["nebula_background_ratio"]),
                float(thresholds["worst_channel_nebula_background_ratio"]),
            )
        else:
            skipped.append(
                {
                    "metric": f"{channel_name}_nebula_background_ratio",
                    "reason": "near-zero starmask background makes the ratio unstable",
                    "background_median": float(detail["background_median"]),
                    "minimum_stable_background": float(floor),
                    "diagnostic_value": float(
                        detail["nebula_background_ratio"]
                    ),
                }
            )

    check_max(
        "luma_structure_correlation",
        float(metrics["luma_structure_correlation"]),
        float(thresholds["luma_structure_correlation"]),
    )
    check_max(
        "worst_channel_structure_correlation",
        float(metrics["worst_channel_structure_correlation"]),
        float(thresholds["worst_channel_structure_correlation"]),
    )
    check_max(
        "luma_relative_nebula_leakage",
        float(metrics["luma_relative_nebula_leakage"]),
        float(thresholds["luma_relative_nebula_leakage"]),
    )
    check_max(
        "worst_channel_relative_nebula_leakage",
        float(metrics["worst_channel_relative_nebula_leakage"]),
        float(thresholds["worst_channel_relative_nebula_leakage"]),
    )
    check_max(
        "remaining_star_peak_energy_ratio",
        float(metrics["remaining_star_peak_energy_ratio"]),
        float(thresholds["remaining_star_peak_energy_ratio"]),
    )

    return {
        "status": "satisfactory" if not failed else "needs_retry",
        "satisfactory": not failed,
        "failed_checks": failed,
        "skipped_unstable_ratio_checks": skipped,
        "ratio_background_fraction_of_star_core": (
            NATIVE_RATIO_BACKGROUND_FRACTION_OF_STAR_CORE
        ),
        "raw_adaptive_status": diffuse.get("status"),
        "metrics": metrics,
        "thresholds": thresholds,
        "interpretation": (
            "Starmask diffuse gates passed. Ratios with near-zero "
            "background denominators were retained only as diagnostics."
            if not failed
            else "The starmask failed one or more stable diffuse-structure "
            "or remaining-star gates."
        ),
    }


def starmask_quality_from_paths(
    stretched_source: Path,
    stretched_starless: Path,
    native_mask: Path,
) -> dict[str, Any]:
    raw_diffuse = adaptive.quality_metrics_from_paths(
        stretched_source,
        stretched_starless,
        native_mask,
    )
    diffuse = starmask_diffuse_gate(raw_diffuse)
    semantics = starmask_semantics(native_mask)
    satisfactory = bool(diffuse["satisfactory"]) and bool(
        semantics["satisfactory"]
    )
    failed = list(diffuse["failed_checks"]) + list(
        semantics["failed_checks"]
    )
    return {
        "status": "satisfactory" if satisfactory else "needs_retry",
        "satisfactory": satisfactory,
        "failed_checks": failed,
        "diffuse_structure_assessment": diffuse,
        "raw_adaptive_diffuse_assessment": raw_diffuse,
        "starmask_semantics": semantics,
        "interpretation": (
            "The StarNet starmask is sparse, 16-bit-derived and free of "
            "unacceptable broad nebular structure."
            if satisfactory
            else "The StarNet starmask failed one or more mask-semantic or "
            "stable diffuse-structure gates; another controlled starmask "
            "attempt is required."
        ),
    }



def _preview_script(
    linear_starless: Path,
    native_mask: Path,
    native_unscreen: Path,
    preview_dir: Path,
) -> str:
    return "\n".join(
        [
            f"requires {base.MINIMUM_SIRIL_VERSION}",
            f'load "{linear_starless}"',
            "autostretch -linked",
            f'savepng "{preview_dir / "SHO-starless-linear-linked"}"',
            "close",
            f'load "{native_mask}"',
            "autostretch -linked",
            f'savepng "{preview_dir / "SHO-starmask-linked"}"',
            "close",
            f'load "{native_mask}"',
            "autostretch",
            f'savepng "{preview_dir / "SHO-starmask-unlinked"}"',
            "close",
            f'load "{native_unscreen}"',
            "autostretch -linked",
            f'savepng "{preview_dir / "SHO-stars-unscreen-linked"}"',
            "close",
            "",
        ]
    )


def starnet_path_budget(workdir: Path) -> dict[str, Any]:
    """Validate expected generated paths against Siril's 255-byte limit."""
    records: list[dict[str, Any]] = []
    for basename in STARNET_GENERATED_BASENAMES:
        candidate = workdir / basename
        byte_length = len(os.fsencode(str(candidate)))
        records.append(
            {
                "path": str(candidate),
                "basename": basename,
                "byte_length": byte_length,
                "limit_bytes": SIRIL_SAVE_PATH_LIMIT_BYTES,
                "within_limit": (
                    byte_length <= SIRIL_SAVE_PATH_LIMIT_BYTES
                ),
            }
        )
    maximum = max(item["byte_length"] for item in records)
    within_limit = all(item["within_limit"] for item in records)
    evidence = {
        "workdir": str(workdir),
        "workdir_byte_length": len(os.fsencode(str(workdir))),
        "limit_bytes": SIRIL_SAVE_PATH_LIMIT_BYTES,
        "maximum_expected_path_bytes": maximum,
        "remaining_margin_bytes": (
            SIRIL_SAVE_PATH_LIMIT_BYTES - maximum
        ),
        "within_limit": within_limit,
        "expected_generated_paths": records,
    }
    if not within_limit:
        longest = max(records, key=lambda item: item["byte_length"])
        raise NativeStarMaskError(
            "StarNet execution path exceeds Siril's 255-byte save-path "
            f"limit before processing begins. Longest expected path is "
            f"{longest['byte_length']} bytes: {longest['path']}. "
            "Use a shorter project or execution-workspace path."
        )
    return evidence


def execute_native_candidate(
    *,
    source_path: Path,
    source_evidence,
    candidate_dir: Path,
    config: dict[str, Any],
    timeout_seconds: int,
) -> dict[str, Any]:
    workdir = candidate_dir / "work"
    logs = candidate_dir / "logs"
    previews = candidate_dir / "previews"
    for directory in (workdir, logs, previews):
        directory.mkdir(parents=True, exist_ok=False)

    path_budget = starnet_path_budget(workdir)

    staged_script, upstream = base.stage_vendored_starnet(workdir)
    cli = base.starnet_cli_version()

    stretched_input = workdir / "SHO_input_stretched.fit"
    stretch_record = adaptive.prepare_controlled_input(
        source_path,
        stretched_input,
        float(config["target_background"]),
    )

    script_path = candidate_dir / "starnet-native.ssf"
    script_path.write_text(
        _native_siril_script(
            Path(cli["path"]),
            source_filename=stretched_input.name,
            saved_starless_stem="SHO_starless_stretched",
            stride=int(config["stride"]),
            upsample=bool(config["upsample"]),
            protect_highlights=bool(config["protect_highlights"]),
        ),
        encoding="utf-8",
    )

    run_record = base.run_siril(
        workdir=workdir,
        script=script_path,
        stdout_path=logs / "stdout.log",
        stderr_path=logs / "stderr.log",
        timeout_seconds=timeout_seconds,
    )
    combined_log = run_record.pop("combined_log_text")
    if run_record["timed_out"]:
        raise NativeStarMaskError(
            f"Native StarNet candidate timed out; preserved at {candidate_dir}"
        )
    if run_record["exit_status"] != 0:
        raise NativeStarMaskError(
            "Native StarNet candidate exited unsuccessfully; preserved at "
            f"{candidate_dir}. Exit={run_record['exit_status']}, "
            f"markers={run_record['fatal_log_markers']}"
        )

    lower = combined_log.lower()
    required_success_markers = (
        "starnet: working: done!",
        "saved starmask",
        "saved unscreen stars",
    )
    missing_success_markers = [
        marker
        for marker in required_success_markers
        if marker not in lower
    ]

    stretched_starless = workdir / "SHO_starless_stretched.fit"
    repository_mask = _find_single_prefixed_fits(
        workdir,
        "starnetmask_",
    )
    repository_unscreen = _find_single_prefixed_fits(
        workdir,
        "starnetdescreen_",
    )
    expected_products_present = (
        stretched_starless.is_file()
        and repository_mask is not None
        and repository_unscreen is not None
    )

    broad_marker = "starnet: could not"
    reported_markers = list(run_record["fatal_log_markers"])
    hard_markers = [
        marker
        for marker in reported_markers
        if marker != broad_marker
    ]
    broad_marker_lines = [
        line.strip()
        for line in combined_log.splitlines()
        if broad_marker in line.lower()
    ]

    if hard_markers:
        raise NativeStarMaskError(
            "Native StarNet candidate produced a hard failure marker; "
            f"preserved at {candidate_dir}. Markers={hard_markers}"
        )
    if missing_success_markers or not expected_products_present:
        raise NativeStarMaskError(
            "Native StarNet completion evidence is incomplete; preserved at "
            f"{candidate_dir}. Missing success markers="
            f"{missing_success_markers}, expected products present="
            f"{expected_products_present}, reported markers="
            f"{reported_markers}, matching log lines="
            f"{broad_marker_lines}"
        )

    ignored_nonfatal_markers: list[dict[str, Any]] = []
    if broad_marker in reported_markers:
        ignored_nonfatal_markers.append(
            {
                "marker": broad_marker,
                "matching_log_lines": broad_marker_lines,
                "reason": (
                    "Siril exited 0, all required StarNet completion "
                    "messages were present, and the starless, native mask, "
                    "and native unscreen products were all created. The "
                    "broad substring is therefore retained as diagnostic "
                    "evidence rather than treated as a standalone failure."
                ),
            }
        )
        run_record["fatal_log_markers"] = [
            marker
            for marker in reported_markers
            if marker != broad_marker
        ]
    run_record["ignored_nonfatal_markers"] = ignored_nonfatal_markers
    run_record["completion_evidence"] = {
        "required_success_markers": list(required_success_markers),
        "missing_success_markers": missing_success_markers,
        "stretched_starless_exists": stretched_starless.is_file(),
        "native_starmask_exists": repository_mask is not None,
        "native_unscreen_exists": repository_unscreen is not None,
    }

    base.inspect_fits(
        stretched_starless,
        expected_channels=3,
        require_float32=True,
    )

    linear_starless = workdir / "SHO_starless_linear.fit"
    linear_starless_evidence = adaptive.destretch_starless(
        stretched_starless,
        source_path,
        linear_starless,
        stretch_record,
    )
    if (
        linear_starless_evidence.width != source_evidence.width
        or linear_starless_evidence.height != source_evidence.height
    ):
        raise NativeStarMaskError("Linear starless dimensions differ from source.")

    native_mask = workdir / "SHO_starmask.fit"
    native_mask_evidence = _copy_fits_with_history(
        repository_mask,
        native_mask,
        filter_value="mixed_Starmask",
        history_lines=[
            "StarNet native -m mask; not original minus starless.",
            f"Starmask helper version {VERSION}.",
        ],
    )
    native_unscreen = workdir / "SHO_stars_unscreen.fit"
    native_unscreen_evidence = _copy_fits_with_history(
        repository_unscreen,
        native_unscreen,
        filter_value="mixed_StarsUnscreen",
        history_lines=[
            "StarNet native -n unscreen layer in nonlinear StarNet domain.",
            f"Starmask helper version {VERSION}.",
        ],
    )

    quality = starmask_quality_from_paths(
        stretched_input,
        stretched_starless,
        native_mask,
    )

    preview_script = candidate_dir / "preview.ssf"
    preview_script.write_text(
        _preview_script(linear_starless, native_mask, native_unscreen, previews),
        encoding="utf-8",
    )
    preview_run = base.run_siril(
        workdir=candidate_dir,
        script=preview_script,
        stdout_path=logs / "preview-stdout.log",
        stderr_path=logs / "preview-stderr.log",
        timeout_seconds=min(timeout_seconds, 600),
    )
    preview_run.pop("combined_log_text", None)

    result = {
        "status": quality["status"],
        "created_at": utc_now(),
        "helper_version": VERSION,
        "adaptive_version": adaptive.VERSION,
        "backend_version": base.VERSION,
        "candidate": candidate_dir.name,
        "candidate_directory": str(candidate_dir),
        "config": dict(config),
        "path_budget": path_budget,
        "controlled_stretch": stretch_record,
        "script": str(script_path),
        "script_sha256": base.sha256_file(script_path),
        "siril_run": run_record,
        "staged_starnet_script": str(staged_script),
        "upstream_starnet": upstream,
        "starnet_cli": cli,
        "linear_starless": asdict(linear_starless_evidence),
        "starmask": asdict(native_mask_evidence),
        "unscreen_stars": asdict(native_unscreen_evidence),
        "repository_starmask": str(repository_mask),
        "repository_unscreen": str(repository_unscreen),
        "quality_assessment": quality,
        "preview_script": str(preview_script),
        "preview_run": preview_run,
        "previews": {
            "starless_linear_linked": str(previews / "SHO-starless-linear-linked.png")
            if (previews / "SHO-starless-linear-linked.png").is_file()
            else None,
            "starmask_linked": str(previews / "SHO-starmask-linked.png")
            if (previews / "SHO-starmask-linked.png").is_file()
            else None,
            "starmask_unlinked": str(previews / "SHO-starmask-unlinked.png")
            if (previews / "SHO-starmask-unlinked.png").is_file()
            else None,
            "unscreen_linked": str(previews / "SHO-stars-unscreen-linked.png")
            if (previews / "SHO-stars-unscreen-linked.png").is_file()
            else None,
        },
    }
    base.json_dump_atomic(candidate_dir / "candidate-result.json", result)
    return result


def _reference_similarity(generated_path: Path, reference_path: Path) -> dict[str, Any]:
    import numpy as np
    from astropy.io import fits

    with fits.open(generated_path, memmap=False, do_not_scale_image_data=False) as hdul:
        generated = np.asarray(hdul[0].data, dtype=np.float32)
    with fits.open(reference_path, memmap=False, do_not_scale_image_data=False) as hdul:
        reference = np.asarray(hdul[0].data, dtype=np.float32)
    if generated.shape != reference.shape:
        return {
            "status": "different_dimensions",
            "generated_shape": list(generated.shape),
            "reference_shape": list(reference.shape),
            "satisfactory": False,
        }

    weights = np.asarray([0.2126, 0.7152, 0.0722], dtype=np.float32)[:, None, None]
    gen_luma = np.sum(np.maximum(generated, 0.0) * weights, axis=0)
    ref_luma = np.sum(np.maximum(reference, 0.0) * weights, axis=0)
    x = gen_luma.ravel().astype(np.float64)
    y = ref_luma.ravel().astype(np.float64)
    x_centered = x - x.mean()
    y_centered = y - y.mean()
    denominator = math.sqrt(float(np.sum(x_centered * x_centered) * np.sum(y_centered * y_centered)))
    correlation = float(np.sum(x_centered * y_centered) / denominator) if denominator > 0 else 0.0

    scale_den = float(np.dot(x, x))
    scale = float(np.dot(x, y) / scale_den) if scale_den > 0 else 0.0
    residual = scale * x - y
    normalized_rmse = float(
        math.sqrt(float(np.mean(residual * residual)))
        / max(1.0e-12, float(np.percentile(y, 99.9)))
    )

    gen_threshold = float(np.percentile(x[x > 0], 95.0)) if np.any(x > 0) else 0.0
    ref_threshold = float(np.percentile(y[y > 0], 95.0)) if np.any(y > 0) else 0.0
    gen_support = x >= gen_threshold if gen_threshold > 0 else np.zeros_like(x, dtype=bool)
    ref_support = y >= ref_threshold if ref_threshold > 0 else np.zeros_like(y, dtype=bool)
    intersection = int(np.sum(gen_support & ref_support))
    support_total = int(np.sum(gen_support) + np.sum(ref_support))
    dice = float(2 * intersection / support_total) if support_total else 1.0

    zero_difference = abs(float(np.mean(generated == 0.0)) - float(np.mean(reference == 0.0)))
    satisfactory = (
        correlation >= 0.65
        and dice >= 0.45
        and normalized_rmse <= 0.35
        and zero_difference <= 0.25
    )
    return {
        "status": "reproduced" if satisfactory else "different_but_reviewable",
        "satisfactory": satisfactory,
        "correlation": correlation,
        "bright_support_dice": dice,
        "least_squares_scale": scale,
        "normalized_rmse": normalized_rmse,
        "exact_zero_fraction_difference": zero_difference,
        "thresholds": {
            "minimum_correlation": 0.65,
            "minimum_bright_support_dice": 0.45,
            "maximum_normalized_rmse": 0.35,
            "maximum_exact_zero_fraction_difference": 0.25,
        },
    }


def reference_check(
    workspace: Path,
    project_name: str,
    source_path: Path,
    reference_mask_path: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    paths = project_paths(workspace, project_name)
    if base.sha256_file(source_path) != REFERENCE_SOURCE_SHA256:
        raise NativeStarMaskError(
            "Reference processed image checksum does not match the uploaded known file."
        )
    if base.sha256_file(reference_mask_path) != REFERENCE_MASK_SHA256:
        raise NativeStarMaskError(
            "Reference good starmask checksum does not match the uploaded known file."
        )

    root = paths["reference_runs"] / base.unique_id()
    workdir = root / "work"
    logs = root / "logs"
    previews = root / "previews"
    for directory in (workdir, logs, previews):
        directory.mkdir(parents=True, exist_ok=False)

    source_evidence = base.inspect_fits(source_path, expected_channels=3, require_float32=True)
    reference_evidence = base.inspect_fits(reference_mask_path, expected_channels=3, require_float32=True)
    if (
        source_evidence.width != reference_evidence.width
        or source_evidence.height != reference_evidence.height
    ):
        raise NativeStarMaskError("Reference source and mask dimensions differ.")

    work_source = workdir / "reference_processed.fit"
    base.copy_verified(source_path, work_source, REFERENCE_SOURCE_SHA256)
    staged_script, upstream = base.stage_vendored_starnet(workdir)
    cli = base.starnet_cli_version()

    script_path = root / "reference-native.ssf"
    script_path.write_text(
        _native_siril_script(
            Path(cli["path"]),
            source_filename=work_source.name,
            saved_starless_stem="reference_starless",
            stride=256,
            upsample=False,
            protect_highlights=True,
        ),
        encoding="utf-8",
    )
    run_record = base.run_siril(
        workdir=workdir,
        script=script_path,
        stdout_path=logs / "stdout.log",
        stderr_path=logs / "stderr.log",
        timeout_seconds=timeout_seconds,
    )
    combined_log = run_record.pop("combined_log_text")
    if run_record["timed_out"] or run_record["exit_status"] != 0 or run_record["fatal_log_markers"]:
        raise NativeStarMaskError(f"Reference StarNet run failed; preserved at {root}")
    if "saved starmask" not in combined_log.lower():
        raise NativeStarMaskError(f"Reference run did not save native Starmask; preserved at {root}")

    generated = _find_single_prefixed_fits(workdir, "starnetmask_")
    unscreen = _find_single_prefixed_fits(workdir, "starnetdescreen_")
    starless = workdir / "reference_starless.fit"
    if generated is None or unscreen is None or not starless.is_file():
        raise NativeStarMaskError(f"Reference outputs are incomplete; preserved at {root}")

    generated_copy = workdir / "reference_generated_starmask_native.fit"
    generated_evidence = _copy_fits_with_history(
        generated,
        generated_copy,
        filter_value="mixed_Starmask",
        history_lines=["Generated from historical processed reference with StarNet -m."],
    )
    quality = starmask_quality_from_paths(work_source, starless, generated_copy)
    similarity = _reference_similarity(generated_copy, reference_mask_path)
    result = {
        "status": (
            "reproduced"
            if quality["satisfactory"] and similarity["satisfactory"]
            else "needs_review"
        ),
        "created_at": utc_now(),
        "helper_version": VERSION,
        "project": project_name,
        "run_root": str(root),
        "source": asdict(source_evidence),
        "reference_mask": asdict(reference_evidence),
        "generated_mask": asdict(generated_evidence),
        "generated_mask_semantics": starmask_semantics(generated_copy),
        "quality_assessment": quality,
        "similarity_to_historical_mask": similarity,
        "script": str(script_path),
        "siril_run": run_record,
        "starnet_cli": cli,
        "upstream_starnet": upstream,
        "staged_starnet_script": str(staged_script),
        "message": (
            "The historical workflow was reproduced closely enough to validate starmask mode."
            if quality["satisfactory"] and similarity["satisfactory"]
            else "The starmask is preserved for review; current StarNet 2.5.4 differs materially from the historical result."
        ),
    }
    base.json_dump_atomic(root / "reference-result.json", result)
    return result


def _evidence_with_final_path(
    evidence: dict[str, Any],
    final_path: Path,
) -> dict[str, Any]:
    record = dict(evidence)
    record["path"] = str(final_path)
    return record


def candidate_quality_score(candidate: dict[str, Any]) -> float:
    assessment = candidate["quality_assessment"]
    raw = assessment.get("raw_adaptive_diffuse_assessment", {})
    metrics = assessment.get(
        "diffuse_structure_assessment", {}
    ).get("metrics", {})
    score = raw.get("quality_score")
    if score is None:
        score = (
            float(metrics.get("remaining_star_peak_energy_ratio", 1.0))
            + float(metrics.get("luma_relative_nebula_leakage", 1.0))
            + float(
                metrics.get(
                    "worst_channel_relative_nebula_leakage",
                    1.0,
                )
            )
        )
    semantics = assessment.get("starmask_semantics", {})
    score += 0.02 * max(
        0.0,
        0.80 - float(semantics.get("exact_zero_fraction", 0.0)),
    )
    return float(score)


def compact_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    assessment = candidate["quality_assessment"]
    metrics = assessment.get(
        "diffuse_structure_assessment", {}
    ).get("metrics", {})
    semantics = assessment.get("starmask_semantics", {})
    return {
        "candidate": candidate["candidate"],
        "status": candidate["status"],
        "config": candidate["config"],
        "quality_score": candidate_quality_score(candidate),
        "remaining_star_peak_energy_ratio": metrics.get(
            "remaining_star_peak_energy_ratio"
        ),
        "luma_relative_nebula_leakage": metrics.get(
            "luma_relative_nebula_leakage"
        ),
        "worst_channel_relative_nebula_leakage": metrics.get(
            "worst_channel_relative_nebula_leakage"
        ),
        "worst_channel_structure_correlation": metrics.get(
            "worst_channel_structure_correlation"
        ),
        "exact_zero_fraction": semantics.get("exact_zero_fraction"),
        "quantized_16bit_fraction": semantics.get(
            "quantized_16bit_fraction"
        ),
        "negative_fraction": semantics.get("negative_fraction"),
        "previews": candidate["previews"],
    }


def build_review_template(
    *,
    project_name: str,
    run_root: Path,
    source_preview: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    candidate_reviews = []
    for candidate in candidates:
        previews: dict[str, Any] = {}
        for kind, path_text in candidate["previews"].items():
            if not path_text:
                continue
            path = Path(path_text)
            previews[kind] = {
                "path": str(path),
                "sha256": base.sha256_file(path),
                "inspected": False,
            }
        candidate_reviews.append(
            {
                "candidate": candidate["candidate"],
                "technical_status": candidate["status"],
                "previews": previews,
                "accepted": False,
                "broad_nebula_in_starmask": None,
                "remaining_stars_in_starless": "",
                "nebula_damage": "",
                "halos_or_artifacts": "",
                "observations": "",
            }
        )
    return {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "project": project_name,
        "run_root": str(run_root),
        "reviewer": "CodeWarrior",
        "reviewed_at": "",
        "instructions": (
            "Open the source preview and every candidate preview with an "
            "image-capable tool. Reject recognizable broad M16 nebulosity "
            "in the starmask, significant stars remaining in starless, "
            "removed nebular knots, holes, halos, seams, or other artifacts."
        ),
        "source_preview": {
            "path": source_preview["path"],
            "sha256": source_preview["sha256"],
            "inspected": False,
        },
        "candidates": candidate_reviews,
        "selected_candidate": "",
        "selection_rationale": "",
    }


def write_review_files(
    *,
    run_record: dict[str, Any],
    review_root: Path,
) -> dict[str, str]:
    review_root.mkdir(parents=True, exist_ok=False)
    summary_path = review_root / "decision-summary.json"
    base.json_dump_atomic(
        summary_path,
        {
            "schema_version": 1,
            "helper_version": VERSION,
            "project": run_record["project_name"],
            "run_root": run_record["run_root"],
            "status": run_record["status"],
            "source": run_record["source"],
            "recommended_candidate": run_record[
                "recommended_candidate"
            ],
            "satisfactory_candidates": run_record[
                "satisfactory_candidates"
            ],
            "candidate_summaries": run_record[
                "candidate_summaries"
            ],
            "source_preview": run_record["source_preview"],
        },
    )

    brief_lines = [
        "# StarNet visual-selection brief",
        "",
        f"Project: `{run_record['project_name']}`  ",
        f"Run root: `{run_record['run_root']}`  ",
        f"Workflow: `{VERSION}`",
        "",
        "Open the source preview and all four previews for every candidate.",
        "",
        "Reject a candidate for:",
        "",
        "- recognizable broad M16 nebulosity in the starmask;",
        "- significant stars left in the starless image;",
        "- removed nebular knots or dark-structure damage;",
        "- holes, halos, seams, ringing, or malformed stars;",
        "- an empty or overly dense mask.",
        "",
        (
            "Numerical recommendation: "
            f"`{run_record['recommended_candidate']}`"
        ),
        "",
        "| Candidate | Status | Target | Upsample | Quality score | "
        "Remaining-star ratio | Relative leakage | Zero fraction |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for item in run_record["candidate_summaries"]:
        config = item["config"]
        brief_lines.append(
            "| {candidate} | {status} | {target:.3f} | {upsample} | "
            "{score:.6g} | {remaining:.6g} | {leakage:.6g} | "
            "{zero:.6g} |".format(
                candidate=item["candidate"],
                status=item["status"],
                target=float(config["target_background"]),
                upsample="x2" if config["upsample"] else "x1",
                score=float(item["quality_score"]),
                remaining=float(
                    item["remaining_star_peak_energy_ratio"]
                    if item["remaining_star_peak_energy_ratio"] is not None
                    else math.nan
                ),
                leakage=float(
                    item["worst_channel_relative_nebula_leakage"]
                    if item[
                        "worst_channel_relative_nebula_leakage"
                    ] is not None
                    else math.nan
                ),
                zero=float(
                    item["exact_zero_fraction"]
                    if item["exact_zero_fraction"] is not None
                    else math.nan
                ),
            )
        )
    brief_lines.extend(
        (
            "",
            "Do not select from metrics alone. Do not ask Peter or ChatGPT",
            "to choose. Complete the structured review JSON and validate it",
            "before publication.",
        )
    )
    brief_path = review_root / "decision-brief.md"
    brief_path.write_text(
        "\n".join(brief_lines) + "\n",
        encoding="utf-8",
    )

    template_path = review_root / "visual-review-template.json"
    base.json_dump_atomic(
        template_path,
        build_review_template(
            project_name=run_record["project_name"],
            run_root=Path(run_record["run_root"]),
            source_preview=run_record["source_preview"],
            candidates=run_record["candidates"],
        ),
    )
    return {
        "decision_summary": str(summary_path),
        "decision_brief": str(brief_path),
        "visual_review_template": str(template_path),
    }


def validate_review_payload(
    *,
    project_name: str,
    run_root: Path,
    run_record: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    errors: list[str] = []
    if payload.get("schema_version") != REVIEW_SCHEMA_VERSION:
        errors.append("schema_version is invalid.")
    if payload.get("project") != project_name:
        errors.append("project does not match.")
    if Path(str(payload.get("run_root", ""))).resolve() != run_root:
        errors.append("run_root does not match.")
    if payload.get("reviewer") != "CodeWarrior":
        errors.append("reviewer must be CodeWarrior.")
    if not str(payload.get("reviewed_at", "")).strip():
        errors.append("reviewed_at is required.")

    supplied_source = payload.get("source_preview", {})
    expected_source = run_record["source_preview"]
    if supplied_source.get("inspected") is not True:
        errors.append("Source preview was not marked inspected.")
    if (
        Path(str(supplied_source.get("path", ""))).resolve()
        != Path(expected_source["path"]).resolve()
    ):
        errors.append("Source preview path does not match.")
    if supplied_source.get("sha256") != expected_source["sha256"]:
        errors.append("Source preview checksum does not match.")

    supplied_candidates = payload.get("candidates", [])
    supplied_by_name = {
        item.get("candidate"): item
        for item in supplied_candidates
        if isinstance(item, dict)
    }
    expected_names = {
        item["candidate"] for item in run_record["candidates"]
    }
    if set(supplied_by_name) != expected_names:
        errors.append("Candidate review set is incomplete.")

    allowed_levels = {"none", "minor", "significant"}
    for candidate in run_record["candidates"]:
        name = candidate["candidate"]
        supplied = supplied_by_name.get(name, {})
        expected_previews = candidate["previews"]
        supplied_previews = supplied.get("previews", {})
        for kind, path_text in expected_previews.items():
            if not path_text:
                errors.append(f"{name} is missing required preview {kind}.")
                continue
            expected_path = Path(path_text)
            preview = supplied_previews.get(kind, {})
            if preview.get("inspected") is not True:
                errors.append(
                    f"{name} {kind} preview was not marked inspected."
                )
            if (
                Path(str(preview.get("path", ""))).resolve()
                != expected_path.resolve()
            ):
                errors.append(f"{name} {kind} preview path does not match.")
            if preview.get("sha256") != base.sha256_file(expected_path):
                errors.append(
                    f"{name} {kind} preview checksum does not match."
                )

        if not isinstance(
            supplied.get("broad_nebula_in_starmask"),
            bool,
        ):
            errors.append(
                f"{name} broad_nebula_in_starmask must be true or false."
            )
        for field in (
            "remaining_stars_in_starless",
            "nebula_damage",
            "halos_or_artifacts",
        ):
            if supplied.get(field) not in allowed_levels:
                errors.append(
                    f"{name} {field} must be none, minor, or significant."
                )
        observations = str(supplied.get("observations", "")).strip()
        if len(observations) < 80:
            errors.append(
                f"{name} observations must contain at least 80 characters."
            )

        accepted = bool(supplied.get("accepted"))
        if accepted and not candidate[
            "quality_assessment"
        ]["satisfactory"]:
            errors.append(f"{name} is technically rejected.")
        if accepted and supplied.get("broad_nebula_in_starmask") is True:
            errors.append(
                f"{name} cannot be accepted with broad nebulosity in mask."
            )
        if accepted and (
            supplied.get("remaining_stars_in_starless") == "significant"
            or supplied.get("nebula_damage") == "significant"
            or supplied.get("halos_or_artifacts") == "significant"
        ):
            errors.append(
                f"{name} cannot be accepted with significant visual defects."
            )

    selected = str(payload.get("selected_candidate", "")).strip()
    if selected not in expected_names:
        errors.append("selected_candidate is invalid.")
    else:
        selected_review = supplied_by_name.get(selected, {})
        if selected_review.get("accepted") is not True:
            errors.append("Selected candidate was not accepted.")
        selected_record = next(
            item
            for item in run_record["candidates"]
            if item["candidate"] == selected
        )
        if not selected_record[
            "quality_assessment"
        ]["satisfactory"]:
            errors.append("Selected candidate is technically rejected.")

    rationale = str(payload.get("selection_rationale", "")).strip()
    if len(rationale) < 80:
        errors.append(
            "selection_rationale must contain at least 80 characters."
        )
    if errors:
        raise NativeStarMaskError(
            "Visual review is invalid: " + " | ".join(errors)
        )

    validated = json.loads(json.dumps(payload))
    validated["validated_at"] = utc_now()
    validated["validated_by_helper_version"] = VERSION
    validated["visual_review_completed"] = True
    return validated


def record_review(
    workspace: Path,
    project_name: str,
    run_root: Path,
    review_json: Path,
) -> dict[str, Any]:
    _ = workspace
    run_manifest = run_root / "run-manifest.json"
    if not run_manifest.is_file():
        raise NativeStarMaskError(
            f"Run manifest is missing: {run_manifest}"
        )
    record = json.loads(run_manifest.read_text(encoding="utf-8"))
    if (
        record.get("helper_version") != VERSION
        or record.get("project_name") != project_name
    ):
        raise NativeStarMaskError(
            "Candidate run is incompatible with this workflow."
        )
    if not review_json.is_file():
        raise NativeStarMaskError(
            f"Completed review JSON is missing: {review_json}"
        )
    payload = json.loads(review_json.read_text(encoding="utf-8"))
    validated = validate_review_payload(
        project_name=project_name,
        run_root=run_root,
        run_record=record,
        payload=payload,
    )
    review_record = run_root / "visual-review-record.json"
    base.json_dump_atomic(review_record, validated)
    record["visual_review_recorded"] = True
    record["visual_review_record"] = str(review_record)
    record["visual_review_record_sha256"] = base.sha256_file(
        review_record
    )
    record["selected_candidate"] = validated["selected_candidate"]
    base.json_dump_atomic(run_manifest, record)
    return {
        "status": "visual_review_recorded",
        "helper_version": VERSION,
        "project": project_name,
        "run_root": str(run_root),
        "selected_candidate": validated["selected_candidate"],
        "visual_review_record": str(review_record),
        "visual_review_record_sha256": base.sha256_file(review_record),
        "next_action": "Publish this exact validated review record.",
    }


def _publish_selected(
    paths: dict[str, Path],
    run_root: Path,
    selected: dict[str, Any],
    candidates: list[dict[str, Any]],
    source_evidence,
    background_manifest: dict[str, Any],
    background_manifest_hash: str,
    source_preview: dict[str, Any],
    review_record: Path,
    *,
    fresh_run: bool,
) -> dict[str, Any]:
    existing_stable = paths["stable"].exists()
    if existing_stable and not fresh_run:
        raise NativeStarMaskError(
            f"Canonical StarNet directory already exists: "
            f"{paths['stable']}. Use --fresh-run for safe replacement."
        )

    publish = run_root / "publish-staging"
    publish.mkdir(parents=True, exist_ok=False)

    selected_work = Path(selected["candidate_directory"]) / "work"
    source_starless = selected_work / "SHO_starless_linear.fit"
    source_mask = selected_work / "SHO_starmask.fit"
    source_unscreen = selected_work / "SHO_stars_unscreen.fit"

    staged_starless = publish / paths["stable_starless"].name
    staged_mask = publish / paths["stable_mask"].name
    staged_unscreen = publish / paths["stable_unscreen"].name
    for source_path, destination in (
        (source_starless, staged_starless),
        (source_mask, staged_mask),
        (source_unscreen, staged_unscreen),
    ):
        shutil.copy2(source_path, destination)

    staged_starless_evidence = asdict(
        base.inspect_fits(
            staged_starless,
            expected_channels=3,
            require_float32=True,
        )
    )
    staged_mask_evidence = asdict(
        base.inspect_fits(
            staged_mask,
            expected_channels=3,
            require_float32=True,
        )
    )
    staged_unscreen_evidence = asdict(
        base.inspect_fits(
            staged_unscreen,
            expected_channels=3,
            require_float32=True,
        )
    )

    final_quality = starmask_quality_from_paths(
        Path(selected["controlled_stretch"]["path"]),
        Path(selected["candidate_directory"])
        / "work"
        / "SHO_starless_stretched.fit",
        staged_mask,
    )
    if not final_quality["satisfactory"]:
        raise NativeStarMaskError(
            "Staged starmask failed revalidation before publication."
        )

    preview_sources = {
        "source": Path(source_preview["path"]),
        "starless": Path(selected["previews"]["starless_linear_linked"]),
        "starmask_linked": Path(selected["previews"]["starmask_linked"]),
        "starmask_unlinked": Path(selected["previews"]["starmask_unlinked"]),
        "unscreen": Path(selected["previews"]["unscreen_linked"]),
    }
    preview_destinations = {
        "source": publish / paths["stable_source_preview"].name,
        "starless": publish / paths["stable_starless_preview"].name,
        "starmask_linked": (
            publish / paths["stable_mask_linked_preview"].name
        ),
        "starmask_unlinked": (
            publish / paths["stable_mask_unlinked_preview"].name
        ),
        "unscreen": publish / paths["stable_unscreen_preview"].name,
    }
    for key, source_path in preview_sources.items():
        shutil.copy2(source_path, preview_destinations[key])

    staged_review = publish / paths["stable_review"].name
    shutil.copy2(review_record, staged_review)

    preserved_existing = (
        run_root / "previous-processing-starnet"
        if existing_stable
        else None
    )
    if preserved_existing is not None and preserved_existing.exists():
        raise NativeStarMaskError(
            f"Preservation destination already exists: "
            f"{preserved_existing}"
        )

    starless_evidence = _evidence_with_final_path(
        staged_starless_evidence,
        paths["stable_starless"],
    )
    mask_evidence = _evidence_with_final_path(
        staged_mask_evidence,
        paths["stable_mask"],
    )
    unscreen_evidence = _evidence_with_final_path(
        staged_unscreen_evidence,
        paths["stable_unscreen"],
    )

    manifest = {
        "schema_version": 3,
        "created_at": utc_now(),
        "status": "ready",
        "helper_version": VERSION,
        "adaptive_version": adaptive.VERSION,
        "backend_version": base.VERSION,
        "project": paths["project"].name,
        "project_path": str(paths["project"]),
        "stage_order": {
            "upstream": UPSTREAM_STAGE,
            "current": CURRENT_STAGE,
            "downstream": NEXT_STAGE,
        },
        "upstream_stage": UPSTREAM_STAGE,
        "background_neutralization_manifest": str(
            paths["background_manifest"]
        ),
        "background_neutralization_manifest_sha256": (
            background_manifest_hash
        ),
        "background_neutralization_helper_version": (
            background_manifest.get("helper_version")
        ),
        "source": asdict(source_evidence),
        "selected_candidate": selected["candidate"],
        "recommended_candidate": min(
            (
                item
                for item in candidates
                if item["quality_assessment"]["satisfactory"]
            ),
            key=candidate_quality_score,
        )["candidate"],
        "candidate_count": len(candidates),
        "maximum_retries": MAX_RETRIES_LIMIT,
        "candidate_summaries": [
            compact_candidate(item) for item in candidates
        ],
        "linear_starless": starless_evidence,
        "starmask": mask_evidence,
        "unscreen_stars": unscreen_evidence,
        "quality_assessment": final_quality,
        "visual_review": {
            "required": True,
            "reviewer": "CodeWarrior",
            "record_path": str(paths["stable_review"]),
            "record_sha256": base.sha256_file(staged_review),
            "all_candidate_previews_inspected": True,
        },
        "previews": {
            key: {
                "path": str(
                    {
                        "source": paths["stable_source_preview"],
                        "starless": paths["stable_starless_preview"],
                        "starmask_linked": (
                            paths["stable_mask_linked_preview"]
                        ),
                        "starmask_unlinked": (
                            paths["stable_mask_unlinked_preview"]
                        ),
                        "unscreen": paths["stable_unscreen_preview"],
                    }[key]
                ),
                "sha256": base.sha256_file(destination),
            }
            for key, destination in preview_destinations.items()
        },
        "visual_review_completed": True,
        "ghs_pass1_permitted": True,
        "starless_processing_permitted": True,
        "starless_background_processing_permitted": False,
        "mask_semantics": (
            "StarNet -m starmask; not original minus starless."
        ),
        "recomposition_method": (
            "Use the StarNet unscreen/screen workflow. "
            "Do not add the starmask linearly to the starless image."
        ),
        "upstream_starnet": selected["upstream_starnet"],
        "starnet_cli": selected["starnet_cli"],
        "previous_processing_starnet_preserved_at": (
            str(preserved_existing)
            if preserved_existing is not None
            else None
        ),
        "legacy_processing_starnet_native_directory_untouched": str(
            paths["legacy_starnet_native"]
        ),
        "stable_paths": {
            "directory": str(paths["stable"]),
            "starless": str(paths["stable_starless"]),
            "starmask": str(paths["stable_mask"]),
            "unscreen_stars": str(paths["stable_unscreen"]),
            "visual_review_record": str(paths["stable_review"]),
            "manifest": str(paths["stable_manifest"]),
        },
    }
    base.json_dump_atomic(publish / "starnet-manifest.json", manifest)

    paths["processing"].mkdir(parents=True, exist_ok=True)
    moved_existing = False
    try:
        if existing_stable:
            paths["stable"].rename(preserved_existing)
            moved_existing = True
        publish.rename(paths["stable"])
    except Exception:
        if moved_existing and not paths["stable"].exists():
            preserved_existing.rename(paths["stable"])
        raise
    return manifest


def run_project(
    workspace: Path,
    project_name: str,
    timeout_seconds: int,
    max_retries: int,
    fresh_run: bool,
) -> dict[str, Any]:
    if max_retries < 0 or max_retries > MAX_RETRIES_LIMIT:
        raise NativeStarMaskError(
            f"max-retries must be between 0 and {MAX_RETRIES_LIMIT}."
        )
    paths = project_paths(workspace, project_name)
    if not paths["project"].is_dir():
        raise NativeStarMaskError(
            f"Project does not exist: {paths['project']}"
        )
    if paths["stable"].exists() and not fresh_run:
        raise NativeStarMaskError(
            f"Canonical StarNet output already exists: {paths['stable']}. "
            "Use --fresh-run to generate candidates while preserving it."
        )

    base.siril_version()
    background_manifest, background_hash = load_background_manifest(paths)
    source_evidence = validate_background_input(
        paths,
        background_manifest,
    )

    run_root = paths["runs"] / f"starnet-{base.unique_id()}"
    run_root.mkdir(parents=True, exist_ok=False)
    source_preview = create_source_preview(
        paths["source"],
        run_root,
        timeout_seconds,
    )

    candidates: list[dict[str, Any]] = []
    allowed = NATIVE_CONFIGS[: max_retries + 1]
    for index, config in enumerate(allowed):
        candidate = execute_native_candidate(
            source_path=paths["source"],
            source_evidence=source_evidence,
            candidate_dir=run_root / f"candidate-{index:02d}",
            config=config,
            timeout_seconds=timeout_seconds,
        )
        candidates.append(candidate)

    satisfactory = [
        item
        for item in candidates
        if item["quality_assessment"]["satisfactory"]
    ]
    if not satisfactory:
        result = {
            "status": "needs_review",
            "created_at": utc_now(),
            "helper_version": VERSION,
            "project": str(paths["project"]),
            "project_name": project_name,
            "run_root": str(run_root),
            "source": asdict(source_evidence),
            "background_neutralization_manifest": str(
                paths["background_manifest"]
            ),
            "background_neutralization_manifest_sha256": (
                background_hash
            ),
            "candidates": candidates,
            "candidate_summaries": [
                compact_candidate(item) for item in candidates
            ],
            "selected_candidate": None,
            "ghs_pass1_permitted": False,
            "message": (
                "No candidate passed the technical native-mask gates. "
                "Existing canonical output was not changed."
            ),
        }
        base.json_dump_atomic(run_root / "run-manifest.json", result)
        return result

    recommended = min(
        satisfactory,
        key=lambda item: (
            candidate_quality_score(item),
            item["candidate"],
        ),
    )
    record = {
        "schema_version": 1,
        "status": "awaiting_visual_selection",
        "created_at": utc_now(),
        "helper_version": VERSION,
        "project": str(paths["project"]),
        "project_name": project_name,
        "run_root": str(run_root),
        "fresh_run_requested": fresh_run,
        "existing_canonical_state_at_start": status_project(
            workspace,
            project_name,
            permit_missing=True,
        ),
        "upstream_stage": UPSTREAM_STAGE,
        "source": asdict(source_evidence),
        "background_neutralization_manifest": str(
            paths["background_manifest"]
        ),
        "background_neutralization_manifest_sha256": background_hash,
        "background_neutralization_helper_version": (
            background_manifest.get("helper_version")
        ),
        "source_preview": source_preview,
        "candidates": candidates,
        "candidate_summaries": [
            compact_candidate(item) for item in candidates
        ],
        "satisfactory_candidates": [
            item["candidate"] for item in satisfactory
        ],
        "recommended_candidate": recommended["candidate"],
        "visual_review_recorded": False,
        "canonical_output_changed": False,
        "ghs_pass1_permitted": False,
    }
    review_files = write_review_files(
        run_record=record,
        review_root=run_root / "compact-review",
    )
    record["review_files"] = review_files
    base.json_dump_atomic(run_root / "run-manifest.json", record)
    return {
        "status": "awaiting_visual_selection",
        "helper_version": VERSION,
        "project": project_name,
        "run_root": str(run_root),
        "source": asdict(source_evidence),
        "background_neutralization_manifest": str(
            paths["background_manifest"]
        ),
        "background_neutralization_manifest_sha256": background_hash,
        "candidate_count": len(candidates),
        "satisfactory_candidates": record["satisfactory_candidates"],
        "recommended_candidate": recommended["candidate"],
        "source_preview": source_preview,
        **review_files,
        "canonical_output_changed": False,
        "visual_review_recorded": False,
        "ghs_pass1_permitted": False,
        "next_action": (
            "CodeWarrior must open the source and every candidate preview, "
            "complete the structured review, record it, publish one "
            "satisfactory candidate, and run status."
        ),
    }


def publish_project(
    workspace: Path,
    project_name: str,
    run_root: Path,
    review_record: Path,
    fresh_run: bool,
) -> dict[str, Any]:
    paths = project_paths(workspace, project_name)
    run_manifest_path = run_root / "run-manifest.json"
    record = json.loads(
        run_manifest_path.read_text(encoding="utf-8")
    )
    if (
        record.get("helper_version") != VERSION
        or record.get("project_name") != project_name
    ):
        raise NativeStarMaskError(
            "Candidate run is incompatible with this workflow."
        )
    if not record.get("visual_review_recorded"):
        raise NativeStarMaskError(
            "Structured visual review has not been recorded."
        )
    if (
        Path(str(record.get("visual_review_record", ""))).resolve()
        != review_record.resolve()
        or base.sha256_file(review_record)
        != record.get("visual_review_record_sha256")
    ):
        raise NativeStarMaskError(
            "Visual-review record path or checksum does not match."
        )
    review = json.loads(review_record.read_text(encoding="utf-8"))
    validated = validate_review_payload(
        project_name=project_name,
        run_root=run_root,
        run_record=record,
        payload=review,
    )
    selected_name = validated["selected_candidate"]
    selected = next(
        item
        for item in record["candidates"]
        if item["candidate"] == selected_name
    )

    background_manifest, background_hash = load_background_manifest(paths)
    source_evidence = validate_background_input(
        paths,
        background_manifest,
    )
    if (
        source_evidence.sha256 != record["source"]["sha256"]
        or background_hash
        != record["background_neutralization_manifest_sha256"]
    ):
        raise NativeStarMaskError(
            "Background-neutralization evidence changed after generation."
        )

    manifest = _publish_selected(
        paths,
        run_root,
        selected,
        record["candidates"],
        source_evidence,
        background_manifest,
        background_hash,
        record["source_preview"],
        review_record,
        fresh_run=fresh_run,
    )
    record["status"] = "published"
    record["published_at"] = utc_now()
    record["selected_candidate"] = selected_name
    record["canonical_output_changed"] = True
    record["ghs_pass1_permitted"] = True
    base.json_dump_atomic(run_manifest_path, record)

    checked = status_project(workspace, project_name)
    if checked.get("status") != "ready":
        raise NativeStarMaskError(
            f"Post-publication status validation failed: {checked}"
        )
    return {
        "status": "ready",
        "helper_version": VERSION,
        "project": project_name,
        "run_root": str(run_root),
        "selected_candidate": selected_name,
        "recommended_candidate": manifest["recommended_candidate"],
        "stable_directory": str(paths["stable"]),
        "stable_manifest": str(paths["stable_manifest"]),
        "stable_starless": str(paths["stable_starless"]),
        "stable_starmask": str(paths["stable_mask"]),
        "stable_unscreen_stars": str(paths["stable_unscreen"]),
        "visual_review_record": str(paths["stable_review"]),
        "previous_processing_starnet_preserved_at": manifest.get(
            "previous_processing_starnet_preserved_at"
        ),
        "legacy_processing_starnet_native_directory_untouched": str(
            paths["legacy_starnet_native"]
        ),
        "next_stage": NEXT_STAGE,
        "ghs_pass1_permitted": True,
        "post_publication_status_verified": True,
        "recomposition_warning": (
            "Do not linearly add the starmask to the starless image."
        ),
    }


def status_project(
    workspace: Path,
    project_name: str,
    permit_missing: bool = False,
) -> dict[str, Any]:
    paths = project_paths(workspace, project_name)
    if not paths["stable_manifest"].is_file():
        return {
            "status": "missing",
            "helper_version": VERSION,
            "project": str(paths["project"]),
            "stable_directory": str(paths["stable"]),
            "ghs_pass1_permitted": False,
        }

    try:
        manifest = json.loads(
            paths["stable_manifest"].read_text(encoding="utf-8")
        )
    except Exception as exc:
        return {
            "status": "invalid",
            "helper_version": VERSION,
            "project": str(paths["project"]),
            "errors": [str(exc)],
            "ghs_pass1_permitted": False,
        }

    if (
        manifest.get("helper_version") != VERSION
        or manifest.get("upstream_stage") != UPSTREAM_STAGE
    ):
        return {
            "status": "obsolete",
            "helper_version": VERSION,
            "manifest_helper_version": manifest.get("helper_version"),
            "required_helper_version": VERSION,
            "upstream_stage": manifest.get("upstream_stage"),
            "reason": (
                "Existing StarNet output predates the reviewed "
                "background-neutralization input contract."
            ),
            "project": str(paths["project"]),
            "stable_directory": str(paths["stable"]),
            "manifest": str(paths["stable_manifest"]),
            "ghs_pass1_permitted": False,
        }

    errors: list[str] = []
    records: dict[str, dict[str, Any]] = {}
    try:
        upstream, upstream_hash = load_background_manifest(paths)
        source = validate_background_input(paths, upstream)
        if upstream_hash != manifest.get(
            "background_neutralization_manifest_sha256"
        ):
            errors.append(
                "Background-neutralization manifest checksum changed."
            )
        if source.sha256 != manifest.get("source", {}).get("sha256"):
            errors.append("StarNet source checksum changed.")

        definitions = (
            (
                "linear_starless",
                paths["stable_starless"],
                manifest.get("linear_starless", {}),
            ),
            (
                "starmask",
                paths["stable_mask"],
                manifest.get("starmask", {}),
            ),
            (
                "unscreen_stars",
                paths["stable_unscreen"],
                manifest.get("unscreen_stars", {}),
            ),
        )
        for key, path, expected in definitions:
            if not path.is_file():
                errors.append(f"Missing {key}: {path}")
                continue
            current = asdict(
                base.inspect_fits(
                    path,
                    expected_channels=3,
                    require_float32=True,
                )
            )
            current["path"] = str(path)
            records[key] = current
            if current["sha256"] != expected.get("sha256"):
                errors.append(f"Checksum mismatch for {key}.")

        semantics = (
            starmask_semantics(paths["stable_mask"])
            if paths["stable_mask"].is_file()
            else None
        )
        if semantics and not semantics["satisfactory"]:
            errors.append("Stable starmask semantics failed.")

        order = manifest.get("stage_order", {})
        if order != {
            "upstream": UPSTREAM_STAGE,
            "current": CURRENT_STAGE,
            "downstream": NEXT_STAGE,
        }:
            errors.append("StarNet stage order is invalid.")
        if manifest.get("visual_review_completed") is not True:
            errors.append("Visual review is incomplete.")
        if manifest.get("ghs_pass1_permitted") is not True:
            errors.append("Manifest does not permit GHS pass 1.")
        if manifest.get(
            "starless_background_processing_permitted"
        ) is not False:
            errors.append(
                "Manifest incorrectly permits another background stage."
            )

        review_hash = manifest.get(
            "visual_review", {}
        ).get("record_sha256")
        if (
            not paths["stable_review"].is_file()
            or base.sha256_file(paths["stable_review"]) != review_hash
        ):
            errors.append("Visual-review record is missing or changed.")

        preview_paths = {
            "source": paths["stable_source_preview"],
            "starless": paths["stable_starless_preview"],
            "starmask_linked": paths["stable_mask_linked_preview"],
            "starmask_unlinked": paths["stable_mask_unlinked_preview"],
            "unscreen": paths["stable_unscreen_preview"],
        }
        for key, path in preview_paths.items():
            expected = (
                manifest.get("previews", {})
                .get(key, {})
                .get("sha256")
            )
            if not path.is_file() or base.sha256_file(path) != expected:
                errors.append(f"Stable preview {key} is missing or changed.")
    except Exception as exc:
        errors.append(str(exc))
        upstream = {}
        upstream_hash = None
        source = None
        semantics = None

    ready = (
        not errors
        and manifest.get("status") == "ready"
        and manifest.get("ghs_pass1_permitted") is True
    )
    return {
        "status": "ready" if ready else "invalid",
        "helper_version": VERSION,
        "project": str(paths["project"]),
        "stable_directory": str(paths["stable"]),
        "manifest": str(paths["stable_manifest"]),
        "errors": errors,
        "upstream_summary": {
            "manifest": str(paths["background_manifest"]),
            "manifest_sha256": upstream_hash,
            "helper_version": upstream.get("helper_version"),
            "status": upstream.get("status"),
            "visual_review_completed": upstream.get(
                "visual_review_completed"
            ),
            "star_removal_permitted": upstream.get(
                "star_removal_permitted"
            ),
        },
        "source": asdict(source) if source else None,
        "linear_starless": records.get("linear_starless"),
        "starmask": records.get("starmask"),
        "unscreen_stars": records.get("unscreen_stars"),
        "starmask_semantics": semantics,
        "quality_assessment": manifest.get("quality_assessment"),
        "selected_candidate": manifest.get("selected_candidate"),
        "recommended_candidate": manifest.get("recommended_candidate"),
        "candidate_count": manifest.get("candidate_count"),
        "previous_processing_starnet_preserved_at": manifest.get(
            "previous_processing_starnet_preserved_at"
        ),
        "legacy_processing_starnet_native_directory_untouched": str(
            paths["legacy_starnet_native"]
        ),
        "next_stage": NEXT_STAGE,
        "visual_review_completed": ready,
        "ghs_pass1_permitted": ready,
        "starless_background_processing_permitted": False,
        "recomposition_method": manifest.get("recomposition_method"),
    }


def complete_synthetic_review(
    template_path: Path,
    selected_candidate: str,
) -> Path:
    payload = json.loads(template_path.read_text(encoding="utf-8"))
    payload["reviewed_at"] = utc_now()
    payload["source_preview"]["inspected"] = True
    for candidate in payload["candidates"]:
        for preview in candidate["previews"].values():
            preview["inspected"] = True
        selected = candidate["candidate"] == selected_candidate
        candidate["accepted"] = selected
        candidate["broad_nebula_in_starmask"] = False
        candidate["remaining_stars_in_starless"] = (
            "none" if selected else "minor"
        )
        candidate["nebula_damage"] = "none"
        candidate["halos_or_artifacts"] = "none"
        candidate["observations"] = (
            "Synthetic visual-evidence attestation confirms a sparse native "
            "starmask, a clean starless image, preserved diffuse structure, "
            "and no significant holes, halos, seams, or broad mask leakage."
        )
    payload["selected_candidate"] = selected_candidate
    payload["selection_rationale"] = (
        "The selected synthetic candidate passes all native-mask technical "
        "gates and its inspected previews show clean star removal without "
        "recognizable broad nebulosity, structural damage, or artifacts."
    )
    completed = template_path.with_name("completed-review.json")
    base.json_dump_atomic(completed, payload)
    return completed


def self_test(workspace: Path, timeout_seconds: int) -> dict[str, Any]:
    root = (
        workspace
        / ".skill-self-tests"
        / "sn"
        / base.unique_id()
    )
    synthetic_workspace = root / "w"
    project_name = "T"
    project = synthetic_workspace / "Projects" / project_name
    upstream_dir = project / "processing" / "background-neutralization"
    upstream_dir.mkdir(parents=True, exist_ok=False)
    source_path = upstream_dir / "SHO-linear-neutralized.fit"
    source_evidence = base.make_synthetic_rgb(source_path)
    upstream_manifest = (
        upstream_dir / "background-neutralization-manifest.json"
    )
    base.json_dump_atomic(
        upstream_manifest,
        {
            "schema_version": 2,
            "helper_version": REQUIRED_BACKGROUND_HELPER_VERSION,
            "status": "ready",
            "project": project_name,
            "project_path": str(project),
            "stage_order": {
                "upstream": "siril-sho-combination",
                "current": UPSTREAM_STAGE,
                "downstream": CURRENT_STAGE,
            },
            "upstream_stage": "siril-sho-combination",
            "output": asdict(source_evidence),
            "visual_review_completed": True,
            "star_removal_permitted": True,
        },
    )

    generated = run_project(
        synthetic_workspace,
        project_name,
        timeout_seconds,
        max_retries=0,
        fresh_run=False,
    )
    completed = complete_synthetic_review(
        Path(generated["visual_review_template"]),
        generated["recommended_candidate"],
    )
    recorded = record_review(
        synthetic_workspace,
        project_name,
        Path(generated["run_root"]),
        completed,
    )
    published = publish_project(
        synthetic_workspace,
        project_name,
        Path(generated["run_root"]),
        Path(recorded["visual_review_record"]),
        fresh_run=False,
    )
    checked = status_project(synthetic_workspace, project_name)
    run_manifest = json.loads(
        (
            Path(generated["run_root"])
            / "run-manifest.json"
        ).read_text(encoding="utf-8")
    )
    first_candidate_budget = (
        run_manifest["candidates"][0]["path_budget"]
    )
    if (
        checked.get("status") != "ready"
        or checked.get("ghs_pass1_permitted") is not True
        or checked.get(
            "starless_background_processing_permitted"
        ) is not False
        or first_candidate_budget.get("within_limit") is not True
        or first_candidate_budget.get(
            "maximum_expected_path_bytes"
        ) > SIRIL_SAVE_PATH_LIMIT_BYTES
    ):
        raise NativeStarMaskError(
            f"Synthetic workflow self-test failed: {checked}"
        )
    return {
        "status": "success",
        "created_at": utc_now(),
        "helper_version": VERSION,
        "adaptive_version": adaptive.VERSION,
        "backend_version": base.VERSION,
        "self_test_directory": str(root),
        "source": asdict(source_evidence),
        "candidate_count": generated["candidate_count"],
        "path_budget": first_candidate_budget,
        "selected_candidate": published["selected_candidate"],
        "final_status": checked["status"],
        "next_stage": checked["next_stage"],
        "ghs_pass1_permitted": checked["ghs_pass1_permitted"],
        "starless_background_processing_permitted": checked[
            "starless_background_processing_permitted"
        ],
        "tests": [
            "background-neutralization 1.1.0 upstream contract",
            "real StarNet2 native -m and -n execution",
            "evidence-based handling of broad StarNet could-not log text",
            "Siril 255-byte generated-path preflight",
            "compact synthetic execution workspace",
            "bounded candidate generation",
            "source plus candidate preview generation",
            "structured image-review evidence validation",
            "native starmask semantic and diffuse-structure gates",
            "atomic canonical publication",
            "correct final stable paths",
            "post-publication status verification",
            "GHS pass-1 downstream permission",
            "background-neutralization rerun permission disabled",
        ],
    }

def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Generate, review and publish canonical StarNet products from "
            "the reviewed background-neutralized SHO image."
        )
    )
    parser.add_argument("--version", action="store_true")
    sub = parser.add_subparsers(dest="command")

    self_parser = sub.add_parser("self-test")
    self_parser.add_argument("--timeout", type=int, default=1800)

    ref_parser = sub.add_parser("reference-check")
    ref_parser.add_argument("--project", required=True)
    ref_parser.add_argument("--source", required=True)
    ref_parser.add_argument("--reference-mask", required=True)
    ref_parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
    )

    run_parser = sub.add_parser("run")
    run_parser.add_argument("--project", required=True)
    run_parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
    )
    run_parser.add_argument(
        "--max-retries",
        type=int,
        default=MAX_RETRIES_LIMIT,
    )
    run_parser.add_argument("--fresh-run", action="store_true")

    review_parser = sub.add_parser("record-review")
    review_parser.add_argument("--project", required=True)
    review_parser.add_argument("--run-root", required=True, type=Path)
    review_parser.add_argument(
        "--review-json",
        required=True,
        type=Path,
    )

    publish_parser = sub.add_parser("publish")
    publish_parser.add_argument("--project", required=True)
    publish_parser.add_argument("--run-root", required=True, type=Path)
    publish_parser.add_argument(
        "--review-record",
        required=True,
        type=Path,
    )
    publish_parser.add_argument("--fresh-run", action="store_true")

    status_parser = sub.add_parser("status")
    status_parser.add_argument("--project", required=True)

    args = parser.parse_args()
    if args.version:
        print(VERSION)
        return 0
    workspace = base.derive_workspace()
    try:
        if args.command == "self-test":
            payload = self_test(workspace, args.timeout)
        elif args.command == "reference-check":
            payload = reference_check(
                workspace,
                args.project,
                Path(args.source).expanduser().resolve(),
                Path(args.reference_mask).expanduser().resolve(),
                args.timeout,
            )
        elif args.command == "run":
            payload = run_project(
                workspace,
                args.project,
                args.timeout,
                args.max_retries,
                args.fresh_run,
            )
        elif args.command == "record-review":
            payload = record_review(
                workspace,
                args.project,
                args.run_root.resolve(),
                args.review_json.resolve(),
            )
        elif args.command == "publish":
            payload = publish_project(
                workspace,
                args.project,
                args.run_root.resolve(),
                args.review_record.resolve(),
                args.fresh_run,
            )
        elif args.command == "status":
            payload = status_project(
                workspace,
                args.project,
            )
        else:
            parser.print_help()
            return 2
        print(json.dumps(payload, indent=2, sort_keys=True))
        return (
            0
            if payload.get("status")
            in {
                "success",
                "ready",
                "reproduced",
                "awaiting_visual_selection",
                "visual_review_recorded",
                "missing",
                "obsolete",
            }
            else 2
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "helper_version": VERSION,
                    "error": str(exc),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
