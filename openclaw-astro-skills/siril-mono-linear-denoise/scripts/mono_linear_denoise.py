#!/usr/bin/env python3
"""Safety-first autonomous Siril mono denoise with validated visual review."""

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
import time
from typing import Any
import zipfile

import numpy as np
from astropy.io import fits


VERSION = "1.0.3"
WORKSPACE = Path("/home/peter/.openclaw/workspace/agents/codewarrior")
SIRIL_APP = Path(
    "/home/peter/.openclaw/runtime/siril-processor/toolchain/.toolchain/"
    "siril/1.4.4/squashfs-root/AppRun"
)
SIRIL_APPDIR = SIRIL_APP.parent
REQUIRED_SIRIL_VERSION = "1.4.4"
REQUIRED_UPSTREAM_HELPER_VERSION = "1.0.1"
FILTERS = ("Ha", "SII", "OIII")
CONTACT_PREVIEW_MAXDIM = 1800
REVIEW_SCHEMA_VERSION = 2

CANDIDATES = (
    {
        "candidate": "candidate-00",
        "modulation": 0.0,
        "command": None,
        "strength": "pass-through source",
        "operation": "pass-through",
        "strength_penalty": 0.00,
    },
    {
        "candidate": "candidate-01",
        "modulation": 0.20,
        "command": "denoise -mod=0.20",
        "strength": "very gentle NL-Bayes",
        "operation": "siril-nl-bayes",
        "strength_penalty": 0.03,
    },
    {
        "candidate": "candidate-02",
        "modulation": 0.40,
        "command": "denoise -mod=0.40",
        "strength": "gentle NL-Bayes",
        "operation": "siril-nl-bayes",
        "strength_penalty": 0.10,
    },
)

SEVERE_VISUAL_FLAGS = {
    "wormy_texture",
    "woven_texture",
    "mottled_blocks",
    "waxy_smoothing",
    "ringing",
    "dark_halo",
    "detail_loss",
    "faint_emission_loss",
    "clipping",
    "missing_area",
}
ALLOWED_VISUAL_FLAGS = SEVERE_VISUAL_FLAGS | {
    "none",
    "minor_residual_noise",
    "minimal_change",
    "indistinguishable",
    "no_material_improvement",
    "background_improved",
    "background_worse",
    "detail_preserved",
    "detail_worse",
}

REVIEW_VIEWS = ("full", "background", "detail")
COMPARISON_VALUES = {"better", "same", "worse"}
BANNED_CONFIDENCE_WORDS = {
    "optimal",
    "maximum",
    "necessary",
    "best",
    "perfect",
    "superior",
}

FATAL_LOG_MARKERS = (
    "script execution failed",
    "command execution failed",
    "cannot load",
    "could not load",
    "not enough memory",
    "segmentation fault",
)


class MonoLinearDenoiseError(RuntimeError):
    """Raised when the mono denoise stage cannot continue safely."""


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
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def json_dump_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{unique_id()}")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def inspect_fits(path: Path) -> FitsEvidence:
    if not path.is_file():
        raise MonoLinearDenoiseError(f"FITS file does not exist: {path}")
    with fits.open(path, memmap=True) as hdul:
        data = hdul[0].data
        header = hdul[0].header
        if data is None:
            raise MonoLinearDenoiseError(f"FITS contains no image data: {path}")
        array = np.asarray(data)
        if array.ndim == 2:
            height, width = array.shape
            channels = 1
        elif array.ndim == 3 and array.shape[0] == 1:
            _, height, width = array.shape
            channels = 1
        else:
            raise MonoLinearDenoiseError(
                f"Expected monochrome FITS data, found {array.shape}: {path}"
            )
        if array.dtype.kind != "f" or array.dtype.itemsize != 4:
            raise MonoLinearDenoiseError(
                f"Expected 32-bit floating-point FITS, found {array.dtype}: {path}"
            )
        finite = np.isfinite(array)
        finite_fraction = float(np.mean(finite))
        if finite_fraction == 0.0:
            minimum = maximum = median = math.nan
        else:
            values = array[finite]
            minimum = float(np.min(values))
            maximum = float(np.max(values))
            median = float(np.median(values))
        return FitsEvidence(
            path=str(path),
            sha256=sha256_file(path),
            size=path.stat().st_size,
            bitpix=int(header.get("BITPIX", 0)),
            dtype=str(array.dtype),
            channels=channels,
            width=int(width),
            height=int(height),
            finite_fraction=finite_fraction,
            minimum=minimum,
            maximum=maximum,
            median=median,
            filter_header=(
                str(header["FILTER"]) if "FILTER" in header else None
            ),
        )


def read_mono(path: Path) -> tuple[np.ndarray, fits.Header]:
    with fits.open(path, memmap=True) as hdul:
        array = np.asarray(hdul[0].data, dtype=np.float32)
        header = hdul[0].header.copy()
    if array.ndim == 3 and array.shape[0] == 1:
        array = array[0]
    if array.ndim != 2:
        raise MonoLinearDenoiseError(
            f"Expected mono image at {path}: {array.shape}"
        )
    return array, header


def project_paths(workspace: Path, project_name: str) -> dict[str, Path]:
    project = workspace / "Projects" / project_name
    processing = project / "processing"
    stable = processing / "mono-linear-denoise"
    return {
        "project": project,
        "upstream": processing / "mono-background-cleanup",
        "upstream_manifest": (
            processing
            / "mono-background-cleanup"
            / "mono-background-cleanup-manifest.json"
        ),
        "runs": project / ".siril-mono-linear-denoise",
        "stable": stable,
        "stable_manifest": stable / "mono-linear-denoise-manifest.json",
        "stable_review_record": stable / "visual-review-record.json",
    }


def source_path(paths: dict[str, Path], filter_name: str) -> Path:
    return paths["upstream"] / f"background-clean_{filter_name}.fit"


def stable_output(paths: dict[str, Path], filter_name: str) -> Path:
    return paths["stable"] / f"denoised_{filter_name}.fit"


def stable_contact(
    paths: dict[str, Path],
    filter_name: str,
    kind: str,
) -> Path:
    return paths["stable"] / f"{filter_name}-{kind}-contact.png"


