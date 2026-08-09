#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from astropy.io import fits

VERSION = "1.0.2"
REQUIRED_SIRIL_VERSION = "1.4.4"
WORKSPACE = Path("/home/peter/.openclaw/workspace/agents/codewarrior")
SIRIL_APP = Path(
    "/home/peter/.openclaw/runtime/siril-processor/toolchain/.toolchain/"
    "siril/1.4.4/squashfs-root/AppRun"
)
SIRIL_APPDIR = SIRIL_APP.parent
UPSTREAM_HELPER_VERSIONS = frozenset({"1.0.4"})
COMPATIBLE_RUN_HELPER_VERSIONS = frozenset({"1.0.1", "1.0.2"})
COMPATIBLE_CANONICAL_HELPER_VERSIONS = frozenset({"1.0.1", "1.0.2"})
MAX_CONTEXT_SAFE_JSON_BYTES = 12000
MAX_CANDIDATES = 3
CANDIDATE_AMOUNTS = {
    "candidate-00": 0.10,
    "candidate-01": 0.15,
    "candidate-02": 0.20,
}
CANDIDATE_CLASSIFICATION = {
    "candidate-00": "conservative",
    "candidate-01": "baseline",
    "candidate-02": "assertive",
}
MANUAL_BASELINE_AMOUNT = 0.15
RM_GREEN_TYPE = 2
PRESERVE_LIGHTNESS = True
MIN_LUMA_CORRELATION = 0.995
MAX_ABS_LUMA_MEDIAN_CHANGE = 0.003
MAX_ADDED_LOW_CLIP_FRACTION = 0.001
MAX_ADDED_HIGH_CLIP_FRACTION = 1e-6
MAX_OUTPUT_VALUE = 1.000001
MIN_OUTPUT_VALUE = -1e-6
MIN_GREEN_EXCESS_REDUCTION = 0.0
ASSERTIVE_OVERRIDE_MIN_CHARS = 80
CLI_SUCCESS_STATUSES = frozenset({
    "success", "ready", "missing", "start_new_run",
    "would_generate_candidates", "visual_review_required",
    "ready_to_publish", "would_publish_recorded_selection",
    "confirmation_required", "fresh_run_authorized",
})
FATAL_LOG_MARKERS = (
    "script execution failed", "error in line", "could not load",
    "cannot open", "unknown command", "not enough memory",
)


class GreenReductionError(RuntimeError):
    pass


@dataclass(frozen=True)
class FitsEvidence:
    path: str
    sha256: str
    size: int
    bitpix: int
    dtype: str
    channels: int
    width: int
    height: int
    minimum: float
    median: float
    maximum: float
    finite_fraction: float
    filter_header: str | None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def unique_id() -> str:
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return f"{now}-p{os.getpid()}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def json_dump_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{unique_id()}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def compact_text(value: Any, limit: int = 420) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def assert_context_safe_payload(payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > MAX_CONTEXT_SAFE_JSON_BYTES:
        raise GreenReductionError("Context-safe response exceeded the 12 KB output budget.")


def project_paths(workspace: Path, project_name: str) -> dict[str, Path]:
    project = workspace / "Projects" / project_name
    stable = project / "processing" / "green-reduction"
    runs = project / ".siril-green-reduction"
    return {
        "project": project,
        "upstream": project / "processing" / "black-point",
        "upstream_output": project / "processing" / "black-point" / "SHO-starless-black-point.fit",
        "upstream_manifest": project / "processing" / "black-point" / "black-point-manifest.json",
        "runs": runs,
        "intents": runs / "stage-intents",
        "stable": stable,
        "stable_output": stable / "SHO-starless-green-reduced.fit",
        "stable_before_preview": stable / "SHO-starless-black-point-before-green-reduction.png",
        "stable_after_preview": stable / "SHO-starless-green-reduced.png",
        "stable_manifest": stable / "green-reduction-manifest.json",
    }


def inspect_fits(path: Path) -> FitsEvidence:
    if not path.is_file():
        raise GreenReductionError(f"FITS file does not exist: {path}")
    with fits.open(path, memmap=True) as hdul:
        hdu = hdul[0]
        data = np.asarray(hdu.data)
        bitpix = int(hdu.header.get("BITPIX", 0))
        filter_header = hdu.header.get("FILTER")
        if data.ndim == 2:
            channels, height, width = 1, data.shape[0], data.shape[1]
        elif data.ndim == 3:
            channels, height, width = data.shape
        else:
            raise GreenReductionError(f"Unsupported FITS dimensions {data.shape}: {path}")
        sample = np.asarray(data, dtype=np.float64)
        finite = np.isfinite(sample)
        finite_fraction = float(np.mean(finite))
        if not np.any(finite):
            raise GreenReductionError(f"FITS has no finite pixels: {path}")
        values = sample[finite]
        minimum = float(np.min(values))
        median = float(np.median(values))
        maximum = float(np.max(values))
        dtype = str(data.dtype)
    return FitsEvidence(
        path=str(path), sha256=sha256_file(path), size=path.stat().st_size,
        bitpix=bitpix, dtype=dtype, channels=int(channels), width=int(width),
        height=int(height), minimum=minimum, median=median, maximum=maximum,
        finite_fraction=finite_fraction,
        filter_header=str(filter_header) if filter_header is not None else None,
    )


def load_rgb(path: Path, stride: int = 1) -> np.ndarray:
    with fits.open(path, memmap=True) as hdul:
        data = np.asarray(hdul[0].data)
        if data.ndim != 3 or data.shape[0] != 3:
            raise GreenReductionError(f"Expected RGB FITS, got shape {data.shape}: {path}")
        sample = np.asarray(data[:, ::stride, ::stride], dtype=np.float64)
    if not np.isfinite(sample).all():
        raise GreenReductionError(f"Non-finite RGB samples: {path}")
    return sample


