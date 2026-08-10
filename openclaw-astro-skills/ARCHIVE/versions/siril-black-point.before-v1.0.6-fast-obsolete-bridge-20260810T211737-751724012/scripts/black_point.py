#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from astropy.io import fits

VERSION = "1.0.4"
REQUIRED_SIRIL_VERSION = "1.4.4"
WORKSPACE = Path("/home/peter/.openclaw/workspace/agents/codewarrior")
SIRIL_APP = Path(
    "/home/peter/.openclaw/runtime/siril-processor/toolchain/.toolchain/"
    "siril/1.4.4/squashfs-root/AppRun"
)
SIRIL_APPDIR = SIRIL_APP.parent
UPSTREAM_HELPER_VERSIONS = frozenset({"1.2.0"})
COMPATIBLE_RUN_HELPER_VERSIONS = frozenset({"1.0.1", "1.0.2", "1.0.3", "1.0.4"})
COMPATIBLE_CANONICAL_HELPER_VERSIONS = frozenset({"1.0.4"})
CLI_SUCCESS_STATUSES = frozenset(
    {
        "success",
        "ready",
        "start_new_run",
        "awaiting_visual_selection",
        "ready_to_publish",
        "confirmation_required",
        "fresh_run_authorized",
        "missing",
        "needs_reprocessing",
        "visual_review_required",
        "would_generate_candidates",
        "would_publish_recorded_selection",
        "needs_reselection",
        "would_prepare_policy_reselection",
    }
)
MAX_CANDIDATES = 3
TARGET_OUTPUT_LOW_TAIL = {
    "candidate-00": 0.0080,
    "candidate-01": 0.0045,
    "candidate-02": 0.0025,
}
TARGET_RECOMMENDED_LOW_TAIL = 0.0045
TARGET_RECOMMENDED_MEDIAN = 0.018
SOURCE_LOW_TAIL_QUANTILE = 0.001
MAX_LUMA_LOW_CLIP_FRACTION = 0.001  # 0.1%, luminance hard ceiling
PREFERRED_LUMA_LOW_CLIP_FRACTION = 0.00001
MAX_CHANNEL_LOW_CLIP_FRACTION = 0.006  # 0.6%, per-channel hard ceiling
PREFERRED_CHANNEL_LOW_CLIP_FRACTION = 0.002  # 0.2%, preferred
SELECTION_POLICY_VERSION = "1.0.4"
SELECTION_POLICY_PREDECESSOR_VERSIONS = frozenset({"1.0.1", "1.0.2", "1.0.3"})
AGGRESSIVE_OVERRIDE_MIN_CHARS = 80
MAX_LUMA_HIGH_CLIP_FRACTION = 1e-7
MIN_LUMA_CORRELATION = 0.995
MAX_OUTPUT_VALUE = 0.98
BALANCED_LOW_TAIL_MIN = 0.0005
BALANCED_LOW_TAIL_MAX = 0.012
BALANCED_MEDIAN_MIN = 0.008
BALANCED_MEDIAN_MAX = 0.040
FATAL_LOG_MARKERS = (
    "script execution failed",
    "error in line",
    "could not load",
    "cannot open",
    "unknown command",
    "not enough memory",
)


class BlackPointError(RuntimeError):
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
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def project_paths(workspace: Path, project_name: str) -> dict[str, Path]:
    project = workspace / "Projects" / project_name
    stable = project / "processing" / "black-point"
    runs = project / ".siril-black-point"
    return {
        "project": project,
        "upstream": project / "processing" / "ghs-pass2",
        "upstream_output": (
            project / "processing" / "ghs-pass2" / "SHO-starless-ghs-pass2.fit"
        ),
        "upstream_manifest": (
            project / "processing" / "ghs-pass2" / "ghs-pass2-manifest.json"
        ),
        "runs": runs,
        "intents": runs / "stage-intents",
        "stable": stable,
        "stable_output": stable / "SHO-starless-black-point.fit",
        "stable_before_preview": (
            stable / "SHO-starless-ghs-pass2-before-black-point.png"
        ),
        "stable_after_preview": (
            stable / "SHO-starless-black-point-linear.png"
        ),
        "stable_manifest": stable / "black-point-manifest.json",
    }


def inspect_fits(path: Path) -> FitsEvidence:
    if not path.is_file():
        raise BlackPointError(f"FITS file does not exist: {path}")
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
            raise BlackPointError(
                f"Unsupported FITS dimensions {data.shape} for {path}"
            )
        sample = np.asarray(data, dtype=np.float64)
        finite = np.isfinite(sample)
        finite_fraction = float(np.mean(finite))
        if not np.any(finite):
            raise BlackPointError(f"FITS has no finite pixels: {path}")
        values = sample[finite]
        minimum = float(np.min(values))
        median = float(np.median(values))
        maximum = float(np.max(values))
        dtype = str(data.dtype)
    return FitsEvidence(
        path=str(path),
        sha256=sha256_file(path),
        size=path.stat().st_size,
        bitpix=bitpix,
        dtype=dtype,
        channels=int(channels),
        width=int(width),
        height=int(height),
        minimum=minimum,
        median=median,
        maximum=maximum,
        finite_fraction=finite_fraction,
        filter_header=str(filter_header) if filter_header is not None else None,
    )


def load_luma(path: Path, stride: int = 1) -> np.ndarray:
    with fits.open(path, memmap=True) as hdul:
        data = np.asarray(hdul[0].data)
        if data.ndim == 2:
            sample = np.asarray(data[::stride, ::stride], dtype=np.float64)
        elif data.ndim == 3 and data.shape[0] == 3:
            sample = np.asarray(
                data[:, ::stride, ::stride], dtype=np.float64
            ).mean(axis=0)
        else:
            raise BlackPointError(f"Unsupported FITS shape {data.shape}: {path}")
    finite = sample[np.isfinite(sample)]
    if finite.size == 0:
        raise BlackPointError(f"No finite luminance samples: {path}")
    return finite


