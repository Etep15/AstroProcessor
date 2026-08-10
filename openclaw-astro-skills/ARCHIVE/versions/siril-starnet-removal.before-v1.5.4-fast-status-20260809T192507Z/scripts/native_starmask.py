#!/usr/bin/env python3
"""Native StarNet mask workflow for validated SHO projects.

This helper deliberately separates two different products:

* a linear starless FITS image, obtained by inverse-transforming the temporary
  nonlinear StarNet result back to the original linear domain; and
* StarNet's native ``-m`` starmask, kept in the nonlinear 16-bit TIFF domain
  that StarNet actually produced.

The native mask is not treated as ``original - starless`` and is not required
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

VERSION = "1.3.1"
ADAPTIVE_REQUIRED_VERSION = "1.2.0"
BACKEND_REQUIRED_VERSION = "1.0.4"
DEFAULT_TIMEOUT_SECONDS = 7200
MAX_RETRIES_LIMIT = 3

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
        "label": "native-baseline-target-0.15-x1",
        "target_background": 0.15,
        "upsample": False,
        "stride": 256,
        "protect_highlights": True,
    },
    {
        "label": "native-retry-1-target-0.10-x1",
        "target_background": 0.10,
        "upsample": False,
        "stride": 256,
        "protect_highlights": True,
    },
    {
        "label": "native-retry-2-target-0.06-x1",
        "target_background": 0.06,
        "upsample": False,
        "stride": 256,
        "protect_highlights": True,
    },
    {
        "label": "native-retry-3-target-0.10-x2",
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
    stable = processing / "starnet-native"
    return {
        "project": project,
        "processing": processing,
        "source": processing / "sho" / "SHO-linear.fit",
        "sho_manifest": processing / "sho" / "sho-combination-manifest.json",
        "runs": project / ".siril-starnet-native",
        "reference_runs": project / ".siril-starnet-native-reference",
        "stable": stable,
        "stable_starless": stable / "SHO-starless-linear.fit",
        "stable_mask": stable / "SHO-starmask-native.fit",
        "stable_unscreen": stable / "SHO-stars-unscreen-native.fit",
        "stable_manifest": stable / "native-starmask-manifest.json",
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


def native_mask_semantics(path: Path) -> dict[str, Any]:
    import numpy as np
    from astropy.io import fits

    with fits.open(path, memmap=False, do_not_scale_image_data=False) as hdul:
        data = np.asarray(hdul[0].data, dtype=np.float32)
        filter_value = str(hdul[0].header.get("FILTER", ""))
    if data.ndim != 3 or data.shape[0] != 3:
        raise NativeStarMaskError(f"Native starmask must be RGB; found {data.shape}")

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


def native_diffuse_gate(
    diffuse: dict[str, Any],
) -> dict[str, Any]:
    """Apply stable diffuse-structure gates to a sparse native StarNet mask."""
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
                "reason": "near-zero native-mask background makes the ratio unstable",
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
                    "reason": "near-zero native-mask background makes the ratio unstable",
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
            "Native-mask diffuse gates passed. Ratios with near-zero "
            "background denominators were retained only as diagnostics."
            if not failed
            else "The native mask failed one or more stable diffuse-structure "
            "or remaining-star gates."
        ),
    }


def native_quality_from_paths(
    stretched_source: Path,
    stretched_starless: Path,
    native_mask: Path,
) -> dict[str, Any]:
    raw_diffuse = adaptive.quality_metrics_from_paths(
        stretched_source,
        stretched_starless,
        native_mask,
    )
    diffuse = native_diffuse_gate(raw_diffuse)
    semantics = native_mask_semantics(native_mask)
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
        "native_mask_semantics": semantics,
        "interpretation": (
            "The native StarNet mask is sparse, 16-bit-derived and free of "
            "unacceptable broad nebular structure."
            if satisfactory
            else "The native StarNet mask failed one or more mask-semantic or "
            "stable diffuse-structure gates; another controlled native-mask "
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
            f'savepng "{preview_dir / "SHO-starmask-native-linked"}"',
            "close",
            f'load "{native_mask}"',
            "autostretch",
            f'savepng "{preview_dir / "SHO-starmask-native-unlinked"}"',
            "close",
            f'load "{native_unscreen}"',
            "autostretch -linked",
            f'savepng "{preview_dir / "SHO-stars-unscreen-native-linked"}"',
            "close",
            "",
        ]
    )


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
    if run_record["exit_status"] != 0 or run_record["fatal_log_markers"]:
        raise NativeStarMaskError(
            "Native StarNet candidate failed technically; preserved at "
            f"{candidate_dir}. Exit={run_record['exit_status']}, "
            f"markers={run_record['fatal_log_markers']}"
        )
    lower = combined_log.lower()
    for required in (
        "starnet: working: done!",
        "saved starmask",
        "saved unscreen stars",
    ):
        if required not in lower:
            raise NativeStarMaskError(
                f"Expected log marker {required!r} is absent; candidate preserved at {candidate_dir}"
            )

    stretched_starless = workdir / "SHO_starless_stretched.fit"
    base.inspect_fits(stretched_starless, expected_channels=3, require_float32=True)

    repository_mask = _find_single_prefixed_fits(workdir, "starnetmask_")
    repository_unscreen = _find_single_prefixed_fits(workdir, "starnetdescreen_")
    if repository_mask is None or repository_unscreen is None:
        raise NativeStarMaskError(
            "Expected exactly one native starmask and one native unscreen layer; "
            f"candidate preserved at {candidate_dir}"
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

    native_mask = workdir / "SHO_starmask_native.fit"
    native_mask_evidence = _copy_fits_with_history(
        repository_mask,
        native_mask,
        filter_value="mixed_StarmaskNative",
        history_lines=[
            "StarNet native -m mask; not original minus starless.",
            f"Native starmask helper version {VERSION}.",
        ],
    )
    native_unscreen = workdir / "SHO_stars_unscreen_native.fit"
    native_unscreen_evidence = _copy_fits_with_history(
        repository_unscreen,
        native_unscreen,
        filter_value="mixed_StarsUnscreenNative",
        history_lines=[
            "StarNet native -n unscreen layer in nonlinear StarNet domain.",
            f"Native starmask helper version {VERSION}.",
        ],
    )

    quality = native_quality_from_paths(
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
        "controlled_stretch": stretch_record,
        "script": str(script_path),
        "script_sha256": base.sha256_file(script_path),
        "siril_run": run_record,
        "staged_starnet_script": str(staged_script),
        "upstream_starnet": upstream,
        "starnet_cli": cli,
        "linear_starless": asdict(linear_starless_evidence),
        "native_mask": asdict(native_mask_evidence),
        "native_unscreen": asdict(native_unscreen_evidence),
        "repository_native_mask": str(repository_mask),
        "repository_native_unscreen": str(repository_unscreen),
        "quality_assessment": quality,
        "preview_script": str(preview_script),
        "preview_run": preview_run,
        "previews": {
            "starless_linear_linked": str(previews / "SHO-starless-linear-linked.png")
            if (previews / "SHO-starless-linear-linked.png").is_file()
            else None,
            "native_mask_linked": str(previews / "SHO-starmask-native-linked.png")
            if (previews / "SHO-starmask-native-linked.png").is_file()
            else None,
            "native_mask_unlinked": str(previews / "SHO-starmask-native-unlinked.png")
            if (previews / "SHO-starmask-native-unlinked.png").is_file()
            else None,
            "native_unscreen_linked": str(previews / "SHO-stars-unscreen-native-linked.png")
            if (previews / "SHO-stars-unscreen-native-linked.png").is_file()
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
        filter_value="mixed_StarmaskNative",
        history_lines=["Generated from historical processed reference with native StarNet -m."],
    )
    quality = native_quality_from_paths(work_source, starless, generated_copy)
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
        "generated_mask_semantics": native_mask_semantics(generated_copy),
        "quality_assessment": quality,
        "similarity_to_historical_mask": similarity,
        "script": str(script_path),
        "siril_run": run_record,
        "starnet_cli": cli,
        "upstream_starnet": upstream,
        "staged_starnet_script": str(staged_script),
        "message": (
            "The historical workflow was reproduced closely enough to validate native-mask mode."
            if quality["satisfactory"] and similarity["satisfactory"]
            else "The native mask is preserved for review; current StarNet 2.5.4 differs materially from the historical result."
        ),
    }
    base.json_dump_atomic(root / "reference-result.json", result)
    return result


def _publish_selected(
    paths: dict[str, Path],
    run_root: Path,
    selected: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    if paths["stable"].exists():
        raise NativeStarMaskError(
            f"Native stable directory already exists and will not be overwritten: {paths['stable']}"
        )
    publish = run_root / "publish-staging"
    publish.mkdir(parents=True, exist_ok=False)

    selected_work = Path(selected["candidate_directory"]) / "work"
    source_starless = selected_work / "SHO_starless_linear.fit"
    source_mask = selected_work / "SHO_starmask_native.fit"
    source_unscreen = selected_work / "SHO_stars_unscreen_native.fit"

    stable_starless = publish / "SHO-starless-linear.fit"
    stable_mask = publish / "SHO-starmask-native.fit"
    stable_unscreen = publish / "SHO-stars-unscreen-native.fit"
    for source, destination in (
        (source_starless, stable_starless),
        (source_mask, stable_mask),
        (source_unscreen, stable_unscreen),
    ):
        shutil.copy2(source, destination)

    starless_evidence = asdict(base.inspect_fits(stable_starless, expected_channels=3, require_float32=True))
    mask_evidence = asdict(base.inspect_fits(stable_mask, expected_channels=3, require_float32=True))
    unscreen_evidence = asdict(base.inspect_fits(stable_unscreen, expected_channels=3, require_float32=True))
    final_quality = native_quality_from_paths(
        Path(selected["controlled_stretch"]["path"]),
        Path(selected["candidate_directory"]) / "work" / "SHO_starless_stretched.fit",
        stable_mask,
    )
    if not final_quality["satisfactory"]:
        raise NativeStarMaskError("Staged native mask failed revalidation before publication.")

    manifest = {
        "schema_version": 1,
        "created_at": utc_now(),
        "status": "ready",
        "helper_version": VERSION,
        "adaptive_version": adaptive.VERSION,
        "backend_version": base.VERSION,
        "project": paths["project"].name,
        "project_path": str(paths["project"]),
        "source": str(paths["source"]),
        "selected_candidate": selected["candidate"],
        "candidate_count": len(candidates),
        "maximum_retries": MAX_RETRIES_LIMIT,
        "candidates": candidates,
        "linear_starless": starless_evidence,
        "native_starmask": mask_evidence,
        "native_unscreen_stars": unscreen_evidence,
        "quality_assessment": final_quality,
        "starless_background_processing_permitted": True,
        "mask_semantics": "StarNet native -m mask; not original minus starless.",
        "recomposition_method": (
            "Use the native unscreen/screen workflow. Do not add the native mask linearly to the starless image."
        ),
        "upstream_starnet": selected["upstream_starnet"],
        "starnet_cli": selected["starnet_cli"],
        "publication_method": "atomic directory rename",
        "legacy_processing_starnet_directory_untouched": str(paths["processing"] / "starnet"),
    }
    base.json_dump_atomic(publish / "native-starmask-manifest.json", manifest)
    paths["processing"].mkdir(parents=True, exist_ok=True)
    publish.rename(paths["stable"])
    return manifest


def run_project(
    workspace: Path,
    project_name: str,
    timeout_seconds: int,
    max_retries: int,
) -> dict[str, Any]:
    if max_retries < 0 or max_retries > MAX_RETRIES_LIMIT:
        raise NativeStarMaskError(
            f"max-retries must be between 0 and {MAX_RETRIES_LIMIT}."
        )
    paths = project_paths(workspace, project_name)
    if not paths["project"].is_dir():
        raise NativeStarMaskError(f"Project does not exist: {paths['project']}")
    if paths["stable"].exists():
        raise NativeStarMaskError(
            f"Stable native-mask output already exists: {paths['stable']}"
        )

    base.siril_version()
    sho_manifest, _sho_hash = base.load_sho_manifest(base.project_paths(workspace, project_name))
    source_evidence = base.validate_input(base.project_paths(workspace, project_name), sho_manifest)

    run_root = paths["runs"] / f"native-{base.unique_id()}"
    run_root.mkdir(parents=True, exist_ok=False)
    candidates = []
    selected = None
    allowed = NATIVE_CONFIGS[: max_retries + 1]
    for index, config in enumerate(allowed):
        candidate_dir = run_root / f"candidate-{index:02d}"
        candidate = execute_native_candidate(
            source_path=paths["source"],
            source_evidence=source_evidence,
            candidate_dir=candidate_dir,
            config=config,
            timeout_seconds=timeout_seconds,
        )
        candidates.append(candidate)
        if candidate["quality_assessment"]["satisfactory"]:
            selected = candidate
            break

    if selected is None:
        result = {
            "status": "needs_review",
            "created_at": utc_now(),
            "helper_version": VERSION,
            "project": str(paths["project"]),
            "run_root": str(run_root),
            "maximum_retries": max_retries,
            "attempts_completed": len(candidates),
            "candidates": candidates,
            "selected_candidate": None,
            "starless_background_processing_permitted": False,
            "message": "No native StarNet mask satisfied all mask-semantic and diffuse-structure gates. Nothing was published.",
        }
        base.json_dump_atomic(run_root / "manifest.json", result)
        return result

    manifest = _publish_selected(paths, run_root, selected, candidates)
    result = {
        "status": "ready",
        "created_at": utc_now(),
        "helper_version": VERSION,
        "project": str(paths["project"]),
        "run_root": str(run_root),
        "selected_candidate": selected["candidate"],
        "attempts_completed": len(candidates),
        "maximum_retries": max_retries,
        "stable_directory": str(paths["stable"]),
        "stable_manifest": str(paths["stable_manifest"]),
        "stable_starless": str(paths["stable_starless"]),
        "stable_native_mask": str(paths["stable_mask"]),
        "stable_native_unscreen": str(paths["stable_unscreen"]),
        "starless_background_processing_permitted": True,
        "recomposition_warning": "Do not linearly add the native mask to the starless image.",
        "manifest": manifest,
    }
    base.json_dump_atomic(run_root / "manifest.json", result)
    return result


def status_project(workspace: Path, project_name: str) -> dict[str, Any]:
    paths = project_paths(workspace, project_name)
    if not paths["stable_manifest"].is_file():
        return {
            "status": "missing",
            "helper_version": VERSION,
            "project": str(paths["project"]),
            "stable_directory": str(paths["stable"]),
            "starless_background_processing_permitted": False,
        }
    manifest = json.loads(paths["stable_manifest"].read_text(encoding="utf-8"))
    errors = []
    for key, path in (
        ("linear_starless", paths["stable_starless"]),
        ("native_starmask", paths["stable_mask"]),
        ("native_unscreen_stars", paths["stable_unscreen"]),
    ):
        if not path.is_file():
            errors.append(f"Missing {key}: {path}")
            continue
        current = base.sha256_file(path)
        expected = manifest.get(key, {}).get("sha256")
        if expected and current != expected:
            errors.append(f"Checksum mismatch for {key}: {path}")
    return {
        "status": "ready" if not errors else "invalid",
        "helper_version": VERSION,
        "project": str(paths["project"]),
        "manifest": str(paths["stable_manifest"]),
        "errors": errors,
        "linear_starless": manifest.get("linear_starless"),
        "native_starmask": manifest.get("native_starmask"),
        "native_unscreen_stars": manifest.get("native_unscreen_stars"),
        "quality_assessment": manifest.get("quality_assessment"),
        "starless_background_processing_permitted": bool(
            manifest.get("starless_background_processing_permitted")
        ) and not errors,
        "recomposition_method": manifest.get("recomposition_method"),
    }


def self_test(workspace: Path, timeout_seconds: int) -> dict[str, Any]:
    root = (
        workspace
        / ".skill-self-tests"
        / "siril-starnet-native-mask"
        / base.unique_id()
    )
    root.mkdir(parents=True, exist_ok=False)
    source = root / "SHO-linear.fit"
    source_evidence = base.make_synthetic_rgb(source)
    candidate = execute_native_candidate(
        source_path=source,
        source_evidence=source_evidence,
        candidate_dir=root / "candidate-00",
        config=NATIVE_CONFIGS[0],
        timeout_seconds=timeout_seconds,
    )
    semantics = candidate["quality_assessment"]["native_mask_semantics"]
    self_test_failures = []
    if candidate["status"] != "satisfactory":
        self_test_failures.append(
            "native candidate did not pass its complete quality gate"
        )
    if semantics["finite_fraction"] != 1.0:
        self_test_failures.append("non-finite native mask")
    if semantics["negative_fraction"] != 0.0:
        self_test_failures.append("negative native mask values")
    if semantics["quantized_16bit_fraction"] < 0.995:
        self_test_failures.append("native mask is not 16-bit-derived")
    if semantics["nonzero_fraction"] < 0.001:
        self_test_failures.append("native mask is empty")

    preview_run = candidate["preview_run"]
    if preview_run["exit_status"] != 0:
        self_test_failures.append(
            f"preview script exited with status {preview_run['exit_status']}"
        )
    if preview_run["fatal_log_markers"]:
        self_test_failures.append(
            f"preview script fatal markers: {preview_run['fatal_log_markers']}"
        )
    missing_previews = [
        name
        for name, value in candidate["previews"].items()
        if value is None
    ]
    if missing_previews:
        self_test_failures.append(
            f"missing required previews: {missing_previews}"
        )

    if self_test_failures:
        raise NativeStarMaskError(
            f"Synthetic native mask semantics failed {self_test_failures}; preserved at {root}"
        )
    result = {
        "status": "success",
        "created_at": utc_now(),
        "helper_version": VERSION,
        "adaptive_version": adaptive.VERSION,
        "backend_version": base.VERSION,
        "maximum_retries": MAX_RETRIES_LIMIT,
        "self_test_directory": str(root),
        "candidate": candidate,
        "tests": [
            "real StarNet2 native -m mask execution",
            "real StarNet2 native -n unscreen execution",
            "native mask 16-bit quantization",
            "native mask nonnegative sparse background",
            "linear starless inverse MTF output",
            "native mask not treated as additive difference",
            "near-zero native-mask ratio stability",
            "linked and unlinked native-mask previews",
            "native unscreen preview",
            "maximum of three retries",
            "evidence preservation",
        ],
    }
    base.json_dump_atomic(root / "self-test.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate and validate StarNet native starmasks."
    )
    parser.add_argument("--version", action="store_true")
    sub = parser.add_subparsers(dest="command")

    self_parser = sub.add_parser("self-test")
    self_parser.add_argument("--timeout", type=int, default=1800)

    ref_parser = sub.add_parser("reference-check")
    ref_parser.add_argument("--project", required=True)
    ref_parser.add_argument("--source", required=True)
    ref_parser.add_argument("--reference-mask", required=True)
    ref_parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)

    run_parser = sub.add_parser("run")
    run_parser.add_argument("--project", required=True)
    run_parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    run_parser.add_argument("--max-retries", type=int, default=MAX_RETRIES_LIMIT)

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
            )
        elif args.command == "status":
            payload = status_project(workspace, args.project)
        else:
            parser.print_help()
            return 2
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload.get("status") in ("success", "ready", "reproduced") else 2
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
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