def validate_upstream_fast(paths: dict[str, Path]) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest_path = paths["upstream_manifest"]
    output_path = paths["upstream_output"]
    if not manifest_path.is_file():
        raise GreenReductionError(f"Missing black-point manifest: {manifest_path}")
    if not output_path.is_file():
        raise GreenReductionError(f"Missing black-point FITS: {output_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise GreenReductionError(f"Could not read black-point manifest: {exc}") from exc
    errors: list[str] = []
    if manifest.get("helper_version") not in UPSTREAM_HELPER_VERSIONS:
        errors.append("black-point helper_version must be 1.0.4")
    if manifest.get("status") != "ready":
        errors.append("black-point status must be ready")
    if manifest.get("next_stage") != "siril-green-reduction":
        errors.append("black-point next_stage must be siril-green-reduction")
    if manifest.get("green_reduction_processing_permitted") is not True:
        errors.append("green_reduction_processing_permitted must be true")
    if manifest.get("visual_review_completed") is not True:
        errors.append("black-point visual review must be complete")
    if manifest.get("quality_assessment", {}).get("satisfactory") is not True:
        errors.append("black-point quality assessment must be satisfactory")
    if manifest.get("selection_policy", {}).get("version") != "1.0.4":
        errors.append("black-point selection policy must be v1.0.4")
    recorded = manifest.get("output", {})
    recorded_path = recorded.get("path")
    if recorded_path and Path(recorded_path).resolve() != output_path.resolve():
        errors.append("black-point manifest output path is not canonical")
    recorded_size = recorded.get("size")
    if recorded_size is not None and int(recorded_size) != output_path.stat().st_size:
        errors.append("black-point FITS size differs from manifest")
    source_sha = recorded.get("sha256")
    if not isinstance(source_sha, str) or len(source_sha) != 64:
        errors.append("black-point manifest output SHA is missing")
    if errors:
        raise GreenReductionError("Upstream black-point contract failed: " + "; ".join(errors))
    return manifest, {"path": str(output_path), "sha256": source_sha, "size": output_path.stat().st_size}


def validate_upstream(paths: dict[str, Path]) -> tuple[dict[str, Any], FitsEvidence]:
    manifest, fast = validate_upstream_fast(paths)
    evidence = inspect_fits(paths["upstream_output"])
    errors: list[str] = []
    if evidence.sha256 != fast["sha256"]:
        errors.append("black-point FITS SHA differs from manifest")
    if evidence.channels != 3:
        errors.append("black-point output must be RGB")
    if evidence.bitpix != -32:
        errors.append("black-point output must be 32-bit floating FITS")
    if evidence.finite_fraction != 1.0:
        errors.append("black-point output must be fully finite")
    if errors:
        raise GreenReductionError("Upstream black-point contract failed: " + "; ".join(errors))
    return manifest, evidence


def siril_version() -> dict[str, Any]:
    if not SIRIL_APP.is_file() or not os.access(SIRIL_APP, os.X_OK):
        raise GreenReductionError(f"Siril AppRun is unavailable: {SIRIL_APP}")
    env = os.environ.copy(); env["APPDIR"] = str(SIRIL_APPDIR)
    completed = subprocess.run(
        [str(SIRIL_APP), "siril-cli", "--version"], text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
        check=False, timeout=60,
    )
    output = completed.stdout.strip()
    if completed.returncode != 0 or REQUIRED_SIRIL_VERSION not in output:
        raise GreenReductionError(
            f"Expected Siril {REQUIRED_SIRIL_VERSION}, got exit {completed.returncode}: {output}"
        )
    return {"path": str(SIRIL_APP), "version": REQUIRED_SIRIL_VERSION, "version_output": output}


def run_siril_script(*, directory: Path, script: Path, stdout_log: Path, stderr_log: Path, timeout_seconds: int) -> dict[str, Any]:
    stdout_log.parent.mkdir(parents=True, exist_ok=True)
    stderr_log.parent.mkdir(parents=True, exist_ok=True)
    command = [str(SIRIL_APP), "siril-cli", "--directory", str(directory), "--script", str(script)]
    env = os.environ.copy(); env["APPDIR"] = str(SIRIL_APPDIR)
    started = time.monotonic(); timed_out = False
    try:
        completed = subprocess.run(
            command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, timeout=timeout_seconds, check=False,
        )
        code, stdout, stderr = completed.returncode, completed.stdout, completed.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True; code = 124; stdout = exc.stdout or ""; stderr = exc.stderr or ""
        if isinstance(stdout, bytes): stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes): stderr = stderr.decode("utf-8", errors="replace")
    duration = time.monotonic() - started
    stdout_log.write_text(stdout, encoding="utf-8")
    stderr_log.write_text(stderr, encoding="utf-8")
    combined = f"{stdout}\n{stderr}".lower()
    fatal = [marker for marker in FATAL_LOG_MARKERS if marker in combined]
    return {
        "command": command, "exit_status": int(code),
        "duration_seconds": round(duration, 3), "timed_out": timed_out,
        "timeout_seconds": int(timeout_seconds), "fatal_log_markers": fatal,
        "stdout_log": str(stdout_log), "stderr_log": str(stderr_log),
    }


def green_reduction_script_text() -> str:
    lines = [
        f"requires {REQUIRED_SIRIL_VERSION}",
        'load "SHO-starless-black-point.fit"',
        'savepng "../common/SHO-starless-black-point-before-green-reduction"',
        "close",
    ]
    for name in ("candidate-00", "candidate-01", "candidate-02"):
        amount = CANDIDATE_AMOUNTS[name]
        lines.extend([
            'load "SHO-starless-black-point.fit"',
            f"rmgreen {RM_GREEN_TYPE} {amount:.3f}",
            f'save "../{name}/work/SHO-starless-green-reduced.fit"',
            f'savepng "../{name}/previews/SHO-starless-green-reduced"',
            "close",
        ])
    lines.append("")
    return "\n".join(lines)


def color_metrics(source_path: Path, output_path: Path, stride: int = 4) -> dict[str, Any]:
    src = load_rgb(source_path, stride=stride)
    out = load_rgb(output_path, stride=stride)
    if src.shape != out.shape:
        raise GreenReductionError("Source/output RGB sample shapes differ")
    # Arithmetic RGB mean is a structural/diagnostic proxy only; it is not CIE L*.
    # Siril Preserve Lightness is enforced by generating rmgreen without -nopreserve.
    src_luma = np.mean(src, axis=0).ravel(); out_luma = np.mean(out, axis=0).ravel()
    corr = float(np.corrcoef(src_luma, out_luma)[0, 1])
    src_luma_median = float(np.median(src_luma)); out_luma_median = float(np.median(out_luma))
    def green_excess(a: np.ndarray) -> np.ndarray:
        return np.maximum(a[1] - np.maximum(a[0], a[2]), 0.0)
    def magenta_pressure(a: np.ndarray) -> np.ndarray:
        return np.maximum(0.5 * (a[0] + a[2]) - a[1], 0.0)
    src_ge = green_excess(src).ravel(); out_ge = green_excess(out).ravel()
    src_mp = magenta_pressure(src).ravel(); out_mp = magenta_pressure(out).ravel()
    src_ge_mean = float(np.mean(src_ge)); out_ge_mean = float(np.mean(out_ge))
    reduction = 0.0 if src_ge_mean <= 1e-12 else float(1.0 - out_ge_mean / src_ge_mean)
    src_low = float(np.mean(src <= 1e-7)); out_low = float(np.mean(out <= 1e-7))
    src_high = float(np.mean(src >= 1.0 - 1e-7)); out_high = float(np.mean(out >= 1.0 - 1e-7))
    return {
        "luma_correlation": corr,
        "source_luma_median": src_luma_median,
        "output_luma_median": out_luma_median,
        "rgb_mean_median_change_diagnostic": abs(out_luma_median - src_luma_median),
        "source_green_excess_mean": src_ge_mean,
        "output_green_excess_mean": out_ge_mean,
        "green_excess_reduction_fraction": reduction,
        "source_green_excess_p90": float(np.quantile(src_ge, 0.90)),
        "output_green_excess_p90": float(np.quantile(out_ge, 0.90)),
        "source_green_dominant_fraction": float(np.mean(src[1] > np.maximum(src[0], src[2]) + 1e-7)),
        "output_green_dominant_fraction": float(np.mean(out[1] > np.maximum(out[0], out[2]) + 1e-7)),
        "source_magenta_pressure_mean": float(np.mean(src_mp)),
        "output_magenta_pressure_mean": float(np.mean(out_mp)),
        "magenta_pressure_increase": float(np.mean(out_mp) - np.mean(src_mp)),
        "source_channel_low_clip_fraction": src_low,
        "output_channel_low_clip_fraction": out_low,
        "added_channel_low_clip_fraction": max(0.0, out_low - src_low),
        "source_channel_high_clip_fraction": src_high,
        "output_channel_high_clip_fraction": out_high,
        "added_channel_high_clip_fraction": max(0.0, out_high - src_high),
        "source_channel_medians": [float(np.median(src[i])) for i in range(3)],
        "output_channel_medians": [float(np.median(out[i])) for i in range(3)],
    }


