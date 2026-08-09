#!/usr/bin/env python3
"""Bounded Siril background-gradient cleanup for aligned mono SHO masters."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import zipfile
from typing import Any

import numpy as np
from astropy.io import fits

VERSION = "1.0.1"
WORKSPACE = Path("/home/peter/.openclaw/workspace/agents/codewarrior")
SIRIL_APP = Path(
    "/home/peter/.openclaw/runtime/siril-processor/toolchain/"
    ".toolchain/siril/1.4.4/squashfs-root/AppRun"
)
SIRIL_APPDIR = SIRIL_APP.parent
REQUIRED_SIRIL_VERSION = "1.4.4"
REQUIRED_ALIGNMENT_VERSION = "1.0.2"
FILTERS = ("Ha", "SII", "OIII")
MAX_CANDIDATES = 3
COMPATIBLE_RUN_VERSIONS = ("1.0.0", "1.0.1")
COMPACT_PREVIEW_MAXDIM = 1200
METHODS: tuple[dict[str, Any], ...] = (
    {
        "candidate": "candidate-00",
        "name": "linear-polynomial",
        "command": "subsky 1 -samples=20 -tolerance=1.0",
        "model": "polynomial",
        "degree": 1,
        "samples_per_line": 20,
        "tolerance_mad": 1.0,
        "smooth": None,
        "complexity_penalty": 0.00,
        "rationale": "Conservative first-order baseline with lowest overfit capacity.",
    },
    {
        "candidate": "candidate-01",
        "name": "quadratic-polynomial",
        "command": "subsky 2 -samples=20 -tolerance=1.0",
        "model": "polynomial",
        "degree": 2,
        "samples_per_line": 20,
        "tolerance_mad": 1.0,
        "smooth": None,
        "complexity_penalty": 0.05,
        "rationale": "Second-order comparison for broad curved gradients.",
    },
    {
        "candidate": "candidate-02",
        "name": "smooth-rbf",
        "command": "subsky -rbf -samples=12 -tolerance=1.0 -smooth=0.75",
        "model": "radial-basis-function",
        "degree": None,
        "samples_per_line": 12,
        "tolerance_mad": 1.0,
        "smooth": 0.75,
        "complexity_penalty": 0.10,
        "rationale": "Smooth RBF comparison; strongest model-preview review required.",
    },
)
FATAL_LOG_MARKERS = (
    "script execution failed",
    "command execution failed",
    "cannot load",
    "could not load",
    "not enough memory",
    "segmentation fault",
)


class CleanupError(RuntimeError):
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
    finite_fraction: float
    minimum: float
    maximum: float
    median: float
    filter_header: str | None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def unique_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return f"{stamp}-p{os.getpid()}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_dump_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{unique_id()}")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def inspect_fits(path: Path) -> FitsEvidence:
    if not path.is_file():
        raise CleanupError(f"FITS file does not exist: {path}")
    with fits.open(path, memmap=True) as hdul:
        data = hdul[0].data
        header = hdul[0].header
        if data is None:
            raise CleanupError(f"FITS contains no image data: {path}")
        array = np.asarray(data)
        if array.ndim == 2:
            height, width = array.shape
        elif array.ndim == 3 and array.shape[0] == 1:
            _, height, width = array.shape
        else:
            raise CleanupError(f"Expected monochrome FITS, found {array.shape}: {path}")
        if array.dtype.kind not in ("f", "u", "i"):
            raise CleanupError(f"Unsupported FITS dtype {array.dtype}: {path}")
        finite = np.isfinite(array)
        values = array[finite]
        return FitsEvidence(
            path=str(path),
            sha256=sha256_file(path),
            size=path.stat().st_size,
            bitpix=int(header.get("BITPIX", 0)),
            dtype=str(array.dtype),
            channels=1,
            width=int(width),
            height=int(height),
            finite_fraction=float(np.mean(finite)),
            minimum=float(np.min(values)) if values.size else math.nan,
            maximum=float(np.max(values)) if values.size else math.nan,
            median=float(np.median(values)) if values.size else math.nan,
            filter_header=str(header["FILTER"]) if "FILTER" in header else None,
        )


def read_mono(path: Path) -> tuple[np.ndarray, fits.Header]:
    with fits.open(path, memmap=True) as hdul:
        data = np.asarray(hdul[0].data, dtype=np.float32)
        header = hdul[0].header.copy()
    if data.ndim == 3 and data.shape[0] == 1:
        data = data[0]
    if data.ndim != 2:
        raise CleanupError(f"Expected mono image at {path}, found {data.shape}")
    return data, header


def project_paths(workspace: Path, project_name: str) -> dict[str, Path]:
    project = workspace / "Projects" / project_name
    processing = project / "processing"
    stable = processing / "mono-background-cleanup"
    return {
        "project": project,
        "aligned": processing / "aligned",
        "alignment_manifest": processing / "aligned" / "alignment-manifest.json",
        "runs": project / ".siril-mono-background-cleanup",
        "stable": stable,
        "stable_manifest": stable / "mono-background-cleanup-manifest.json",
    }


def output_path(directory: Path, filter_name: str) -> Path:
    return directory / f"background-clean_{filter_name}.fit"


def model_path(directory: Path, filter_name: str) -> Path:
    return directory / f"background-model_{filter_name}.fit"


def preview_path(directory: Path, filter_name: str, kind: str) -> Path:
    return directory / f"{filter_name}-{kind}.png"


def siril_version() -> dict[str, Any]:
    if not SIRIL_APP.is_file() or not os.access(SIRIL_APP, os.X_OK):
        raise CleanupError(f"Siril AppRun unavailable: {SIRIL_APP}")
    env = os.environ.copy()
    env["APPDIR"] = str(SIRIL_APPDIR)
    completed = subprocess.run(
        [str(SIRIL_APP), "siril-cli", "--version"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        timeout=60,
        check=False,
    )
    combined = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
    if completed.returncode != 0 or REQUIRED_SIRIL_VERSION not in combined:
        raise CleanupError(f"Expected Siril {REQUIRED_SIRIL_VERSION}; got: {combined}")
    return {"version": REQUIRED_SIRIL_VERSION, "version_output": combined, "path": str(SIRIL_APP)}


def run_siril(directory: Path, script: Path, logs: Path, timeout_seconds: int) -> dict[str, Any]:
    logs.mkdir(parents=True, exist_ok=True)
    command = [str(SIRIL_APP), "siril-cli", "--directory", str(directory), "--script", str(script)]
    env = os.environ.copy()
    env["APPDIR"] = str(SIRIL_APPDIR)
    started = time.monotonic()
    timed_out = False
    try:
        completed = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            timeout=timeout_seconds,
            check=False,
        )
        returncode, stdout, stderr = completed.returncode, completed.stdout, completed.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out, returncode = True, 124
        stdout, stderr = exc.stdout or "", exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
    duration = time.monotonic() - started
    stdout_path = logs / f"{script.stem}-stdout.log"
    stderr_path = logs / f"{script.stem}-stderr.log"
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    combined = f"{stdout}\n{stderr}".lower()
    return {
        "command": command,
        "display_command": f'env APPDIR="{SIRIL_APPDIR}" ' + " ".join(f'"{part}"' for part in command),
        "exit_status": int(returncode),
        "duration_seconds": round(duration, 3),
        "timed_out": timed_out,
        "timeout_seconds": timeout_seconds,
        "fatal_log_markers": [marker for marker in FATAL_LOG_MARKERS if marker in combined],
        "stdout_log": str(stdout_path),
        "stderr_log": str(stderr_path),
    }


def cleanup_script(method: dict[str, Any]) -> str:
    return "\n".join((
        f"requires {REQUIRED_SIRIL_VERSION}",
        'load "source.fit"',
        method["command"],
        'save "clean.fit"',
        "close",
        "",
    ))


def preview_script() -> str:
    return "\n".join((
        f"requires {REQUIRED_SIRIL_VERSION}",
        'load "source.fit"', "autostretch", 'savepng "../previews/before"', "close",
        'load "clean.fit"', "autostretch", 'savepng "../previews/after"', "close",
        'load "background-model.fit"', "autostretch", 'savepng "../previews/model"', "close",
        "",
    ))


def validate_alignment(paths: dict[str, Path]) -> tuple[dict[str, Any], dict[str, FitsEvidence]]:
    manifest_path = paths["alignment_manifest"]
    if not manifest_path.is_file():
        raise CleanupError(f"Alignment manifest missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("project") != paths["project"].name:
        raise CleanupError("Alignment manifest belongs to a different project")
    if manifest.get("helper_version") != REQUIRED_ALIGNMENT_VERSION:
        raise CleanupError(
            f"Expected alignment helper {REQUIRED_ALIGNMENT_VERSION}; "
            f"manifest reports {manifest.get('helper_version')}"
        )
    if not manifest.get("sho_composition_permitted"):
        raise CleanupError("Alignment manifest does not permit downstream processing")
    evidence: dict[str, FitsEvidence] = {}
    dimensions: set[tuple[int, int, int]] = set()
    for filter_name in FILTERS:
        record = manifest.get("outputs", {}).get(filter_name, {})
        path = Path(str(record.get("path", "")))
        expected = paths["aligned"] / f"aligned_{filter_name}.fit"
        if path.resolve() != expected.resolve():
            raise CleanupError(f"Unexpected aligned {filter_name} path: {path}")
        item = inspect_fits(path)
        if item.sha256 != record.get("sha256"):
            raise CleanupError(f"Aligned {filter_name} checksum mismatch")
        if item.channels != 1 or item.finite_fraction != 1.0:
            raise CleanupError(f"Aligned {filter_name} is not finite mono FITS")
        evidence[filter_name] = item
        dimensions.add((item.width, item.height, item.channels))
    if len(dimensions) != 1:
        raise CleanupError("Aligned masters do not share identical dimensions")
    return manifest, evidence


def robust_span(array: np.ndarray) -> float:
    finite = array[np.isfinite(array)]
    return max(float(np.percentile(finite, 99.5) - np.percentile(finite, 0.5)), 1.0e-12)


def background_score(array: np.ndarray) -> float:
    height, width = array.shape
    values: list[float] = []
    for row in range(8):
        for column in range(8):
            tile = array[
                row * height // 8 : (row + 1) * height // 8 : 3,
                column * width // 8 : (column + 1) * width // 8 : 3,
            ]
            finite = tile[np.isfinite(tile)]
            if finite.size >= 100:
                values.append(float(np.percentile(finite, 25.0)))
    if len(values) < 16:
        return math.nan
    data = np.asarray(values, dtype=np.float64)
    centre = float(np.median(data))
    return 1.4826 * float(np.median(np.abs(data - centre)))


def high_frequency(array: np.ndarray) -> np.ndarray:
    sample = array[::2, ::2].astype(np.float64, copy=False)
    centre = sample[1:-1, 1:-1]
    neighbours = (
        sample[:-2, 1:-1] + sample[2:, 1:-1]
        + sample[1:-1, :-2] + sample[1:-1, 2:]
    ) / 4.0
    return centre - neighbours


def correlation(a: np.ndarray, b: np.ndarray) -> float:
    valid = np.isfinite(a) & np.isfinite(b)
    av = a[valid].astype(np.float64, copy=False)
    bv = b[valid].astype(np.float64, copy=False)
    av -= float(np.mean(av))
    bv -= float(np.mean(bv))
    denominator = math.sqrt(float(np.dot(av, av)) * float(np.dot(bv, bv)))
    return float(np.dot(av, bv) / denominator) if denominator > 0.0 else math.nan

def build_model(source_path: Path, clean_path: Path, model_path_: Path, filter_name: str, method: dict[str, Any]) -> None:
    source, header = read_mono(source_path)
    clean, _ = read_mono(clean_path)
    if source.shape != clean.shape:
        raise CleanupError("Source and cleaned dimensions differ")
    model = (source.astype(np.float64) - clean.astype(np.float64)).astype(np.float32)
    header["FILTER"] = f"{filter_name}_BackgroundModel"
    header.add_history("Background model = aligned source - Siril subsky cleaned output")
    header.add_history(f"Method: {method['command']}")
    fits.PrimaryHDU(data=model, header=header).writeto(model_path_, overwrite=False, output_verify="fix")


def assess_quality(source_path: Path, clean_path: Path, model_path_: Path) -> dict[str, Any]:
    source_e = inspect_fits(source_path)
    clean_e = inspect_fits(clean_path)
    model_e = inspect_fits(model_path_)
    source, _ = read_mono(source_path)
    clean, _ = read_mono(clean_path)
    model, _ = read_mono(model_path_)
    failed: list[dict[str, Any]] = []

    def fail(metric: str, value: Any, requirement: str) -> None:
        failed.append({"metric": metric, "value": value, "requirement": requirement})

    if source.shape != clean.shape or source.shape != model.shape:
        fail("dimensions", {"source": list(source.shape), "clean": list(clean.shape), "model": list(model.shape)}, "all dimensions must match")
    if clean_e.finite_fraction != 1.0:
        fail("clean_finite_fraction", clean_e.finite_fraction, "must equal 1.0")
    if model_e.finite_fraction != 1.0:
        fail("model_finite_fraction", model_e.finite_fraction, "must equal 1.0")
    if clean_e.sha256 == source_e.sha256:
        fail("clean_sha256", clean_e.sha256, "must differ from source")

    span = robust_span(source)
    source_gradient = background_score(source)
    clean_gradient = background_score(clean)
    gradient_ratio = clean_gradient / source_gradient if source_gradient > 0.0 else math.nan
    source_hf = high_frequency(source)
    clean_hf = high_frequency(clean)
    model_hf = high_frequency(model)
    detail_correlation = correlation(source_hf, clean_hf)
    source_hf_rms = math.sqrt(float(np.mean(source_hf * source_hf)))
    model_hf_rms = math.sqrt(float(np.mean(model_hf * model_hf)))
    model_hf_ratio = model_hf_rms / source_hf_rms if source_hf_rms > 0.0 else math.nan
    finite_model = model[np.isfinite(model)]
    model_amplitude = float(np.percentile(finite_model, 99.0) - np.percentile(finite_model, 1.0))
    model_amplitude_ratio = model_amplitude / span
    strong_negative_fraction = float(np.mean(clean < -0.02 * span))
    high_clip_fraction = float(np.mean(clean > 1.0))
    median_shift_ratio = abs(clean_e.median - source_e.median) / span

    thresholds = {
        "maximum_gradient_ratio": 1.10,
        "minimum_detail_correlation": 0.98,
        "maximum_model_high_frequency_ratio": 0.35,
        "maximum_model_amplitude_ratio": 2.0,
        "maximum_strong_negative_fraction": 0.01,
        "maximum_high_clip_fraction": 1.0e-6,
        "maximum_median_shift_ratio": 1.0,
    }
    checks = (
        ("gradient_ratio", gradient_ratio, math.isfinite(gradient_ratio) and gradient_ratio <= thresholds["maximum_gradient_ratio"], f"must be <= {thresholds['maximum_gradient_ratio']}"),
        ("detail_correlation", detail_correlation, math.isfinite(detail_correlation) and detail_correlation >= thresholds["minimum_detail_correlation"], f"must be >= {thresholds['minimum_detail_correlation']}"),
        ("model_high_frequency_ratio", model_hf_ratio, math.isfinite(model_hf_ratio) and model_hf_ratio <= thresholds["maximum_model_high_frequency_ratio"], f"must be <= {thresholds['maximum_model_high_frequency_ratio']}"),
        ("model_amplitude_ratio", model_amplitude_ratio, math.isfinite(model_amplitude_ratio) and model_amplitude_ratio <= thresholds["maximum_model_amplitude_ratio"], f"must be <= {thresholds['maximum_model_amplitude_ratio']}"),
        ("strong_negative_fraction", strong_negative_fraction, strong_negative_fraction <= thresholds["maximum_strong_negative_fraction"], f"must be <= {thresholds['maximum_strong_negative_fraction']}"),
        ("high_clip_fraction", high_clip_fraction, high_clip_fraction <= thresholds["maximum_high_clip_fraction"], f"must be <= {thresholds['maximum_high_clip_fraction']}"),
        ("median_shift_ratio", median_shift_ratio, median_shift_ratio <= thresholds["maximum_median_shift_ratio"], f"must be <= {thresholds['maximum_median_shift_ratio']}"),
    )
    for metric, value, passed, requirement in checks:
        if not passed:
            fail(metric, value, requirement)

    satisfactory = not failed
    return {
        "status": "satisfactory" if satisfactory else "needs_review",
        "satisfactory": satisfactory,
        "failed_checks": failed,
        "metrics": {
            "source_background_gradient_score": source_gradient,
            "clean_background_gradient_score": clean_gradient,
            "gradient_ratio": gradient_ratio,
            "detail_correlation": detail_correlation,
            "source_high_frequency_rms": source_hf_rms,
            "model_high_frequency_rms": model_hf_rms,
            "model_high_frequency_ratio": model_hf_ratio,
            "source_robust_span": span,
            "model_amplitude": model_amplitude,
            "model_amplitude_ratio": model_amplitude_ratio,
            "strong_negative_fraction": strong_negative_fraction,
            "high_clip_fraction": high_clip_fraction,
            "median_shift_ratio": median_shift_ratio,
            "source_minimum": source_e.minimum,
            "source_maximum": source_e.maximum,
            "clean_minimum": clean_e.minimum,
            "clean_maximum": clean_e.maximum,
            "model_minimum": model_e.minimum,
            "model_maximum": model_e.maximum,
        },
        "thresholds": thresholds,
        "interpretation": (
            "Broad background variation was reduced or preserved while high-frequency mono detail remained correlated and the removed model stayed predominantly low-frequency."
            if satisfactory else
            "The candidate requires review because one or more gradient, detail, clipping, or model-smoothness safeguards failed."
        ),
    }


def classify(quality: dict[str, Any]) -> str:
    metrics = quality["metrics"]
    if not quality["satisfactory"]:
        return "technically-unsatisfactory"
    if metrics["model_high_frequency_ratio"] > 0.20:
        return "overfit-risk"
    if metrics["gradient_ratio"] > 0.95:
        return "minimal-gradient-change"
    if metrics["gradient_ratio"] < 0.40:
        return "strong-gradient-reduction"
    return "moderate-gradient-reduction"


def candidate_score(method: dict[str, Any], quality: dict[str, Any]) -> float:
    metrics = quality["metrics"]
    score = (
        float(metrics["gradient_ratio"])
        + 2.0 * max(0.0, 1.0 - float(metrics["detail_correlation"]))
        + 1.5 * float(metrics["model_high_frequency_ratio"])
        + 0.25 * float(metrics["model_amplitude_ratio"])
        + float(method["complexity_penalty"])
    )
    return float(score + (1000.0 if not quality["satisfactory"] else 0.0))


def execute_candidate(filter_name: str, source_path: Path, run_root: Path, method: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
    candidate = run_root / filter_name / method["candidate"]
    work = candidate / "work"
    logs = candidate / "logs"
    previews = candidate / "previews"
    work.mkdir(parents=True, exist_ok=False)
    logs.mkdir()
    previews.mkdir()
    source_copy = work / "source.fit"
    clean = work / "clean.fit"
    model = work / "background-model.fit"
    shutil.copy2(source_path, source_copy)

    cleanup = candidate / "cleanup.ssf"
    cleanup.write_text(cleanup_script(method), encoding="utf-8")
    cleanup_run = run_siril(work, cleanup, logs, timeout_seconds)
    if cleanup_run["exit_status"] != 0 or cleanup_run["timed_out"] or cleanup_run["fatal_log_markers"]:
        raise CleanupError(f"Siril subsky failed for {filter_name} {method['candidate']}; evidence: {candidate}")
    inspect_fits(clean)
    build_model(source_copy, clean, model, filter_name, method)
    quality = assess_quality(source_copy, clean, model)

    preview = candidate / "previews.ssf"
    preview.write_text(preview_script(), encoding="utf-8")
    preview_run = run_siril(work, preview, logs, min(timeout_seconds, 600))
    preview_files = {
        "before": previews / "before.png",
        "after": previews / "after.png",
        "model": previews / "model.png",
    }
    if preview_run["exit_status"] != 0 or preview_run["timed_out"] or preview_run["fatal_log_markers"] or not all(path.is_file() for path in preview_files.values()):
        raise CleanupError(f"Preview generation failed for {filter_name} {method['candidate']}; evidence: {candidate}")

    return {
        "candidate": method["candidate"],
        "filter": filter_name,
        "candidate_directory": str(candidate),
        "created_at": utc_now(),
        "helper_version": VERSION,
        "method": method,
        "classification": classify(quality),
        "source": asdict(inspect_fits(source_copy)),
        "clean": asdict(inspect_fits(clean)),
        "background_model": asdict(inspect_fits(model)),
        "quality_assessment": quality,
        "selection_score": candidate_score(method, quality),
        "cleanup_script": str(cleanup),
        "cleanup_script_sha256": sha256_file(cleanup),
        "cleanup_run": cleanup_run,
        "preview_script": str(preview),
        "preview_script_sha256": sha256_file(preview),
        "preview_run": preview_run,
        "previews": {key: str(value) for key, value in preview_files.items()},
        "status": "satisfactory" if quality["satisfactory"] else "needs_review",
    }


def recommended(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    eligible = [item for item in candidates if item["quality_assessment"]["satisfactory"]]
    return min(eligible, key=lambda item: (float(item["selection_score"]), item["candidate"])) if eligible else None



def compact_metric(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return "nan"
        return f"{value:.6g}"
    return str(value)


def reduced_preview_script(
    *,
    before_output: Path | None,
    after_output: Path,
    model_output: Path,
) -> str:
    lines = [f"requires {REQUIRED_SIRIL_VERSION}"]
    if before_output is not None:
        lines.extend(
            (
                'load "source.fit"',
                "autostretch",
                f"resample -maxdim={COMPACT_PREVIEW_MAXDIM} -interp=area",
                f'savepng "{before_output.with_suffix("")}"',
                "close",
            )
        )
    lines.extend(
        (
            'load "clean.fit"',
            "autostretch",
            f"resample -maxdim={COMPACT_PREVIEW_MAXDIM} -interp=area",
            f'savepng "{after_output.with_suffix("")}"',
            "close",
            'load "background-model.fit"',
            "autostretch",
            f"resample -maxdim={COMPACT_PREVIEW_MAXDIM} -interp=area",
            f'savepng "{model_output.with_suffix("")}"',
            "close",
            "",
        )
    )
    return "\n".join(lines)


def candidate_summary(item: dict[str, Any]) -> dict[str, Any]:
    metrics = item["quality_assessment"]["metrics"]
    return {
        "candidate": item["candidate"],
        "method": item["method"]["name"],
        "command": item["method"]["command"],
        "status": item["status"],
        "classification": item["classification"],
        "selection_score": float(item["selection_score"]),
        "gradient_ratio": float(metrics["gradient_ratio"]),
        "detail_correlation": float(metrics["detail_correlation"]),
        "model_high_frequency_ratio": float(
            metrics["model_high_frequency_ratio"]
        ),
        "model_amplitude_ratio": float(
            metrics["model_amplitude_ratio"]
        ),
        "strong_negative_fraction": float(
            metrics["strong_negative_fraction"]
        ),
        "high_clip_fraction": float(metrics["high_clip_fraction"]),
        "clean_sha256": item["clean"]["sha256"],
        "model_sha256": item["background_model"]["sha256"],
    }


def write_decision_brief(
    *,
    run_record: dict[str, Any],
    review_root: Path,
    compact_previews: dict[str, dict[str, Any]],
) -> tuple[Path, Path, Path]:
    summaries = {
        filter_name: [
            candidate_summary(item)
            for item in run_record["candidates"][filter_name]
        ]
        for filter_name in FILTERS
    }
    summary_path = review_root / "decision-summary.json"
    summary_payload = {
        "schema_version": 1,
        "created_at": utc_now(),
        "helper_version": VERSION,
        "candidate_generation_helper_version": run_record.get(
            "helper_version"
        ),
        "project": run_record["project_name"],
        "run_root": run_record["run_root"],
        "status": run_record["status"],
        "recommended_candidates": run_record[
            "recommended_candidates"
        ],
        "satisfactory_candidates": run_record[
            "satisfactory_candidates"
        ],
        "candidate_summaries": summaries,
        "compact_previews": compact_previews,
    }
    json_dump_atomic(summary_path, summary_payload)

    lines = [
        "# Mono background cleanup — compact decision brief",
        "",
        f"Project: `{run_record['project_name']}`  ",
        f"Run root: `{run_record['run_root']}`  ",
        f"Candidate generator: `{run_record.get('helper_version')}`  ",
        f"Review helper: `{VERSION}`",
        "",
        "## Review layout",
        "",
        "For each filter, inspect the single `before` preview once, then the",
        "three candidate `after` and `model` previews. All compact previews",
        f"have maximum dimension {COMPACT_PREVIEW_MAXDIM} pixels.",
        "",
        "Reject a candidate when its model contains recognizable Eagle",
        "Nebula, Pillars, stars, or fine filaments. Prefer the lowest-complexity",
        "candidate that adequately removes the broad gradient.",
        "",
    ]
    for filter_name in FILTERS:
        lines.extend(
            (
                f"## {filter_name}",
                "",
                f"Recommended: `{run_record['recommended_candidates'][filter_name]}`",
                "",
                "| Candidate | Method | Status | Grad ratio | Detail corr | Model HF | Model amp | Score |",
                "|---|---|---:|---:|---:|---:|---:|---:|",
            )
        )
        for item in summaries[filter_name]:
            lines.append(
                "| {candidate} | {method} | {status} | {gradient_ratio} | "
                "{detail_correlation} | {model_high_frequency_ratio} | "
                "{model_amplitude_ratio} | {selection_score} |".format(
                    **{
                        **item,
                        "gradient_ratio": compact_metric(
                            item["gradient_ratio"]
                        ),
                        "detail_correlation": compact_metric(
                            item["detail_correlation"]
                        ),
                        "model_high_frequency_ratio": compact_metric(
                            item["model_high_frequency_ratio"]
                        ),
                        "model_amplitude_ratio": compact_metric(
                            item["model_amplitude_ratio"]
                        ),
                        "selection_score": compact_metric(
                            item["selection_score"]
                        ),
                    }
                )
            )
        lines.extend(("", "Compact preview paths:", ""))
        preview_record = compact_previews[filter_name]
        lines.append(f"- Before: `{preview_record['before']}`")
        for candidate_name in (
            "candidate-00",
            "candidate-01",
            "candidate-02",
        ):
            row = preview_record[candidate_name]
            lines.append(
                f"- {candidate_name} after: `{row['after']}`"
            )
            lines.append(
                f"- {candidate_name} model: `{row['model']}`"
            )
        lines.append("")

    brief_path = review_root / "decision-brief.md"
    brief_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    continuation = review_root / "CONTINUE-IN-FRESH-SESSION.txt"
    continuation.write_text(
        "\n".join(
            (
                "Use the installed siril-mono-background-cleanup 1.0.1 skill ",
                f"to finish visual review for {run_record['project_name']}.",
                "",
                "Read only this compact decision brief:",
                str(brief_path),
                "",
                "Inspect only the compact preview paths listed in that brief.",
                "Do not print or read the full run-manifest.json into chat.",
                "Do not enumerate candidate directory trees or recalculate every checksum.",
                "Choose one satisfactory candidate independently for Ha, SII, and OIII.",
                "Reject any model containing recognizable nebula, Pillars, stars, or filaments.",
                "Prefer the lowest-complexity candidate that adequately removes the gradient.",
                "",
                "Publish with:",
                f"{WORKSPACE}/AstroProcessor/.venv/bin/python \\",
                f"  {WORKSPACE}/skills/siril-mono-background-cleanup/scripts/mono_background_cleanup.py \\",
                f"  publish --project {json.dumps(run_record['project_name'])} \\",
                f"  --run-root {json.dumps(run_record['run_root'])} \\",
                "  --ha-candidate <candidate-XX> \\",
                "  --sii-candidate <candidate-XX> \\",
                "  --oiii-candidate <candidate-XX> \\",
                "  --visual-notes <concise per-filter rationale> \\",
                "  --fresh-run",
                "",
                "Then run status. Return a compact result only: selected candidates,",
                "canonical output paths and SHA-256 values, manifest path, final status,",
                "visual_review_completed, and mono_linear_denoise_permitted.",
                "Do not report all nine full candidate records.",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    return summary_path, brief_path, continuation


def prepare_review_bundle(
    *,
    run_root: Path,
    project_name: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    manifest_path = run_root / "run-manifest.json"
    if not manifest_path.is_file():
        raise CleanupError(f"Run manifest missing: {manifest_path}")
    run_record = json.loads(manifest_path.read_text(encoding="utf-8"))
    if run_record.get("helper_version") not in COMPATIBLE_RUN_VERSIONS:
        raise CleanupError(
            "Candidate run version is not compatible with compact review: "
            f"{run_record.get('helper_version')}"
        )
    if run_record.get("project_name") != project_name:
        raise CleanupError("Run manifest belongs to another project")

    review_root = run_root / f"compact-review-{unique_id()}"
    review_root.mkdir(parents=True, exist_ok=False)
    compact_previews: dict[str, dict[str, Any]] = {}
    preview_runs: list[dict[str, Any]] = []

    for filter_name in FILTERS:
        filter_dir = review_root / filter_name
        filter_dir.mkdir(parents=True)
        before_output = filter_dir / "before.png"
        compact_previews[filter_name] = {
            "before": str(before_output),
        }
        for index, item in enumerate(run_record["candidates"][filter_name]):
            candidate_name = item["candidate"]
            candidate_dir = Path(item["candidate_directory"])
            work = candidate_dir / "work"
            after_output = filter_dir / f"{candidate_name}-after.png"
            model_output = filter_dir / f"{candidate_name}-model.png"
            script = review_root / f"{filter_name}-{candidate_name}.ssf"
            script.write_text(
                reduced_preview_script(
                    before_output=before_output if index == 0 else None,
                    after_output=after_output,
                    model_output=model_output,
                ),
                encoding="utf-8",
            )
            run = run_siril(
                work,
                script,
                review_root / "logs" / filter_name / candidate_name,
                min(timeout_seconds, 900),
            )
            preview_runs.append(
                {
                    "filter": filter_name,
                    "candidate": candidate_name,
                    **run,
                }
            )
            if (
                run["exit_status"] != 0
                or run["timed_out"]
                or run["fatal_log_markers"]
            ):
                raise CleanupError(
                    f"Compact preview failed for {filter_name} "
                    f"{candidate_name}; evidence preserved at {review_root}"
                )
            for path in (after_output, model_output):
                if not path.is_file():
                    raise CleanupError(f"Compact preview missing: {path}")
            compact_previews[filter_name][candidate_name] = {
                "after": str(after_output),
                "model": str(model_output),
            }
        if not before_output.is_file():
            raise CleanupError(f"Compact before preview missing: {before_output}")

    summary_path, brief_path, continuation = write_decision_brief(
        run_record=run_record,
        review_root=review_root,
        compact_previews=compact_previews,
    )

    archive_path = review_root / "mono-background-cleanup-review.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(review_root.rglob("*")):
            if path.is_file() and path != archive_path:
                archive.write(path, path.relative_to(review_root))

    bundle = {
        "schema_version": 1,
        "created_at": utc_now(),
        "helper_version": VERSION,
        "candidate_generation_helper_version": run_record.get(
            "helper_version"
        ),
        "project": project_name,
        "run_root": str(run_root),
        "review_root": str(review_root),
        "decision_summary": str(summary_path),
        "decision_brief": str(brief_path),
        "continuation_prompt": str(continuation),
        "review_archive": str(archive_path),
        "review_archive_sha256": sha256_file(archive_path),
        "compact_preview_max_dimension": COMPACT_PREVIEW_MAXDIM,
        "compact_previews": compact_previews,
        "preview_runs": preview_runs,
        "recommended_candidates": run_record[
            "recommended_candidates"
        ],
        "status": "awaiting_visual_selection",
    }
    bundle_path = review_root / "review-bundle.json"
    json_dump_atomic(bundle_path, bundle)
    bundle["review_bundle"] = str(bundle_path)

    run_record.setdefault("review_bundles", []).append(
        {
            "created_at": bundle["created_at"],
            "review_root": str(review_root),
            "review_bundle": str(bundle_path),
            "decision_brief": str(brief_path),
            "continuation_prompt": str(continuation),
            "review_archive": str(archive_path),
            "review_archive_sha256": bundle[
                "review_archive_sha256"
            ],
        }
    )
    run_record["latest_review_bundle"] = str(bundle_path)
    run_record["review_helper_version"] = VERSION
    json_dump_atomic(manifest_path, run_record)

    return {
        "status": "awaiting_visual_selection",
        "helper_version": VERSION,
        "candidate_generation_helper_version": run_record.get(
            "helper_version"
        ),
        "project": project_name,
        "run_root": str(run_root),
        "recommended_candidates": run_record[
            "recommended_candidates"
        ],
        "satisfactory_candidates": run_record[
            "satisfactory_candidates"
        ],
        "decision_brief": str(brief_path),
        "decision_summary": str(summary_path),
        "continuation_prompt": str(continuation),
        "review_archive": str(archive_path),
        "review_archive_sha256": bundle[
            "review_archive_sha256"
        ],
        "review_bundle": str(bundle_path),
        "compact_preview_count": 21,
        "full_run_manifest": str(manifest_path),
        "canonical_output_changed": bool(
            run_record.get("canonical_output_changed")
        ),
        "mono_linear_denoise_permitted": False,
        "message": (
            "Stop this session. Start a fresh session with the generated "
            "continuation prompt; do not print the full run manifest."
        ),
    }

def run_project(
    workspace: Path,
    project_name: str,
    timeout_seconds: int,
    fresh_run: bool,
) -> dict[str, Any]:
    paths = project_paths(workspace, project_name)
    if paths["stable"].exists() and not fresh_run:
        raise CleanupError(
            f"Canonical output exists: {paths['stable']}; use --fresh-run"
        )
    siril = siril_version()
    alignment_manifest, sources = validate_alignment(paths)
    run_root = paths["runs"] / f"cleanup-{unique_id()}"
    run_root.mkdir(parents=True, exist_ok=False)
    candidates: dict[str, list[dict[str, Any]]] = {}
    recommendations: dict[str, str | None] = {}
    satisfactory: dict[str, list[str]] = {}
    for filter_name in FILTERS:
        items = [
            execute_candidate(
                filter_name,
                Path(sources[filter_name].path),
                run_root,
                method,
                timeout_seconds,
            )
            for method in METHODS
        ]
        candidates[filter_name] = items
        choice = recommended(items)
        recommendations[filter_name] = (
            choice["candidate"] if choice else None
        )
        satisfactory[filter_name] = [
            item["candidate"]
            for item in items
            if item["quality_assessment"]["satisfactory"]
        ]
    ready_for_review = all(satisfactory[name] for name in FILTERS)
    record = {
        "schema_version": 1,
        "status": (
            "awaiting_visual_selection"
            if ready_for_review
            else "needs_review"
        ),
        "created_at": utc_now(),
        "run_started_at": utc_now(),
        "helper_version": VERSION,
        "project": str(paths["project"]),
        "project_name": project_name,
        "run_root": str(run_root),
        "fresh_run_requested": fresh_run,
        "alignment_manifest": str(paths["alignment_manifest"]),
        "alignment_manifest_sha256": sha256_file(
            paths["alignment_manifest"]
        ),
        "alignment_helper_version": alignment_manifest.get(
            "helper_version"
        ),
        "alignment_method": alignment_manifest.get("method"),
        "sources": {key: asdict(value) for key, value in sources.items()},
        "candidate_methods": list(METHODS),
        "maximum_candidates_per_filter": MAX_CANDIDATES,
        "candidates": candidates,
        "satisfactory_candidates": satisfactory,
        "recommended_candidates": recommendations,
        "siril": siril,
        "canonical_output_changed": False,
        "visual_selection_required": True,
        "mono_linear_denoise_permitted": False,
        "message": (
            "Prepare a compact review bundle, then continue visual review "
            "in a fresh session."
            if ready_for_review
            else "At least one filter lacks a satisfactory candidate."
        ),
    }
    json_dump_atomic(run_root / "run-manifest.json", record)
    if not ready_for_review:
        return {
            "status": "needs_review",
            "helper_version": VERSION,
            "project": project_name,
            "run_root": str(run_root),
            "full_run_manifest": str(run_root / "run-manifest.json"),
            "canonical_output_changed": False,
            "mono_linear_denoise_permitted": False,
        }
    return prepare_review_bundle(
        run_root=run_root,
        project_name=project_name,
        timeout_seconds=timeout_seconds,
    )

def publish_project(workspace: Path, project_name: str, run_root: Path, selections: dict[str, str], visual_notes: str, fresh_run: bool) -> dict[str, Any]:
    paths = project_paths(workspace, project_name)
    run_manifest_path = run_root / "run-manifest.json"
    if not run_manifest_path.is_file():
        raise CleanupError(f"Run manifest missing: {run_manifest_path}")
    run_record = json.loads(run_manifest_path.read_text(encoding="utf-8"))
    if run_record.get("helper_version") not in COMPATIBLE_RUN_VERSIONS or run_record.get("project_name") != project_name:
        raise CleanupError("Run manifest does not match a compatible helper/project")
    latest_review_bundle = run_record.get("latest_review_bundle")
    if not latest_review_bundle or not Path(latest_review_bundle).is_file():
        raise CleanupError("Compact review bundle is required before publication")
    if run_record.get("canonical_output_changed"):
        raise CleanupError("This run was already published")
    if not visual_notes.strip():
        raise CleanupError("Visual-selection notes are required")
    alignment_manifest, sources = validate_alignment(paths)
    for filter_name in FILTERS:
        if sources[filter_name].sha256 != run_record["sources"][filter_name]["sha256"]:
            raise CleanupError(f"Aligned {filter_name} changed after candidate generation")

    selected: dict[str, dict[str, Any]] = {}
    recommendations: dict[str, str | None] = {}
    for filter_name in FILTERS:
        matches = [item for item in run_record["candidates"][filter_name] if item["candidate"] == selections[filter_name]]
        if len(matches) != 1:
            raise CleanupError(f"{filter_name} candidate {selections[filter_name]!r} is not unique")
        item = matches[0]
        if not item["quality_assessment"]["satisfactory"]:
            raise CleanupError(f"Unsatisfactory {filter_name} candidate cannot publish")
        selected[filter_name] = item
        choice = recommended(run_record["candidates"][filter_name])
        recommendations[filter_name] = choice["candidate"] if choice else None

    existing = paths["stable"].exists()
    if existing and not fresh_run:
        raise CleanupError(f"Canonical directory exists: {paths['stable']}; use --fresh-run")
    staging = run_root / "publish-staging"
    if staging.exists():
        raise CleanupError(f"Publish staging already exists: {staging}")
    staging.mkdir(parents=True)
    final_outputs: dict[str, dict[str, Any]] = {}
    final_models: dict[str, dict[str, Any]] = {}
    final_previews: dict[str, dict[str, str]] = {}
    for filter_name in FILTERS:
        item = selected[filter_name]
        candidate_dir = Path(item["candidate_directory"])
        source_clean = candidate_dir / "work" / "clean.fit"
        source_model = candidate_dir / "work" / "background-model.fit"
        staged_clean = output_path(staging, filter_name)
        staged_model = model_path(staging, filter_name)
        shutil.copy2(source_clean, staged_clean)
        shutil.copy2(source_model, staged_model)
        for kind in ("before", "after", "model"):
            shutil.copy2(candidate_dir / "previews" / f"{kind}.png", preview_path(staging, filter_name, kind))
        clean_e = inspect_fits(staged_clean)
        model_e = inspect_fits(staged_model)
        if clean_e.sha256 != item["clean"]["sha256"] or model_e.sha256 != item["background_model"]["sha256"]:
            raise CleanupError(f"{filter_name} checksum changed during staging")
        clean_record = asdict(clean_e)
        clean_record["path"] = str(output_path(paths["stable"], filter_name))
        model_record = asdict(model_e)
        model_record["path"] = str(model_path(paths["stable"], filter_name))
        final_outputs[filter_name] = clean_record
        final_models[filter_name] = model_record
        final_previews[filter_name] = {
            kind: str(preview_path(paths["stable"], filter_name, kind))
            for kind in ("before", "after", "model")
        }

    previous = run_root / "previous-processing-mono-background-cleanup" if existing else None
    if previous is not None and previous.exists():
        raise CleanupError(f"Preservation destination exists: {previous}")
    manifest = {
        "schema_version": 1,
        "created_at": utc_now(),
        "status": "ready",
        "helper_version": VERSION,
        "candidate_generation_helper_version": run_record.get("helper_version"),
        "review_bundle": latest_review_bundle,
        "project": project_name,
        "project_path": str(paths["project"]),
        "stage_order": {
            "upstream": "siril-master-alignment",
            "current": "siril-mono-background-cleanup",
            "downstream": "siril-mono-linear-denoise",
        },
        "alignment_manifest": str(paths["alignment_manifest"]),
        "alignment_manifest_sha256": sha256_file(paths["alignment_manifest"]),
        "alignment_helper_version": alignment_manifest.get("helper_version"),
        "sources": {key: asdict(value) for key, value in sources.items()},
        "candidate_methods": list(METHODS),
        "all_candidates": run_record["candidates"],
        "recommended_candidates": recommendations,
        "selected_candidates": selections,
        "selected_candidate_was_recommended": {
            name: selections[name] == recommendations[name] for name in FILTERS
        },
        "visual_selection": {
            "required": True,
            "reviewer": "CodeWarrior",
            "notes": visual_notes.strip(),
            "required_checks": [
                "cleaned image preserves faint nebular structure",
                "background model contains only smooth gradients",
                "no recognizable Eagle Nebula, Pillars, stars, or filaments in model",
                "no banding, rings, blocks, or hard sample artefacts",
            ],
        },
        "selected_records": selected,
        "outputs": final_outputs,
        "background_models": final_models,
        "previews": final_previews,
        "stable_paths": {
            "directory": str(paths["stable"]),
            "manifest": str(paths["stable_manifest"]),
            "outputs": {name: str(output_path(paths["stable"], name)) for name in FILTERS},
        },
        "previous_processing_mono_background_cleanup_preserved_at": str(previous) if previous else None,
        "publication_method": "Generate and preserve three fixed Siril subsky candidates per filter, require cleaned-image and model-preview review, preserve previous canonical output, then atomically publish.",
        "siril": siril_version(),
        "visual_review_completed": True,
        "mono_linear_denoise_permitted": True,
    }
    json_dump_atomic(staging / "mono-background-cleanup-manifest.json", manifest)
    moved_existing = False
    try:
        if existing:
            paths["stable"].rename(previous)
            moved_existing = True
        staging.rename(paths["stable"])
    except Exception:
        if moved_existing and not paths["stable"].exists():
            previous.rename(paths["stable"])
        raise
    run_record.update({
        "status": "published",
        "published_at": utc_now(),
        "canonical_output_changed": True,
        "selected_candidates": selections,
        "visual_selection_notes": visual_notes.strip(),
        "mono_linear_denoise_permitted": True,
    })
    json_dump_atomic(run_manifest_path, run_record)
    full_result = {
        "status": "ready",
        "created_at": utc_now(),
        "helper_version": VERSION,
        "candidate_generation_helper_version": run_record.get(
            "helper_version"
        ),
        "project": str(paths["project"]),
        "run_root": str(run_root),
        "selected_candidates": selections,
        "recommended_candidates": recommendations,
        "stable_directory": str(paths["stable"]),
        "stable_manifest": str(paths["stable_manifest"]),
        "outputs": final_outputs,
        "background_models": final_models,
        "previews": final_previews,
        "previous_processing_mono_background_cleanup_preserved_at": manifest[
            "previous_processing_mono_background_cleanup_preserved_at"
        ],
        "canonical_output_changed": True,
        "visual_review_completed": True,
        "mono_linear_denoise_permitted": True,
        "manifest": manifest,
    }
    publication_result = run_root / "publication-result.json"
    json_dump_atomic(publication_result, full_result)
    return {
        "status": "ready",
        "helper_version": VERSION,
        "project": str(paths["project"]),
        "run_root": str(run_root),
        "selected_candidates": selections,
        "recommended_candidates": recommendations,
        "stable_directory": str(paths["stable"]),
        "stable_manifest": str(paths["stable_manifest"]),
        "publication_result": str(publication_result),
        "outputs": {
            filter_name: {
                "path": final_outputs[filter_name]["path"],
                "sha256": final_outputs[filter_name]["sha256"],
            }
            for filter_name in FILTERS
        },
        "previous_processing_mono_background_cleanup_preserved_at": manifest[
            "previous_processing_mono_background_cleanup_preserved_at"
        ],
        "canonical_output_changed": True,
        "visual_review_completed": True,
        "mono_linear_denoise_permitted": True,
    }


def status_project(
    workspace: Path,
    project_name: str,
) -> dict[str, Any]:
    paths = project_paths(workspace, project_name)
    if not paths["stable_manifest"].is_file():
        return {
            "status": "missing",
            "helper_version": VERSION,
            "project": str(paths["project"]),
            "stable_directory": str(paths["stable"]),
            "mono_linear_denoise_permitted": False,
        }
    manifest = json.loads(
        paths["stable_manifest"].read_text(encoding="utf-8")
    )
    errors: list[str] = []
    compact_outputs: dict[str, dict[str, Any]] = {}
    for filter_name in FILTERS:
        out = output_path(paths["stable"], filter_name)
        mod = model_path(paths["stable"], filter_name)
        if not out.is_file():
            errors.append(f"Missing cleaned {filter_name}: {out}")
        else:
            current = asdict(inspect_fits(out))
            expected = manifest.get("outputs", {}).get(
                filter_name,
                {},
            ).get("sha256")
            if expected and current["sha256"] != expected:
                errors.append(
                    f"Cleaned {filter_name} checksum mismatch"
                )
            compact_outputs[filter_name] = {
                "path": str(out),
                "sha256": current["sha256"],
                "width": current["width"],
                "height": current["height"],
                "bitpix": current["bitpix"],
            }
        if not mod.is_file():
            errors.append(f"Missing model {filter_name}: {mod}")
        for kind in ("before", "after", "model"):
            if not preview_path(
                paths["stable"],
                filter_name,
                kind,
            ).is_file():
                errors.append(f"Missing preview {filter_name}-{kind}")
    ready = manifest.get("status") == "ready" and not errors
    return {
        "status": "ready" if ready else "invalid",
        "helper_version": VERSION,
        "project": str(paths["project"]),
        "stable_directory": str(paths["stable"]),
        "stable_manifest": str(paths["stable_manifest"]),
        "errors": errors,
        "selected_candidates": manifest.get("selected_candidates"),
        "recommended_candidates": manifest.get(
            "recommended_candidates"
        ),
        "selected_candidate_was_recommended": manifest.get(
            "selected_candidate_was_recommended"
        ),
        "outputs": compact_outputs,
        "visual_review_completed": manifest.get(
            "visual_review_completed",
            False,
        ),
        "mono_linear_denoise_permitted": ready,
    }

def write_synthetic_mono(path: Path) -> None:
    rng = np.random.default_rng(20260806)
    height = width = 640
    yy, xx = np.mgrid[0:height, 0:width]
    gradient = 0.004 + 0.0020 * xx / width + 0.0012 * yy / height + 0.0010 * (xx / width) * (yy / height)
    nebula = 0.010 * np.exp(-(((xx - 335.0) / 125.0) ** 2 + ((yy - 315.0) / 105.0) ** 2))
    image = gradient + nebula
    for cy, cx, amplitude in ((100, 120, 0.10), (500, 515, 0.08), (185, 500, 0.05)):
        image += amplitude * np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2.0 * 2.0**2))
    image += rng.normal(0.0, 0.00012, (height, width))
    image = np.clip(image, 0.0001, 0.5).astype(np.float32)
    header = fits.Header()
    header["FILTER"] = "Ha"
    header["OBJECT"] = "Synthetic mono background-cleanup self-test"
    fits.PrimaryHDU(data=image, header=header).writeto(path, overwrite=False, output_verify="fix")


def self_test(timeout_seconds: int) -> dict[str, Any]:
    siril = siril_version()
    root = (
        WORKSPACE
        / ".skill-self-tests"
        / "siril-mono-background-cleanup"
        / unique_id()
    )
    root.mkdir(parents=True, exist_ok=False)
    source = root / "synthetic-aligned-Ha.fit"
    write_synthetic_mono(source)
    candidates = [
        execute_candidate("Ha", source, root, method, timeout_seconds)
        for method in METHODS
    ]
    failed: list[dict[str, Any]] = []
    for item in candidates:
        for run_name in ("cleanup_run", "preview_run"):
            record = item[run_name]
            if (
                record["exit_status"] != 0
                or record["timed_out"]
                or record["fatal_log_markers"]
            ):
                failed.append(
                    {
                        "metric": f"{item['candidate']}_{run_name}",
                        "value": record,
                    }
                )
        for preview in item["previews"].values():
            if not Path(preview).is_file():
                failed.append(
                    {
                        "metric": f"{item['candidate']}_preview",
                        "value": preview,
                    }
                )
    if failed:
        raise CleanupError(
            f"Self-test failed {failed}; evidence preserved at {root}"
        )

    # Reuse the same synthetic candidates for all three filters solely to
    # verify compact review generation without nine additional subsky runs.
    synthetic_candidates: dict[str, list[dict[str, Any]]] = {}
    for filter_name in FILTERS:
        rows: list[dict[str, Any]] = []
        for item in candidates:
            clone = json.loads(json.dumps(item))
            clone["filter"] = filter_name
            rows.append(clone)
        synthetic_candidates[filter_name] = rows
    run_record = {
        "schema_version": 1,
        "status": "awaiting_visual_selection",
        "created_at": utc_now(),
        "run_started_at": utc_now(),
        "helper_version": VERSION,
        "project": str(root),
        "project_name": "synthetic-self-test",
        "run_root": str(root),
        "candidates": synthetic_candidates,
        "recommended_candidates": {
            filter_name: recommended(
                synthetic_candidates[filter_name]
            )["candidate"]
            for filter_name in FILTERS
        },
        "satisfactory_candidates": {
            filter_name: [item["candidate"] for item in candidates]
            for filter_name in FILTERS
        },
        "canonical_output_changed": False,
        "mono_linear_denoise_permitted": False,
    }
    json_dump_atomic(root / "run-manifest.json", run_record)
    review = prepare_review_bundle(
        run_root=root,
        project_name="synthetic-self-test",
        timeout_seconds=timeout_seconds,
    )
    return {
        "status": "success",
        "created_at": utc_now(),
        "helper_version": VERSION,
        "self_test_directory": str(root),
        "siril": siril,
        "candidate_count": len(candidates),
        "decision_brief": review["decision_brief"],
        "continuation_prompt": review["continuation_prompt"],
        "review_archive": review["review_archive"],
        "review_archive_sha256": review[
            "review_archive_sha256"
        ],
        "compact_preview_count": review["compact_preview_count"],
        "tests": [
            "real degree-1 polynomial subsky",
            "real degree-2 polynomial subsky",
            "real smooth RBF subsky",
            "compact max-dimension preview generation",
            "decision brief and summary generation",
            "review ZIP generation",
            "legacy-compatible run-manifest review",
            "compact stdout contract",
        ],
    }

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate, compactly review, and publish bounded Siril "
            "background cleanup candidates for aligned mono masters."
        )
    )
    parser.add_argument("--version", action="version", version=VERSION)
    subs = parser.add_subparsers(dest="command")

    test = subs.add_parser("self-test")
    test.add_argument("--timeout", type=int, default=1800)

    run = subs.add_parser("run")
    run.add_argument("--project", required=True)
    run.add_argument("--timeout", type=int, default=7200)
    run.add_argument("--fresh-run", action="store_true")

    review = subs.add_parser("prepare-review")
    review.add_argument("--project", required=True)
    review.add_argument("--run-root", required=True, type=Path)
    review.add_argument("--timeout", type=int, default=1800)

    publish = subs.add_parser("publish")
    publish.add_argument("--project", required=True)
    publish.add_argument("--run-root", required=True, type=Path)
    publish.add_argument("--ha-candidate", required=True)
    publish.add_argument("--sii-candidate", required=True)
    publish.add_argument("--oiii-candidate", required=True)
    publish.add_argument("--visual-notes", required=True)
    publish.add_argument("--fresh-run", action="store_true")

    status = subs.add_parser("status")
    status.add_argument("--project", required=True)
    return parser

def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        return 2
    try:
        if args.command == "self-test":
            payload = self_test(args.timeout)
        elif args.command == "run":
            payload = run_project(
                WORKSPACE,
                args.project,
                args.timeout,
                args.fresh_run,
            )
        elif args.command == "prepare-review":
            payload = prepare_review_bundle(
                run_root=args.run_root.resolve(),
                project_name=args.project,
                timeout_seconds=args.timeout,
            )
        elif args.command == "publish":
            payload = publish_project(
                WORKSPACE,
                args.project,
                args.run_root.resolve(),
                {
                    "Ha": args.ha_candidate,
                    "SII": args.sii_candidate,
                    "OIII": args.oiii_candidate,
                },
                args.visual_notes,
                args.fresh_run,
            )
        elif args.command == "status":
            payload = status_project(WORKSPACE, args.project)
        else:
            raise CleanupError(f"Unsupported command: {args.command}")
    except Exception as exc:
        payload = {
            "status": "blocked",
            "helper_version": VERSION,
            "error": str(exc),
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 2
    print(json.dumps(payload, indent=2, sort_keys=True))
    return (
        0
        if payload.get("status")
        in ("success", "ready", "awaiting_visual_selection")
        else 2
    )


if __name__ == "__main__":
    raise SystemExit(main())