def validate_upstream(paths: dict[str, Path]) -> tuple[dict[str, Any], FitsEvidence]:
    manifest_path = paths["upstream_manifest"]
    if not manifest_path.is_file():
        raise BlackPointError(f"Upstream GHS pass-2 manifest missing: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise BlackPointError(f"Could not read upstream manifest: {exc}") from exc

    errors: list[str] = []
    if manifest.get("helper_version") not in UPSTREAM_HELPER_VERSIONS:
        errors.append(
            f"upstream helper_version {manifest.get('helper_version')!r} is not "
            f"one of {sorted(UPSTREAM_HELPER_VERSIONS)}"
        )
    if manifest.get("status") != "ready":
        errors.append("upstream status must be ready")
    if manifest.get("visual_review_completed") is not True:
        errors.append("upstream visual_review_completed must be true")
    if manifest.get("black_point_processing_permitted") is not True:
        errors.append("upstream black_point_processing_permitted must be true")
    if manifest.get("next_stage") != "siril-black-point":
        errors.append("upstream next_stage must be siril-black-point")

    output_path = paths["upstream_output"]
    evidence = inspect_fits(output_path)
    manifest_output = manifest.get("output", {})
    if manifest_output.get("sha256") != evidence.sha256:
        errors.append("upstream output SHA does not match canonical FITS")
    recorded_path = manifest_output.get("path")
    if recorded_path and Path(recorded_path).resolve() != output_path.resolve():
        errors.append("upstream manifest output path is not canonical GHS pass-2 FITS")
    if evidence.channels != 3:
        errors.append("upstream GHS pass-2 output must be RGB")
    if evidence.bitpix != -32:
        errors.append("upstream GHS pass-2 output must be 32-bit floating FITS")
    if evidence.finite_fraction != 1.0:
        errors.append("upstream GHS pass-2 output must be fully finite")

    quality = manifest.get("quality_assessment", {})
    if quality.get("satisfactory") is not True:
        errors.append("upstream quality assessment must be satisfactory")

    if errors:
        raise BlackPointError("Upstream GHS pass-2 contract failed: " + "; ".join(errors))
    return manifest, evidence


def siril_version() -> dict[str, Any]:
    if not SIRIL_APP.is_file() or not os.access(SIRIL_APP, os.X_OK):
        raise BlackPointError(f"Siril AppRun is unavailable: {SIRIL_APP}")
    env = os.environ.copy()
    env["APPDIR"] = str(SIRIL_APPDIR)
    completed = subprocess.run(
        [str(SIRIL_APP), "siril-cli", "--version"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        check=False,
        timeout=60,
    )
    output = completed.stdout.strip()
    if completed.returncode != 0 or REQUIRED_SIRIL_VERSION not in output:
        raise BlackPointError(
            f"Expected Siril {REQUIRED_SIRIL_VERSION}, got exit "
            f"{completed.returncode}: {output}"
        )
    return {
        "path": str(SIRIL_APP),
        "version": REQUIRED_SIRIL_VERSION,
        "version_output": output,
    }


def run_siril_script(
    *,
    directory: Path,
    script: Path,
    stdout_log: Path,
    stderr_log: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    stdout_log.parent.mkdir(parents=True, exist_ok=True)
    stderr_log.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(SIRIL_APP),
        "siril-cli",
        "--directory",
        str(directory),
        "--script",
        str(script),
    ]
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
        code = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        code = 124
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
    duration = time.monotonic() - started
    stdout_log.write_text(stdout, encoding="utf-8")
    stderr_log.write_text(stderr, encoding="utf-8")
    combined = f"{stdout}\n{stderr}".lower()
    fatal = [marker for marker in FATAL_LOG_MARKERS if marker in combined]
    return {
        "command": command,
        "display_command": (
            f'env APPDIR="{SIRIL_APPDIR}" '
            + " ".join(f'"{part}"' for part in command)
        ),
        "exit_status": int(code),
        "duration_seconds": round(duration, 3),
        "timed_out": timed_out,
        "timeout_seconds": int(timeout_seconds),
        "fatal_log_markers": fatal,
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
    }


def black_point_for_target(source_low_tail: float, target_output: float) -> float:
    if not (0.0 < target_output < 1.0):
        raise BlackPointError("Target output low-tail must be within (0, 1).")
    bp = (source_low_tail - target_output) / (1.0 - target_output)
    return float(max(0.0, min(bp, source_low_tail - 1e-7)))


def candidate_parameters(source_path: Path) -> list[dict[str, Any]]:
    luma = load_luma(source_path)
    q = float(np.quantile(luma, SOURCE_LOW_TAIL_QUANTILE))
    median = float(np.median(luma))
    if median < 0.08:
        raise BlackPointError(
            "Upstream GHS pass-2 luminance median is unexpectedly low; "
            "automatic black-point adjustment is blocked rather than applying "
            "an unnecessary or destructive shift."
        )
    result = []
    for name in ("candidate-00", "candidate-01", "candidate-02"):
        target = TARGET_OUTPUT_LOW_TAIL[name]
        bp = black_point_for_target(q, target)
        result.append(
            {
                "candidate": name,
                "BP": round(bp, 8),
                "target_output_luma_p001": target,
                "source_luma_p001": q,
                "source_luma_median": median,
                "adaptation_reason": {
                    "candidate-00": (
                        "Conservative black-point shift keeping the 0.1th "
                        "luminance percentile visibly above black."
                    ),
                    "candidate-01": (
                        "Moderate near-black target intended to darken the "
                        "raised background while preserving faint emission."
                    ),
                    "candidate-02": (
                        "Assertive bounded comparison moving the low luminance "
                        "tail closer to black while remaining under the hard "
                        "clipping ceiling."
                    ),
                }[name],
            }
        )
    if len({item["BP"] for item in result}) != len(result):
        raise BlackPointError("Adaptive black-point candidates are not unique.")
    return result


def black_point_script_text(bp: float) -> str:
    return "\n".join(
        (
            f"requires {REQUIRED_SIRIL_VERSION}",
            'load "SHO-starless-ghs-pass2.fit"',
            f"linstretch -BP={bp:.8f} -clipmode=rgbblend",
            'save "SHO-starless-black-point.fit"',
            "close",
            "",
        )
    )


def preview_script_text() -> str:
    return "\n".join(
        (
            f"requires {REQUIRED_SIRIL_VERSION}",
            'load "SHO-starless-ghs-pass2.fit"',
            'savepng "../previews/SHO-starless-ghs-pass2-before-black-point"',
            "close",
            'load "SHO-starless-black-point.fit"',
            'savepng "../previews/SHO-starless-black-point-linear"',
            "close",
            "",
        )
    )


def production_quality_assessment(
    source_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    source_evidence = inspect_fits(source_path)
    output_evidence = inspect_fits(output_path)
    failed: list[dict[str, Any]] = []

    def fail(metric: str, value: Any, requirement: str) -> None:
        failed.append({"metric": metric, "value": value, "requirement": requirement})

    if (
        source_evidence.width != output_evidence.width
        or source_evidence.height != output_evidence.height
        or source_evidence.channels != output_evidence.channels
    ):
        fail("dimensions", asdict(output_evidence), "must match source")
    if output_evidence.bitpix != -32:
        fail("bitpix", output_evidence.bitpix, "must equal -32")
    if output_evidence.finite_fraction != 1.0:
        fail("finite_fraction", output_evidence.finite_fraction, "must equal 1.0")
    if output_evidence.sha256 == source_evidence.sha256:
        fail("output_sha256", output_evidence.sha256, "must differ from source")

    source_luma = load_luma(source_path)
    output_luma = load_luma(output_path)
    if source_luma.size != output_luma.size:
        fail("luma_sample_size", output_luma.size, "must match source")

    source_p001 = float(np.quantile(source_luma, 0.001))
    output_p001 = float(np.quantile(output_luma, 0.001))
    source_median = float(np.median(source_luma))
    output_median = float(np.median(output_luma))
    output_p90 = float(np.quantile(output_luma, 0.90))
    output_p99 = float(np.quantile(output_luma, 0.99))
    low_clip = float(np.mean(output_luma <= 1e-7))
    high_clip = float(np.mean(output_luma >= 1.0 - 1e-7))

    source_sample = load_luma(source_path, stride=4)
    output_sample = load_luma(output_path, stride=4)
    corr = float(np.corrcoef(source_sample, output_sample)[0, 1])

    with fits.open(output_path, memmap=True) as hdul:
        out = np.asarray(hdul[0].data)
        finite = np.isfinite(out)
        channel_low_clip = float(np.mean(out[finite] <= 1e-7))
        channel_high_clip = float(np.mean(out[finite] >= 1.0 - 1e-7))

    if low_clip > MAX_LUMA_LOW_CLIP_FRACTION:
        fail(
            "low_luma_clip_fraction",
            low_clip,
            f"must be <= {MAX_LUMA_LOW_CLIP_FRACTION}",
        )
    if high_clip > MAX_LUMA_HIGH_CLIP_FRACTION:
        fail(
            "high_luma_clip_fraction",
            high_clip,
            f"must be <= {MAX_LUMA_HIGH_CLIP_FRACTION}",
        )
    if channel_low_clip > MAX_CHANNEL_LOW_CLIP_FRACTION:
        fail(
            "channel_low_clip_fraction",
            channel_low_clip,
            f"must be <= {MAX_CHANNEL_LOW_CLIP_FRACTION}",
        )
    if corr < MIN_LUMA_CORRELATION:
        fail("luma_correlation", corr, f"must be >= {MIN_LUMA_CORRELATION}")
    if output_evidence.maximum > MAX_OUTPUT_VALUE:
        fail("output_maximum", output_evidence.maximum, f"must be <= {MAX_OUTPUT_VALUE}")

    metrics = {
        "source_luma_p001": source_p001,
        "source_luma_median": source_median,
        "output_luma_p001": output_p001,
        "output_luma_median": output_median,
        "output_luma_p90": output_p90,
        "output_luma_p99": output_p99,
        "output_minimum": output_evidence.minimum,
        "output_maximum": output_evidence.maximum,
        "low_luma_clip_fraction": low_clip,
        "high_luma_clip_fraction": high_clip,
        "channel_low_clip_fraction": channel_low_clip,
        "channel_high_clip_fraction": channel_high_clip,
        "luma_correlation": corr,
        "preferred_low_luma_clip_fraction": PREFERRED_LUMA_LOW_CLIP_FRACTION,
        "preferred_channel_low_clip_fraction": PREFERRED_CHANNEL_LOW_CLIP_FRACTION,
    }
    satisfactory = not failed
    return {
        "status": "satisfactory" if satisfactory else "needs_review",
        "satisfactory": satisfactory,
        "failed_checks": failed,
        "metrics": metrics,
        "thresholds": {
            "maximum_low_luma_clip_fraction": MAX_LUMA_LOW_CLIP_FRACTION,
            "preferred_low_luma_clip_fraction": PREFERRED_LUMA_LOW_CLIP_FRACTION,
            "maximum_channel_low_clip_fraction": MAX_CHANNEL_LOW_CLIP_FRACTION,
            "preferred_channel_low_clip_fraction": PREFERRED_CHANNEL_LOW_CLIP_FRACTION,
            "maximum_high_luma_clip_fraction": MAX_LUMA_HIGH_CLIP_FRACTION,
            "minimum_luma_correlation": MIN_LUMA_CORRELATION,
            "maximum_output_value": MAX_OUTPUT_VALUE,
        },
        "interpretation": (
            "The linear black-point shift is accepted only when the low "
            "luminance tail is moved toward black without exceeding either "
            "the luminance or per-channel floor-clipping ceiling, while "
            "preserving monotonic luminance structure."
        ),
    }


def histogram_classification(metrics: dict[str, Any]) -> str:
    if (
        metrics["low_luma_clip_fraction"] > MAX_LUMA_LOW_CLIP_FRACTION
        or metrics["channel_low_clip_fraction"] > MAX_CHANNEL_LOW_CLIP_FRACTION
        or metrics["output_luma_p001"] < BALANCED_LOW_TAIL_MIN
        or metrics["output_luma_median"] < BALANCED_MEDIAN_MIN
    ):
        return "too_strong"
    if (
        metrics["output_luma_p001"] > BALANCED_LOW_TAIL_MAX
        or metrics["output_luma_median"] > BALANCED_MEDIAN_MAX
    ):
        return "too_gentle"
    return "balanced"


def candidate_selection_score(quality: dict[str, Any]) -> float:
    m = quality["metrics"]
    return float(
        abs(m["output_luma_p001"] - TARGET_RECOMMENDED_LOW_TAIL)
        / TARGET_RECOMMENDED_LOW_TAIL
        + 0.5
        * abs(m["output_luma_median"] - TARGET_RECOMMENDED_MEDIAN)
        / TARGET_RECOMMENDED_MEDIAN
        + 1000.0 * m["low_luma_clip_fraction"]
        + 10.0 * m["channel_low_clip_fraction"]
    )


def run_candidate(
    *,
    run_root: Path,
    source_path: Path,
    params: dict[str, Any],
    timeout_seconds: int,
) -> dict[str, Any]:
    name = params["candidate"]
    candidate = run_root / name
    work = candidate / "work"
    previews = candidate / "previews"
    logs = candidate / "logs"
    work.mkdir(parents=True, exist_ok=False)
    previews.mkdir(parents=True, exist_ok=False)
    logs.mkdir(parents=True, exist_ok=False)

    working_source = work / "SHO-starless-ghs-pass2.fit"
    shutil.copy2(source_path, working_source)

    script = candidate / "black-point.ssf"
    script.write_text(black_point_script_text(params["BP"]), encoding="utf-8")
    run = run_siril_script(
        directory=work,
        script=script,
        stdout_log=logs / "stretch-stdout.log",
        stderr_log=logs / "stretch-stderr.log",
        timeout_seconds=timeout_seconds,
    )
    output = work / "SHO-starless-black-point.fit"
    failures: list[str] = []
    if run["exit_status"] != 0:
        failures.append(f"Siril exit {run['exit_status']}")
    if run["timed_out"]:
        failures.append("Siril timed out")
    if run["fatal_log_markers"]:
        failures.append(f"fatal log markers {run['fatal_log_markers']}")
    if not output.is_file():
        failures.append("expected black-point FITS missing")
    if failures:
        raise BlackPointError(
            f"Candidate {name} execution failed ({failures}); evidence preserved at {candidate}"
        )

    source_evidence = inspect_fits(working_source)
    output_evidence = inspect_fits(output)
    quality = production_quality_assessment(working_source, output)
    classification = histogram_classification(quality["metrics"])

    preview_script = candidate / "previews.ssf"
    preview_script.write_text(preview_script_text(), encoding="utf-8")
    preview_run = run_siril_script(
        directory=work,
        script=preview_script,
        stdout_log=logs / "preview-stdout.log",
        stderr_log=logs / "preview-stderr.log",
        timeout_seconds=min(timeout_seconds, 600),
    )
    before = previews / "SHO-starless-ghs-pass2-before-black-point.png"
    after = previews / "SHO-starless-black-point-linear.png"
    preview_failures = []
    if preview_run["exit_status"] != 0:
        preview_failures.append(f"preview exit {preview_run['exit_status']}")
    if preview_run["fatal_log_markers"]:
        preview_failures.append(f"preview fatal markers {preview_run['fatal_log_markers']}")
    for path in (before, after):
        if not path.is_file():
            preview_failures.append(f"missing preview {path}")
    before_sha = sha256_file(before) if before.is_file() else None
    after_sha = sha256_file(after) if after.is_file() else None
    if before_sha is not None and after_sha is not None and before_sha == after_sha:
        preview_failures.append("before and after previews are byte-identical")
    if preview_failures:
        raise BlackPointError(
            f"Candidate {name} preview failed ({preview_failures}); evidence preserved at {candidate}"
        )

    return {
        "candidate": name,
        "candidate_directory": str(candidate),
        "created_at": utc_now(),
        "helper_version": VERSION,
        "parameters": {
            "BP": params["BP"],
            "clip_mode": "RGB Blend",
            "colour_model": "even weighted luminance",
            "channels": "RGB",
        },
        "adaptive_target": {
            "source_luma_quantile": SOURCE_LOW_TAIL_QUANTILE,
            "source_luma_value": params["source_luma_p001"],
            "target_output_luma_value": params["target_output_luma_p001"],
        },
        "adaptation_reason": params["adaptation_reason"],
        "histogram_classification": classification,
        "selection_score": candidate_selection_score(quality),
        "source": asdict(source_evidence),
        "output": asdict(output_evidence),
        "quality_assessment": quality,
        "script": str(script),
        "script_sha256": sha256_file(script),
        "siril_run": run,
        "preview_script": str(preview_script),
        "preview_run": preview_run,
        "previews": {
            "before_linear": str(before),
            "after_linear": str(after),
        },
        "preview_provenance": {
            "before_source_fits": str(working_source),
            "before_source_fits_sha256": source_evidence.sha256,
            "after_source_fits": str(output),
            "after_source_fits_sha256": output_evidence.sha256,
            "before_png_sha256": before_sha,
            "after_png_sha256": after_sha,
            "before_after_pngs_distinct": before_sha != after_sha,
        },
        "status": "satisfactory" if quality["satisfactory"] else "needs_review",
    }


def candidate_publication_eligible(candidate: dict[str, Any]) -> bool:
    return (
        candidate.get("quality_assessment", {}).get("satisfactory") is True
        and candidate.get("histogram_classification") == "balanced"
    )



def candidate_channel_low_clip_fraction(candidate: dict[str, Any]) -> float:
    return float(
        candidate.get("quality_assessment", {})
        .get("metrics", {})
        .get("channel_low_clip_fraction", math.inf)
    )


def selection_policy_classification(candidate: dict[str, Any]) -> str:
    if not candidate_publication_eligible(candidate):
        return "ineligible"
    channel_clip = candidate_channel_low_clip_fraction(candidate)
    if channel_clip <= PREFERRED_CHANNEL_LOW_CLIP_FRACTION:
        return "preferred"
    if channel_clip <= MAX_CHANNEL_LOW_CLIP_FRACTION:
        return "aggressive"
    return "too_strong"


def selection_policy_recommended_candidate(
    candidates: list[dict[str, Any]],
) -> dict[str, Any] | None:
    eligible = [
        item
        for item in candidates
        if candidate_publication_eligible(item)
    ]
    if not eligible:
        return None

    preferred = [
        item
        for item in eligible
        if selection_policy_classification(item) == "preferred"
    ]
    pool = preferred if preferred else eligible
    return min(
        pool,
        key=lambda item: (
            candidate_channel_low_clip_fraction(item),
            float(item.get("selection_score", math.inf)),
        ),
    )


def selection_policy_summary(
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    eligible = [
        item
        for item in candidates
        if candidate_publication_eligible(item)
    ]
    numerical = recommended_candidate(candidates)
    policy = selection_policy_recommended_candidate(candidates)
    details: dict[str, Any] = {}

    for item in eligible:
        name = item["candidate"]
        clip = candidate_channel_low_clip_fraction(item)
        classification = selection_policy_classification(item)
        details[name] = {
            "classification": classification,
            "channel_low_clip_fraction": clip,
            "preferred_channel_low_clip_fraction": (
                PREFERRED_CHANNEL_LOW_CLIP_FRACTION
            ),
            "maximum_channel_low_clip_fraction": (
                MAX_CHANNEL_LOW_CLIP_FRACTION
            ),
            "within_preferred_channel_floor_range": (
                clip <= PREFERRED_CHANNEL_LOW_CLIP_FRACTION
            ),
            "above_preferred_by": max(
                0.0,
                clip - PREFERRED_CHANNEL_LOW_CLIP_FRACTION,
            ),
        }

    return {
        "version": SELECTION_POLICY_VERSION,
        "numerical_recommended_candidate": (
            numerical.get("candidate") if numerical else None
        ),
        "recommended_candidate": (
            policy.get("candidate") if policy else None
        ),
        "preferred_channel_low_clip_fraction": (
            PREFERRED_CHANNEL_LOW_CLIP_FRACTION
        ),
        "maximum_channel_low_clip_fraction": (
            MAX_CHANNEL_LOW_CLIP_FRACTION
        ),
        "candidate_policy": details,
        "rules": [
            (
                "Do not prefer a candidate merely because its background is "
                "darker or its contrast is higher."
            ),
            (
                "Preservation of faint outer emission and low-contrast dust "
                "outranks achieving a deeper black background."
            ),
            (
                "Candidates at or below the preferred per-channel floor "
                "clipping level receive a meaningful selection preference."
            ),
            (
                "A candidate above the preferred clipping level but below the "
                "hard ceiling remains eligible but is classified aggressive."
            ),
            (
                "When both candidates are visually acceptable, prefer the "
                "candidate with materially less channel-floor clipping unless "
                "the aggressive candidate visibly improves structure without "
                "losing faint emission."
            ),
        ],
    }


def aggressive_policy_override_required(
    selected_candidate: str,
    candidates: list[dict[str, Any]],
) -> bool:
    eligible = [
        item
        for item in candidates
        if candidate_publication_eligible(item)
    ]
    by_name = {item["candidate"]: item for item in eligible}
    selected = by_name.get(selected_candidate)
    if selected is None:
        return False
    preferred_exists = any(
        selection_policy_classification(item) == "preferred"
        for item in eligible
    )
    return (
        preferred_exists
        and selection_policy_classification(selected) == "aggressive"
    )


def validate_aggressive_override_reason(reason: str | None) -> str:
    if reason is None:
        raise BlackPointError(
            "The selected candidate is aggressive while a preferred-range "
            "candidate is available. Supply --policy-override-reason "
            "describing the specific visible structural improvement and how "
            "faint emission remains preserved."
        )
    clean = reason.strip()
    if len(clean) < AGGRESSIVE_OVERRIDE_MIN_CHARS:
        raise BlackPointError(
            "Aggressive selection override reason is too short. Describe the "
            "specific visible structural improvement and preservation of "
            "faint emission in at least "
            f"{AGGRESSIVE_OVERRIDE_MIN_CHARS} characters."
        )
    lower = clean.lower()
    if not any(term in lower for term in ("faint", "emission", "low-contrast")):
        raise BlackPointError(
            "Aggressive selection override must explicitly address faint "
            "emission or low-contrast structure preservation."
        )
    if not any(
        term in lower
        for term in (
            "structure",
            "detail",
            "pillar",
            "lane",
            "separation",
            "definition",
        )
    ):
        raise BlackPointError(
            "Aggressive selection override must explicitly identify the "
            "visible structural/detail improvement that justifies the tradeoff."
        )
    return clean


def publication_gate(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    eligible = [
        item["candidate"] for item in candidates if candidate_publication_eligible(item)
    ]
    if not eligible:
        return {
            "status": "blocked",
            "publication_permitted": False,
            "publication_eligible_candidates": [],
            "reason": (
                "No candidate is both technically satisfactory and balanced. "
                "Do not generate an unbounded replacement set."
            ),
        }
    return {
        "status": "awaiting_visual_selection",
        "publication_permitted": True,
        "publication_eligible_candidates": eligible,
        "reason": (
            "At least one technically satisfactory balanced black-point candidate "
            "may proceed to CodeWarrior visual selection."
        ),
    }


def recommended_candidate(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    eligible = [item for item in candidates if candidate_publication_eligible(item)]
    if not eligible:
        return None
    return min(eligible, key=lambda item: float(item["selection_score"]))


def load_run_record(run_root: Path) -> tuple[Path, dict[str, Any]]:
    path = run_root / "run-manifest.json"
    if not path.is_file():
        raise BlackPointError(f"Run manifest missing: {path}")
    try:
        return path, json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise BlackPointError(f"Could not read run manifest: {exc}") from exc


def validate_run_record(
    *,
    record: dict[str, Any],
    project_name: str,
    source_sha256: str,
) -> None:
    if record.get("helper_version") not in COMPATIBLE_RUN_HELPER_VERSIONS:
        raise BlackPointError("Run helper version is not compatible.")
    if record.get("project_name") != project_name:
        raise BlackPointError("Run project does not match requested project.")
    if record.get("source", {}).get("sha256") != source_sha256:
        raise BlackPointError("Run source SHA does not match current GHS pass-2 source.")


def run_project(
    *,
    workspace: Path,
    project_name: str,
    timeout_seconds: int,
    max_candidates: int,
) -> dict[str, Any]:
    if max_candidates < 1 or max_candidates > MAX_CANDIDATES:
        raise BlackPointError(f"max-candidates must be between 1 and {MAX_CANDIDATES}")
    paths = project_paths(workspace, project_name)
    source_manifest, source_evidence = validate_upstream(paths)

    state = workflow_state(workspace, project_name)
    if state.get("action") in (
        "review_select_publish",
        "publish_recorded_selection",
        "prepare_policy_reselection",
    ):
        raise BlackPointError(
            "A compatible incomplete or policy-reselection black-point run "
            "already exists. Resume it through advance; do not create "
            "duplicate candidates."
        )
    if state.get("action") == "stop":
        raise BlackPointError(
            "The latest compatible black-point run is blocked; preserve its evidence rather "
            "than automatically starting another bounded search."
        )

    fresh_auth_path: Path | None = None
    fresh_auth: dict[str, Any] | None = None
    if paths["stable"].exists():
        canonical = state.get("canonical_status", {})
        canonical_status = canonical.get("status")

        if canonical_status == "needs_reprocessing":
            # Preserve the policy-obsolete canonical as safety evidence, but
            # do not require a fresh-rerun confirmation: v1.0.1 does not
            # consider it a valid completed result for downstream handoff.
            pass
        elif canonical_status == "ready":
            canonical_sha = current_canonical_sha(paths, canonical)
            authorized = matching_stage_intent(
                paths=paths,
                project_name=project_name,
                source_sha256=source_evidence.sha256,
                canonical_sha256=canonical_sha,
                statuses={"fresh_run_authorized"},
            )
            if authorized is None:
                raise BlackPointError(
                    "Black point already has a completed compatible canonical "
                    "result. Fresh-run confirmation is required. Run begin, "
                    "ask the user, then run confirm-fresh after an affirmative "
                    "response."
                )
            fresh_auth_path, fresh_auth = authorized
        else:
            raise BlackPointError(
                "Existing black-point canonical result is invalid rather than "
                "merely policy-obsolete. Preserve it and resolve the status "
                "errors before starting replacement processing."
            )

    siril = siril_version()
    params = candidate_parameters(paths["upstream_output"])
    run_root = paths["runs"] / f"black-point-{unique_id()}"
    run_root.mkdir(parents=True, exist_ok=False)
    candidates: list[dict[str, Any]] = []
    for item in params[:max_candidates]:
        candidates.append(
            run_candidate(
                run_root=run_root,
                source_path=paths["upstream_output"],
                params=item,
                timeout_seconds=timeout_seconds,
            )
        )

    gate = publication_gate(candidates)
    recommended = recommended_candidate(candidates)
    policy_summary = selection_policy_summary(candidates)
    record = {
        "schema_version": 1,
        "helper_version": VERSION,
        "created_at": utc_now(),
        "project": str(paths["project"]),
        "project_name": project_name,
        "run_root": str(run_root),
        "status": gate["status"],
        "source": asdict(source_evidence),
        "upstream_manifest": str(paths["upstream_manifest"]),
        "upstream_manifest_sha256": sha256_file(paths["upstream_manifest"]),
        "upstream_helper_version": source_manifest.get("helper_version"),
        "siril": siril,
        "maximum_total_candidates": MAX_CANDIDATES,
        "completed_candidate_count": len(candidates),
        "candidates": candidates,
        "publication_gate": gate,
        "publication_permitted": gate["publication_permitted"],
        "publication_eligible_candidates": gate["publication_eligible_candidates"],
        "recommended_candidate": (
            recommended.get("candidate") if recommended else None
        ),
        "numerical_recommended_candidate": (
            policy_summary.get("numerical_recommended_candidate")
        ),
        "selection_policy_recommended_candidate": (
            policy_summary.get("recommended_candidate")
        ),
        "selection_policy": policy_summary,
        "visual_selection_required": gate["publication_permitted"],
        "visual_review_completed": False,
        "visual_selection": None,
        "selected_candidate": None,
        "canonical_output_changed": False,
        "green_reduction_processing_permitted": False,
        "fresh_run_authorized": fresh_auth is not None,
        "fresh_run_request_id": fresh_auth.get("request_id") if fresh_auth else None,
        "fresh_run_intent": str(fresh_auth_path) if fresh_auth_path else None,
        "adaptive_policy": {
            "source_low_tail_quantile": SOURCE_LOW_TAIL_QUANTILE,
            "candidate_target_output_luma_p001": TARGET_OUTPUT_LOW_TAIL,
            "balanced_output_luma_p001": [BALANCED_LOW_TAIL_MIN, BALANCED_LOW_TAIL_MAX],
            "balanced_output_luma_median": [BALANCED_MEDIAN_MIN, BALANCED_MEDIAN_MAX],
            "maximum_low_luma_clip_fraction": MAX_LUMA_LOW_CLIP_FRACTION,
            "preferred_low_luma_clip_fraction": PREFERRED_LUMA_LOW_CLIP_FRACTION,
            "maximum_channel_low_clip_fraction": MAX_CHANNEL_LOW_CLIP_FRACTION,
            "preferred_channel_low_clip_fraction": PREFERRED_CHANNEL_LOW_CLIP_FRACTION,
            "manual_m16_historical_bp": 0.24105,
            "manual_value_is_universal_default": False,
        },
    }
    json_dump_atomic(run_root / "run-manifest.json", record)

    if fresh_auth_path is not None and fresh_auth is not None:
        consume_fresh_run_authorization(
            intent_path=fresh_auth_path,
            intent=fresh_auth,
            run_root=run_root,
        )
    return record


def parse_candidate_mapping(
    values: list[str],
    *,
    label: str,
) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise BlackPointError(
                f"{label} entries must use candidate=value syntax: {value!r}"
            )
        candidate, payload = value.split("=", 1)
        candidate = candidate.strip()
        payload = payload.strip()
        if not candidate or not payload:
            raise BlackPointError(
                f"{label} entries require non-empty candidate and value."
            )
        if candidate in result:
            raise BlackPointError(
                f"Duplicate {label} entry for {candidate}."
            )
        result[candidate] = payload
    return result


def parse_candidate_notes_flexible(
    values: list[str],
    eligible_candidates: list[str],
) -> dict[str, str]:
    """Parse forgiving candidate=value visual notes."""
    if not values:
        raise BlackPointError("At least one candidate visual note is required.")

    eligible = list(eligible_candidates)
    notes: dict[str, str] = {}
    for raw in values:
        raw = raw.strip()
        if not raw:
            continue
        matches: list[tuple[int, str, int]] = []
        for name in eligible:
            for match in re.finditer(
                rf"(?<![A-Za-z0-9_-]){re.escape(name)}\s*=",
                raw,
            ):
                matches.append((match.start(), name, match.end()))
        matches.sort(key=lambda item: item[0])
        if not matches:
            raise BlackPointError(
                "Candidate notes must use candidate=value syntax. "
                f"Could not parse: {raw!r}"
            )
        for index, (_, name, value_start) in enumerate(matches):
            if name in notes:
                raise BlackPointError(f"Duplicate candidate note for {name}.")
            value_end = (
                matches[index + 1][0]
                if index + 1 < len(matches)
                else len(raw)
            )
            value = raw[value_start:value_end].strip(" ;,\t\n")
            if len(value) < 20:
                raise BlackPointError(
                    f"{name} visual note is too short. Describe what was "
                    "actually seen in the rendered image."
                )
            notes[name] = value

    expected = sorted(eligible)
    actual = sorted(notes)
    if actual != expected:
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        raise BlackPointError(
            "Candidate visual notes must cover every eligible candidate. "
            f"Missing={missing}; extra={extra}; expected={expected}."
        )
    return notes


def review_plan(
    *,
    workspace: Path,
    project_name: str,
    run_root: Path,
) -> dict[str, Any]:
    paths = project_paths(workspace, project_name)
    _, source_sha256 = validate_upstream_fast(paths)
    _, record = load_run_record(run_root)
    validate_run_record(
        record=record,
        project_name=project_name,
        source_sha256=source_sha256,
    )

    gate = publication_gate(record.get("candidates", []))
    if not gate["publication_permitted"]:
        raise BlackPointError(
            f"Publication gate is blocked: {gate['reason']}"
        )

    eligible_names = list(gate["publication_eligible_candidates"])
    candidates_by_name = {
        item["candidate"]: item
        for item in record.get("candidates", [])
    }
    eligible = [
        candidates_by_name[name]
        for name in eligible_names
    ]

    before_shas = {
        item["preview_provenance"]["before_png_sha256"]
        for item in eligible
    }
    if len(before_shas) != 1:
        raise BlackPointError(
            "Eligible candidates do not share identical before-preview "
            "content/provenance."
        )

    before_sha = next(iter(before_shas))
    before_preview_paths: dict[str, str] = {}

    for item in eligible:
        candidate_name = item["candidate"]
        candidate_before_path = Path(
            item["previews"]["before_linear"]
        )
        if not candidate_before_path.is_file():
            raise BlackPointError(
                f"{candidate_name} before-preview is missing: "
                f"{candidate_before_path}"
            )

        expected_before_sha = item["preview_provenance"][
            "before_png_sha256"
        ]
        actual_before_sha = sha256_file(candidate_before_path)
        if actual_before_sha != expected_before_sha:
            raise BlackPointError(
                f"{candidate_name} before-preview SHA no longer matches "
                "candidate provenance."
            )
        if actual_before_sha != before_sha:
            raise BlackPointError(
                f"{candidate_name} before-preview content differs from "
                "the common before-preview provenance."
            )

        before_preview_paths[candidate_name] = str(
            candidate_before_path
        )

    representative_name = eligible_names[0]
    before_path = Path(
        before_preview_paths[representative_name]
    )

    policy = selection_policy_summary(
        record.get("candidates", [])
    )

    review_candidates: list[dict[str, Any]] = []
    for item in eligible:
        after_path = Path(item["previews"]["after_linear"])
        expected_after_sha = item["preview_provenance"][
            "after_png_sha256"
        ]
        if not after_path.is_file():
            raise BlackPointError(
                f"Candidate after-preview is missing: {after_path}"
            )
        actual_after_sha = sha256_file(after_path)
        if actual_after_sha != expected_after_sha:
            raise BlackPointError(
                f"{item['candidate']} after-preview SHA changed."
            )

        metrics = item["quality_assessment"]["metrics"]
        policy_item = policy["candidate_policy"][
            item["candidate"]
        ]

        review_candidates.append(
            {
                "candidate": item["candidate"],
                "BP": item["parameters"]["BP"],
                "histogram_classification": item[
                    "histogram_classification"
                ],
                "selection_policy_classification": policy_item[
                    "classification"
                ],
                "within_preferred_channel_floor_range": policy_item[
                    "within_preferred_channel_floor_range"
                ],
                "after_preview": str(after_path),
                "after_preview_sha256": expected_after_sha,
                "output_luma_p001": metrics["output_luma_p001"],
                "output_luma_median": metrics["output_luma_median"],
                "channel_low_clip_fraction": metrics[
                    "channel_low_clip_fraction"
                ],
                "preferred_channel_low_clip_fraction": (
                    PREFERRED_CHANNEL_LOW_CLIP_FRACTION
                ),
                "maximum_channel_low_clip_fraction": (
                    MAX_CHANNEL_LOW_CLIP_FRACTION
                ),
                "low_luma_clip_fraction": metrics[
                    "low_luma_clip_fraction"
                ],
                "selection_score": item["selection_score"],
            }
        )

    return {
        "status": "visual_review_required",
        "helper_version": VERSION,
        "project": str(paths["project"]),
        "project_name": project_name,
        "run_root": str(run_root),
        "review_method_required": "openclaw-read",
        "copying_files_counts_as_review": False,
        "hash_only_review_counts_as_review": False,
        "before_preview": str(before_path),
        "before_preview_sha256": before_sha,
        "before_preview_paths": before_preview_paths,
        "publication_eligible_candidates": eligible_names,
        "numerical_recommended_candidate": policy[
            "numerical_recommended_candidate"
        ],
        "recommended_candidate": policy[
            "recommended_candidate"
        ],
        "selection_policy": policy,
        "candidates": review_candidates,
        "instructions": (
            "CodeWarrior must use OpenClaw Read on the before preview and "
            "every eligible after preview. Do not prefer a candidate merely "
            "because the background is darker or contrast is higher. "
            "Preserving faint outer emission and low-contrast dust outranks "
            "achieving a deeper black background. A candidate above the "
            "preferred channel-floor clipping range is aggressive even when "
            "it remains technically eligible. When two candidates are "
            "visually acceptable, prefer materially less channel-floor "
            "clipping unless the aggressive candidate visibly improves "
            "structure without losing faint emission."
        ),
        "select_evidence_contract": {
            "review_method": "openclaw-read",
            "before_reviewed_sha256": before_sha,
            "reviewed_preview_format": (
                "candidate=after_preview_sha256"
            ),
            "candidate_note_format": (
                "candidate=qualitative_visual_note"
            ),
        },
        "green_reduction_processing_permitted": False,
    }


def record_visual_selection(
    *,
    workspace: Path,
    project_name: str,
    run_root: Path,
    candidate_name: str,
    compared_candidates: list[str],
    visual_notes: str,
    review_method: str,
    before_reviewed_sha256: str,
    reviewed_previews: list[str],
    candidate_notes: list[str],
    policy_override_reason: str | None = None,
) -> dict[str, Any]:
    paths = project_paths(workspace, project_name)
    _, source_sha256 = validate_upstream_fast(paths)
    manifest_path, record = load_run_record(run_root)
    validate_run_record(
        record=record,
        project_name=project_name,
        source_sha256=source_sha256,
    )

    if record.get("canonical_output_changed") is True:
        raise BlackPointError("Run has already been published.")

    gate = publication_gate(record.get("candidates", []))
    if not gate["publication_permitted"]:
        raise BlackPointError(
            f"Publication gate is blocked: {gate['reason']}"
        )

    eligible = sorted(
        gate["publication_eligible_candidates"]
    )
    compared = sorted(set(compared_candidates))
    if compared != eligible:
        raise BlackPointError(
            "CodeWarrior must visually compare every eligible candidate. "
            f"Expected {eligible}, got {compared}."
        )

    if review_method != "openclaw-read":
        raise BlackPointError(
            "Visual selection requires openclaw-read. Copying previews or "
            "reviewing metrics alone is not accepted."
        )

    reviewed_map = parse_candidate_mapping(
        reviewed_previews,
        label="reviewed-preview",
    )
    notes_map = parse_candidate_mapping(
        candidate_notes,
        label="candidate-note",
    )

    if sorted(reviewed_map) != eligible:
        raise BlackPointError(
            "Reviewed-preview evidence must cover every eligible candidate "
            f"exactly. Expected {eligible}, got {sorted(reviewed_map)}."
        )
    if sorted(notes_map) != eligible:
        raise BlackPointError(
            "Candidate visual notes must cover every eligible candidate "
            f"exactly. Expected {eligible}, got {sorted(notes_map)}."
        )

    candidates_by_name = {
        item["candidate"]: item
        for item in record["candidates"]
    }

    before_expected = {
        candidates_by_name[name]["preview_provenance"][
            "before_png_sha256"
        ]
        for name in eligible
    }
    if len(before_expected) != 1:
        raise BlackPointError(
            "Eligible candidates do not share one before-preview SHA."
        )

    expected_before_sha = next(iter(before_expected))
    if before_reviewed_sha256 != expected_before_sha:
        raise BlackPointError(
            "Declared before-preview review SHA does not match provenance."
        )

    review_evidence_candidates: dict[str, Any] = {}

    for name in eligible:
        item = candidates_by_name[name]
        after_path = Path(item["previews"]["after_linear"])
        expected_after_sha = item["preview_provenance"][
            "after_png_sha256"
        ]

        if reviewed_map[name] != expected_after_sha:
            raise BlackPointError(
                f"{name} reviewed-preview SHA does not match provenance."
            )
        if not after_path.is_file():
            raise BlackPointError(
                f"{name} after-preview no longer exists."
            )
        if sha256_file(after_path) != expected_after_sha:
            raise BlackPointError(
                f"{name} after-preview changed after review."
            )

        note = notes_map[name].strip()
        if len(note) < 20:
            raise BlackPointError(
                f"{name} candidate-note is too short for an auditable "
                "qualitative visual comparison."
            )

        policy_class = selection_policy_classification(item)
        review_evidence_candidates[name] = {
            "after_preview": str(after_path),
            "after_preview_sha256": expected_after_sha,
            "visual_note": note,
            "selection_policy_classification": policy_class,
            "channel_low_clip_fraction": (
                candidate_channel_low_clip_fraction(item)
            ),
        }

    before_path = Path(
        candidates_by_name[eligible[0]]["previews"]["before_linear"]
    )
    if not before_path.is_file():
        raise BlackPointError("Before-preview no longer exists.")
    if sha256_file(before_path) != expected_before_sha:
        raise BlackPointError(
            "Before-preview changed after visual review."
        )

    matches = [
        item
        for item in record["candidates"]
        if item.get("candidate") == candidate_name
    ]
    if (
        len(matches) != 1
        or not candidate_publication_eligible(matches[0])
    ):
        raise BlackPointError(
            f"Candidate {candidate_name!r} is not publication-eligible."
        )

    notes = visual_notes.strip()
    if not notes:
        raise BlackPointError("Visual selection notes are required.")

    policy = selection_policy_summary(
        record["candidates"]
    )
    numerical_name = policy.get(
        "numerical_recommended_candidate"
    )
    policy_name = policy.get("recommended_candidate")
    selected_policy_class = selection_policy_classification(
        matches[0]
    )

    override_required = aggressive_policy_override_required(
        candidate_name,
        record["candidates"],
    )
    override_reason_clean: str | None = None
    if override_required:
        override_reason_clean = validate_aggressive_override_reason(
            policy_override_reason
        )
    elif policy_override_reason is not None:
        override_reason_clean = policy_override_reason.strip() or None

    selection = {
        "required": True,
        "completed": True,
        "reviewer": "CodeWarrior",
        "recorded_at": utc_now(),
        "selected_candidate": candidate_name,
        "selected_candidate_was_recommended": (
            policy_name == candidate_name
        ),
        "selected_candidate_was_numerical_recommendation": (
            numerical_name == candidate_name
        ),
        "satisfactory_candidates_compared": compared,
        "notes": notes,
        "selected_output_sha256": matches[0]["output"]["sha256"],
        "selected_before_preview_sha256": matches[0][
            "preview_provenance"
        ]["before_png_sha256"],
        "selected_after_preview_sha256": matches[0][
            "preview_provenance"
        ]["after_png_sha256"],
        "selection_policy": {
            **policy,
            "selected_candidate_classification": (
                selected_policy_class
            ),
            "aggressive_override_required": override_required,
            "aggressive_override_used": (
                override_reason_clean is not None
            ),
            "aggressive_override_reason": override_reason_clean,
        },
        "visual_review_evidence": {
            "method": review_method,
            "copying_files_counts_as_review": False,
            "before_preview": str(before_path),
            "before_preview_sha256": expected_before_sha,
            "candidates": review_evidence_candidates,
        },
    }

    record["status"] = "ready_to_publish"
    record["visual_selection"] = selection
    record["selected_candidate"] = candidate_name
    record["visual_review_completed"] = True
    record["numerical_recommended_candidate"] = numerical_name
    record["selection_policy_recommended_candidate"] = policy_name
    record["selection_policy"] = policy
    json_dump_atomic(manifest_path, record)

    return {
        "status": "ready_to_publish",
        "helper_version": VERSION,
        "project": str(paths["project"]),
        "run_root": str(run_root),
        "selected_candidate": candidate_name,
        "numerical_recommended_candidate": numerical_name,
        "recommended_candidate": policy_name,
        "selected_candidate_was_recommended": (
            selection["selected_candidate_was_recommended"]
        ),
        "selected_candidate_was_numerical_recommendation": (
            selection[
                "selected_candidate_was_numerical_recommendation"
            ]
        ),
        "selected_candidate_policy_classification": (
            selected_policy_class
        ),
        "aggressive_override_used": (
            selection["selection_policy"][
                "aggressive_override_used"
            ]
        ),
        "satisfactory_candidates_compared": compared,
        "visual_review_completed": True,
        "visual_review_evidence_verified": True,
        "review_method": review_method,
        "publication_permitted": True,
        "green_reduction_processing_permitted": False,
    }


def preserve_failed_publish_staging(run_root: Path) -> Path | None:
    staging = run_root / "publish-staging"
    if not staging.exists():
        return None
    destination = run_root / f"failed-publish-staging-{unique_id()}"
    staging.rename(destination)
    return destination


def publish_project(
    *,
    workspace: Path,
    project_name: str,
    run_root: Path,
) -> dict[str, Any]:
    paths = project_paths(workspace, project_name)
    upstream_manifest, source_evidence = validate_upstream(paths)
    run_manifest_path, record = load_run_record(run_root)
    validate_run_record(
        record=record,
        project_name=project_name,
        source_sha256=source_evidence.sha256,
    )

    selection = record.get("visual_selection")
    if (
        not isinstance(selection, dict)
        or selection.get("completed") is not True
    ):
        raise BlackPointError(
            "Durable CodeWarrior visual selection is required before "
            "publication."
        )

    selection_policy = selection.get("selection_policy")
    if (
        not isinstance(selection_policy, dict)
        or selection_policy.get("version") != SELECTION_POLICY_VERSION
    ):
        raise BlackPointError(
            "Publication requires a visual selection recorded under the "
            "current v1.0.4 selection policy."
        )

    gate = publication_gate(
        record.get("candidates", [])
    )
    selected_name = selection.get("selected_candidate")
    if selected_name not in gate[
        "publication_eligible_candidates"
    ]:
        raise BlackPointError(
            "Recorded selected candidate is no longer publication-eligible."
        )

    selected = [
        item
        for item in record["candidates"]
        if item.get("candidate") == selected_name
    ][0]

    selected_output = Path(selected["output"]["path"])
    before_preview = Path(selected["previews"]["before_linear"])
    after_preview = Path(selected["previews"]["after_linear"])

    if (
        sha256_file(selected_output)
        != selection.get("selected_output_sha256")
    ):
        raise BlackPointError(
            "Selected candidate output checksum changed after visual review."
        )
    if (
        sha256_file(before_preview)
        != selection.get("selected_before_preview_sha256")
    ):
        raise BlackPointError(
            "Selected before-preview checksum changed after visual review."
        )
    if (
        sha256_file(after_preview)
        != selection.get("selected_after_preview_sha256")
    ):
        raise BlackPointError(
            "Selected after-preview checksum changed after visual review."
        )
    if (
        selected["preview_provenance"][
            "before_source_fits_sha256"
        ]
        != source_evidence.sha256
    ):
        raise BlackPointError(
            "Selected preview source does not match current GHS pass-2 "
            "canonical source."
        )

    preserved_failed = preserve_failed_publish_staging(run_root)
    staging = run_root / "publish-staging"
    staging.mkdir(parents=True, exist_ok=False)

    staged_output = staging / "SHO-starless-black-point.fit"
    staged_before = (
        staging / "SHO-starless-ghs-pass2-before-black-point.png"
    )
    staged_after = (
        staging / "SHO-starless-black-point-linear.png"
    )
    staged_manifest = staging / "black-point-manifest.json"

    shutil.copy2(selected_output, staged_output)
    shutil.copy2(before_preview, staged_before)
    shutil.copy2(after_preview, staged_after)

    policy_name = selection_policy.get(
        "recommended_candidate"
    )
    numerical_name = selection_policy.get(
        "numerical_recommended_candidate"
    )

    stable_payload = {
        "schema_version": 1,
        "helper_version": VERSION,
        "created_at": utc_now(),
        "project": project_name,
        "project_path": str(paths["project"]),
        "stage_order": {
            "upstream": "siril-ghs-stretch-pass2",
            "current": "siril-black-point",
            "downstream": "siril-green-reduction",
        },
        "status": "ready",
        "visual_review_completed": True,
        "selected_candidate": selected_name,
        "recommended_candidate": policy_name,
        "numerical_recommended_candidate": numerical_name,
        "selected_candidate_was_recommended": selection.get(
            "selected_candidate_was_recommended"
        ),
        "selected_candidate_was_numerical_recommendation": (
            selection.get(
                "selected_candidate_was_numerical_recommendation"
            )
        ),
        "selection_policy": selection_policy,
        "visual_selection": selection,
        "method": {
            "algorithm": "Siril linear stretch (black-point shift)",
            "command": (
                f"linstretch -BP={selected['parameters']['BP']:.8f} "
                "-clipmode=rgbblend"
            ),
            "black_point_BP": selected["parameters"]["BP"],
            "colour_model": "even weighted luminance",
            "clip_mode": "RGB Blend",
            "channels": "RGB",
        },
        "quality_assessment": selected["quality_assessment"],
        "output": {
            **selected["output"],
            "path": str(paths["stable_output"]),
        },
        "source": {
            **asdict(source_evidence),
            "path": str(paths["upstream_output"]),
        },
        "previews": {
            "before_linear": str(
                paths["stable_before_preview"]
            ),
            "after_linear": str(
                paths["stable_after_preview"]
            ),
        },
        "upstream_summary": {
            "helper_version": upstream_manifest.get(
                "helper_version"
            ),
            "manifest": str(paths["upstream_manifest"]),
            "manifest_sha256": sha256_file(
                paths["upstream_manifest"]
            ),
            "status": upstream_manifest.get("status"),
            "visual_review_completed": upstream_manifest.get(
                "visual_review_completed"
            ),
            "black_point_processing_permitted": (
                upstream_manifest.get(
                    "black_point_processing_permitted"
                )
            ),
        },
        "next_stage": "siril-green-reduction",
        "green_reduction_processing_permitted": True,
        "run_root": str(run_root),
        "failed_publish_staging_preserved_at": (
            str(preserved_failed)
            if preserved_failed is not None
            else None
        ),
        "adaptive_policy": record.get("adaptive_policy"),
    }
    json_dump_atomic(staged_manifest, stable_payload)

    previous = None
    if paths["stable"].exists():
        previous = (
            run_root
            / f"previous-processing-black-point-{unique_id()}"
        )
        paths["stable"].rename(previous)

    try:
        staging.rename(paths["stable"])
    except Exception as exc:
        if (
            previous is not None
            and previous.exists()
            and not paths["stable"].exists()
        ):
            previous.rename(paths["stable"])
        raise BlackPointError(
            f"Could not publish black-point staging: {exc}"
        ) from exc

    canonical_sha = sha256_file(
        paths["stable_output"]
    )
    if canonical_sha != selected["output"]["sha256"]:
        raise BlackPointError(
            "Published canonical FITS SHA does not match selected candidate."
        )

    record["status"] = "ready"
    record["canonical_output_changed"] = True
    record["published_at"] = utc_now()
    record["stable_directory"] = str(paths["stable"])
    record["stable_manifest"] = str(paths["stable_manifest"])
    record["previous_processing_black_point_preserved_at"] = (
        str(previous) if previous is not None else None
    )
    record["failed_publish_staging_preserved_at"] = (
        str(preserved_failed)
        if preserved_failed is not None
        else None
    )
    record["green_reduction_processing_permitted"] = True
    record["selection_policy"] = selection_policy
    record["selection_policy_recommended_candidate"] = policy_name
    record["numerical_recommended_candidate"] = numerical_name
    json_dump_atomic(run_manifest_path, record)

    return {
        "status": "ready",
        "helper_version": VERSION,
        "project": str(paths["project"]),
        "run_root": str(run_root),
        "selected_candidate": selected_name,
        "recommended_candidate": policy_name,
        "numerical_recommended_candidate": numerical_name,
        "selected_candidate_policy_classification": (
            selection_policy.get(
                "selected_candidate_classification"
            )
        ),
        "aggressive_override_used": selection_policy.get(
            "aggressive_override_used", False
        ),
        "visual_review_completed": True,
        "canonical_output_changed": True,
        "canonical_output": str(paths["stable_output"]),
        "canonical_output_sha256": canonical_sha,
        "previous_processing_black_point_preserved_at": (
            str(previous) if previous is not None else None
        ),
        "failed_publish_staging_preserved_at": (
            str(preserved_failed)
            if preserved_failed is not None
            else None
        ),
        "next_stage": "siril-green-reduction",
        "green_reduction_processing_permitted": True,
    }


def validate_upstream_fast(
    paths: dict[str, Path],
) -> tuple[dict[str, Any], str]:
    """Validate durable upstream metadata without rescanning the FITS."""
    manifest_path = paths["upstream_manifest"]
    if not manifest_path.is_file():
        raise BlackPointError(
            f"Upstream GHS pass-2 manifest missing: {manifest_path}"
        )
    try:
        manifest = json.loads(
            manifest_path.read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise BlackPointError(
            f"Could not read upstream manifest: {exc}"
        ) from exc

    errors: list[str] = []
    if manifest.get("helper_version") not in UPSTREAM_HELPER_VERSIONS:
        errors.append("upstream helper version is incompatible")
    if manifest.get("status") != "ready":
        errors.append("upstream status must be ready")
    if manifest.get("visual_review_completed") is not True:
        errors.append("upstream visual review must be complete")
    if manifest.get("black_point_processing_permitted") is not True:
        errors.append("upstream black-point permission must be true")
    if manifest.get("next_stage") != "siril-black-point":
        errors.append("upstream next_stage must be siril-black-point")
    if manifest.get("quality_assessment", {}).get("satisfactory") is not True:
        errors.append("upstream quality must be satisfactory")

    output = manifest.get("output", {})
    source_sha = output.get("sha256")
    if (
        not isinstance(source_sha, str)
        or re.fullmatch(r"[0-9a-f]{64}", source_sha) is None
    ):
        errors.append("upstream output SHA is missing or invalid")

    output_path = paths["upstream_output"]
    if not output_path.is_file():
        errors.append("canonical upstream FITS is missing")
    else:
        recorded_size = output.get("size")
        if (
            isinstance(recorded_size, int)
            and output_path.stat().st_size != recorded_size
        ):
            errors.append("canonical upstream FITS size differs from manifest")

    recorded_path = output.get("path")
    if recorded_path and Path(recorded_path).resolve() != output_path.resolve():
        errors.append("upstream manifest output path is not canonical")

    if errors:
        raise BlackPointError(
            "Upstream GHS pass-2 fast contract failed: "
            + "; ".join(errors)
        )
    return manifest, source_sha


def status_project_fast(
    workspace: Path,
    project_name: str,
    *,
    upstream_sha256: str | None = None,
) -> dict[str, Any]:
    """Fast orchestration status using durable manifests and file metadata."""
    paths = project_paths(workspace, project_name)
    if upstream_sha256 is None:
        _, upstream_sha256 = validate_upstream_fast(paths)

    if not paths["stable_manifest"].is_file():
        return {
            "status": "missing",
            "helper_version": VERSION,
            "manifest_helper_version": None,
            "project": str(paths["project"]),
            "canonical_manifest_compatible": False,
            "output": None,
            "selected_candidate": None,
            "visual_review_completed": False,
            "next_stage": "siril-black-point",
            "green_reduction_processing_permitted": False,
            "policy_errors": [],
            "reprocessing_policy_errors": [],
            "reselection_policy_errors": [],
            "selection_policy_status": "missing",
            "run_root": None,
            "errors": ["No canonical black-point manifest exists."],
        }

    try:
        manifest = json.loads(
            paths["stable_manifest"].read_text(encoding="utf-8")
        )
    except Exception as exc:
        return {
            "status": "invalid",
            "helper_version": VERSION,
            "manifest_helper_version": None,
            "project": str(paths["project"]),
            "canonical_manifest_compatible": False,
            "output": None,
            "selected_candidate": None,
            "visual_review_completed": False,
            "next_stage": "siril-black-point",
            "green_reduction_processing_permitted": False,
            "policy_errors": [],
            "reprocessing_policy_errors": [],
            "reselection_policy_errors": [],
            "selection_policy_status": "invalid",
            "run_root": None,
            "errors": [f"Could not read black-point manifest: {exc}"],
        }

    errors: list[str] = []
    manifest_version = str(manifest.get("helper_version", ""))
    reprocessing_errors, reselection_errors = canonical_policy_requirements(
        manifest_version,
        manifest,
    )

    output = manifest.get("output", {})
    if not paths["stable_output"].is_file():
        errors.append("canonical black-point FITS is missing")
    else:
        recorded_size = output.get("size")
        if (
            isinstance(recorded_size, int)
            and paths["stable_output"].stat().st_size != recorded_size
        ):
            errors.append(
                "canonical black-point FITS size differs from manifest"
            )

    if manifest.get("source", {}).get("sha256") != upstream_sha256:
        errors.append("canonical source SHA differs from current upstream")

    for preview in (
        paths["stable_before_preview"],
        paths["stable_after_preview"],
    ):
        if not preview.is_file():
            errors.append(f"missing canonical preview {preview}")

    visual = manifest.get("visual_review_completed") is True
    if not visual:
        errors.append("visual_review_completed is not true")

    if manifest_version != "1.0.0":
        evidence = manifest.get("visual_selection", {}).get(
            "visual_review_evidence"
        )
        if not isinstance(evidence, dict):
            errors.append("canonical visual review evidence is missing")
        elif evidence.get("method") != "openclaw-read":
            errors.append(
                "canonical visual review method is not openclaw-read"
            )

    if manifest.get("next_stage") != "siril-green-reduction":
        errors.append("next_stage is not siril-green-reduction")

    effective = effective_canonical_status(
        manifest_status=manifest.get("status"),
        errors=errors,
        reprocessing_errors=reprocessing_errors,
        reselection_errors=reselection_errors,
    )
    ready = effective == "ready"

    return {
        "status": effective,
        "helper_version": VERSION,
        "manifest_helper_version": manifest_version,
        "project": str(paths["project"]),
        "canonical_manifest_compatible": ready,
        "output": (
            {
                "sha256": output.get("sha256"),
                "size": output.get("size"),
            }
            if output.get("sha256")
            else None
        ),
        "selected_candidate": manifest.get("selected_candidate"),
        "visual_review_completed": visual,
        "next_stage": (
            "siril-green-reduction" if ready else "siril-black-point"
        ),
        "green_reduction_processing_permitted": ready,
        "policy_errors": reprocessing_errors + reselection_errors,
        "reprocessing_policy_errors": reprocessing_errors,
        "reselection_policy_errors": reselection_errors,
        "selection_policy_status": (
            "current"
            if ready
            else "outdated"
            if effective == "needs_reselection"
            else "not_current"
        ),
        "run_root": manifest.get("run_root"),
        "errors": errors,
    }


def context_safe_status_fast(
    workspace: Path,
    project_name: str,
) -> dict[str, Any]:
    payload = compact_status_payload(
        status_project_fast(workspace, project_name)
    )
    assert_context_safe_payload(payload)
    return payload



def canonical_policy_requirements(
    manifest_version: str,
    manifest: dict[str, Any],
) -> tuple[list[str], list[str]]:
    """Return (reprocessing_errors, reselection_errors)."""
    reprocessing: list[str] = []
    reselection: list[str] = []

    if manifest_version == "1.0.0":
        reprocessing.extend(
            [
                (
                    "canonical helper 1.0.0 predates the retained per-channel "
                    "floor-clipping safety gate"
                ),
                (
                    "canonical helper 1.0.0 predates the retained explicit "
                    "image-view evidence contract"
                ),
            ]
        )
    elif manifest_version in SELECTION_POLICY_PREDECESSOR_VERSIONS:
        reselection.extend(
            [
                (
                    f"canonical helper {manifest_version} predates the "
                    "v1.0.4 preferred-channel-clipping selection policy"
                ),
                (
                    f"canonical helper {manifest_version} predates the "
                    "v1.0.4 faint-emission-over-background-darkness visual "
                    "selection priority"
                ),
            ]
        )
    elif manifest_version == VERSION:
        policy = manifest.get("selection_policy")
        if not isinstance(policy, dict):
            reselection.append(
                "canonical v1.0.4 selection_policy evidence is missing"
            )
        elif policy.get("version") != SELECTION_POLICY_VERSION:
            reselection.append(
                "canonical selection_policy version is not v1.0.4"
            )
    else:
        reprocessing.append(
            f"canonical helper version {manifest_version!r} is incompatible"
        )

    return reprocessing, reselection


def effective_canonical_status(
    *,
    manifest_status: Any,
    errors: list[str],
    reprocessing_errors: list[str],
    reselection_errors: list[str],
) -> str:
    if errors:
        return "invalid"
    if manifest_status != "ready":
        return "invalid"
    if reprocessing_errors:
        return "needs_reprocessing"
    if reselection_errors:
        return "needs_reselection"
    return "ready"


def status_project(workspace: Path, project_name: str) -> dict[str, Any]:
    paths = project_paths(workspace, project_name)
    if not paths["stable_manifest"].is_file():
        return {
            "status": "missing",
            "helper_version": VERSION,
            "project": str(paths["project"]),
            "next_stage": "siril-black-point",
            "green_reduction_processing_permitted": False,
            "errors": ["No canonical black-point manifest exists."],
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
            "green_reduction_processing_permitted": False,
            "errors": [f"Could not read black-point manifest: {exc}"],
        }

    errors: list[str] = []
    manifest_version = str(manifest.get("helper_version", ""))
    reprocessing_errors, reselection_errors = canonical_policy_requirements(
        manifest_version,
        manifest,
    )

    try:
        _, source_evidence = validate_upstream(paths)
    except Exception as exc:
        source_evidence = None
        errors.append(str(exc))

    try:
        output_evidence = inspect_fits(paths["stable_output"])
    except Exception as exc:
        output_evidence = None
        errors.append(str(exc))

    if output_evidence is not None:
        if manifest.get("output", {}).get("sha256") != output_evidence.sha256:
            errors.append("canonical output SHA differs from manifest")

    if source_evidence is not None:
        if manifest.get("source", {}).get("sha256") != source_evidence.sha256:
            errors.append(
                "canonical black-point source SHA differs from current "
                "GHS pass-2 source"
            )

    for preview in (
        paths["stable_before_preview"],
        paths["stable_after_preview"],
    ):
        if not preview.is_file():
            errors.append(f"missing canonical preview {preview}")

    visual = manifest.get("visual_review_completed") is True
    if not visual:
        errors.append("visual_review_completed is not true")

    if manifest_version != "1.0.0":
        evidence = manifest.get("visual_selection", {}).get(
            "visual_review_evidence"
        )
        if not isinstance(evidence, dict):
            errors.append("canonical visual_review_evidence is missing")
        elif evidence.get("method") != "openclaw-read":
            errors.append(
                "canonical visual review method is not openclaw-read"
            )

    if manifest.get("next_stage") != "siril-green-reduction":
        errors.append("next_stage is not siril-green-reduction")

    effective = effective_canonical_status(
        manifest_status=manifest.get("status"),
        errors=errors,
        reprocessing_errors=reprocessing_errors,
        reselection_errors=reselection_errors,
    )
    ready = effective == "ready"

    return {
        **manifest,
        "status": effective,
        "helper_version": VERSION,
        "manifest_helper_version": manifest_version,
        "canonical_manifest_compatible": ready,
        "policy_errors": reprocessing_errors + reselection_errors,
        "reprocessing_policy_errors": reprocessing_errors,
        "reselection_policy_errors": reselection_errors,
        "selection_policy_status": (
            "current"
            if ready
            else "outdated"
            if effective == "needs_reselection"
            else "not_current"
        ),
        "manifest": str(paths["stable_manifest"]),
        "project": str(paths["project"]),
        "output": (
            asdict(output_evidence)
            if output_evidence is not None
            else None
        ),
        "errors": errors,
        "green_reduction_processing_permitted": ready,
        "next_stage": (
            "siril-green-reduction" if ready else "siril-black-point"
        ),
        "run_root": manifest.get("run_root"),
    }


MAX_CONTEXT_SAFE_JSON_BYTES = 12000
MAX_COMPACT_ERRORS = 5
MAX_COMPACT_TEXT = 320


def compact_text(value: Any, limit: int = MAX_COMPACT_TEXT) -> str:
    text = str(value)
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def compact_list(values: Any, limit: int = MAX_COMPACT_ERRORS) -> list[Any]:
    if not isinstance(values, list):
        return []
    result = []
    for value in values[:limit]:
        if isinstance(value, str):
            result.append(compact_text(value))
        else:
            result.append(value)
    return result


def compact_status_payload(status: dict[str, Any]) -> dict[str, Any]:
    output = status.get("output")
    output_sha = output.get("sha256") if isinstance(output, dict) else None
    return {
        "status": status.get("status"),
        "helper_version": VERSION,
        "manifest_helper_version": status.get("manifest_helper_version"),
        "project": status.get("project"),
        "canonical_manifest_compatible": status.get(
            "canonical_manifest_compatible", False
        ),
        "canonical_output_sha256": output_sha,
        "selected_candidate": status.get("selected_candidate"),
        "visual_review_completed": status.get(
            "visual_review_completed", False
        ),
        "selection_policy_status": status.get(
            "selection_policy_status"
        ),
        "next_stage": status.get("next_stage"),
        "green_reduction_processing_permitted": status.get(
            "green_reduction_processing_permitted", False
        ),
        "policy_errors": compact_list(status.get("policy_errors")),
        "errors": compact_list(status.get("errors")),
    }


def compact_workflow_payload(state: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "status": state.get("status"),
        "action": state.get("action"),
        "helper_version": VERSION,
        "project": state.get("project"),
        "run_root": state.get("run_root"),
        "selected_candidate": state.get("selected_candidate"),
        "publication_eligible_candidates": state.get(
            "publication_eligible_candidates", []
        ),
        "numerical_recommended_candidate": state.get(
            "numerical_recommended_candidate"
        ),
        "recommended_candidate": state.get(
            "recommended_candidate"
        ),
        "visual_review_completed": state.get(
            "visual_review_completed", False
        ),
        "green_reduction_processing_permitted": state.get(
            "green_reduction_processing_permitted", False
        ),
    }
    canonical = state.get("canonical_status")
    if isinstance(canonical, dict):
        payload["canonical"] = compact_status_payload(canonical)
    if state.get("reason"):
        payload["reason"] = compact_text(state["reason"])
    return payload


def compact_begin_payload(entry: dict[str, Any]) -> dict[str, Any]:
    payload = compact_workflow_payload(entry)
    payload.update(
        {
            "stage_entry": "begin",
            "confirmation_required": entry.get(
                "confirmation_required", False
            ),
            "fresh_run_authorized": entry.get(
                "fresh_run_authorized", False
            ),
            "fresh_run_request_id": entry.get(
                "fresh_run_request_id"
            ),
        }
    )
    if entry.get("question"):
        payload["question"] = compact_text(entry["question"])
    if entry.get("message"):
        payload["message"] = compact_text(entry["message"])
    return payload


def compact_run_payload(record: dict[str, Any]) -> dict[str, Any]:
    gate = record.get("publication_gate", {})
    return {
        "status": record.get("status"),
        "helper_version": VERSION,
        "project": record.get("project"),
        "run_root": record.get("run_root"),
        "completed_candidate_count": record.get(
            "completed_candidate_count"
        ),
        "maximum_total_candidates": record.get(
            "maximum_total_candidates"
        ),
        "publication_permitted": record.get(
            "publication_permitted", False
        ),
        "publication_eligible_candidates": record.get(
            "publication_eligible_candidates", []
        ),
        "numerical_recommended_candidate": record.get(
            "numerical_recommended_candidate",
            record.get("recommended_candidate"),
        ),
        "recommended_candidate": record.get(
            "selection_policy_recommended_candidate",
            record.get("recommended_candidate"),
        ),
        "publication_gate_status": gate.get("status"),
        "publication_gate_reason": compact_text(
            gate.get("reason", "")
        ),
        "canonical_output_changed": record.get(
            "canonical_output_changed", False
        ),
        "green_reduction_processing_permitted": False,
    }


def compact_review_plan_payload(
    plan: dict[str, Any],
) -> dict[str, Any]:
    eligible = list(
        plan.get("publication_eligible_candidates", [])
    )
    read_targets = [
        {
            "role": "before",
            "path": plan.get("before_preview"),
            "sha256": plan.get("before_preview_sha256"),
        }
    ]
    candidates = []

    for item in plan.get("candidates", []):
        candidates.append(
            {
                "candidate": item.get("candidate"),
                "BP": item.get("BP"),
                "selection_policy_classification": item.get(
                    "selection_policy_classification"
                ),
                "within_preferred_channel_floor_range": item.get(
                    "within_preferred_channel_floor_range"
                ),
                "channel_low_clip_fraction": item.get(
                    "channel_low_clip_fraction"
                ),
                "preferred_channel_low_clip_fraction": item.get(
                    "preferred_channel_low_clip_fraction"
                ),
                "low_luma_clip_fraction": item.get(
                    "low_luma_clip_fraction"
                ),
                "output_luma_p001": item.get("output_luma_p001"),
                "output_luma_median": item.get(
                    "output_luma_median"
                ),
            }
        )
        read_targets.append(
            {
                "role": "candidate",
                "candidate": item.get("candidate"),
                "path": item.get("after_preview"),
                "sha256": item.get("after_preview_sha256"),
            }
        )

    project_name = (
        plan.get("project_name")
        or Path(str(plan.get("project"))).name
    )
    note_args = " ".join(
        f'--note "{name}=<what was actually seen>"'
        for name in eligible
    )
    command = (
        "/home/peter/.openclaw/workspace/agents/codewarrior/"
        "skills/siril-black-point/bin/black-point "
        f'select-publish --project "{project_name}" '
        '--candidate "<selected-candidate>" '
        '--visual-notes "<overall comparison>" '
        f"{note_args}"
    )

    policy = plan.get("selection_policy", {})

    return {
        "status": "visual_review_required",
        "action": "read_previews_then_select_publish",
        "helper_version": VERSION,
        "project": plan.get("project"),
        "project_name": project_name,
        "run_root": plan.get("run_root"),
        "review_method_required": plan.get(
            "review_method_required"
        ),
        "read_targets": read_targets,
        "publication_eligible_candidates": eligible,
        "required_candidate_notes": eligible,
        "numerical_recommended_candidate": plan.get(
            "numerical_recommended_candidate"
        ),
        "recommended_candidate": plan.get(
            "recommended_candidate"
        ),
        "selection_policy": {
            "version": policy.get("version"),
            "preferred_channel_low_clip_fraction": policy.get(
                "preferred_channel_low_clip_fraction"
            ),
            "maximum_channel_low_clip_fraction": policy.get(
                "maximum_channel_low_clip_fraction"
            ),
            "rules": policy.get("rules", []),
        },
        "candidates": candidates,
        "selection_command_template": command,
        "aggressive_override_instruction": (
            "If selecting an aggressive candidate while a preferred-range "
            "candidate exists, add --policy-override-reason with a specific "
            "visual explanation of the structural improvement and how faint "
            "emission remains preserved."
        ),
        "instruction": (
            "Use OpenClaw Read on every read_targets.path exactly as "
            "returned. Do not locate files yourself. Do not prefer deeper "
            "blacks or higher contrast by themselves. Faint-emission "
            "preservation outranks background darkness. Prefer a "
            "preferred-range candidate when both look acceptable."
        ),
        "green_reduction_processing_permitted": False,
    }


def compact_selection_payload(
    selection: dict[str, Any],
) -> dict[str, Any]:
    return {
        "status": selection.get("status"),
        "helper_version": VERSION,
        "project": selection.get("project"),
        "run_root": selection.get("run_root"),
        "selected_candidate": selection.get(
            "selected_candidate"
        ),
        "recommended_candidate": selection.get(
            "recommended_candidate"
        ),
        "numerical_recommended_candidate": selection.get(
            "numerical_recommended_candidate"
        ),
        "selected_candidate_policy_classification": selection.get(
            "selected_candidate_policy_classification"
        ),
        "aggressive_override_used": selection.get(
            "aggressive_override_used", False
        ),
        "visual_review_completed": selection.get(
            "visual_review_completed", False
        ),
        "visual_review_evidence_verified": selection.get(
            "visual_review_evidence_verified", False
        ),
        "review_method": selection.get("review_method"),
        "publication_permitted": selection.get(
            "publication_permitted", False
        ),
        "green_reduction_processing_permitted": False,
    }


def compact_publish_payload(
    published: dict[str, Any],
) -> dict[str, Any]:
    return {
        "status": published.get("status"),
        "helper_version": VERSION,
        "project": published.get("project"),
        "run_root": published.get("run_root"),
        "selected_candidate": published.get(
            "selected_candidate"
        ),
        "recommended_candidate": published.get(
            "recommended_candidate"
        ),
        "numerical_recommended_candidate": published.get(
            "numerical_recommended_candidate"
        ),
        "selected_candidate_policy_classification": published.get(
            "selected_candidate_policy_classification"
        ),
        "aggressive_override_used": published.get(
            "aggressive_override_used", False
        ),
        "visual_review_completed": published.get(
            "visual_review_completed", False
        ),
        "canonical_output_sha256": published.get(
            "canonical_output_sha256"
        ),
        "previous_processing_black_point_preserved_at": (
            published.get(
                "previous_processing_black_point_preserved_at"
            )
        ),
        "failed_publish_staging_preserved_at": published.get(
            "failed_publish_staging_preserved_at"
        ),
        "next_stage": published.get("next_stage"),
        "green_reduction_processing_permitted": published.get(
            "green_reduction_processing_permitted", False
        ),
    }


def assert_context_safe_payload(payload: dict[str, Any]) -> None:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > MAX_CONTEXT_SAFE_JSON_BYTES:
        raise BlackPointError(
            "Context-safe orchestration response exceeded the "
            f"{MAX_CONTEXT_SAFE_JSON_BYTES}-byte output budget."
        )


def context_safe_status(
    workspace: Path,
    project_name: str,
) -> dict[str, Any]:
    payload = compact_status_payload(
        status_project(workspace, project_name)
    )
    assert_context_safe_payload(payload)
    return payload


def advance_stage(
    *,
    workspace: Path,
    project_name: str,
    timeout_seconds: int,
    max_candidates: int,
    plan_only: bool = False,
) -> dict[str, Any]:
    """Perform the next authoritative machine step and return compact state."""
    entry = begin_stage(workspace, project_name)
    action = entry.get("action")
    status = entry.get("status")

    if status == "confirmation_required":
        payload = {
            "status": "confirmation_required",
            "action": "await_user_confirmation",
            "helper_version": VERSION,
            "project": entry.get("project"),
            "confirmation_required": True,
            "fresh_run_request_id": entry.get(
                "fresh_run_request_id"
            ),
            "question": compact_text(
                entry.get("question", "")
            ),
            "green_reduction_processing_permitted": False,
        }
        assert_context_safe_payload(payload)
        return payload

    if action == "stop":
        payload = {
            "status": "blocked",
            "action": "stop",
            "helper_version": VERSION,
            "project": entry.get("project"),
            "run_root": entry.get("run_root"),
            "reason": compact_text(
                entry.get("reason", "")
            ),
            "green_reduction_processing_permitted": False,
        }
        assert_context_safe_payload(payload)
        return payload

    if action == "prepare_policy_reselection":
        if plan_only:
            payload = {
                "status": "would_prepare_policy_reselection",
                "action": "prepare_policy_reselection",
                "helper_version": VERSION,
                "project": entry.get("project"),
                "run_root": entry.get("run_root"),
                "numerical_recommended_candidate": entry.get(
                    "numerical_recommended_candidate"
                ),
                "recommended_candidate": entry.get(
                    "recommended_candidate"
                ),
                "green_reduction_processing_permitted": False,
            }
            assert_context_safe_payload(payload)
            return payload

        prepared = prepare_policy_reselection(
            workspace=workspace,
            project_name=project_name,
            run_root=Path(entry["run_root"]),
        )
        plan = review_plan(
            workspace=workspace,
            project_name=project_name,
            run_root=Path(entry["run_root"]),
        )
        payload = compact_review_plan_payload(plan)
        payload["prepared_policy_reselection"] = True
        payload["run_manifest_backup"] = prepared.get(
            "run_manifest_backup"
        )
        assert_context_safe_payload(payload)
        return payload

    if action == "publish_recorded_selection":
        if plan_only:
            payload = {
                "status": "would_publish_recorded_selection",
                "action": "publish_recorded_selection",
                "helper_version": VERSION,
                "project": entry.get("project"),
                "run_root": entry.get("run_root"),
                "selected_candidate": entry.get(
                    "selected_candidate"
                ),
                "green_reduction_processing_permitted": False,
            }
            assert_context_safe_payload(payload)
            return payload

        published = publish_project(
            workspace=workspace,
            project_name=project_name,
            run_root=Path(entry["run_root"]),
        )
        final = context_safe_status(
            workspace,
            project_name,
        )
        payload = {
            **compact_publish_payload(published),
            "verification": final,
        }
        assert_context_safe_payload(payload)
        return payload

    if action == "review_select_publish":
        plan = review_plan(
            workspace=workspace,
            project_name=project_name,
            run_root=Path(entry["run_root"]),
        )
        payload = compact_review_plan_payload(plan)
        assert_context_safe_payload(payload)
        return payload

    if action == "run_review_select_publish":
        if plan_only:
            payload = {
                "status": "would_generate_candidates",
                "action": "run_review_select_publish",
                "helper_version": VERSION,
                "project": entry.get("project"),
                "confirmation_required": False,
                "canonical": compact_status_payload(
                    entry.get("canonical_status", {})
                ),
                "green_reduction_processing_permitted": False,
            }
            assert_context_safe_payload(payload)
            return payload

        record = run_project(
            workspace=workspace,
            project_name=project_name,
            timeout_seconds=timeout_seconds,
            max_candidates=max_candidates,
        )
        if record.get("publication_permitted") is not True:
            gate = record.get("publication_gate", {})
            payload = {
                "status": "blocked",
                "action": "stop",
                "helper_version": VERSION,
                "project": record.get("project"),
                "run_root": record.get("run_root"),
                "reason": compact_text(
                    gate.get(
                        "reason",
                        "Publication gate blocked.",
                    )
                ),
                "green_reduction_processing_permitted": False,
            }
            assert_context_safe_payload(payload)
            return payload

        plan = review_plan(
            workspace=workspace,
            project_name=project_name,
            run_root=Path(record["run_root"]),
        )
        payload = compact_review_plan_payload(plan)
        payload["generated_new_run"] = True
        assert_context_safe_payload(payload)
        return payload

    raise BlackPointError(
        f"Context-safe advance encountered unsupported action {action!r}."
    )


def select_publish_stage(
    *,
    workspace: Path,
    project_name: str,
    candidate_name: str,
    visual_notes: str,
    candidate_notes: list[str],
    policy_override_reason: str | None = None,
) -> dict[str, Any]:
    """Persist visual judgment and publish with helper-owned evidence wiring."""
    state = workflow_state(workspace, project_name)
    if state.get("action") != "review_select_publish":
        raise BlackPointError(
            "select-publish requires the current durable state to be "
            "awaiting visual selection. Re-enter through advance."
        )

    run_root = Path(state["run_root"])
    plan = review_plan(
        workspace=workspace,
        project_name=project_name,
        run_root=run_root,
    )

    eligible = list(
        plan["publication_eligible_candidates"]
    )
    if candidate_name not in eligible:
        raise BlackPointError(
            f"Selected candidate {candidate_name!r} is not eligible. "
            f"Eligible candidates: {eligible}."
        )

    notes_map = parse_candidate_notes_flexible(
        candidate_notes,
        eligible,
    )

    reviewed_previews = [
        (
            f"{item['candidate']}="
            f"{item['after_preview_sha256']}"
        )
        for item in plan["candidates"]
    ]
    normalized_notes = [
        f"{name}={notes_map[name]}"
        for name in eligible
    ]

    selection = record_visual_selection(
        workspace=workspace,
        project_name=project_name,
        run_root=run_root,
        candidate_name=candidate_name,
        compared_candidates=eligible,
        visual_notes=visual_notes,
        review_method="openclaw-read",
        before_reviewed_sha256=plan[
            "before_preview_sha256"
        ],
        reviewed_previews=reviewed_previews,
        candidate_notes=normalized_notes,
        policy_override_reason=policy_override_reason,
    )

    published = publish_project(
        workspace=workspace,
        project_name=project_name,
        run_root=run_root,
    )

    final = context_safe_status(
        workspace,
        project_name,
    )
    payload = {
        **compact_publish_payload(published),
        "selection": compact_selection_payload(selection),
        "verification": final,
    }
    assert_context_safe_payload(payload)
    return payload



def prepare_policy_reselection(
    *,
    workspace: Path,
    project_name: str,
    run_root: Path,
) -> dict[str, Any]:
    """Preserve published run evidence and reopen it for v1.0.4 reselection."""
    paths = project_paths(workspace, project_name)
    _, source_sha256 = validate_upstream_fast(paths)

    canonical = status_project_fast(
        workspace,
        project_name,
        upstream_sha256=source_sha256,
    )
    if canonical.get("status") != "needs_reselection":
        raise BlackPointError(
            "Policy reselection requires a canonical result in "
            "needs_reselection state."
        )
    if canonical.get("run_root") != str(run_root):
        raise BlackPointError(
            "Canonical run_root does not match the requested reselection run."
        )

    manifest_path, record = load_run_record(run_root)
    validate_run_record(
        record=record,
        project_name=project_name,
        source_sha256=source_sha256,
    )

    current_marker = record.get("selection_policy_reselection")
    if (
        record.get("canonical_output_changed") is False
        and isinstance(current_marker, dict)
        and current_marker.get("version") == SELECTION_POLICY_VERSION
    ):
        return {
            "status": "awaiting_visual_selection",
            "action": "review_select_publish",
            "helper_version": VERSION,
            "project": str(paths["project"]),
            "run_root": str(run_root),
            "reselection_prepared": True,
            "run_manifest_backup": current_marker.get(
                "run_manifest_backup"
            ),
            "green_reduction_processing_permitted": False,
        }

    if record.get("canonical_output_changed") is not True:
        raise BlackPointError(
            "Policy reselection expected a previously published run."
        )

    backup = run_root / (
        "run-manifest-before-v1.0.4-reselection-"
        f"{unique_id()}.json"
    )
    shutil.copy2(manifest_path, backup)

    previous_selection = record.get("visual_selection")
    history = list(
        record.get("selection_policy_reselection_history", [])
    )
    history.append(
        {
            "at": utc_now(),
            "policy_version": SELECTION_POLICY_VERSION,
            "run_manifest_backup": str(backup),
            "previous_selected_candidate": record.get(
                "selected_candidate"
            ),
            "previous_visual_selection": previous_selection,
            "previous_published_at": record.get("published_at"),
            "previous_canonical_output_sha256": canonical.get(
                "output", {}
            ).get("sha256"),
        }
    )

    summary = selection_policy_summary(record.get("candidates", []))
    record["selection_policy_reselection_history"] = history
    record["selection_policy_reselection"] = {
        "version": SELECTION_POLICY_VERSION,
        "prepared_at": utc_now(),
        "run_manifest_backup": str(backup),
        "canonical_helper_version_before_reselection": canonical.get(
            "manifest_helper_version"
        ),
        "canonical_selected_candidate_before_reselection": canonical.get(
            "selected_candidate"
        ),
    }
    record["numerical_recommended_candidate"] = summary.get(
        "numerical_recommended_candidate"
    )
    record["selection_policy_recommended_candidate"] = summary.get(
        "recommended_candidate"
    )
    record["selection_policy"] = summary
    record["status"] = "awaiting_visual_selection"
    record["canonical_output_changed"] = False
    record["visual_selection"] = None
    record["selected_candidate"] = None
    record["visual_review_completed"] = False
    record["published_at"] = None
    record["green_reduction_processing_permitted"] = False
    json_dump_atomic(manifest_path, record)

    return {
        "status": "awaiting_visual_selection",
        "action": "review_select_publish",
        "helper_version": VERSION,
        "project": str(paths["project"]),
        "run_root": str(run_root),
        "reselection_prepared": True,
        "run_manifest_backup": str(backup),
        "numerical_recommended_candidate": summary.get(
            "numerical_recommended_candidate"
        ),
        "recommended_candidate": summary.get(
            "recommended_candidate"
        ),
        "green_reduction_processing_permitted": False,
    }


def workflow_state(workspace: Path, project_name: str) -> dict[str, Any]:
    """Fast durable state discovery for routine orchestration."""
    paths = project_paths(workspace, project_name)
    _, source_sha256 = validate_upstream_fast(paths)
    canonical_status = status_project_fast(
        workspace,
        project_name,
        upstream_sha256=source_sha256,
    )

    compatible: list[tuple[float, Path, dict[str, Any]]] = []
    blocked: list[tuple[float, Path, dict[str, Any]]] = []

    if paths["runs"].is_dir():
        for run_root in paths["runs"].iterdir():
            if not run_root.is_dir() or run_root.name == "stage-intents":
                continue
            manifest_path = run_root / "run-manifest.json"
            if not manifest_path.is_file():
                continue
            try:
                record = json.loads(
                    manifest_path.read_text(encoding="utf-8")
                )
            except Exception:
                continue
            if record.get("project_name") != project_name:
                continue
            if record.get("helper_version") not in COMPATIBLE_RUN_HELPER_VERSIONS:
                continue
            if record.get("source", {}).get("sha256") != source_sha256:
                continue
            if record.get("canonical_output_changed") is True:
                continue

            item = (manifest_path.stat().st_mtime, run_root, record)
            if record.get("publication_permitted") is True:
                compatible.append(item)
            elif record.get("completed_candidate_count", 0):
                blocked.append(item)

    if compatible:
        _, run_root, record = max(
            compatible,
            key=lambda item: item[0],
        )
        selection = record.get("visual_selection")
        summary = selection_policy_summary(record.get("candidates", []))
        if isinstance(selection, dict) and selection.get("completed") is True:
            return {
                "status": "ready_to_publish",
                "action": "publish_recorded_selection",
                "helper_version": VERSION,
                "project": str(paths["project"]),
                "run_root": str(run_root),
                "selected_candidate": selection.get(
                    "selected_candidate"
                ),
                "publication_eligible_candidates": record.get(
                    "publication_eligible_candidates", []
                ),
                "numerical_recommended_candidate": summary.get(
                    "numerical_recommended_candidate"
                ),
                "recommended_candidate": summary.get(
                    "recommended_candidate"
                ),
                "visual_review_completed": True,
                "canonical_status": canonical_status,
                "source_sha256": source_sha256,
                "green_reduction_processing_permitted": False,
            }

        return {
            "status": "awaiting_visual_selection",
            "action": "review_select_publish",
            "helper_version": VERSION,
            "project": str(paths["project"]),
            "run_root": str(run_root),
            "publication_eligible_candidates": record.get(
                "publication_eligible_candidates", []
            ),
            "numerical_recommended_candidate": summary.get(
                "numerical_recommended_candidate"
            ),
            "recommended_candidate": summary.get(
                "recommended_candidate"
            ),
            "visual_review_completed": False,
            "canonical_status": canonical_status,
            "source_sha256": source_sha256,
            "green_reduction_processing_permitted": False,
        }

    if canonical_status.get("status") == "needs_reselection":
        run_root_value = canonical_status.get("run_root")
        if not run_root_value:
            return {
                "status": "blocked",
                "action": "stop",
                "helper_version": VERSION,
                "project": str(paths["project"]),
                "reason": (
                    "Canonical requires v1.0.4 reselection but has no "
                    "recorded run_root."
                ),
                "canonical_status": canonical_status,
                "source_sha256": source_sha256,
                "green_reduction_processing_permitted": False,
            }

        run_root = Path(run_root_value)
        manifest_path = run_root / "run-manifest.json"
        if not manifest_path.is_file():
            return {
                "status": "blocked",
                "action": "stop",
                "helper_version": VERSION,
                "project": str(paths["project"]),
                "run_root": str(run_root),
                "reason": (
                    "Canonical requires v1.0.4 reselection but its source "
                    "run manifest is missing."
                ),
                "canonical_status": canonical_status,
                "source_sha256": source_sha256,
                "green_reduction_processing_permitted": False,
            }

        try:
            record = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )
            validate_run_record(
                record=record,
                project_name=project_name,
                source_sha256=source_sha256,
            )
        except Exception as exc:
            return {
                "status": "blocked",
                "action": "stop",
                "helper_version": VERSION,
                "project": str(paths["project"]),
                "run_root": str(run_root),
                "reason": (
                    "Canonical requires v1.0.4 reselection but its source "
                    f"run is not compatible: {exc}"
                ),
                "canonical_status": canonical_status,
                "source_sha256": source_sha256,
                "green_reduction_processing_permitted": False,
            }

        if record.get("canonical_output_changed") is not True:
            return {
                "status": "blocked",
                "action": "stop",
                "helper_version": VERSION,
                "project": str(paths["project"]),
                "run_root": str(run_root),
                "reason": (
                    "Canonical requires reselection but the referenced run "
                    "is not in published state."
                ),
                "canonical_status": canonical_status,
                "source_sha256": source_sha256,
                "green_reduction_processing_permitted": False,
            }

        summary = selection_policy_summary(
            record.get("candidates", [])
        )
        return {
            "status": "needs_reselection",
            "action": "prepare_policy_reselection",
            "helper_version": VERSION,
            "project": str(paths["project"]),
            "run_root": str(run_root),
            "publication_eligible_candidates": record.get(
                "publication_eligible_candidates", []
            ),
            "numerical_recommended_candidate": summary.get(
                "numerical_recommended_candidate"
            ),
            "recommended_candidate": summary.get(
                "recommended_candidate"
            ),
            "canonical_status": canonical_status,
            "source_sha256": source_sha256,
            "green_reduction_processing_permitted": False,
        }

    if blocked:
        _, run_root, record = max(
            blocked,
            key=lambda item: item[0],
        )
        return {
            "status": "blocked",
            "action": "stop",
            "helper_version": VERSION,
            "project": str(paths["project"]),
            "run_root": str(run_root),
            "reason": record.get("publication_gate", {}).get("reason"),
            "canonical_status": canonical_status,
            "source_sha256": source_sha256,
            "green_reduction_processing_permitted": False,
        }

    return {
        "status": "start_new_run",
        "action": "run_review_select_publish",
        "helper_version": VERSION,
        "project": str(paths["project"]),
        "source_sha256": source_sha256,
        "canonical_status": canonical_status,
        "green_reduction_processing_permitted": False,
        "message": "No compatible incomplete black-point run exists.",
    }


def load_stage_intents(paths: dict[str, Path]) -> list[tuple[float, Path, dict[str, Any]]]:
    if not paths["intents"].is_dir():
        return []
    result = []
    for path in paths["intents"].glob("fresh-run-*.json"):
        if not path.is_file():
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        result.append((path.stat().st_mtime, path, record))
    return sorted(result, key=lambda x: x[0], reverse=True)


def matching_stage_intent(
    *,
    paths: dict[str, Path],
    project_name: str,
    source_sha256: str,
    canonical_sha256: str,
    statuses: set[str] | frozenset[str],
) -> tuple[Path, dict[str, Any]] | None:
    for _, path, record in load_stage_intents(paths):
        if record.get("project_name") != project_name:
            continue
        if record.get("source_sha256") != source_sha256:
            continue
        if record.get("canonical_output_sha256") != canonical_sha256:
            continue
        if record.get("status") not in statuses:
            continue
        return path, record
    return None


def current_canonical_sha(paths: dict[str, Path], status: dict[str, Any]) -> str:
    if status.get("status") != "ready" or status.get("canonical_manifest_compatible") is not True:
        raise BlackPointError("Fresh rerun confirmation requires a valid ready canonical result.")
    expected = status.get("output", {}).get("sha256")
    actual = sha256_file(paths["stable_output"])
    if expected != actual:
        raise BlackPointError("Canonical black-point checksum changed since status validation.")
    return actual


def begin_stage(workspace: Path, project_name: str) -> dict[str, Any]:
    paths = project_paths(workspace, project_name)
    _, source_sha256 = validate_upstream_fast(paths)
    state = workflow_state(workspace, project_name)
    action = state.get("action")

    if action in (
        "review_select_publish",
        "publish_recorded_selection",
        "prepare_policy_reselection",
        "stop",
    ):
        return {
            **state,
            "stage_entry": "begin",
            "confirmation_required": False,
        }

    if action != "run_review_select_publish":
        raise BlackPointError(
            f"Unsupported begin action {action!r}"
        )

    canonical = state.get("canonical_status", {})

    if canonical.get("status") == "needs_reprocessing":
        return {
            **state,
            "stage_entry": "begin",
            "confirmation_required": False,
            "message": (
                "An existing canonical black-point result is preserved, "
                "but it predates required processing/review safety policy. "
                "Reprocess the stage now; preserve the old canonical until "
                "successful publication."
            ),
        }

    if canonical.get("status") != "ready":
        return {
            **state,
            "stage_entry": "begin",
            "confirmation_required": False,
            "message": (
                "No valid completed black-point result exists. Proceed with "
                "the full stage now."
            ),
        }

    canonical_sha = current_canonical_sha(paths, canonical)

    authorized = matching_stage_intent(
        paths=paths,
        project_name=project_name,
        source_sha256=source_sha256,
        canonical_sha256=canonical_sha,
        statuses={"fresh_run_authorized"},
    )
    if authorized is not None:
        intent_path, intent = authorized
        return {
            "status": "fresh_run_authorized",
            "action": "run_review_select_publish",
            "helper_version": VERSION,
            "project": str(paths["project"]),
            "stage_entry": "begin",
            "confirmation_required": False,
            "fresh_run_authorized": True,
            "fresh_run_request_id": intent.get("request_id"),
            "fresh_run_intent": str(intent_path),
            "canonical_status": canonical,
            "green_reduction_processing_permitted": False,
            "message": (
                "Fresh rerun is already authorized. Continue without "
                "asking again."
            ),
        }

    pending = matching_stage_intent(
        paths=paths,
        project_name=project_name,
        source_sha256=source_sha256,
        canonical_sha256=canonical_sha,
        statuses={"confirmation_required"},
    )

    if pending is None:
        paths["intents"].mkdir(parents=True, exist_ok=True)
        request_id = unique_id()
        intent_path = (
            paths["intents"] / f"fresh-run-{request_id}.json"
        )
        requested_at = utc_now()
        intent = {
            "schema_version": 1,
            "helper_version": VERSION,
            "project": str(paths["project"]),
            "project_name": project_name,
            "request_id": request_id,
            "status": "confirmation_required",
            "requested_at": requested_at,
            "authorized_at": None,
            "consumed_at": None,
            "consumed_by_run_root": None,
            "source_sha256": source_sha256,
            "canonical_output_sha256": canonical_sha,
            "canonical_manifest_sha256": sha256_file(
                paths["stable_manifest"]
            ),
            "history": [
                {
                    "at": requested_at,
                    "status": "confirmation_required",
                    "reason": (
                        "Explicit black-point processing request encountered "
                        "an already completed canonical result."
                    ),
                }
            ],
        }
        json_dump_atomic(intent_path, intent)
    else:
        intent_path, intent = pending

    return {
        "status": "confirmation_required",
        "action": "confirm_fresh_run",
        "helper_version": VERSION,
        "project": str(paths["project"]),
        "stage_entry": "begin",
        "confirmation_required": True,
        "fresh_run_authorized": False,
        "fresh_run_request_id": intent.get("request_id"),
        "fresh_run_intent": str(intent_path),
        "canonical_status": canonical,
        "green_reduction_processing_permitted": False,
        "question": (
            f"Black point for {project_name} has already completed "
            "successfully. Do you want me to run it again as a fresh run?"
        ),
    }


def confirm_fresh_run(
    workspace: Path,
    project_name: str,
) -> dict[str, Any]:
    paths = project_paths(workspace, project_name)
    _, source_sha256 = validate_upstream_fast(paths)
    state = workflow_state(workspace, project_name)

    if state.get("action") in (
        "review_select_publish",
        "publish_recorded_selection",
        "prepare_policy_reselection",
    ):
        return {
            **state,
            "confirmation_required": False,
            "fresh_run_authorized": False,
            "message": (
                "A compatible incomplete or policy-reselection run already "
                "exists; resume it instead of authorizing another fresh run."
            ),
        }

    if state.get("action") == "stop":
        return {
            **state,
            "confirmation_required": False,
            "fresh_run_authorized": False,
        }

    canonical = state.get("canonical_status", {})
    canonical_sha = current_canonical_sha(paths, canonical)

    existing = matching_stage_intent(
        paths=paths,
        project_name=project_name,
        source_sha256=source_sha256,
        canonical_sha256=canonical_sha,
        statuses={"fresh_run_authorized"},
    )
    if existing is not None:
        path, intent = existing
        return {
            "status": "fresh_run_authorized",
            "action": "run_review_select_publish",
            "helper_version": VERSION,
            "project": str(paths["project"]),
            "confirmation_required": False,
            "fresh_run_authorized": True,
            "fresh_run_request_id": intent.get("request_id"),
            "fresh_run_intent": str(path),
            "canonical_status": canonical,
            "green_reduction_processing_permitted": False,
        }

    pending = matching_stage_intent(
        paths=paths,
        project_name=project_name,
        source_sha256=source_sha256,
        canonical_sha256=canonical_sha,
        statuses={"confirmation_required"},
    )
    if pending is None:
        raise BlackPointError(
            "No pending fresh-run confirmation exists. Begin the stage first."
        )

    path, intent = pending
    authorized_at = utc_now()
    intent["status"] = "fresh_run_authorized"
    intent["authorized_at"] = authorized_at
    history = list(intent.get("history", []))
    history.append(
        {
            "at": authorized_at,
            "status": "fresh_run_authorized",
            "reason": (
                "User explicitly confirmed a fresh black-point rerun."
            ),
        }
    )
    intent["history"] = history
    json_dump_atomic(path, intent)

    return {
        "status": "fresh_run_authorized",
        "action": "run_review_select_publish",
        "helper_version": VERSION,
        "project": str(paths["project"]),
        "confirmation_required": False,
        "fresh_run_authorized": True,
        "fresh_run_request_id": intent.get("request_id"),
        "fresh_run_intent": str(path),
        "authorized_at": authorized_at,
        "canonical_status": canonical,
        "green_reduction_processing_permitted": False,
    }


def consume_fresh_run_authorization(*, intent_path: Path, intent: dict[str, Any], run_root: Path) -> None:
    consumed_at = utc_now()
    intent["status"] = "consumed"
    intent["consumed_at"] = consumed_at
    intent["consumed_by_run_root"] = str(run_root)
    history = list(intent.get("history", []))
    history.append(
        {"at": consumed_at, "status": "consumed", "reason": "Fresh black-point run manifest was durably created.", "run_root": str(run_root)}
    )
    intent["history"] = history
    json_dump_atomic(intent_path, intent)


def write_synthetic_upstream(workspace: Path, project_name: str) -> None:
    project = workspace / "Projects" / project_name
    upstream = project / "processing" / "ghs-pass2"
    upstream.mkdir(parents=True, exist_ok=False)
    yy, xx = np.mgrid[0:256, 0:256]
    glow = np.exp(-(((xx - 132.0) / 70.0) ** 2 + ((yy - 126.0) / 58.0) ** 2))
    structure = 0.010 * np.exp(-(((xx - 132.0) / 20.0) ** 2 + ((yy - 150.0) / 34.0) ** 2))
    texture = 0.0025 * np.sin(xx / 13.0) * np.cos(yy / 17.0)
    base = 0.166 + 0.038 * glow + structure + texture
    data = np.stack((base * 0.96, base * 1.04, base), axis=0).astype(np.float32)
    output = upstream / "SHO-starless-ghs-pass2.fit"
    hdu = fits.PrimaryHDU(data)
    hdu.header["FILTER"] = "mixed_Starless"
    hdu.writeto(output)
    evidence = inspect_fits(output)
    manifest = {
        "schema_version": 1,
        "helper_version": "1.2.0",
        "project": project_name,
        "project_path": str(project),
        "status": "ready",
        "visual_review_completed": True,
        "black_point_processing_permitted": True,
        "next_stage": "siril-black-point",
        "quality_assessment": {"satisfactory": True},
        "output": asdict(evidence),
    }
    json_dump_atomic(upstream / "ghs-pass2-manifest.json", manifest)


def self_test(timeout_seconds: int) -> dict[str, Any]:
    siril = siril_version()
    root = WORKSPACE / ".skill-self-tests" / "siril-black-point" / unique_id()
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=False)
    project_name = "Synthetic Black Point"
    write_synthetic_upstream(workspace, project_name)
    state = begin_stage(workspace, project_name)
    if state.get("status") != "start_new_run":
        raise BlackPointError(f"Synthetic begin state invalid: {state}")
    record = run_project(
        workspace=workspace,
        project_name=project_name,
        timeout_seconds=timeout_seconds,
        max_candidates=3,
    )
    gate = record.get("publication_gate", {})
    if gate.get("publication_permitted") is not True:
        raise BlackPointError(f"Synthetic black-point gate failed: {gate}")
    if not record.get("recommended_candidate"):
        raise BlackPointError("Synthetic black-point run produced no recommendation.")
    return {
        "status": "success",
        "helper_version": VERSION,
        "siril": siril,
        "self_test_directory": str(root),
        "recommended_candidate": record["recommended_candidate"],
        "publication_eligible_candidates": record["publication_eligible_candidates"],
        "candidates": [
            {
                "candidate": item["candidate"],
                "BP": item["parameters"]["BP"],
                "classification": item["histogram_classification"],
                "output_luma_p001": item["quality_assessment"]["metrics"]["output_luma_p001"],
                "output_luma_median": item["quality_assessment"]["metrics"]["output_luma_median"],
                "low_luma_clip_fraction": item["quality_assessment"]["metrics"]["low_luma_clip_fraction"],
                "channel_low_clip_fraction": item["quality_assessment"]["metrics"]["channel_low_clip_fraction"],
                "selection_score": item["selection_score"],
            }
            for item in record["candidates"]
        ],
        "tests": [
            "real Siril 1.4.4 linstretch execution",
            "three bounded adaptive black-point candidates",
            "RGB Blend clipping mode",
            "32-bit RGB FITS preservation",
            "low-luminance clipping guard",
            "per-channel floor-clipping guard",
            "same-scale permanent before/after previews",
            "explicit image-view evidence contract",
            "bounded publication gate",
            "numerical recommendation",
            "evidence preservation",
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run or resume a preservation-safe adaptive Siril black-point stage "
            "from the canonical GHS pass-2 starless FITS."
        )
    )
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("self-test")
    p.add_argument("--timeout", type=int, default=1800)

    p = sub.add_parser("advance")
    p.add_argument("--project", required=True)
    p.add_argument("--timeout", type=int, default=7200)
    p.add_argument("--max-candidates", type=int, default=3)
    p.add_argument("--plan-only", action="store_true")

    p = sub.add_parser("begin")
    p.add_argument("--project", required=True)

    p = sub.add_parser("confirm-fresh")
    p.add_argument("--project", required=True)

    p = sub.add_parser("workflow-state")
    p.add_argument("--project", required=True)

    p = sub.add_parser("run")
    p.add_argument("--project", required=True)
    p.add_argument("--timeout", type=int, default=1800)
    p.add_argument("--max-candidates", type=int, default=3)

    p = sub.add_parser("review-plan")
    p.add_argument("--project", required=True)
    p.add_argument("--run-root", required=True, type=Path)

    p = sub.add_parser("select")
    p.add_argument("--project", required=True)
    p.add_argument("--run-root", required=True, type=Path)
    p.add_argument("--candidate", required=True)
    p.add_argument("--compared", action="append", required=True)
    p.add_argument("--visual-notes", required=True)
    p.add_argument(
        "--review-method",
        required=True,
        choices=["openclaw-read"],
    )
    p.add_argument("--before-reviewed-sha256", required=True)
    p.add_argument(
        "--reviewed-preview",
        action="append",
        required=True,
        help="Repeat candidate=after_preview_sha256 for every eligible candidate.",
    )
    p.add_argument(
        "--candidate-note",
        action="append",
        required=True,
        help="Repeat candidate=qualitative visual note for every eligible candidate.",
    )
    p.add_argument(
        "--policy-override-reason",
        required=False,
        help=(
            "Required only when deliberately selecting an aggressive "
            "candidate while a preferred-range candidate exists."
        ),
    )

    p = sub.add_parser("select-publish")
    p.add_argument("--project", required=True)
    p.add_argument("--candidate", required=True)
    p.add_argument("--visual-notes", required=True)
    p.add_argument(
        "--note",
        "--candidate-note",
        dest="candidate_notes",
        action="append",
        required=True,
        help=(
            "Repeat candidate=visual-note for every eligible candidate. "
            "One argument containing multiple candidate= sections is also "
            "accepted."
        ),
    )
    p.add_argument(
        "--policy-override-reason",
        required=False,
        help=(
            "Required only when selecting an aggressive candidate while a "
            "preferred-range candidate exists; explain the visible structural "
            "gain and how faint emission remains preserved."
        ),
    )

    p = sub.add_parser("publish")
    p.add_argument("--project", required=True)
    p.add_argument("--run-root", required=True, type=Path)

    p = sub.add_parser("stage-status")
    p.add_argument("--project", required=True)

    p = sub.add_parser("status")
    p.add_argument("--project", required=True)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "self-test":
            payload = self_test(args.timeout)
        elif args.command == "advance":
            payload = advance_stage(
                workspace=WORKSPACE,
                project_name=args.project,
                timeout_seconds=args.timeout,
                max_candidates=args.max_candidates,
                plan_only=args.plan_only,
            )
        elif args.command == "begin":
            payload = compact_begin_payload(
                begin_stage(WORKSPACE, args.project)
            )
        elif args.command == "confirm-fresh":
            payload = compact_begin_payload(
                confirm_fresh_run(WORKSPACE, args.project)
            )
        elif args.command == "workflow-state":
            payload = compact_workflow_payload(
                workflow_state(WORKSPACE, args.project)
            )
        elif args.command == "run":
            payload = compact_run_payload(
                run_project(
                    workspace=WORKSPACE,
                    project_name=args.project,
                    timeout_seconds=args.timeout,
                    max_candidates=args.max_candidates,
                )
            )
        elif args.command == "review-plan":
            payload = compact_review_plan_payload(
                review_plan(
                    workspace=WORKSPACE,
                    project_name=args.project,
                    run_root=args.run_root.resolve(),
                )
            )
        elif args.command == "select":
            payload = compact_selection_payload(
                record_visual_selection(
                    workspace=WORKSPACE,
                    project_name=args.project,
                    run_root=args.run_root.resolve(),
                    candidate_name=args.candidate,
                    compared_candidates=args.compared,
                    visual_notes=args.visual_notes,
                    review_method=args.review_method,
                    before_reviewed_sha256=args.before_reviewed_sha256,
                    reviewed_previews=args.reviewed_preview,
                    candidate_notes=args.candidate_note,
                    policy_override_reason=args.policy_override_reason,
                )
            )
        elif args.command == "select-publish":
            payload = select_publish_stage(
                workspace=WORKSPACE,
                project_name=args.project,
                candidate_name=args.candidate,
                visual_notes=args.visual_notes,
                candidate_notes=args.candidate_notes,
                policy_override_reason=args.policy_override_reason,
            )
        elif args.command == "publish":
            payload = compact_publish_payload(
                publish_project(
                    workspace=WORKSPACE,
                    project_name=args.project,
                    run_root=args.run_root.resolve(),
                )
            )
        elif args.command == "stage-status":
            payload = context_safe_status_fast(
                WORKSPACE,
                args.project,
            )
        elif args.command == "status":
            payload = context_safe_status(
                WORKSPACE,
                args.project,
            )
        else:
            raise BlackPointError(
                f"Unsupported command {args.command!r}"
            )

        if args.command != "self-test":
            assert_context_safe_payload(payload)

    except BlackPointError as exc:
        payload = {
            "status": "blocked",
            "helper_version": VERSION,
            "error": compact_text(exc),
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 2

    print(json.dumps(payload, indent=2, sort_keys=True))
    return (
        0
        if payload.get("status") in CLI_SUCCESS_STATUSES
        else 2
    )


if __name__ == "__main__":
    raise SystemExit(main())