def production_quality_assessment(source_path: Path, output_path: Path) -> dict[str, Any]:
    source = inspect_fits(source_path); output = inspect_fits(output_path)
    metrics = color_metrics(source_path, output_path); failed: list[dict[str, Any]] = []
    def fail(metric: str, value: Any, requirement: str) -> None:
        failed.append({"metric": metric, "value": value, "requirement": requirement})
    if (source.width, source.height, source.channels) != (output.width, output.height, output.channels):
        fail("dimensions", asdict(output), "must match source")
    if output.bitpix != -32: fail("bitpix", output.bitpix, "must equal -32")
    if output.finite_fraction != 1.0: fail("finite_fraction", output.finite_fraction, "must equal 1.0")
    if output.sha256 == source.sha256: fail("output_sha256", output.sha256, "must differ from source")
    if output.minimum < MIN_OUTPUT_VALUE: fail("output_minimum", output.minimum, f"must be >= {MIN_OUTPUT_VALUE}")
    if output.maximum > MAX_OUTPUT_VALUE: fail("output_maximum", output.maximum, f"must be <= {MAX_OUTPUT_VALUE}")
    if metrics["luma_correlation"] < MIN_LUMA_CORRELATION:
        fail("luma_correlation", metrics["luma_correlation"], f"must be >= {MIN_LUMA_CORRELATION}")
    # Do not gate publication on arithmetic RGB-mean median change. It is not CIE L*.
    if metrics["added_channel_low_clip_fraction"] > MAX_ADDED_LOW_CLIP_FRACTION:
        fail("added_channel_low_clip_fraction", metrics["added_channel_low_clip_fraction"], f"must be <= {MAX_ADDED_LOW_CLIP_FRACTION}")
    if metrics["added_channel_high_clip_fraction"] > MAX_ADDED_HIGH_CLIP_FRACTION:
        fail("added_channel_high_clip_fraction", metrics["added_channel_high_clip_fraction"], f"must be <= {MAX_ADDED_HIGH_CLIP_FRACTION}")
    if metrics["green_excess_reduction_fraction"] < MIN_GREEN_EXCESS_REDUCTION:
        fail("green_excess_reduction_fraction", metrics["green_excess_reduction_fraction"], "must not increase positive green excess")
    satisfactory = not failed
    return {
        "status": "satisfactory" if satisfactory else "needs_review",
        "satisfactory": satisfactory, "failed_checks": failed, "metrics": metrics,
        "thresholds": {
            "minimum_luma_correlation": MIN_LUMA_CORRELATION,
            "rgb_mean_median_change_is_diagnostic_only": True,
            "maximum_added_channel_low_clip_fraction": MAX_ADDED_LOW_CLIP_FRACTION,
            "maximum_added_channel_high_clip_fraction": MAX_ADDED_HIGH_CLIP_FRACTION,
            "minimum_green_excess_reduction_fraction": MIN_GREEN_EXCESS_REDUCTION,
        },
        "interpretation": (
            "Green reduction is technically acceptable only when Siril produces a finite 32-bit RGB result, "
            "preserves luminance structure and clipping, and does not increase the positive green-excess proxy. "
            "Visual review remains authoritative for residual green versus magenta/purple over-correction."
        ),
    }


def candidate_publication_eligible(candidate: dict[str, Any]) -> bool:
    return candidate.get("quality_assessment", {}).get("satisfactory") is True