def siril_version() -> dict[str, Any]:
    if not SIRIL_APP.is_file() or not os.access(SIRIL_APP, os.X_OK):
        raise MonoLinearDenoiseError(
            f"Siril AppRun is unavailable or not executable: {SIRIL_APP}"
        )
    environment = os.environ.copy()
    environment["APPDIR"] = str(SIRIL_APPDIR)
    completed = subprocess.run(
        [str(SIRIL_APP), "siril-cli", "--version"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        timeout=60,
        check=False,
    )
    combined = "\n".join(
        part for part in (completed.stdout, completed.stderr) if part
    ).strip()
    if completed.returncode != 0 or REQUIRED_SIRIL_VERSION not in combined:
        raise MonoLinearDenoiseError(
            f"Expected Siril {REQUIRED_SIRIL_VERSION}; got: {combined}"
        )
    return {
        "version": REQUIRED_SIRIL_VERSION,
        "version_output": combined,
        "path": str(SIRIL_APP),
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
    environment = os.environ.copy()
    environment["APPDIR"] = str(SIRIL_APPDIR)
    started = time.monotonic()
    timed_out = False
    try:
        completed = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            timeout=timeout_seconds,
            check=False,
        )
        returncode = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        returncode = 124
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")

    duration = time.monotonic() - started
    stdout_log.write_text(stdout, encoding="utf-8")
    stderr_log.write_text(stderr, encoding="utf-8")
    combined_lower = f"{stdout}\n{stderr}".lower()
    fatal = [
        marker for marker in FATAL_LOG_MARKERS
        if marker in combined_lower
    ]
    return {
        "command": command,
        "display_command": (
            f'env APPDIR="{SIRIL_APPDIR}" '
            + " ".join(f'"{part}"' for part in command)
        ),
        "exit_status": int(returncode),
        "duration_seconds": round(duration, 3),
        "timed_out": timed_out,
        "timeout_seconds": int(timeout_seconds),
        "fatal_log_markers": fatal,
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
    }


def validate_upstream(
    paths: dict[str, Path],
) -> tuple[dict[str, Any], dict[str, FitsEvidence]]:
    manifest_path = paths["upstream_manifest"]
    if not manifest_path.is_file():
        raise MonoLinearDenoiseError(
            f"Mono background-cleanup manifest is missing: {manifest_path}"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "ready":
        raise MonoLinearDenoiseError("Upstream manifest status is not ready.")
    if manifest.get("helper_version") != REQUIRED_UPSTREAM_HELPER_VERSION:
        raise MonoLinearDenoiseError(
            f"Expected upstream helper {REQUIRED_UPSTREAM_HELPER_VERSION}; "
            f"manifest reports {manifest.get('helper_version')}."
        )
    if manifest.get("project") != paths["project"].name:
        raise MonoLinearDenoiseError(
            "Upstream manifest belongs to another project."
        )
    if not manifest.get("visual_review_completed"):
        raise MonoLinearDenoiseError("Upstream visual review is incomplete.")
    if not manifest.get("mono_linear_denoise_permitted"):
        raise MonoLinearDenoiseError(
            "Upstream does not permit mono denoise."
        )

    records = manifest.get("outputs", {})
    evidence: dict[str, FitsEvidence] = {}
    dimensions: set[tuple[int, int]] = set()
    for filter_name in FILTERS:
        path = source_path(paths, filter_name)
        item = inspect_fits(path)
        record = records.get(filter_name, {})
        expected = record.get("sha256")
        if not expected or item.sha256 != expected:
            raise MonoLinearDenoiseError(
                f"{filter_name} source checksum does not match manifest."
            )
        record_path = record.get("path")
        if (
            record_path
            and Path(record_path).resolve() != path.resolve()
        ):
            raise MonoLinearDenoiseError(
                f"{filter_name} source path does not match manifest."
            )
        if item.finite_fraction != 1.0 or item.channels != 1:
            raise MonoLinearDenoiseError(
                f"{filter_name} source is not a finite mono FITS."
            )
        evidence[filter_name] = item
        dimensions.add((item.width, item.height))
    if len(dimensions) != 1:
        raise MonoLinearDenoiseError(
            "The three cleaned mono masters differ in size."
        )
    return manifest, evidence


def denoise_script_text(candidate: dict[str, Any]) -> str:
    if candidate["operation"] == "pass-through":
        raise MonoLinearDenoiseError(
            "Pass-through does not use a Siril denoise script."
        )
    return "\n".join(
        (
            f"requires {REQUIRED_SIRIL_VERSION}",
            'load "source.fit"',
            candidate["command"],
            'save "denoised.fit"',
            "close",
            "",
        )
    )


def contact_preview_script_text() -> str:
    lines = [f"requires {REQUIRED_SIRIL_VERSION}"]
    for kind in ("full", "background", "detail"):
        lines.extend(
            (
                f'load "{kind}-contact.fit"',
                "autostretch",
                (
                    f"resample -maxdim={CONTACT_PREVIEW_MAXDIM} "
                    "-interp=area"
                ),
                f'savepng "{kind}-contact"',
                "close",
            )
        )
    lines.append("")
    return "\n".join(lines)


def robust_noise_proxy(array: np.ndarray) -> float:
    channel = np.asarray(array, dtype=np.float64)
    finite_values = channel[np.isfinite(channel)]
    if finite_values.size < 100:
        return math.nan
    threshold = float(np.percentile(finite_values, 80.0))
    left = channel[::4, :-1:4]
    right = channel[::4, 1::4]
    valid = (
        np.isfinite(left)
        & np.isfinite(right)
        & (left <= threshold)
        & (right <= threshold)
    )
    differences = (right - left)[valid]
    if differences.size < 100:
        return math.nan
    centre = float(np.median(differences))
    mad = float(np.median(np.abs(differences - centre)))
    return 1.4826 * mad / math.sqrt(2.0)


def high_frequency_component(array: np.ndarray) -> np.ndarray:
    sample = array[::2, ::2].astype(np.float64, copy=False)
    centre = sample[1:-1, 1:-1]
    neighbours = (
        sample[:-2, 1:-1]
        + sample[2:, 1:-1]
        + sample[1:-1, :-2]
        + sample[1:-1, 2:]
    ) / 4.0
    return centre - neighbours


def correlation(a: np.ndarray, b: np.ndarray) -> float:
    valid = np.isfinite(a) & np.isfinite(b)
    av = a[valid].astype(np.float64, copy=False)
    bv = b[valid].astype(np.float64, copy=False)
    if av.size < 100:
        return math.nan
    av = av - float(np.mean(av))
    bv = bv - float(np.mean(bv))
    denominator = math.sqrt(
        float(np.dot(av, av)) * float(np.dot(bv, bv))
    )
    return (
        float(np.dot(av, bv) / denominator)
        if denominator > 0.0
        else math.nan
    )


def _masked_pair_correlation(
    first: np.ndarray,
    second: np.ndarray,
    mask: np.ndarray,
) -> float:
    valid = mask & np.isfinite(first) & np.isfinite(second)
    a = first[valid].astype(np.float64, copy=False)
    b = second[valid].astype(np.float64, copy=False)
    if a.size < 500:
        return math.nan
    a = a - float(np.mean(a))
    b = b - float(np.mean(b))
    denominator = math.sqrt(
        float(np.dot(a, a)) * float(np.dot(b, b))
    )
    return (
        float(np.dot(a, b) / denominator)
        if denominator > 0.0
        else math.nan
    )


def background_texture_metrics(
    source: np.ndarray,
    output: np.ndarray,
) -> dict[str, float]:
    source_sample = source[::2, ::2].astype(np.float64, copy=False)
    output_sample = output[::2, ::2].astype(np.float64, copy=False)
    source_centre = source_sample[1:-1, 1:-1]
    output_centre = output_sample[1:-1, 1:-1]

    source_hp = source_centre - (
        source_sample[:-2, 1:-1]
        + source_sample[2:, 1:-1]
        + source_sample[1:-1, :-2]
        + source_sample[1:-1, 2:]
    ) / 4.0
    output_hp = output_centre - (
        output_sample[:-2, 1:-1]
        + output_sample[2:, 1:-1]
        + output_sample[1:-1, :-2]
        + output_sample[1:-1, 2:]
    ) / 4.0

    finite_source = source_centre[np.isfinite(source_centre)]
    if finite_source.size < 1000:
        return {
            "source_texture_correlation": math.nan,
            "output_texture_correlation": math.nan,
            "texture_correlation_increase": math.nan,
            "residual_texture_correlation": math.nan,
            "background_high_frequency_ratio": math.nan,
        }

    signal_limit = float(np.percentile(finite_source, 65.0))
    source_gradient = np.abs(source_hp)
    finite_gradient = source_gradient[np.isfinite(source_gradient)]
    gradient_limit = float(np.percentile(finite_gradient, 65.0))
    base_mask = (
        np.isfinite(source_centre)
        & np.isfinite(output_centre)
        & np.isfinite(source_hp)
        & np.isfinite(output_hp)
        & (source_centre <= signal_limit)
        & (source_gradient <= gradient_limit)
    )

    def texture_correlation(hp: np.ndarray) -> float:
        values: list[float] = []
        for dy, dx in ((0, 1), (1, 0), (1, 1), (0, 2), (2, 0)):
            y_end = hp.shape[0] - dy if dy else hp.shape[0]
            x_end = hp.shape[1] - dx if dx else hp.shape[1]
            first = hp[:y_end, :x_end]
            second = hp[dy:, dx:]
            mask = (
                base_mask[:y_end, :x_end]
                & base_mask[dy:, dx:]
            )
            value = _masked_pair_correlation(first, second, mask)
            if math.isfinite(value):
                values.append(abs(value))
        return float(np.mean(values)) if values else math.nan

    source_corr = texture_correlation(source_hp)
    output_corr = texture_correlation(output_hp)
    residual_corr = texture_correlation(output_hp - source_hp)

    source_values = source_hp[base_mask]
    output_values = output_hp[base_mask]
    source_rms = (
        float(math.sqrt(float(np.mean(source_values * source_values))))
        if source_values.size
        else math.nan
    )
    output_rms = (
        float(math.sqrt(float(np.mean(output_values * output_values))))
        if output_values.size
        else math.nan
    )
    hf_ratio = (
        output_rms / source_rms
        if source_rms > 0.0
        else math.nan
    )

    return {
        "source_texture_correlation": source_corr,
        "output_texture_correlation": output_corr,
        "texture_correlation_increase": output_corr - source_corr,
        "residual_texture_correlation": residual_corr,
        "background_high_frequency_ratio": hf_ratio,
    }


def quality_assessment(
    source_path: Path,
    output_path: Path,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    source_evidence = inspect_fits(source_path)
    output_evidence = inspect_fits(output_path)
    source, _ = read_mono(source_path)
    output, _ = read_mono(output_path)
    failed: list[dict[str, Any]] = []

    def fail(metric: str, value: Any, requirement: str) -> None:
        failed.append(
            {
                "metric": metric,
                "value": value,
                "requirement": requirement,
            }
        )

    pass_through = candidate["operation"] == "pass-through"

    if source.shape != output.shape:
        fail(
            "shape",
            {"source": list(source.shape), "output": list(output.shape)},
            "must match",
        )
    if output_evidence.finite_fraction != 1.0:
        fail(
            "finite_fraction",
            output_evidence.finite_fraction,
            "must equal 1.0",
        )
    if pass_through:
        if source_evidence.sha256 != output_evidence.sha256:
            fail(
                "pass_through_sha256",
                output_evidence.sha256,
                "must exactly match source",
            )
    elif source_evidence.sha256 == output_evidence.sha256:
        fail(
            "output_sha256",
            output_evidence.sha256,
            "must differ from source",
        )

    sample_source = source[::3, ::3].astype(np.float64, copy=False)
    sample_output = output[::3, ::3].astype(np.float64, copy=False)
    valid = np.isfinite(sample_source) & np.isfinite(sample_output)
    sv = sample_source[valid]
    ov = sample_output[valid]
    source_low = float(np.percentile(sv, 0.5))
    source_high = float(np.percentile(sv, 99.5))
    span = max(source_high - source_low, 1.0e-12)
    delta = ov - sv
    relative_rms_change = float(
        math.sqrt(float(np.mean(delta * delta))) / span
    )
    relative_median_shift = float(
        abs(float(np.median(ov)) - float(np.median(sv))) / span
    )
    global_correlation = correlation(sample_source, sample_output)
    detail_correlation = correlation(
        high_frequency_component(source),
        high_frequency_component(output),
    )
    source_p99 = float(np.percentile(sv, 99.0))
    output_p99 = float(np.percentile(ov, 99.0))
    p99_retention = (
        output_p99 / source_p99
        if abs(source_p99) > 1.0e-12
        else math.nan
    )
    source_noise = robust_noise_proxy(source)
    output_noise = robust_noise_proxy(output)
    noise_ratio = (
        output_noise / source_noise
        if source_noise > 0.0
        else math.nan
    )
    texture = background_texture_metrics(source, output)

    source_low_clip = float(np.mean(source <= 0.0))
    output_low_clip = float(np.mean(output <= 0.0))
    source_high_clip = float(np.mean(source >= 1.0))
    output_high_clip = float(np.mean(output >= 1.0))
    low_clip_increase = max(
        0.0,
        output_low_clip - source_low_clip,
    )
    high_clip_increase = max(
        0.0,
        output_high_clip - source_high_clip,
    )
    minimum_floor = source_evidence.minimum - 0.05 * span

    thresholds = {
        "finite_fraction": 1.0,
        "minimum_global_correlation": 0.998,
        "minimum_detail_correlation": 0.99,
        "maximum_relative_rms_change": 0.06,
        "maximum_relative_median_shift": 0.02,
        "minimum_p99_retention": 0.95,
        "maximum_p99_retention": 1.05,
        "minimum_noise_ratio": 0.45,
        "maximum_noise_ratio": 1.02,
        "maximum_texture_correlation_increase": 0.08,
        "maximum_output_texture_correlation": 0.25,
        "maximum_residual_texture_correlation": 0.30,
        "maximum_low_clip_increase": 1.0e-6,
        "maximum_high_clip_increase": 1.0e-6,
        "minimum_output_value": minimum_floor,
    }

    if not pass_through:
        checks = (
            ("global_correlation", global_correlation, ">=", 0.998),
            ("detail_correlation", detail_correlation, ">=", 0.99),
            (
                "relative_rms_change",
                relative_rms_change,
                "<=",
                0.06,
            ),
            (
                "relative_median_shift",
                relative_median_shift,
                "<=",
                0.02,
            ),
            ("p99_retention", p99_retention, ">=", 0.95),
            ("p99_retention", p99_retention, "<=", 1.05),
            ("noise_ratio", noise_ratio, ">=", 0.45),
            ("noise_ratio", noise_ratio, "<=", 1.02),
            (
                "texture_correlation_increase",
                texture["texture_correlation_increase"],
                "<=",
                0.08,
            ),
            (
                "output_texture_correlation",
                texture["output_texture_correlation"],
                "<=",
                0.25,
            ),
            (
                "residual_texture_correlation",
                texture["residual_texture_correlation"],
                "<=",
                0.30,
            ),
            (
                "low_clip_increase",
                low_clip_increase,
                "<=",
                1.0e-6,
            ),
            (
                "high_clip_increase",
                high_clip_increase,
                "<=",
                1.0e-6,
            ),
            (
                "output_minimum",
                output_evidence.minimum,
                ">=",
                minimum_floor,
            ),
        )
        for metric, value, operator, limit in checks:
            bad = (not math.isfinite(value)) or (
                value < limit if operator == ">=" else value > limit
            )
            if bad:
                fail(metric, value, f"must be {operator} {limit}")

    satisfactory = not failed
    metrics = {
        "finite_fraction": output_evidence.finite_fraction,
        "global_correlation": global_correlation,
        "detail_correlation": detail_correlation,
        "relative_rms_change": relative_rms_change,
        "relative_median_shift": relative_median_shift,
        "p99_retention": p99_retention,
        "source_noise_proxy": source_noise,
        "output_noise_proxy": output_noise,
        "noise_ratio": noise_ratio,
        **texture,
        "source_low_clip_fraction": source_low_clip,
        "output_low_clip_fraction": output_low_clip,
        "low_clip_increase": low_clip_increase,
        "source_high_clip_fraction": source_high_clip,
        "output_high_clip_fraction": output_high_clip,
        "high_clip_increase": high_clip_increase,
        "source_minimum": source_evidence.minimum,
        "source_maximum": source_evidence.maximum,
        "output_minimum": output_evidence.minimum,
        "output_maximum": output_evidence.maximum,
        "source_robust_span": span,
    }
    return {
        "status": "satisfactory" if satisfactory else "rejected",
        "satisfactory": satisfactory,
        "pass_through": pass_through,
        "failed_checks": failed,
        "metrics": metrics,
        "thresholds": thresholds,
        "interpretation": (
            "Exact pass-through baseline; no denoise was applied."
            if pass_through
            else (
                "Gentle NL-Bayes passed structure, clipping, and "
                "correlated-texture safeguards."
                if satisfactory
                else (
                    "Candidate failed one or more structure, clipping, "
                    "noise-reduction, or correlated-texture safeguards."
                )
            )
        ),
    }


def metric_materiality_assessment(
    candidate: dict[str, Any],
    quality: dict[str, Any],
) -> dict[str, Any]:
    """Conservatively decide whether denoise may justify replacing source."""
    if candidate["operation"] == "pass-through":
        return {
            "materially_better": False,
            "eligible_for_nonpass_selection": False,
            "reason": "Pass-through is the default baseline.",
            "failed_checks": [],
            "thresholds": {},
        }

    metrics = quality["metrics"]
    thresholds = {
        "maximum_noise_ratio": 0.70,
        "minimum_global_correlation": 0.9999,
        "minimum_detail_correlation": 0.9995,
        "maximum_relative_rms_change": 0.008,
        "minimum_p99_retention": 0.995,
        "maximum_p99_retention": 1.005,
        "maximum_texture_correlation_increase": 0.0,
        "maximum_residual_texture_correlation": 0.12,
    }
    failed: list[dict[str, Any]] = []

    checks = (
        ("noise_ratio", metrics["noise_ratio"], "<=", 0.70),
        (
            "global_correlation",
            metrics["global_correlation"],
            ">=",
            0.9999,
        ),
        (
            "detail_correlation",
            metrics["detail_correlation"],
            ">=",
            0.9995,
        ),
        (
            "relative_rms_change",
            metrics["relative_rms_change"],
            "<=",
            0.008,
        ),
        ("p99_retention", metrics["p99_retention"], ">=", 0.995),
        ("p99_retention", metrics["p99_retention"], "<=", 1.005),
        (
            "texture_correlation_increase",
            metrics["texture_correlation_increase"],
            "<=",
            0.0,
        ),
        (
            "residual_texture_correlation",
            metrics["residual_texture_correlation"],
            "<=",
            0.12,
        ),
    )
    for metric, value, operator, limit in checks:
        bad = (not math.isfinite(value)) or (
            value < limit if operator == ">=" else value > limit
        )
        if bad:
            failed.append(
                {
                    "metric": metric,
                    "value": value,
                    "requirement": f"must be {operator} {limit}",
                }
            )

    materially_better = quality["satisfactory"] and not failed
    return {
        "materially_better": materially_better,
        "eligible_for_nonpass_selection": materially_better,
        "reason": (
            "Metrics clear the conservative material-improvement gate."
            if materially_better
            else (
                "Candidate may be technically acceptable but does not clear "
                "the conservative material-improvement gate."
            )
        ),
        "failed_checks": failed,
        "thresholds": thresholds,
    }


def selection_score(
    candidate: dict[str, Any],
    quality: dict[str, Any],
) -> float:
    """Provide a metric advisory only; pass-through remains the default."""
    if candidate["operation"] == "pass-through":
        return 0.0
    metrics = quality["metrics"]
    score = (
        float(metrics["noise_ratio"])
        + 10.0 * max(0.0, 1.0 - float(metrics["detail_correlation"]))
        + 5.0 * max(0.0, 1.0 - float(metrics["global_correlation"]))
        + 4.0 * max(0.0, float(metrics["texture_correlation_increase"]))
        + 2.0 * float(metrics["relative_rms_change"])
        + abs(float(metrics["p99_retention"]) - 1.0)
        + float(candidate["strength_penalty"])
    )
    if not quality["satisfactory"]:
        score += 1000.0
    return float(score)


def candidate_summary(item: dict[str, Any]) -> dict[str, Any]:
    metrics = item["quality_assessment"]["metrics"]
    materiality = item["metric_materiality_assessment"]
    return {
        "candidate": item["candidate"],
        "filter": item["filter"],
        "operation": item["method"]["operation"],
        "modulation": item["method"]["modulation"],
        "command": item["method"]["command"],
        "strength": item["method"]["strength"],
        "status": item["status"],
        "selection_score": item["selection_score"],
        "metric_materially_better": materiality["materially_better"],
        "materiality_failed_checks": materiality["failed_checks"],
        "noise_ratio": metrics["noise_ratio"],
        "global_correlation": metrics["global_correlation"],
        "detail_correlation": metrics["detail_correlation"],
        "relative_rms_change": metrics["relative_rms_change"],
        "p99_retention": metrics["p99_retention"],
        "source_texture_correlation": metrics[
            "source_texture_correlation"
        ],
        "output_texture_correlation": metrics[
            "output_texture_correlation"
        ],
        "texture_correlation_increase": metrics[
            "texture_correlation_increase"
        ],
        "residual_texture_correlation": metrics[
            "residual_texture_correlation"
        ],
        "source_sha256": item["source"]["sha256"],
        "output_sha256": item["output"]["sha256"],
    }


def recommended_candidate(
    items: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Default safely to pass-through before validated visual comparison."""
    for item in items:
        if (
            item["candidate"] == "candidate-00"
            and item["quality_assessment"]["satisfactory"]
        ):
            return item
    return None


def metric_best_candidate(
    items: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Return a denoise metric advisory only when material gate is cleared."""
    eligible = [
        item
        for item in items
        if item["metric_materiality_assessment"]["materially_better"]
    ]
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda item: (item["selection_score"], item["candidate"]),
    )

def execute_candidate(
    *,
    filter_name: str,
    source: Path,
    run_root: Path,
    candidate: dict[str, Any],
    timeout_seconds: int,
) -> dict[str, Any]:
    candidate_dir = run_root / filter_name / candidate["candidate"]
    work = candidate_dir / "work"
    logs = candidate_dir / "logs"
    work.mkdir(parents=True, exist_ok=False)
    logs.mkdir()
    working_source = work / "source.fit"
    output = work / "denoised.fit"
    shutil.copy2(source, working_source)

    if candidate["operation"] == "pass-through":
        shutil.copy2(working_source, output)
        denoise_script = None
        denoise_run = {
            "command": ["internal-pass-through-copy"],
            "display_command": "internal pass-through copy",
            "exit_status": 0,
            "duration_seconds": 0.0,
            "timed_out": False,
            "timeout_seconds": 0,
            "fatal_log_markers": [],
            "stdout_log": None,
            "stderr_log": None,
        }
    else:
        denoise_script = candidate_dir / "denoise.ssf"
        denoise_script.write_text(
            denoise_script_text(candidate),
            encoding="utf-8",
        )
        denoise_run = run_siril_script(
            directory=work,
            script=denoise_script,
            stdout_log=logs / "denoise-stdout.log",
            stderr_log=logs / "denoise-stderr.log",
            timeout_seconds=timeout_seconds,
        )
        if (
            denoise_run["exit_status"] != 0
            or denoise_run["timed_out"]
            or denoise_run["fatal_log_markers"]
        ):
            raise MonoLinearDenoiseError(
                f"Siril denoise failed for {filter_name} "
                f"{candidate['candidate']}; evidence preserved at "
                f"{candidate_dir}"
            )

    output_evidence = inspect_fits(output)
    quality = quality_assessment(
        working_source,
        output,
        candidate,
    )
    materiality = metric_materiality_assessment(candidate, quality)
    score = selection_score(candidate, quality)
    return {
        "candidate": candidate["candidate"],
        "filter": filter_name,
        "candidate_directory": str(candidate_dir),
        "created_at": utc_now(),
        "helper_version": VERSION,
        "method": {
            "algorithm": (
                "none"
                if candidate["operation"] == "pass-through"
                else "Siril NL-Bayes"
            ),
            "operation": candidate["operation"],
            "command": candidate["command"],
            "modulation": candidate["modulation"],
            "strength": candidate["strength"],
            "cosmetic_correction": (
                False
                if candidate["operation"] == "pass-through"
                else True
            ),
            "vst": False,
            "da3d": False,
            "sos": False,
            "independent_channels": False,
        },
        "source": asdict(inspect_fits(working_source)),
        "output": asdict(output_evidence),
        "denoise_script": (
            str(denoise_script) if denoise_script else None
        ),
        "denoise_script_sha256": (
            sha256_file(denoise_script) if denoise_script else None
        ),
        "denoise_run": denoise_run,
        "quality_assessment": quality,
        "metric_materiality_assessment": materiality,
        "selection_score": score,
        "status": (
            "satisfactory"
            if quality["satisfactory"]
            else "rejected"
        ),
    }


def choose_review_regions(
    source: np.ndarray,
    crop_size: int = 512,
) -> dict[str, dict[str, int]]:
    height, width = source.shape
    size = min(crop_size, height, width)
    stride = max(size // 2, 128)
    candidates: list[dict[str, Any]] = []

    y_positions = list(range(0, max(height - size + 1, 1), stride))
    x_positions = list(range(0, max(width - size + 1, 1), stride))
    if y_positions[-1] != height - size:
        y_positions.append(height - size)
    if x_positions[-1] != width - size:
        x_positions.append(width - size)

    for y in y_positions:
        for x in x_positions:
            tile = source[y:y + size:4, x:x + size:4]
            finite = tile[np.isfinite(tile)]
            if finite.size < 1000:
                continue
            median = float(np.median(finite))
            p10 = float(np.percentile(finite, 10.0))
            p90 = float(np.percentile(finite, 90.0))
            mad = 1.4826 * float(
                np.median(np.abs(finite - median))
            )
            outer = (
                x < 0.30 * width
                or x + size > 0.70 * width
                or y < 0.30 * height
                or y + size > 0.70 * height
            )
            candidates.append(
                {
                    "x": x,
                    "y": y,
                    "width": size,
                    "height": size,
                    "median": median,
                    "contrast": p90 - p10,
                    "mad": mad,
                    "outer": outer,
                }
            )

    if not candidates:
        raise MonoLinearDenoiseError(
            "Could not choose review crop regions."
        )

    outer_candidates = [
        item for item in candidates if item["outer"]
    ] or candidates
    background = min(
        outer_candidates,
        key=lambda item: (
            item["mad"] + item["contrast"] + 0.10 * item["median"]
        ),
    )
    detail = max(
        candidates,
        key=lambda item: (
            item["median"] + 0.50 * item["contrast"]
        ),
    )
    return {
        "background": {
            key: int(background[key])
            for key in ("x", "y", "width", "height")
        },
        "detail": {
            key: int(detail[key])
            for key in ("x", "y", "width", "height")
        },
    }


def contact_array(
    panels: list[np.ndarray],
    region: dict[str, int] | None,
) -> np.ndarray:
    extracted: list[np.ndarray] = []
    for panel in panels:
        if region is None:
            extracted.append(panel)
        else:
            x = region["x"]
            y = region["y"]
            width = region["width"]
            height = region["height"]
            extracted.append(panel[y:y + height, x:x + width])

    panel_height = extracted[0].shape[0]
    separator_width = max(8, panel_height // 100)
    finite = extracted[0][np.isfinite(extracted[0])]
    separator_value = (
        float(np.median(finite))
        if finite.size
        else 0.0
    )
    separator = np.full(
        (panel_height, separator_width),
        separator_value,
        dtype=np.float32,
    )
    pieces: list[np.ndarray] = []
    for index, panel in enumerate(extracted):
        if index:
            pieces.append(separator)
        pieces.append(np.asarray(panel, dtype=np.float32))
    return np.concatenate(pieces, axis=1)


def create_contact_previews(
    *,
    filter_name: str,
    source: Path,
    candidate_items: list[dict[str, Any]],
    review_root: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    filter_review = review_root / filter_name
    filter_review.mkdir(parents=True, exist_ok=False)
    logs = filter_review / "logs"
    logs.mkdir()

    source_array, source_header = read_mono(source)
    outputs = [
        read_mono(
            Path(item["candidate_directory"])
            / "work"
            / "denoised.fit"
        )[0]
        for item in candidate_items
    ]
    panels = [source_array, *outputs]
    regions = choose_review_regions(source_array)

    contact_fits: dict[str, Path] = {}
    contact_pngs: dict[str, Path] = {}
    for kind, region in (
        ("full", None),
        ("background", regions["background"]),
        ("detail", regions["detail"]),
    ):
        contact = contact_array(panels, region)
        header = source_header.copy()
        header["FILTER"] = f"{filter_name}_DenoiseReview"
        header.add_history(
            "Panel order: source | candidate-00 pass-through | "
            "candidate-01 mod 0.20 | candidate-02 mod 0.40"
        )
        fits_path = filter_review / f"{kind}-contact.fit"
        fits.PrimaryHDU(
            data=contact,
            header=header,
        ).writeto(
            fits_path,
            overwrite=False,
            output_verify="fix",
        )
        contact_fits[kind] = fits_path
        contact_pngs[kind] = filter_review / f"{kind}-contact.png"

    script = filter_review / "contact-previews.ssf"
    script.write_text(
        contact_preview_script_text(),
        encoding="utf-8",
    )
    run = run_siril_script(
        directory=filter_review,
        script=script,
        stdout_log=logs / "preview-stdout.log",
        stderr_log=logs / "preview-stderr.log",
        timeout_seconds=min(timeout_seconds, 1200),
    )
    missing = [
        str(path)
        for path in contact_pngs.values()
        if not path.is_file()
    ]
    if (
        run["exit_status"] != 0
        or run["timed_out"]
        or run["fatal_log_markers"]
        or missing
    ):
        raise MonoLinearDenoiseError(
            f"Contact preview generation failed for {filter_name}; "
            f"missing={missing}; evidence preserved at {filter_review}"
        )

    return {
        "filter": filter_name,
        "panel_order": [
            "source",
            "candidate-00",
            "candidate-01",
            "candidate-02",
        ],
        "regions": regions,
        "contact_fits": {
            kind: {
                "path": str(path),
                "sha256": sha256_file(path),
            }
            for kind, path in contact_fits.items()
        },
        "contact_previews": {
            kind: {
                "path": str(path),
                "sha256": sha256_file(path),
            }
            for kind, path in contact_pngs.items()
        },
        "preview_script": str(script),
        "preview_script_sha256": sha256_file(script),
        "preview_run": run,
    }


def build_review_template(
    *,
    project_name: str,
    run_root: Path,
    candidates: dict[str, list[dict[str, Any]]],
    review_evidence: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    filters: dict[str, Any] = {}
    for filter_name in FILTERS:
        candidate_rows = []
        for item in candidates[filter_name]:
            is_pass = item["candidate"] == "candidate-00"
            candidate_rows.append(
                {
                    "candidate": item["candidate"],
                    "technical_status": item["status"],
                    "metric_materially_better": item[
                        "metric_materiality_assessment"
                    ]["materially_better"],
                    "accepted": is_pass,
                    "material_improvement": False,
                    "improved_views": [],
                    "worse_views": [],
                    "indistinguishable_views": [],
                    "background_naturalness": "same" if is_pass else "",
                    "detail_preservation": "same" if is_pass else "",
                    "artifact_flags": ["none"] if is_pass else [],
                    "benefit_description": (
                        "Exact pass-through baseline; no processing benefit "
                        "is claimed."
                        if is_pass
                        else ""
                    ),
                    "observations": "",
                }
            )
        filters[filter_name] = {
            "previews": {
                kind: {**record, "inspected": False}
                for kind, record in review_evidence[
                    filter_name
                ]["contact_previews"].items()
            },
            "panel_order": review_evidence[filter_name]["panel_order"],
            "candidates": candidate_rows,
            "selected_candidate": "candidate-00",
            "selection_rationale": "",
            "comparison_outcome": "ambiguous_or_no_material_gain",
        }
    return {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "project": project_name,
        "run_root": str(run_root),
        "reviewer": "CodeWarrior",
        "reviewed_at": "",
        "instructions": (
            "Open all nine contact PNGs. Pass-through is the default. A "
            "denoise candidate may replace it only when metrics clear the "
            "material gate and the image is visibly better in at least two "
            "of full, background, and detail views, with no worse view."
        ),
        "filters": filters,
    }


def write_review_files(
    *,
    run_record: dict[str, Any],
    review_root: Path,
) -> dict[str, str]:
    summaries = {
        filter_name: [
            candidate_summary(item)
            for item in run_record["candidates"][filter_name]
        ]
        for filter_name in FILTERS
    }

    summary_path = review_root / "decision-summary.json"
    json_dump_atomic(
        summary_path,
        {
            "schema_version": 4,
            "created_at": utc_now(),
            "helper_version": VERSION,
            "project": run_record["project_name"],
            "run_root": run_record["run_root"],
            "status": run_record["status"],
            "default_recommendations": run_record[
                "recommended_candidates"
            ],
            "metric_best_candidates": run_record[
                "metric_best_candidates"
            ],
            "satisfactory_candidates": run_record[
                "satisfactory_candidates"
            ],
            "candidate_summaries": summaries,
            "review_evidence": run_record["review_evidence"],
        },
    )

    lines = [
        "# Mono linear denoise — material-improvement review",
        "",
        f"Project: `{run_record['project_name']}`  ",
        f"Run root: `{run_record['run_root']}`  ",
        f"Helper: `{VERSION}`",
        "",
        "The default decision is pass-through candidate-00.",
        "",
        "A denoise candidate may replace pass-through only when:",
        "",
        "- it clears the conservative metric materiality gate;",
        "- it is visibly better in at least two of full/background/detail;",
        "- it is not worse in any view;",
        "- background naturalness is better;",
        "- detail preservation is same or better;",
        "- the improvement is specific and clearly visible.",
        "",
        "Close, mixed, ambiguous, or merely different results must select",
        "candidate-00. Less measured noise alone is not sufficient.",
        "",
        "Do not use unsupported terms such as optimal, maximum, necessary,",
        "best, perfect, or superior in the review record.",
        "",
        "Each contact sheet panel order is:",
        "source | pass-through | mod 0.20 | mod 0.40",
        "",
    ]
    for filter_name in FILTERS:
        lines.extend(
            (
                f"## {filter_name}",
                "",
                "Default recommendation: `candidate-00`",
                (
                    "Metric advisory: `"
                    f"{run_record['metric_best_candidates'][filter_name]}`"
                ),
                "",
                (
                    "| Candidate | Status | Metric material gain | Noise "
                    "ratio | Detail corr | RMS change | Score |"
                ),
                "|---|---:|---:|---:|---:|---:|---:|",
            )
        )
        for item in summaries[filter_name]:
            lines.append(
                "| {candidate} | {status} | {metric_materially_better} | "
                "{noise_ratio:.6g} | {detail_correlation:.6g} | "
                "{relative_rms_change:.6g} | {selection_score:.6g} |".format(
                    **item
                )
            )
        lines.append("")
        evidence = run_record["review_evidence"][filter_name]
        for kind in REVIEW_VIEWS:
            record = evidence["contact_previews"][kind]
            lines.append(
                f"- {kind.title()} contact: `{record['path']}` "
                f"(SHA-256 `{record['sha256']}`)"
            )
        lines.append("")

    lines.extend(
        (
            "## Required autonomous next actions",
            "",
            "1. Open and inspect all nine contact PNGs.",
            "2. Complete every comparison field in the review template.",
            "3. Record improved, worse, and indistinguishable views.",
            "4. Select pass-through unless every non-pass gate is met.",
            "5. Run record-review, publish, and status.",
            "",
            "Do not ask Peter or ChatGPT to choose candidates.",
        )
    )

    brief_path = review_root / "decision-brief.md"
    brief_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    template = build_review_template(
        project_name=run_record["project_name"],
        run_root=Path(run_record["run_root"]),
        candidates=run_record["candidates"],
        review_evidence=run_record["review_evidence"],
    )
    template_path = review_root / "visual-review-template.json"
    json_dump_atomic(template_path, template)
    bundle_path = review_root / "review-bundle.json"
    json_dump_atomic(
        bundle_path,
        {
            "schema_version": 4,
            "created_at": utc_now(),
            "project": run_record["project_name"],
            "run_root": run_record["run_root"],
            "decision_brief": str(brief_path),
            "decision_summary": str(summary_path),
            "visual_review_template": str(template_path),
            "review_evidence": run_record["review_evidence"],
        },
    )
    archive_path = review_root / "mono-linear-denoise-review.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in (brief_path, summary_path, template_path, bundle_path):
            archive.write(path, path.name)
        for filter_name in FILTERS:
            evidence = run_record["review_evidence"][filter_name]
            for kind in REVIEW_VIEWS:
                path = Path(evidence["contact_previews"][kind]["path"])
                archive.write(path, f"{filter_name}/{path.name}")
    return {
        "decision_brief": str(brief_path),
        "decision_summary": str(summary_path),
        "visual_review_template": str(template_path),
        "review_bundle": str(bundle_path),
        "review_archive": str(archive_path),
        "review_archive_sha256": sha256_file(archive_path),
    }

def current_canonical_state(paths: dict[str, Path]) -> dict[str, Any]:
    if not paths["stable_manifest"].is_file():
        return {
            "status": "missing",
            "sho_combination_permitted": False,
        }
    try:
        manifest = json.loads(
            paths["stable_manifest"].read_text(encoding="utf-8")
        )
    except Exception as exc:
        return {
            "status": "invalid",
            "error": str(exc),
            "sho_combination_permitted": False,
        }
    version = manifest.get("helper_version")
    if version != VERSION:
        return {
            "status": "obsolete",
            "manifest_helper_version": version,
            "required_helper_version": VERSION,
            "reason": (
                "Existing result predates the material-improvement gate. "
                "Technically acceptable denoise is not enough; version "
                "1.0.3 requires clear benefit over pass-through."
            ),
            "sho_combination_permitted": False,
        }
    return {
        "status": manifest.get("status", "invalid"),
        "manifest_helper_version": version,
        "sho_combination_permitted": bool(
            manifest.get("sho_combination_permitted")
        ),
    }


def run_project(
    *,
    workspace: Path,
    project_name: str,
    timeout_seconds: int,
    fresh_run: bool,
) -> dict[str, Any]:
    paths = project_paths(workspace, project_name)
    existing_state = current_canonical_state(paths)
    if paths["stable"].exists() and not fresh_run:
        raise MonoLinearDenoiseError(
            f"Canonical mono-linear-denoise directory exists: "
            f"{paths['stable']}. Use --fresh-run to preserve it while "
            "generating a replacement."
        )

    siril = siril_version()
    upstream_manifest, sources = validate_upstream(paths)
    run_root = paths["runs"] / f"denoise-{unique_id()}"
    review_root = run_root / "compact-review"
    run_root.mkdir(parents=True, exist_ok=False)
    review_root.mkdir()

    candidates: dict[str, list[dict[str, Any]]] = {}
    recommendations: dict[str, str | None] = {}
    metric_best: dict[str, str | None] = {}
    satisfactory: dict[str, list[str]] = {}
    review_evidence: dict[str, dict[str, Any]] = {}

    for filter_name in FILTERS:
        items = [
            execute_candidate(
                filter_name=filter_name,
                source=Path(sources[filter_name].path),
                run_root=run_root,
                candidate=candidate,
                timeout_seconds=timeout_seconds,
            )
            for candidate in CANDIDATES
        ]
        candidates[filter_name] = items
        recommended = recommended_candidate(items)
        recommendations[filter_name] = (
            recommended["candidate"] if recommended else None
        )
        metric_candidate = metric_best_candidate(items)
        metric_best[filter_name] = (
            metric_candidate["candidate"] if metric_candidate else None
        )
        satisfactory[filter_name] = [
            item["candidate"]
            for item in items
            if item["quality_assessment"]["satisfactory"]
        ]
        review_evidence[filter_name] = create_contact_previews(
            filter_name=filter_name,
            source=Path(sources[filter_name].path),
            candidate_items=items,
            review_root=review_root,
            timeout_seconds=timeout_seconds,
        )

    ready_for_review = all(
        satisfactory[filter_name] for filter_name in FILTERS
    )
    status = (
        "awaiting_visual_selection"
        if ready_for_review
        else "blocked"
    )
    run_record = {
        "schema_version": 2,
        "status": status,
        "created_at": utc_now(),
        "helper_version": VERSION,
        "project": str(paths["project"]),
        "project_name": project_name,
        "run_root": str(run_root),
        "fresh_run_requested": fresh_run,
        "existing_canonical_state_at_start": existing_state,
        "upstream_manifest": str(paths["upstream_manifest"]),
        "upstream_manifest_sha256": sha256_file(
            paths["upstream_manifest"]
        ),
        "upstream_helper_version": upstream_manifest.get(
            "helper_version"
        ),
        "sources": {
            name: asdict(item) for name, item in sources.items()
        },
        "candidate_methods": list(CANDIDATES),
        "candidates": candidates,
        "candidate_summaries": {
            name: [
                candidate_summary(item)
                for item in candidates[name]
            ]
            for name in FILTERS
        },
        "recommended_candidates": recommendations,
        "metric_best_candidates": metric_best,
        "satisfactory_candidates": satisfactory,
        "review_evidence": review_evidence,
        "siril": siril,
        "canonical_output_changed": False,
        "visual_selection_required": True,
        "visual_review_recorded": False,
        "sho_combination_permitted": False,
    }
    run_manifest_path = run_root / "run-manifest.json"
    json_dump_atomic(run_manifest_path, run_record)
    review_files = write_review_files(
        run_record=run_record,
        review_root=review_root,
    )
    run_record["review_files"] = review_files
    json_dump_atomic(run_manifest_path, run_record)

    return {
        "status": status,
        "helper_version": VERSION,
        "project": project_name,
        "run_root": str(run_root),
        "existing_canonical_state_at_start": existing_state,
        "source_sha256": {
            name: item.sha256 for name, item in sources.items()
        },
        "satisfactory_candidates": satisfactory,
        "recommended_candidates": recommendations,
        "metric_best_candidates": metric_best,
        "contact_preview_count": 9,
        **review_files,
        "canonical_output_changed": False,
        "visual_review_recorded": False,
        "sho_combination_permitted": False,
        "next_action": (
            "CodeWarrior must open all nine contact previews, complete "
            "the structured review JSON, run record-review, publish the "
            "validated selections, and run status in this same session."
        ),
    }


def _contains_banned_confidence_word(text: str) -> str | None:
    lowered = text.lower()
    for word in sorted(BANNED_CONFIDENCE_WORDS):
        if word in lowered:
            return word
    return None


def validate_review_payload(
    *,
    project_name: str,
    run_root: Path,
    run_record: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    errors: list[str] = []
    if payload.get("schema_version") != REVIEW_SCHEMA_VERSION:
        errors.append("Review schema_version is invalid.")
    if payload.get("project") != project_name:
        errors.append("Review project does not match.")
    if Path(str(payload.get("run_root", ""))).resolve() != run_root:
        errors.append("Review run_root does not match.")
    if payload.get("reviewer") != "CodeWarrior":
        errors.append("Reviewer must be CodeWarrior.")
    if not str(payload.get("reviewed_at", "")).strip():
        errors.append("reviewed_at is required.")

    filters_payload = payload.get("filters", {})
    selections: dict[str, str] = {}
    material_candidates: dict[str, list[str]] = {}
    for filter_name in FILTERS:
        review = filters_payload.get(filter_name)
        if not isinstance(review, dict):
            errors.append(f"Missing {filter_name} review.")
            continue

        expected_previews = run_record["review_evidence"][filter_name][
            "contact_previews"
        ]
        supplied_previews = review.get("previews", {})
        for kind in REVIEW_VIEWS:
            supplied = supplied_previews.get(kind, {})
            expected = expected_previews[kind]
            if not supplied.get("inspected"):
                errors.append(
                    f"{filter_name} {kind} preview was not marked inspected."
                )
            if (
                Path(str(supplied.get("path", ""))).resolve()
                != Path(expected["path"]).resolve()
            ):
                errors.append(
                    f"{filter_name} {kind} preview path does not match."
                )
            if supplied.get("sha256") != expected["sha256"]:
                errors.append(
                    f"{filter_name} {kind} preview checksum does not match."
                )

        supplied_candidates = review.get("candidates", [])
        supplied_by_name = {
            item.get("candidate"): item
            for item in supplied_candidates
            if isinstance(item, dict)
        }
        expected_names = {
            item["candidate"]
            for item in run_record["candidates"][filter_name]
        }
        if set(supplied_by_name) != expected_names:
            errors.append(
                f"{filter_name} candidate review set is incomplete."
            )

        qualified: list[str] = []
        for candidate_item in run_record["candidates"][filter_name]:
            name = candidate_item["candidate"]
            supplied = supplied_by_name.get(name, {})
            observations = str(supplied.get("observations", "")).strip()
            if len(observations) < 60:
                errors.append(
                    f"{filter_name} {name} observations must contain "
                    "at least 60 characters."
                )
            banned = _contains_banned_confidence_word(observations)
            if banned:
                errors.append(
                    f"{filter_name} {name} observations use banned "
                    f"confidence word {banned!r}."
                )
            flags = supplied.get("artifact_flags", [])
            if not isinstance(flags, list) or not flags:
                errors.append(
                    f"{filter_name} {name} artifact_flags are required."
                )
                flags = []
            unknown = set(flags) - ALLOWED_VISUAL_FLAGS
            if unknown:
                errors.append(
                    f"{filter_name} {name} has unknown flags: "
                    f"{sorted(unknown)}"
                )
            accepted = bool(supplied.get("accepted"))
            if accepted and set(flags) & SEVERE_VISUAL_FLAGS:
                errors.append(
                    f"{filter_name} {name} cannot be accepted with "
                    "severe visual artifact flags."
                )
            if accepted and not candidate_item["quality_assessment"][
                "satisfactory"
            ]:
                errors.append(
                    f"{filter_name} {name} is technically rejected."
                )

            improved = supplied.get("improved_views", [])
            worse = supplied.get("worse_views", [])
            indist = supplied.get("indistinguishable_views", [])
            for field_name, values in (
                ("improved_views", improved),
                ("worse_views", worse),
                ("indistinguishable_views", indist),
            ):
                if not isinstance(values, list):
                    errors.append(
                        f"{filter_name} {name} {field_name} must be a list."
                    )
                    values = []
                unknown_views = set(values) - set(REVIEW_VIEWS)
                if unknown_views:
                    errors.append(
                        f"{filter_name} {name} {field_name} has invalid "
                        f"views: {sorted(unknown_views)}"
                    )
            if (
                set(improved) & set(worse)
                or set(improved) & set(indist)
                or set(worse) & set(indist)
            ):
                errors.append(
                    f"{filter_name} {name} view classifications overlap."
                )

            background = str(
                supplied.get("background_naturalness", "")
            ).strip()
            detail = str(supplied.get("detail_preservation", "")).strip()
            if background not in COMPARISON_VALUES:
                errors.append(
                    f"{filter_name} {name} background_naturalness must be "
                    "better, same, or worse."
                )
            if detail not in COMPARISON_VALUES:
                errors.append(
                    f"{filter_name} {name} detail_preservation must be "
                    "better, same, or worse."
                )
            benefit = str(
                supplied.get("benefit_description", "")
            ).strip()
            if len(benefit) < 60:
                errors.append(
                    f"{filter_name} {name} benefit_description must contain "
                    "at least 60 characters."
                )
            banned = _contains_banned_confidence_word(benefit)
            if banned:
                errors.append(
                    f"{filter_name} {name} benefit_description uses banned "
                    f"confidence word {banned!r}."
                )

            claimed_material = bool(supplied.get("material_improvement"))
            if name == "candidate-00":
                if claimed_material or improved or worse:
                    errors.append(
                        f"{filter_name} pass-through cannot claim material "
                        "improvement or worse/improved views."
                    )
                continue

            metric_ok = candidate_item[
                "metric_materiality_assessment"
            ]["materially_better"]
            visually_qualified = (
                claimed_material
                and metric_ok
                and len(set(improved)) >= 2
                and not worse
                and background == "better"
                and detail in {"same", "better"}
                and accepted
                and not (set(flags) & SEVERE_VISUAL_FLAGS)
            )
            if claimed_material and not metric_ok:
                errors.append(
                    f"{filter_name} {name} cannot claim material improvement "
                    "because the metric materiality gate failed."
                )
            if claimed_material and len(set(improved)) < 2:
                errors.append(
                    f"{filter_name} {name} must improve at least two views."
                )
            if claimed_material and worse:
                errors.append(
                    f"{filter_name} {name} cannot be material with a worse view."
                )
            if claimed_material and background != "better":
                errors.append(
                    f"{filter_name} {name} must improve background naturalness."
                )
            if claimed_material and detail == "worse":
                errors.append(
                    f"{filter_name} {name} cannot reduce detail."
                )
            if visually_qualified:
                qualified.append(name)

        material_candidates[filter_name] = qualified
        selected = str(review.get("selected_candidate", "")).strip()
        if selected not in expected_names:
            errors.append(f"{filter_name} selected_candidate is invalid.")
        else:
            selected_review = supplied_by_name.get(selected, {})
            if not selected_review.get("accepted"):
                errors.append(
                    f"{filter_name} selected candidate was not accepted."
                )
            if selected != "candidate-00" and selected not in qualified:
                errors.append(
                    f"{filter_name} non-pass selection did not clear all "
                    "material-improvement gates."
                )
            if not qualified and selected != "candidate-00":
                errors.append(
                    f"{filter_name} must select pass-through because no "
                    "denoise candidate is materially better."
                )
            selections[filter_name] = selected

        outcome = str(review.get("comparison_outcome", "")).strip()
        allowed_outcomes = {
            "clear_material_gain",
            "ambiguous_or_no_material_gain",
            "mixed_result",
        }
        if outcome not in allowed_outcomes:
            errors.append(
                f"{filter_name} comparison_outcome is invalid."
            )
        if selected == "candidate-00" and outcome == "clear_material_gain":
            errors.append(
                f"{filter_name} pass-through conflicts with clear gain outcome."
            )
        if selected != "candidate-00" and outcome != "clear_material_gain":
            errors.append(
                f"{filter_name} denoise selection requires clear gain outcome."
            )

        rationale = str(review.get("selection_rationale", "")).strip()
        if len(rationale) < 60:
            errors.append(
                f"{filter_name} selection_rationale must contain at least "
                "60 characters."
            )
        banned = _contains_banned_confidence_word(rationale)
        if banned:
            errors.append(
                f"{filter_name} selection_rationale uses banned confidence "
                f"word {banned!r}."
            )

    if errors:
        raise MonoLinearDenoiseError(
            "Visual review record is invalid: " + " | ".join(errors)
        )

    validated = json.loads(json.dumps(payload))
    validated["validated_at"] = utc_now()
    validated["validated_by_helper_version"] = VERSION
    validated["visual_review_completed"] = True
    validated["selected_candidates"] = selections
    validated["material_improvement_candidates"] = material_candidates
    validated["review_evidence_sha256"] = {
        filter_name: {
            kind: run_record["review_evidence"][filter_name][
                "contact_previews"
            ][kind]["sha256"]
            for kind in REVIEW_VIEWS
        }
        for filter_name in FILTERS
    }
    return validated

def record_review(
    *,
    workspace: Path,
    project_name: str,
    run_root: Path,
    review_json: Path,
) -> dict[str, Any]:
    _ = workspace
    run_manifest_path = run_root / "run-manifest.json"
    if not run_manifest_path.is_file():
        raise MonoLinearDenoiseError(
            f"Run manifest is missing: {run_manifest_path}"
        )
    run_record = json.loads(
        run_manifest_path.read_text(encoding="utf-8")
    )
    if run_record.get("helper_version") != VERSION:
        raise MonoLinearDenoiseError(
            "Only version 1.0.3 candidate runs may be reviewed."
        )
    if run_record.get("project_name") != project_name:
        raise MonoLinearDenoiseError(
            "Candidate run belongs to another project."
        )
    if run_record.get("canonical_output_changed"):
        raise MonoLinearDenoiseError(
            "This candidate run has already been published."
        )
    if not review_json.is_file():
        raise MonoLinearDenoiseError(
            f"Review JSON is missing: {review_json}"
        )

    payload = json.loads(review_json.read_text(encoding="utf-8"))
    validated = validate_review_payload(
        project_name=project_name,
        run_root=run_root,
        run_record=run_record,
        payload=payload,
    )
    record_path = run_root / "visual-review-record.json"
    json_dump_atomic(record_path, validated)
    run_record["visual_review_recorded"] = True
    run_record["visual_review_record"] = str(record_path)
    run_record["visual_review_record_sha256"] = sha256_file(
        record_path
    )
    run_record["selected_candidates"] = validated[
        "selected_candidates"
    ]
    json_dump_atomic(run_manifest_path, run_record)

    return {
        "status": "visual_review_recorded",
        "helper_version": VERSION,
        "project": project_name,
        "run_root": str(run_root),
        "visual_review_record": str(record_path),
        "visual_review_record_sha256": sha256_file(record_path),
        "selected_candidates": validated["selected_candidates"],
        "next_action": (
            "Publish this exact validated review record, then run status."
        ),
    }


def validate_record_file(
    *,
    project_name: str,
    run_root: Path,
    run_record: dict[str, Any],
    record_path: Path,
) -> dict[str, Any]:
    if not record_path.is_file():
        raise MonoLinearDenoiseError(
            f"Validated review record is missing: {record_path}"
        )
    expected_path = run_record.get("visual_review_record")
    if (
        not expected_path
        or Path(expected_path).resolve() != record_path.resolve()
    ):
        raise MonoLinearDenoiseError(
            "Review record path does not match the recorded review."
        )
    expected_sha = run_record.get("visual_review_record_sha256")
    actual_sha = sha256_file(record_path)
    if not expected_sha or actual_sha != expected_sha:
        raise MonoLinearDenoiseError(
            "Review record checksum does not match."
        )
    record = json.loads(record_path.read_text(encoding="utf-8"))
    validated = validate_review_payload(
        project_name=project_name,
        run_root=run_root,
        run_record=run_record,
        payload=record,
    )
    if validated["selected_candidates"] != record.get(
        "selected_candidates"
    ):
        raise MonoLinearDenoiseError(
            "Review-record selections are inconsistent."
        )
    return record


def publish_project(
    *,
    workspace: Path,
    project_name: str,
    run_root: Path,
    review_record: Path,
    fresh_run: bool,
) -> dict[str, Any]:
    paths = project_paths(workspace, project_name)
    run_manifest_path = run_root / "run-manifest.json"
    if not run_manifest_path.is_file():
        raise MonoLinearDenoiseError(
            f"Run manifest is missing: {run_manifest_path}"
        )
    run_record = json.loads(
        run_manifest_path.read_text(encoding="utf-8")
    )
    if run_record.get("helper_version") != VERSION:
        raise MonoLinearDenoiseError(
            "Only version 1.0.3 candidate runs may publish."
        )
    if run_record.get("project_name") != project_name:
        raise MonoLinearDenoiseError("Run belongs to another project.")
    if run_record.get("canonical_output_changed"):
        raise MonoLinearDenoiseError(
            "This run has already been published."
        )
    if not run_record.get("visual_review_recorded"):
        raise MonoLinearDenoiseError(
            "Structured visual review was not recorded."
        )

    record = validate_record_file(
        project_name=project_name,
        run_root=run_root,
        run_record=run_record,
        record_path=review_record,
    )
    selections = record["selected_candidates"]

    upstream_manifest, sources = validate_upstream(paths)
    for filter_name in FILTERS:
        if (
            sources[filter_name].sha256
            != run_record["sources"][filter_name]["sha256"]
        ):
            raise MonoLinearDenoiseError(
                f"{filter_name} source changed after generation."
            )

    selected: dict[str, dict[str, Any]] = {}
    recommendations: dict[str, str | None] = {}
    metric_best: dict[str, str | None] = {}
    for filter_name in FILTERS:
        matches = [
            item
            for item in run_record["candidates"][filter_name]
            if item["candidate"] == selections[filter_name]
        ]
        if len(matches) != 1:
            raise MonoLinearDenoiseError(
                f"{filter_name} selection is not unique."
            )
        item = matches[0]
        if not item["quality_assessment"]["satisfactory"]:
            raise MonoLinearDenoiseError(
                f"Rejected {filter_name} candidate cannot publish."
            )
        selected[filter_name] = item
        recommended = recommended_candidate(
            run_record["candidates"][filter_name]
        )
        recommendations[filter_name] = (
            recommended["candidate"] if recommended else None
        )
        metric_candidate = metric_best_candidate(
            run_record["candidates"][filter_name]
        )
        metric_best[filter_name] = (
            metric_candidate["candidate"] if metric_candidate else None
        )

    existing = paths["stable"].exists()
    if existing and not fresh_run:
        raise MonoLinearDenoiseError(
            f"Canonical directory exists: {paths['stable']}. "
            "Use --fresh-run for preservation-safe replacement."
        )

    staging = run_root / "publish-staging"
    if staging.exists():
        raise MonoLinearDenoiseError(
            f"Publish staging exists: {staging}"
        )
    staging.mkdir(parents=True, exist_ok=False)

    final_outputs: dict[str, dict[str, Any]] = {}
    final_contacts: dict[str, dict[str, str]] = {}
    for filter_name in FILTERS:
        item = selected[filter_name]
        candidate_output = (
            Path(item["candidate_directory"])
            / "work"
            / "denoised.fit"
        )
        staged_output = staging / f"denoised_{filter_name}.fit"
        shutil.copy2(candidate_output, staged_output)
        evidence = inspect_fits(staged_output)
        if evidence.sha256 != item["output"]["sha256"]:
            raise MonoLinearDenoiseError(
                f"{filter_name} checksum changed during staging."
            )
        output_record = asdict(evidence)
        output_record["path"] = str(
            stable_output(paths, filter_name)
        )
        output_record["denoise_applied"] = (
            item["method"]["operation"] != "pass-through"
        )
        output_record["selected_modulation"] = item[
            "method"
        ]["modulation"]
        final_outputs[filter_name] = output_record

        final_contacts[filter_name] = {}
        for kind in ("full", "background", "detail"):
            source_contact = Path(
                run_record["review_evidence"][filter_name][
                    "contact_previews"
                ][kind]["path"]
            )
            staged_contact = (
                staging / f"{filter_name}-{kind}-contact.png"
            )
            shutil.copy2(source_contact, staged_contact)
            final_contacts[filter_name][kind] = str(
                stable_contact(paths, filter_name, kind)
            )

    staged_review = staging / "visual-review-record.json"
    shutil.copy2(review_record, staged_review)
    staged_review_sha = sha256_file(staged_review)

    previous = (
        run_root / "previous-processing-mono-linear-denoise"
        if existing
        else None
    )
    if previous is not None and previous.exists():
        raise MonoLinearDenoiseError(
            f"Preservation destination exists: {previous}"
        )

    manifest = {
        "schema_version": 2,
        "created_at": utc_now(),
        "status": "ready",
        "helper_version": VERSION,
        "project": project_name,
        "project_path": str(paths["project"]),
        "stage_order": {
            "upstream": "siril-mono-background-cleanup",
            "current": "siril-mono-linear-denoise",
            "downstream": "siril-sho-combination",
        },
        "upstream_manifest": str(paths["upstream_manifest"]),
        "upstream_manifest_sha256": sha256_file(
            paths["upstream_manifest"]
        ),
        "upstream_helper_version": upstream_manifest.get(
            "helper_version"
        ),
        "sources": {
            name: asdict(item) for name, item in sources.items()
        },
        "candidate_methods": list(CANDIDATES),
        "candidate_summaries": run_record["candidate_summaries"],
        "recommended_candidates": recommendations,
        "metric_best_candidates": metric_best,
        "material_improvement_candidates": record.get(
            "material_improvement_candidates", {}
        ),
        "selected_candidates": selections,
        "selected_candidate_was_recommended": {
            name: selections[name] == recommendations[name]
            for name in FILTERS
        },
        "visual_review": {
            "required": True,
            "reviewer": "CodeWarrior",
            "record_path": str(paths["stable_review_record"]),
            "record_sha256": staged_review_sha,
            "structured_candidate_reviews": True,
            "all_contact_previews_inspected": True,
        },
        "selected_records": selected,
        "outputs": final_outputs,
        "contact_previews": final_contacts,
        "stable_paths": {
            "directory": str(paths["stable"]),
            "manifest": str(paths["stable_manifest"]),
            "visual_review_record": str(
                paths["stable_review_record"]
            ),
            "outputs": {
                name: str(stable_output(paths, name))
                for name in FILTERS
            },
        },
        "previous_processing_mono_linear_denoise_preserved_at": (
            str(previous) if previous is not None else None
        ),
        "publication_method": (
            "default to pass-through, require conservative metric and "
            "two-of-three-view material-improvement gates for any denoise "
            "selection, preserve existing canonical output, then atomically "
            "publish"
        ),
        "siril": siril_version(),
        "visual_review_completed": True,
        "sho_combination_permitted": True,
    }
    json_dump_atomic(
        staging / "mono-linear-denoise-manifest.json",
        manifest,
    )

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

    run_record["status"] = "published"
    run_record["published_at"] = utc_now()
    run_record["canonical_output_changed"] = True
    run_record["selected_candidates"] = selections
    run_record["sho_combination_permitted"] = True
    json_dump_atomic(run_manifest_path, run_record)

    status = status_project(workspace, project_name)
    if status.get("status") != "ready":
        raise MonoLinearDenoiseError(
            f"Post-publication status verification failed: {status}"
        )

    result = {
        "status": "ready",
        "helper_version": VERSION,
        "project": str(paths["project"]),
        "run_root": str(run_root),
        "selected_candidates": selections,
        "recommended_candidates": recommendations,
        "metric_best_candidates": metric_best,
        "material_improvement_candidates": record.get(
            "material_improvement_candidates", {}
        ),
        "outputs": {
            name: {
                "path": final_outputs[name]["path"],
                "sha256": final_outputs[name]["sha256"],
                "denoise_applied": final_outputs[name][
                    "denoise_applied"
                ],
                "selected_modulation": final_outputs[name][
                    "selected_modulation"
                ],
            }
            for name in FILTERS
        },
        "stable_directory": str(paths["stable"]),
        "stable_manifest": str(paths["stable_manifest"]),
        "visual_review_record": str(
            paths["stable_review_record"]
        ),
        "previous_processing_mono_linear_denoise_preserved_at": (
            manifest[
                "previous_processing_mono_linear_denoise_preserved_at"
            ]
        ),
        "canonical_output_changed": True,
        "visual_review_completed": True,
        "sho_combination_permitted": True,
        "post_publication_status_verified": True,
    }
    json_dump_atomic(
        run_root / "publication-result.json",
        result,
    )
    return result


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
            "errors": [],
            "visual_review_completed": False,
            "sho_combination_permitted": False,
        }

    manifest = json.loads(
        paths["stable_manifest"].read_text(encoding="utf-8")
    )
    errors: list[str] = []
    manifest_version = manifest.get("helper_version")
    if manifest_version != VERSION:
        errors.append(
            f"Canonical manifest helper {manifest_version!r} is obsolete; "
            f"version {VERSION} is required."
        )

    outputs: dict[str, dict[str, Any]] = {}
    for filter_name in FILTERS:
        path = stable_output(paths, filter_name)
        if not path.is_file():
            errors.append(
                f"Missing {filter_name} output: {path}"
            )
        else:
            current = asdict(inspect_fits(path))
            expected = (
                manifest.get("outputs", {})
                .get(filter_name, {})
                .get("sha256")
            )
            if expected and current["sha256"] != expected:
                errors.append(
                    f"{filter_name} output checksum mismatch."
                )
            outputs[filter_name] = current

        for kind in ("full", "background", "detail"):
            contact = stable_contact(
                paths,
                filter_name,
                kind,
            )
            if manifest_version == VERSION and not contact.is_file():
                errors.append(f"Missing contact preview: {contact}")

    review_path = paths["stable_review_record"]
    review_sha = (
        manifest.get("visual_review", {})
        .get("record_sha256")
    )
    if manifest_version == VERSION:
        if not review_path.is_file():
            errors.append(
                f"Missing visual review record: {review_path}"
            )
        elif not review_sha or sha256_file(review_path) != review_sha:
            errors.append("Visual review record checksum mismatch.")

    ready = (
        manifest_version == VERSION
        and manifest.get("status") == "ready"
        and manifest.get("visual_review_completed") is True
        and manifest.get("sho_combination_permitted") is True
        and not errors
    )
    status = (
        "ready"
        if ready
        else (
            "obsolete"
            if manifest_version != VERSION
            else "invalid"
        )
    )
    return {
        "status": status,
        "helper_version": VERSION,
        "canonical_manifest_helper_version": manifest_version,
        "project": str(paths["project"]),
        "stable_directory": str(paths["stable"]),
        "stable_manifest": str(paths["stable_manifest"]),
        "errors": errors,
        "selected_candidates": manifest.get(
            "selected_candidates"
        ),
        "recommended_candidates": manifest.get(
            "recommended_candidates"
        ),
        "outputs": {
            name: {
                "path": outputs[name]["path"],
                "sha256": outputs[name]["sha256"],
                "width": outputs[name]["width"],
                "height": outputs[name]["height"],
                "bitpix": outputs[name]["bitpix"],
            }
            for name in outputs
        },
        "visual_review_completed": (
            manifest.get("visual_review_completed") is True
            and manifest_version == VERSION
            and not errors
        ),
        "sho_combination_permitted": ready,
    }


def write_synthetic_fits(path: Path) -> None:
    rng = np.random.default_rng(20260806)
    height = width = 640
    yy, xx = np.mgrid[0:height, 0:width]
    image = (
        0.004
        + 0.009
        * np.exp(
            -(
                ((xx - 335.0) / 125.0) ** 2
                + ((yy - 315.0) / 105.0) ** 2
            )
        )
    )
    for cy, cx, amplitude in (
        (100, 120, 0.10),
        (500, 515, 0.08),
        (185, 500, 0.05),
    ):
        image += amplitude * np.exp(
            -(
                (xx - cx) ** 2 + (yy - cy) ** 2
            ) / (2.0 * 2.0**2)
        )
    image += rng.normal(
        0.0,
        0.00018,
        (height, width),
    )
    image = np.clip(
        image,
        0.0001,
        0.5,
    ).astype(np.float32)
    header = fits.Header()
    header["FILTER"] = "Ha"
    header["OBJECT"] = "Synthetic mono linear-denoise safety self-test"
    fits.PrimaryHDU(
        data=image,
        header=header,
    ).writeto(
        path,
        overwrite=False,
        output_verify="fix",
    )


def self_test(timeout_seconds: int) -> dict[str, Any]:
    siril = siril_version()
    root = (
        WORKSPACE
        / ".skill-self-tests"
        / "siril-mono-linear-denoise"
        / unique_id()
    )
    root.mkdir(parents=True, exist_ok=False)
    source = root / "synthetic-background-clean-Ha.fit"
    write_synthetic_fits(source)

    items = [
        execute_candidate(
            filter_name="Ha",
            source=source,
            run_root=root,
            candidate=candidate,
            timeout_seconds=timeout_seconds,
        )
        for candidate in CANDIDATES
    ]
    review_root = root / "compact-review"
    review_root.mkdir()
    evidence = create_contact_previews(
        filter_name="Ha",
        source=source,
        candidate_items=items,
        review_root=review_root,
        timeout_seconds=timeout_seconds,
    )

    failures: list[dict[str, Any]] = []
    if items[0]["output"]["sha256"] != items[0]["source"]["sha256"]:
        failures.append(
            {"candidate-00": "pass-through checksum differs"}
        )
    for item in items[1:]:
        run = item["denoise_run"]
        if (
            run["exit_status"] != 0
            or run["timed_out"]
            or run["fatal_log_markers"]
        ):
            failures.append(
                {
                    "candidate": item["candidate"],
                    "denoise_run": run,
                }
            )
    for kind in ("full", "background", "detail"):
        if not Path(
            evidence["contact_previews"][kind]["path"]
        ).is_file():
            failures.append({"missing_contact": kind})

    if failures:
        raise MonoLinearDenoiseError(
            f"Self-test failed {failures}; evidence preserved at {root}"
        )

    return {
        "status": "success",
        "created_at": utc_now(),
        "helper_version": VERSION,
        "self_test_directory": str(root),
        "siril": siril,
        "candidate_count": len(items),
        "candidates": [
            candidate_summary(item) for item in items
        ],
        "recommended_candidate": (
            recommended_candidate(items)["candidate"]
            if recommended_candidate(items)
            else None
        ),
        "metric_best_candidate": (
            metric_best_candidate(items)["candidate"]
            if metric_best_candidate(items)
            else None
        ),
        "contact_preview_count": 3,
        "tests": [
            "exact pass-through baseline",
            "real Siril NL-Bayes modulation 0.20",
            "real Siril NL-Bayes modulation 0.40",
            "default cosmetic correction retained for NL-Bayes",
            "mono 32-bit FITS preservation",
            "correlated-texture safeguards",
            "material-improvement metric gate",
            "pass-through default recommendation",
            "same-scale four-panel full-frame contact",
            "same-scale four-panel background crop",
            "same-scale four-panel detail crop",
            "all evidence preserved",
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Material-improvement-gated Siril mono denoise with pass-through, "
            "gentle candidates, texture safeguards, and structured review."
        )
    )
    parser.add_argument("--version", action="version", version=VERSION)
    subparsers = parser.add_subparsers(dest="command")

    self_test_parser = subparsers.add_parser("self-test")
    self_test_parser.add_argument("--timeout", type=int, default=1800)

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--project", required=True)
    run_parser.add_argument("--timeout", type=int, default=7200)
    run_parser.add_argument("--fresh-run", action="store_true")

    record_parser = subparsers.add_parser("record-review")
    record_parser.add_argument("--project", required=True)
    record_parser.add_argument("--run-root", required=True, type=Path)
    record_parser.add_argument("--review-json", required=True, type=Path)

    publish_parser = subparsers.add_parser("publish")
    publish_parser.add_argument("--project", required=True)
    publish_parser.add_argument("--run-root", required=True, type=Path)
    publish_parser.add_argument(
        "--review-record",
        required=True,
        type=Path,
    )
    publish_parser.add_argument("--fresh-run", action="store_true")

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--project", required=True)
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
                workspace=WORKSPACE,
                project_name=args.project,
                timeout_seconds=args.timeout,
                fresh_run=args.fresh_run,
            )
        elif args.command == "record-review":
            payload = record_review(
                workspace=WORKSPACE,
                project_name=args.project,
                run_root=args.run_root.resolve(),
                review_json=args.review_json.resolve(),
            )
        elif args.command == "publish":
            payload = publish_project(
                workspace=WORKSPACE,
                project_name=args.project,
                run_root=args.run_root.resolve(),
                review_record=args.review_record.resolve(),
                fresh_run=args.fresh_run,
            )
        elif args.command == "status":
            payload = status_project(
                WORKSPACE,
                args.project,
            )
        else:
            raise MonoLinearDenoiseError(
                f"Unsupported command: {args.command}"
            )
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
        in (
            "success",
            "ready",
            "awaiting_visual_selection",
            "visual_review_recorded",
            "obsolete",
            "missing",
        )
        else 2
    )


if __name__ == "__main__":
    raise SystemExit(main())