def publication_gate(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    eligible = [c["candidate"] for c in candidates if candidate_publication_eligible(c)]
    if not eligible:
        return {"status": "blocked", "publication_permitted": False, "publication_eligible_candidates": [], "reason": "No green-reduction candidate passed the technical quality gate."}
    recommended = "candidate-01" if "candidate-01" in eligible else min(
        eligible, key=lambda name: abs(CANDIDATE_AMOUNTS[name] - MANUAL_BASELINE_AMOUNT)
    )
    return {
        "status": "ready_for_visual_review", "publication_permitted": True,
        "publication_eligible_candidates": eligible, "recommended_candidate": recommended,
        "reason": (
            "The successful manual M16 baseline is Maximum Mask amount 0.15. Visual review may choose the "
            "conservative candidate if 0.15 introduces magenta/purple, or the assertive candidate only when "
            "residual green is clearly visible and faint structure remains natural."
        ),
    }


def run_project(*, workspace: Path, project_name: str, timeout_seconds: int, max_candidates: int) -> dict[str, Any]:
    if max_candidates != MAX_CANDIDATES:
        raise GreenReductionError(f"This skill requires exactly {MAX_CANDIDATES} bounded candidates.")
    paths = project_paths(workspace, project_name)
    upstream_manifest, source_evidence = validate_upstream(paths)
    siril = siril_version()
    run_root = paths["runs"] / f"green-reduction-{unique_id()}"
    work = run_root / "work"; common = run_root / "common"; logs = run_root / "logs"
    run_root.mkdir(parents=True, exist_ok=False); work.mkdir(); common.mkdir(); logs.mkdir()
    for name in CANDIDATE_AMOUNTS:
        (run_root / name / "work").mkdir(parents=True)
        (run_root / name / "previews").mkdir(parents=True)
    working_source = work / "SHO-starless-black-point.fit"
    shutil.copy2(paths["upstream_output"], working_source)
    if sha256_file(working_source) != source_evidence.sha256:
        raise GreenReductionError("Working source SHA changed while staging green reduction.")
    script = run_root / "green-reduction.ssf"; script.write_text(green_reduction_script_text(), encoding="utf-8")
    run = run_siril_script(
        directory=work, script=script, stdout_log=logs / "siril-stdout.log",
        stderr_log=logs / "siril-stderr.log", timeout_seconds=timeout_seconds,
    )
    failures: list[str] = []
    if run["exit_status"] != 0: failures.append(f"Siril exit {run['exit_status']}")
    if run["timed_out"]: failures.append("Siril timed out")
    if run["fatal_log_markers"]: failures.append(f"fatal log markers {run['fatal_log_markers']}")
    before = common / "SHO-starless-black-point-before-green-reduction.png"
    if not before.is_file(): failures.append("common before preview missing")
    candidates: list[dict[str, Any]] = []
    for name, amount in CANDIDATE_AMOUNTS.items():
        output = run_root / name / "work" / "SHO-starless-green-reduced.fit"
        after = run_root / name / "previews" / "SHO-starless-green-reduced.png"
        if not output.is_file(): failures.append(f"{name} output FITS missing"); continue
        if not after.is_file(): failures.append(f"{name} after preview missing"); continue
        quality = production_quality_assessment(working_source, output)
        output_evidence = inspect_fits(output)
        candidates.append({
            "candidate": name, "amount": amount,
            "classification": CANDIDATE_CLASSIFICATION[name],
            "method": {"command": f"rmgreen {RM_GREEN_TYPE} {amount:.3f}", "type": RM_GREEN_TYPE, "protection_method": "Maximum Mask", "amount": amount, "preserve_lightness": True},
            "output": asdict(output_evidence), "quality_assessment": quality,
            "previews": {"before": str(before), "after": str(after)},
            "preview_provenance": {"before_png_sha256": sha256_file(before), "after_png_sha256": sha256_file(after), "source_fits_sha256": source_evidence.sha256, "output_fits_sha256": output_evidence.sha256},
            "status": "satisfactory" if quality["satisfactory"] else "needs_review",
        })
    if failures:
        raise GreenReductionError(f"Green-reduction Siril run failed ({failures}); evidence preserved at {run_root}")
    if len(candidates) != MAX_CANDIDATES:
        raise GreenReductionError(f"Expected {MAX_CANDIDATES} candidates, produced {len(candidates)}; evidence preserved at {run_root}")
    gate = publication_gate(candidates)
    record = {
        "schema_version": 1, "helper_version": VERSION, "created_at": utc_now(),
        "project": str(paths["project"]), "project_name": project_name, "run_root": str(run_root),
        "status": "awaiting_visual_selection" if gate["publication_permitted"] else "blocked",
        "source": asdict(source_evidence), "upstream_manifest": str(paths["upstream_manifest"]),
        "upstream_manifest_sha256": sha256_file(paths["upstream_manifest"]), "siril": siril,
        "script": str(script), "script_sha256": sha256_file(script), "siril_run": run,
        "candidate_policy": {
            "candidate_amounts": CANDIDATE_AMOUNTS, "candidate_classification": CANDIDATE_CLASSIFICATION,
            "manual_successful_baseline": {"protection_method": "Maximum Mask", "amount": MANUAL_BASELINE_AMOUNT, "preserve_lightness": True},
            "assertive_candidate_requires_override": True,
        },
        "candidates": candidates, "completed_candidate_count": len(candidates),
        "publication_permitted": gate["publication_permitted"],
        "publication_eligible_candidates": gate["publication_eligible_candidates"],
        "recommended_candidate": gate.get("recommended_candidate"), "publication_gate": gate,
        "visual_selection": None, "canonical_output_changed": False,
        "saturation_processing_permitted": False,
    }
    json_dump_atomic(run_root / "run-manifest.json", record)
    return record


def load_run_record(run_root: Path) -> tuple[Path, dict[str, Any]]:
    manifest = run_root / "run-manifest.json"
    if not manifest.is_file(): raise GreenReductionError(f"Run manifest is missing: {manifest}")
    try: record = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception as exc: raise GreenReductionError(f"Could not read run manifest: {exc}") from exc
    return manifest, record


def stage_status(workspace: Path, project_name: str) -> dict[str, Any]:
    paths = project_paths(workspace, project_name)
    if not paths["stable_manifest"].is_file():
        return {"status": "missing", "helper_version": VERSION, "project": str(paths["project"]), "next_stage": "siril-green-reduction", "saturation_processing_permitted": False, "errors": ["No canonical green-reduction manifest exists."]}
    try: manifest = json.loads(paths["stable_manifest"].read_text(encoding="utf-8"))
    except Exception as exc:
        return {"status": "invalid", "helper_version": VERSION, "project": str(paths["project"]), "next_stage": "siril-green-reduction", "saturation_processing_permitted": False, "errors": [f"Could not read green-reduction manifest: {exc}"]}
    errors: list[str] = []
    if manifest.get("helper_version") not in COMPATIBLE_CANONICAL_HELPER_VERSIONS: errors.append("canonical helper version is incompatible")
    if manifest.get("status") != "ready": errors.append("canonical manifest status is not ready")
    if manifest.get("visual_review_completed") is not True: errors.append("visual review is not complete")
    if manifest.get("next_stage") != "siril-saturation": errors.append("next_stage is not siril-saturation")
    if manifest.get("saturation_processing_permitted") is not True: errors.append("saturation_processing_permitted is not true")
    if manifest.get("quality_assessment", {}).get("satisfactory") is not True: errors.append("canonical quality assessment is not satisfactory")
    if manifest.get("visual_selection", {}).get("visual_review_evidence", {}).get("method") != "openclaw-read": errors.append("canonical visual review method is not openclaw-read")
    output = manifest.get("output", {})
    if not paths["stable_output"].is_file(): errors.append("canonical output FITS is missing")
    elif output.get("size") is not None and int(output["size"]) != paths["stable_output"].stat().st_size: errors.append("canonical output FITS size differs from manifest")
    try:
        _, upstream = validate_upstream_fast(paths)
        if manifest.get("source", {}).get("sha256") != upstream["sha256"]: errors.append("canonical source SHA differs from current black-point source")
    except Exception as exc: errors.append(str(exc))
    ready = not errors
    return {
        "status": "ready" if ready else "invalid", "helper_version": VERSION,
        "manifest_helper_version": manifest.get("helper_version"), "project": str(paths["project"]),
        "canonical_manifest_compatible": ready, "canonical_output_sha256": output.get("sha256"),
        "selected_candidate": manifest.get("selected_candidate"), "selected_amount": manifest.get("method", {}).get("amount"),
        "visual_review_completed": manifest.get("visual_review_completed", False),
        "next_stage": "siril-saturation" if ready else "siril-green-reduction",
        "saturation_processing_permitted": ready, "errors": errors,
    }


def status_project(workspace: Path, project_name: str) -> dict[str, Any]:
    status = stage_status(workspace, project_name)
    if status.get("status") != "ready": return status
    paths = project_paths(workspace, project_name)
    try:
        manifest = json.loads(paths["stable_manifest"].read_text(encoding="utf-8"))
        output = inspect_fits(paths["stable_output"]); _, upstream = validate_upstream(paths)
    except Exception as exc:
        return {**status, "status": "invalid", "canonical_manifest_compatible": False, "saturation_processing_permitted": False, "next_stage": "siril-green-reduction", "errors": [str(exc)]}
    errors: list[str] = []
    if output.sha256 != manifest.get("output", {}).get("sha256"): errors.append("canonical output SHA differs from manifest")
    if manifest.get("source", {}).get("sha256") != upstream.sha256: errors.append("canonical source SHA differs from current black-point source")
    for preview in (paths["stable_before_preview"], paths["stable_after_preview"]):
        if not preview.is_file(): errors.append(f"missing canonical preview {preview}")
    if errors:
        return {**status, "status": "invalid", "canonical_manifest_compatible": False, "saturation_processing_permitted": False, "next_stage": "siril-green-reduction", "errors": errors}
    return status


def workflow_state(workspace: Path, project_name: str) -> dict[str, Any]:
    paths = project_paths(workspace, project_name); _, source = validate_upstream_fast(paths)
    stable = stage_status(workspace, project_name)
    compatible: list[tuple[float, Path, dict[str, Any]]] = []; blocked: list[tuple[float, Path, dict[str, Any]]] = []
    if paths["runs"].is_dir():
        for run_root in paths["runs"].iterdir():
            if not run_root.is_dir() or run_root.name == "stage-intents": continue
            manifest = run_root / "run-manifest.json"
            if not manifest.is_file(): continue
            try: record = json.loads(manifest.read_text(encoding="utf-8"))
            except Exception: continue
            if record.get("project_name") != project_name: continue
            if record.get("helper_version") not in COMPATIBLE_RUN_HELPER_VERSIONS: continue
            if record.get("source", {}).get("sha256") != source["sha256"]: continue
            if record.get("canonical_output_changed") is True: continue
            mtime = manifest.stat().st_mtime
            if record.get("publication_permitted") is True: compatible.append((mtime, run_root, record))
            elif record.get("completed_candidate_count", 0): blocked.append((mtime, run_root, record))
    if compatible:
        _, run_root, record = sorted(compatible, key=lambda x: x[0], reverse=True)[0]
        selection = record.get("visual_selection")
        if isinstance(selection, dict) and selection.get("completed") is True:
            return {"status": "ready_to_publish", "action": "publish_recorded_selection", "run_root": str(run_root), "selected_candidate": selection.get("selected_candidate"), "recommended_candidate": record.get("recommended_candidate"), "canonical_status": stable}
        return {"status": "awaiting_visual_selection", "action": "review_select_publish", "run_root": str(run_root), "recommended_candidate": record.get("recommended_candidate"), "publication_eligible_candidates": record.get("publication_eligible_candidates", []), "canonical_status": stable}
    if blocked:
        _, run_root, record = sorted(blocked, key=lambda x: x[0], reverse=True)[0]
        return {"status": "blocked", "action": "stop", "run_root": str(run_root), "reason": record.get("publication_gate", {}).get("reason"), "canonical_status": stable}
    return {"status": "start_new_run", "action": "run_review_select_publish", "source_sha256": source["sha256"], "canonical_status": stable}


def latest_fresh_intent(paths: dict[str, Path], project_name: str, source_sha: str, canonical_sha: str, statuses: set[str]) -> tuple[Path, dict[str, Any]] | None:
    if not paths["intents"].is_dir(): return None
    rows = []
    for path in paths["intents"].glob("fresh-run-*.json"):
        try: record = json.loads(path.read_text(encoding="utf-8"))
        except Exception: continue
        if record.get("project_name") == project_name and record.get("source_sha256") == source_sha and record.get("canonical_output_sha256") == canonical_sha and record.get("status") in statuses:
            rows.append((path.stat().st_mtime, path, record))
    if not rows: return None
    _, path, record = sorted(rows, reverse=True)[0]; return path, record


def begin_stage(workspace: Path, project_name: str) -> dict[str, Any]:
    paths = project_paths(workspace, project_name); state = workflow_state(workspace, project_name)
    if state.get("action") in ("review_select_publish", "publish_recorded_selection", "stop"):
        return {**state, "confirmation_required": False}
    canonical = state.get("canonical_status", {})
    if canonical.get("status") != "ready": return {**state, "confirmation_required": False}
    _, source = validate_upstream_fast(paths); canonical_sha = canonical.get("canonical_output_sha256")
    authorized = latest_fresh_intent(paths, project_name, source["sha256"], canonical_sha, {"fresh_run_authorized"})
    if authorized is not None:
        path, intent = authorized
        return {"status": "fresh_run_authorized", "action": "run_review_select_publish", "project": str(paths["project"]), "confirmation_required": False, "fresh_run_authorized": True, "fresh_run_intent": str(path), "fresh_run_request_id": intent.get("request_id"), "canonical_status": canonical}
    pending = latest_fresh_intent(paths, project_name, source["sha256"], canonical_sha, {"confirmation_required"})
    if pending is None:
        paths["intents"].mkdir(parents=True, exist_ok=True); request_id = unique_id(); path = paths["intents"] / f"fresh-run-{request_id}.json"
        intent = {"schema_version": 1, "helper_version": VERSION, "project_name": project_name, "request_id": request_id, "status": "confirmation_required", "requested_at": utc_now(), "source_sha256": source["sha256"], "canonical_output_sha256": canonical_sha}
        json_dump_atomic(path, intent)
    else: path, intent = pending
    return {"status": "confirmation_required", "action": "confirm_fresh_run", "project": str(paths["project"]), "confirmation_required": True, "fresh_run_authorized": False, "fresh_run_request_id": intent.get("request_id"), "question": f"Green reduction for {project_name} has already completed successfully. Do you want me to run it again as a fresh run?", "canonical_status": canonical}


def confirm_fresh_run(workspace: Path, project_name: str) -> dict[str, Any]:
    paths = project_paths(workspace, project_name); state = workflow_state(workspace, project_name); canonical = state.get("canonical_status", {})
    if canonical.get("status") != "ready": raise GreenReductionError("Fresh-run confirmation requires a valid completed canonical result.")
    _, source = validate_upstream_fast(paths); canonical_sha = canonical.get("canonical_output_sha256")
    existing = latest_fresh_intent(paths, project_name, source["sha256"], canonical_sha, {"fresh_run_authorized"})
    if existing is not None:
        path, intent = existing; return {"status": "fresh_run_authorized", "action": "run_review_select_publish", "fresh_run_authorized": True, "fresh_run_intent": str(path), "fresh_run_request_id": intent.get("request_id")}
    pending = latest_fresh_intent(paths, project_name, source["sha256"], canonical_sha, {"confirmation_required"})
    if pending is None: raise GreenReductionError("No pending fresh-run confirmation exists. Begin the stage first.")
    path, intent = pending; intent["status"] = "fresh_run_authorized"; intent["authorized_at"] = utc_now(); json_dump_atomic(path, intent)
    return {"status": "fresh_run_authorized", "action": "run_review_select_publish", "fresh_run_authorized": True, "fresh_run_intent": str(path), "fresh_run_request_id": intent.get("request_id")}


def consume_fresh_authorization(workspace: Path, project_name: str, run_root: Path) -> None:
    paths = project_paths(workspace, project_name); stable = stage_status(workspace, project_name)
    if stable.get("status") != "ready": return
    _, source = validate_upstream_fast(paths)
    auth = latest_fresh_intent(paths, project_name, source["sha256"], stable.get("canonical_output_sha256"), {"fresh_run_authorized"})
    if auth is None: return
    path, intent = auth; intent["status"] = "consumed"; intent["consumed_at"] = utc_now(); intent["consumed_by_run_root"] = str(run_root); json_dump_atomic(path, intent)


def review_plan(*, workspace: Path, project_name: str, run_root: Path) -> dict[str, Any]:
    paths = project_paths(workspace, project_name); _, source = validate_upstream_fast(paths); _, record = load_run_record(run_root)
    if record.get("project_name") != project_name: raise GreenReductionError("Run project does not match requested project.")
    if record.get("helper_version") not in COMPATIBLE_RUN_HELPER_VERSIONS: raise GreenReductionError("Run helper version is incompatible.")
    if record.get("source", {}).get("sha256") != source["sha256"]: raise GreenReductionError("Run source SHA differs from current black-point canonical source.")
    eligible = list(record.get("publication_eligible_candidates", []))
    if not eligible: raise GreenReductionError("No publication-eligible candidates are available.")
    by_name = {c["candidate"]: c for c in record.get("candidates", [])}
    common_before = Path(by_name[eligible[0]]["previews"]["before"]); before_sha = by_name[eligible[0]]["preview_provenance"]["before_png_sha256"]
    if not common_before.is_file() or sha256_file(common_before) != before_sha: raise GreenReductionError("Common before preview is missing or changed.")
    read_targets = [{"role": "before", "path": str(common_before), "sha256": before_sha}]; candidates = []
    for name in eligible:
        item = by_name[name]; after = Path(item["previews"]["after"]); expected = item["preview_provenance"]["after_png_sha256"]
        if not after.is_file() or sha256_file(after) != expected: raise GreenReductionError(f"{name} after preview is missing or changed.")
        metrics = item["quality_assessment"]["metrics"]
        candidates.append({"candidate": name, "amount": item["amount"], "classification": item["classification"], "after_preview": str(after), "after_preview_sha256": expected, "green_excess_reduction_fraction": metrics["green_excess_reduction_fraction"], "magenta_pressure_increase": metrics["magenta_pressure_increase"], "luma_correlation": metrics["luma_correlation"]})
        read_targets.append({"role": "candidate", "candidate": name, "path": str(after), "sha256": expected})
    return {
        "status": "visual_review_required", "action": "read_previews_then_select_publish", "helper_version": VERSION,
        "project": str(paths["project"]), "project_name": project_name, "run_root": str(run_root), "read_targets": read_targets,
        "publication_eligible_candidates": eligible, "required_candidate_notes": eligible, "recommended_candidate": record.get("recommended_candidate"),
        "candidates": candidates, "review_method_required": "openclaw-read",
        "selection_policy": {
            "manual_successful_baseline": "Maximum Mask amount 0.15, preserve lightness on",
            "candidate_00": "conservative 0.10", "candidate_01": "baseline 0.15", "candidate_02": "assertive 0.20; requires override reason if selected",
            "rules": [
                "Remove the obvious unwanted green cast without neutralizing all SHO green structure.",
                "Do not select a stronger amount merely because it removes more green numerically.",
                "Reject magenta/purple sky or nebular cast introduced by over-correction.",
                "Preserve faint outer emission, Pillars, dark lanes, and luminance structure.",
                "Prefer the 0.15 manual baseline when it looks natural; choose 0.10 when 0.15 is visibly too strong.",
                "Choose 0.20 only when lower amounts leave obvious unwanted green and 0.20 does not introduce magenta/purple or erase faint structure.",
            ],
        },
        "assertive_override_instruction": "Selecting candidate-02 requires --policy-override-reason describing the residual green seen in lower candidates, absence of magenta/purple in candidate-02, and preservation of faint structure.",
        "saturation_processing_permitted": False,
    }


def parse_candidate_notes(values: list[str], eligible: list[str]) -> dict[str, str]:
    notes: dict[str, str] = {}
    for raw in values:
        if "=" not in raw: raise GreenReductionError(f"Candidate note must use candidate=value syntax: {raw!r}")
        name, value = raw.split("=", 1); name = name.strip(); value = value.strip()
        if name in notes: raise GreenReductionError(f"Duplicate candidate note for {name}.")
        if len(value) < 20: raise GreenReductionError(f"{name} visual note is too short; describe what was actually seen.")
        notes[name] = value
    expected = sorted(eligible); actual = sorted(notes)
    if actual != expected: raise GreenReductionError(f"Candidate visual notes must cover every eligible candidate exactly. Expected {expected}, got {actual}.")
    return notes


def validate_assertive_override_reason(reason: str | None) -> str:
    if reason is None or len(reason.strip()) < ASSERTIVE_OVERRIDE_MIN_CHARS:
        raise GreenReductionError("Selecting candidate-02 requires a substantive --policy-override-reason (at least 80 characters).")
    lowered = reason.lower()
    if "green" not in lowered or not any(word in lowered for word in ("magenta", "purple")) or not any(word in lowered for word in ("faint", "preserv")):
        raise GreenReductionError("Assertive override must mention residual green, absence/control of magenta or purple, and preservation of faint structure.")
    return reason.strip()


def record_visual_selection(*, workspace: Path, project_name: str, run_root: Path, candidate_name: str, visual_notes: str, candidate_notes: list[str], policy_override_reason: str | None) -> dict[str, Any]:
    manifest_path, record = load_run_record(run_root); plan = review_plan(workspace=workspace, project_name=project_name, run_root=run_root); eligible = plan["publication_eligible_candidates"]
    if candidate_name not in eligible: raise GreenReductionError(f"Selected candidate {candidate_name!r} is not publication-eligible. Eligible: {eligible}")
    notes = parse_candidate_notes(candidate_notes, eligible); override_used = False; override = None
    if candidate_name == "candidate-02": override = validate_assertive_override_reason(policy_override_reason); override_used = True
    elif policy_override_reason: override = policy_override_reason.strip()
    by_name = {c["candidate"]: c for c in record["candidates"]}; selected = by_name[candidate_name]; evidence_candidates = {}
    for name in eligible:
        item = by_name[name]; after = Path(item["previews"]["after"]); expected = item["preview_provenance"]["after_png_sha256"]
        if sha256_file(after) != expected: raise GreenReductionError(f"{name} preview changed after review.")
        evidence_candidates[name] = {"after_preview": str(after), "after_preview_sha256": expected, "visual_note": notes[name], "amount": item["amount"], "classification": item["classification"]}
    before = Path(selected["previews"]["before"]); before_sha = selected["preview_provenance"]["before_png_sha256"]
    if sha256_file(before) != before_sha: raise GreenReductionError("Before preview changed after visual review.")
    selection = {
        "completed": True, "recorded_at": utc_now(), "reviewer": "CodeWarrior", "selected_candidate": candidate_name,
        "selected_candidate_was_recommended": candidate_name == record.get("recommended_candidate"), "selected_output_sha256": selected["output"]["sha256"],
        "visual_notes": visual_notes, "satisfactory_candidates_compared": eligible, "policy_override_reason": override, "policy_override_used": override_used,
        "visual_review_evidence": {"method": "openclaw-read", "before_preview": str(before), "before_preview_sha256": before_sha, "candidates": evidence_candidates, "copying_files_counts_as_review": False},
    }
    record["visual_selection"] = selection; record["selected_candidate"] = candidate_name; record["visual_review_completed"] = True; record["status"] = "ready_to_publish"; json_dump_atomic(manifest_path, record)
    return selection


def preserve_failed_publish_staging(run_root: Path) -> Path | None:
    staging = run_root / "publish-staging"
    if not staging.exists(): return None
    preserved = run_root / f"failed-publish-staging-{unique_id()}"; staging.rename(preserved); return preserved


def publish_project(*, workspace: Path, project_name: str, run_root: Path) -> dict[str, Any]:
    paths = project_paths(workspace, project_name); upstream_manifest, source = validate_upstream(paths); manifest_path, record = load_run_record(run_root)
    if record.get("project_name") != project_name or record.get("source", {}).get("sha256") != source.sha256: raise GreenReductionError("Run is not compatible with current project/upstream source.")
    selection = record.get("visual_selection")
    if not isinstance(selection, dict) or selection.get("completed") is not True: raise GreenReductionError("Durable visual selection is required before publication.")
    selected_name = selection.get("selected_candidate")
    if selected_name not in record.get("publication_eligible_candidates", []): raise GreenReductionError("Selected candidate is no longer publication-eligible.")
    selected = next(c for c in record["candidates"] if c["candidate"] == selected_name); output = Path(selected["output"]["path"]); before = Path(selected["previews"]["before"]); after = Path(selected["previews"]["after"])
    if sha256_file(output) != selection.get("selected_output_sha256"): raise GreenReductionError("Selected output changed after visual review.")
    before_expected = selection["visual_review_evidence"]["before_preview_sha256"]; after_expected = selection["visual_review_evidence"]["candidates"][selected_name]["after_preview_sha256"]
    if sha256_file(before) != before_expected or sha256_file(after) != after_expected: raise GreenReductionError("Selected preview evidence changed after visual review.")
    quality = production_quality_assessment(paths["upstream_output"], output)
    if quality.get("satisfactory") is not True: raise GreenReductionError("Selected candidate failed full publication quality revalidation.")
    preserved_failed = preserve_failed_publish_staging(run_root); staging = run_root / "publish-staging"; staging.mkdir(parents=True, exist_ok=False)
    staged_output = staging / "SHO-starless-green-reduced.fit"; staged_before = staging / "SHO-starless-black-point-before-green-reduction.png"; staged_after = staging / "SHO-starless-green-reduced.png"; staged_manifest = staging / "green-reduction-manifest.json"
    shutil.copy2(output, staged_output); shutil.copy2(before, staged_before); shutil.copy2(after, staged_after)
    stable_payload = {
        "schema_version": 1, "helper_version": VERSION, "created_at": utc_now(), "project": project_name, "project_path": str(paths["project"]),
        "stage_order": {"upstream": "siril-black-point", "current": "siril-green-reduction", "downstream": "siril-saturation"},
        "status": "ready", "visual_review_completed": True, "selected_candidate": selected_name, "recommended_candidate": record.get("recommended_candidate"),
        "selected_candidate_was_recommended": selection.get("selected_candidate_was_recommended"), "method": selected["method"], "quality_assessment": quality,
        "output": {**selected["output"], "path": str(paths["stable_output"])}, "source": {**asdict(source), "path": str(paths["upstream_output"])},
        "previews": {"before": str(paths["stable_before_preview"]), "after": str(paths["stable_after_preview"])}, "candidate_policy": record.get("candidate_policy"),
        "visual_selection": selection,
        "upstream_summary": {"helper_version": upstream_manifest.get("helper_version"), "manifest": str(paths["upstream_manifest"]), "manifest_sha256": sha256_file(paths["upstream_manifest"]), "status": upstream_manifest.get("status"), "visual_review_completed": upstream_manifest.get("visual_review_completed"), "green_reduction_processing_permitted": upstream_manifest.get("green_reduction_processing_permitted"), "selection_policy_version": upstream_manifest.get("selection_policy", {}).get("version")},
        "next_stage": "siril-saturation", "saturation_processing_permitted": True, "run_root": str(run_root), "failed_publish_staging_preserved_at": str(preserved_failed) if preserved_failed else None,
    }
    json_dump_atomic(staged_manifest, stable_payload)
    previous = None
    if paths["stable"].exists(): previous = run_root / f"previous-processing-green-reduction-{unique_id()}"; paths["stable"].rename(previous)
    try: staging.rename(paths["stable"])
    except Exception as exc:
        if previous is not None and previous.exists() and not paths["stable"].exists(): previous.rename(paths["stable"])
        raise GreenReductionError(f"Could not publish green-reduction staging: {exc}") from exc
    canonical_sha = sha256_file(paths["stable_output"])
    if canonical_sha != selected["output"]["sha256"]: raise GreenReductionError("Published canonical SHA does not match selected candidate.")
    record["status"] = "ready"; record["canonical_output_changed"] = True; record["published_at"] = utc_now(); record["stable_directory"] = str(paths["stable"]); record["stable_manifest"] = str(paths["stable_manifest"]); record["previous_processing_green_reduction_preserved_at"] = str(previous) if previous else None; record["saturation_processing_permitted"] = True; json_dump_atomic(manifest_path, record)
    return {"status": "ready", "helper_version": VERSION, "project": str(paths["project"]), "run_root": str(run_root), "selected_candidate": selected_name, "selected_amount": selected["amount"], "recommended_candidate": record.get("recommended_candidate"), "canonical_output_sha256": canonical_sha, "previous_processing_green_reduction_preserved_at": str(previous) if previous else None, "failed_publish_staging_preserved_at": str(preserved_failed) if preserved_failed else None, "visual_review_completed": True, "next_stage": "siril-saturation", "saturation_processing_permitted": True}


def compact_review_plan(plan: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "status": plan["status"],
        "action": plan["action"],
        "helper_version": VERSION,
        "project": plan["project"],
        "project_name": plan["project_name"],
        "run_root": plan["run_root"],
        "read_targets": plan["read_targets"],
        "publication_eligible_candidates": plan["publication_eligible_candidates"],
        "required_candidate_notes": plan["required_candidate_notes"],
        "recommended_candidate": plan["recommended_candidate"],
        "candidates": plan["candidates"],
        "review_method_required": "openclaw-read",
        "selection_policy": plan["selection_policy"],
        "assertive_override_instruction": plan["assertive_override_instruction"],
        "read_target_policy": {
            "path_handling": "verbatim",
            "read_every_target_exactly_once_or_more": True,
            "directory_discovery_forbidden": True,
            "forbidden_recovery_tools": ["ls", "find", "cat", "grep", "jq", "globbing"],
            "on_read_failure": "stop_and_report_exact_failed_path",
            "do_not_construct_or_repair_paths": True,
        },
        "candidate_note_requirements": [
            "For every eligible candidate, state whether unwanted/residual green remains.",
            "For every eligible candidate, state whether magenta or purple over-correction is visible.",
            "For every eligible candidate, state whether faint outer emission, Pillars, and dark-lane structure remain preserved.",
            "Do not justify a choice only by saying that it matches the baseline or recommendation.",
        ],
        "instruction": (
            "Use OpenClaw Read on every read_targets[].path exactly as returned, passing each path verbatim. "
            "Do not construct, shorten, normalize, infer, repair, or rediscover any path. "
            "Do not use ls, find, cat, grep, jq, globbing, or directory inspection as a fallback. "
            "If any Read fails, STOP and report the exact failed path; do not attempt recovery by inspecting the run directory. "
            "For each eligible candidate, record what is actually visible regarding residual green, magenta/purple, "
            "and preservation of faint outer emission/Pillars/dark lanes. "
            "Choose the least aggressive amount that removes the unwanted green cast without introducing magenta/purple "
            "or suppressing faint structure. Then call select-publish."
        ),
        "saturation_processing_permitted": False,
    }
    assert_context_safe_payload(payload)
    return payload


def advance_stage(*, workspace: Path, project_name: str, timeout_seconds: int, max_candidates: int, plan_only: bool = False) -> dict[str, Any]:
    entry = begin_stage(workspace, project_name); action = entry.get("action")
    if entry.get("status") == "confirmation_required":
        payload = {"status": "confirmation_required", "action": "await_user_confirmation", "helper_version": VERSION, "project": entry.get("project"), "question": entry.get("question"), "confirmation_required": True, "saturation_processing_permitted": False}; assert_context_safe_payload(payload); return payload
    if action == "stop":
        payload = {"status": "blocked", "action": "stop", "helper_version": VERSION, "reason": compact_text(entry.get("reason", "")), "saturation_processing_permitted": False}; assert_context_safe_payload(payload); return payload
    if action == "review_select_publish": return compact_review_plan(review_plan(workspace=workspace, project_name=project_name, run_root=Path(entry["run_root"])))
    if action == "publish_recorded_selection":
        if plan_only:
            payload = {"status": "would_publish_recorded_selection", "action": action, "helper_version": VERSION, "run_root": entry.get("run_root"), "selected_candidate": entry.get("selected_candidate"), "saturation_processing_permitted": False}; assert_context_safe_payload(payload); return payload
        published = publish_project(workspace=workspace, project_name=project_name, run_root=Path(entry["run_root"])); final = status_project(workspace, project_name); payload = {**published, "verification": final}; assert_context_safe_payload(payload); return payload
    if action == "run_review_select_publish":
        if plan_only:
            payload = {"status": "would_generate_candidates", "action": action, "helper_version": VERSION, "project": str(project_paths(workspace, project_name)["project"]), "candidate_amounts": CANDIDATE_AMOUNTS, "manual_baseline_amount": MANUAL_BASELINE_AMOUNT, "confirmation_required": False, "saturation_processing_permitted": False}; assert_context_safe_payload(payload); return payload
        record = run_project(workspace=workspace, project_name=project_name, timeout_seconds=timeout_seconds, max_candidates=max_candidates); consume_fresh_authorization(workspace, project_name, Path(record["run_root"]))
        if record.get("publication_permitted") is not True:
            payload = {"status": "blocked", "action": "stop", "helper_version": VERSION, "run_root": record.get("run_root"), "reason": compact_text(record.get("publication_gate", {}).get("reason", "Publication blocked.")), "saturation_processing_permitted": False}; assert_context_safe_payload(payload); return payload
        plan = review_plan(workspace=workspace, project_name=project_name, run_root=Path(record["run_root"])); payload = compact_review_plan(plan); payload["generated_new_run"] = True; assert_context_safe_payload(payload); return payload
    raise GreenReductionError(f"Unsupported advance action {action!r}")


def select_publish_stage(*, workspace: Path, project_name: str, candidate_name: str, visual_notes: str, candidate_notes: list[str], policy_override_reason: str | None) -> dict[str, Any]:
    state = workflow_state(workspace, project_name)
    if state.get("action") != "review_select_publish": raise GreenReductionError("select-publish requires a durable run awaiting visual selection. Re-enter through advance.")
    run_root = Path(state["run_root"])
    selection = record_visual_selection(workspace=workspace, project_name=project_name, run_root=run_root, candidate_name=candidate_name, visual_notes=visual_notes, candidate_notes=candidate_notes, policy_override_reason=policy_override_reason)
    published = publish_project(workspace=workspace, project_name=project_name, run_root=run_root); final = status_project(workspace, project_name)
    payload = {**published, "selection": {"selected_candidate": selection["selected_candidate"], "selected_candidate_was_recommended": selection["selected_candidate_was_recommended"], "policy_override_used": selection["policy_override_used"], "visual_review_completed": True, "review_method": "openclaw-read"}, "verification": final}; assert_context_safe_payload(payload); return payload


def write_synthetic_upstream(workspace: Path, project_name: str) -> dict[str, Any]:
    paths = project_paths(workspace, project_name); paths["upstream"].mkdir(parents=True, exist_ok=True)
    h = w = 96; y, x = np.mgrid[0:h, 0:w]; radial = np.exp(-((x - w / 2) ** 2 + (y - h / 2) ** 2) / (2 * 18.0 ** 2))
    r = 0.08 + 0.10 * radial; g = 0.12 + 0.18 * radial; b = 0.07 + 0.09 * radial; data = np.stack([r, g, b]).astype(np.float32)
    hdu = fits.PrimaryHDU(data=data); hdu.header["FILTER"] = "mixed_Starless"; hdu.writeto(paths["upstream_output"], overwrite=False)
    evidence = inspect_fits(paths["upstream_output"])
    manifest = {"schema_version": 1, "helper_version": "1.0.4", "status": "ready", "visual_review_completed": True, "quality_assessment": {"satisfactory": True, "status": "satisfactory"}, "selection_policy": {"version": "1.0.4"}, "output": asdict(evidence), "next_stage": "siril-green-reduction", "green_reduction_processing_permitted": True}
    json_dump_atomic(paths["upstream_manifest"], manifest); return {"project": str(paths["project"]), "source_sha256": evidence.sha256}


def self_test(timeout_seconds: int) -> dict[str, Any]:
    root = WORKSPACE / ".skill-self-tests" / "siril-green-reduction" / unique_id()
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=False)
    project_name = "Synthetic Green Reduction v1.0.2"
    write_synthetic_upstream(workspace, project_name)
    generated_script = green_reduction_script_text()
    expected_rmgreen = ["rmgreen 2 0.100", "rmgreen 2 0.150", "rmgreen 2 0.200"]
    missing_rmgreen = [cmd for cmd in expected_rmgreen if cmd not in generated_script]
    if missing_rmgreen:
        raise GreenReductionError(f"Self-test generated Siril script is missing required commands: {missing_rmgreen}")
    if "-nopreserve" in generated_script:
        raise GreenReductionError("Self-test generated Siril script disabled Preserve Lightness with -nopreserve.")
    record = run_project(workspace=workspace, project_name=project_name, timeout_seconds=timeout_seconds, max_candidates=3)
    if record.get("completed_candidate_count") != 3: raise GreenReductionError("Self-test did not produce exactly three candidates.")
    eligible = record.get("publication_eligible_candidates", [])
    expected_eligible = {"candidate-00", "candidate-01", "candidate-02"}
    if set(eligible) != expected_eligible:
        by_name = {c.get("candidate"): c for c in record.get("candidates", [])}
        failed = {
            name: by_name.get(name, {}).get("quality_assessment", {}).get("failed_checks", [])
            for name in sorted(expected_eligible - set(eligible))
        }
        raise GreenReductionError(
            f"Self-test candidate eligibility mismatch. Expected all three candidates eligible; "
            f"eligible={eligible}; failed_checks={failed}"
        )
    if record.get("recommended_candidate") != "candidate-01":
        raise GreenReductionError(
            f"Self-test expected candidate-01 (0.15) recommendation after eligibility validation; "
            f"got {record.get('recommended_candidate')!r}."
        )
    plan = review_plan(workspace=workspace, project_name=project_name, run_root=Path(record["run_root"]))
    if plan.get("status") != "visual_review_required" or len(plan.get("read_targets", [])) < 2: raise GreenReductionError("Self-test review plan is invalid.")
    return {"status": "success", "helper_version": VERSION, "siril": siril_version(), "candidate_amounts": CANDIDATE_AMOUNTS, "recommended_candidate": record.get("recommended_candidate"), "publication_eligible_candidates": eligible, "single_siril_process_for_all_candidates": True, "review_method_required": "openclaw-read", "test_root": str(root)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run/resume preservation-safe Siril green reduction after black point.")
    parser.add_argument("--version", action="version", version=VERSION); sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("self-test"); p.add_argument("--timeout", type=int, default=1800)
    p = sub.add_parser("advance"); p.add_argument("--project", required=True); p.add_argument("--timeout", type=int, default=7200); p.add_argument("--max-candidates", type=int, default=3); p.add_argument("--plan-only", action="store_true")
    p = sub.add_parser("confirm-fresh"); p.add_argument("--project", required=True)
    p = sub.add_parser("select-publish"); p.add_argument("--project", required=True); p.add_argument("--candidate", required=True); p.add_argument("--visual-notes", required=True); p.add_argument("--note", "--candidate-note", dest="candidate_notes", action="append", required=True); p.add_argument("--policy-override-reason")
    p = sub.add_parser("stage-status"); p.add_argument("--project", required=True)
    p = sub.add_parser("status"); p.add_argument("--project", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "self-test": payload = self_test(args.timeout)
        elif args.command == "advance": payload = advance_stage(workspace=WORKSPACE, project_name=args.project, timeout_seconds=args.timeout, max_candidates=args.max_candidates, plan_only=args.plan_only)
        elif args.command == "confirm-fresh": payload = confirm_fresh_run(WORKSPACE, args.project)
        elif args.command == "select-publish": payload = select_publish_stage(workspace=WORKSPACE, project_name=args.project, candidate_name=args.candidate, visual_notes=args.visual_notes, candidate_notes=args.candidate_notes, policy_override_reason=args.policy_override_reason)
        elif args.command == "stage-status": payload = stage_status(WORKSPACE, args.project)
        elif args.command == "status": payload = status_project(WORKSPACE, args.project)
        else: raise GreenReductionError(f"Unsupported command {args.command!r}")
        if args.command != "self-test": assert_context_safe_payload(payload)
    except GreenReductionError as exc:
        payload = {"status": "blocked", "helper_version": VERSION, "error": compact_text(exc)}; print(json.dumps(payload, indent=2, sort_keys=True)); return 2
    print(json.dumps(payload, indent=2, sort_keys=True)); return 0 if payload.get("status") in CLI_SUCCESS_STATUSES else 2


if __name__ == "__main__":
    raise SystemExit(main())
