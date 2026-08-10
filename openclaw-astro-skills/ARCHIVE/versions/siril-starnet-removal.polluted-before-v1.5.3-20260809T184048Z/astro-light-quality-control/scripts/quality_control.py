#!/usr/bin/env python3
"""
Deterministic light-frame quality-control helper.

This tool has two deliberately separate phases:

1. analyze:
   - Reads every direct-child FITS light.
   - Computes checksums, FITS metadata, robust statistics.
   - Runs Siril deterministically for star measurements and an autostretched
     preview.
   - Writes reports and a decision template.
   - Never moves or deletes a light.

2. review-summary:
   - Reuses an existing successful analysis manifest.
   - Produces compact session and evidence-group summaries.
   - Separates mixed sessions by deterministic analysis evidence.
   - Selects bounded representative previews for every evidence group.
   - Creates a small evidence-group review-plan template.
   - Does not rerun Siril or modify light frames.

3. build-decisions:
   - Expands a completed evidence-group review plan into a full checksum-bound
     decisions file covering every analyzed frame.
   - Allows precise per-file overrides for genuine exceptions.
   - Supports legacy homogeneous session plans from helper 1.1.
   - Does not move or delete light frames.

4. apply:
   - Consumes a completed decisions JSON produced from an analysis run.
   - Verifies filenames and SHA-256 checksums.
   - Moves only entries explicitly marked reject.
   - Never overwrites an existing reject.
   - Deletes only a newly recopied direct-child duplicate when an identical
     reject is already safely preserved.

The helper never decides artistic quality by itself. It produces deterministic
evidence and fixed outlier flags for review by the calling agent.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import html
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

VERSION = "1.2.0"
FITS_SUFFIXES = {".fit", ".fits", ".fts"}
VALID_FILTERS = ("Ha", "SII", "OIII")
VALID_DECISIONS = {"accept", "reject", "needs_review"}

DEFAULT_SIRIL_ROOT = Path(
    "/home/peter/.openclaw/runtime/siril-processor/toolchain/"
    ".toolchain/siril/1.4.4/squashfs-root"
)
REQUIRED_SIRIL_VERSION = "1.4.4"


class QCError(RuntimeError):
    """Controlled quality-control failure."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def run_id() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def json_dump_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def sha256_file(path: Path, chunk_size: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def sanitize_project_name(name: str) -> str:
    if not name or not name.strip():
        raise QCError("Project name cannot be empty.")
    if name != name.strip():
        raise QCError("Project name must be supplied exactly without outer whitespace.")
    if "/" in name or "\\" in name or ".." in name:
        raise QCError("Project name contains prohibited path elements.")
    if name.startswith("."):
        raise QCError("Project name cannot begin with a dot.")
    return name


def normalize_filter(value: str) -> list[str]:
    normalized = value.strip().lower()
    aliases = {
        "ha": "Ha",
        "h-alpha": "Ha",
        "halpha": "Ha",
        "sii": "SII",
        "s2": "SII",
        "oiii": "OIII",
        "o3": "OIII",
    }
    if normalized == "all":
        return list(VALID_FILTERS)
    if normalized not in aliases:
        raise QCError(f"Unsupported filter: {value!r}")
    return [aliases[normalized]]


def derive_workspace(script_file: Path, override: str | None) -> Path:
    if override:
        workspace = Path(override).expanduser().resolve()
    else:
        # <workspace>/skills/<skill>/scripts/quality_control.py
        resolved = script_file.resolve()
        try:
            skill_dir = resolved.parent.parent
            skills_dir = skill_dir.parent
            workspace = skills_dir.parent
        except IndexError as exc:
            raise QCError("Could not derive owning agent workspace.") from exc
        if skills_dir.name != "skills":
            raise QCError(
                "Helper is not installed beneath "
                "<agent workspace>/skills/<skill>/scripts."
            )
    if not workspace.is_dir():
        raise QCError(f"Workspace does not exist: {workspace}")
    return workspace


def project_paths(workspace: Path, project_name: str, filter_name: str) -> dict[str, Path]:
    projects_root = workspace / "Projects"
    project = projects_root / project_name
    processing = project / "processing" / filter_name
    lights = processing / "lights"
    qc_root = project / ".astro-light-quality-control" / filter_name
    rejects = lights / "rejects"
    return {
        "projects_root": projects_root,
        "project": project,
        "processing": processing,
        "lights": lights,
        "qc_root": qc_root,
        "rejects": rejects,
    }


def verify_prepared_lights(paths: dict[str, Path]) -> None:
    if not paths["project"].is_dir():
        raise QCError(f"Exact project directory not found: {paths['project']}")
    if not paths["processing"].is_dir():
        raise QCError(f"Prepared filter directory not found: {paths['processing']}")
    if not paths["lights"].exists():
        raise QCError(f"Prepared lights path not found: {paths['lights']}")
    if not paths["lights"].is_dir():
        raise QCError(f"Prepared lights path is not a directory: {paths['lights']}")
    if not os.access(paths["lights"], os.R_OK | os.X_OK):
        raise QCError(f"Prepared lights path is not readable: {paths['lights']}")


def direct_fits_files(lights: Path) -> list[Path]:
    candidates: list[Path] = []
    for entry in lights.iterdir():
        if entry.is_file() and entry.suffix.lower() in FITS_SUFFIXES:
            candidates.append(entry)
    return sorted(candidates, key=lambda item: item.name)


def safe_direct_child(lights: Path, candidate: Path) -> bool:
    return candidate.parent == lights and candidate.name not in {"", ".", ".."}


def safe_siril_text(value: str) -> str:
    if any(char in value for char in ('"', "\r", "\n", "\x00")):
        raise QCError(f"Path cannot be represented safely in a Siril script: {value!r}")
    return value


def robust_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    return result


def header_value(header: Any, *names: str) -> Any:
    for name in names:
        if name in header:
            value = header.get(name)
            if value is not None:
                return value
    return None


def read_fits_evidence(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        import numpy as np
        from astropy.io import fits
    except ImportError as exc:
        raise QCError(
            "Astropy and NumPy are required. Run this helper with the "
            "AstroProcessor virtual-environment Python."
        ) from exc

    metadata: dict[str, Any] = {}
    stats: dict[str, Any] = {}
    # ASIAIR unsigned 16-bit FITS commonly use BZERO/BSCALE. Astropy cannot
    # expose scaled image data through a memory map, so open without memmap.
    with fits.open(path, memmap=False, do_not_scale_image_data=False) as hdul:
        if not hdul:
            raise QCError("FITS file contains no HDU.")
        hdu = hdul[0]
        header = hdu.header
        data = hdu.data
        if data is None:
            raise QCError("FITS primary HDU contains no image data.")

        array = np.asarray(data)
        array = np.squeeze(array)
        if array.ndim != 2:
            raise QCError(f"Expected a 2-D monochrome image, found shape {array.shape!r}.")

        metadata = {
            "DATE-OBS": header_value(header, "DATE-OBS", "DATEOBS"),
            "FILTER": header_value(header, "FILTER"),
            "EXPTIME": header_value(header, "EXPTIME", "EXPOSURE"),
            "GAIN": header_value(header, "GAIN", "EGAIN"),
            "OFFSET": header_value(header, "OFFSET"),
            "CCD-TEMP": header_value(header, "CCD-TEMP", "CCD_TEMP", "SENSOR_TEMP"),
            "XBINNING": header_value(header, "XBINNING", "BINX"),
            "YBINNING": header_value(header, "YBINNING", "BINY"),
            "NAXIS1": header.get("NAXIS1"),
            "NAXIS2": header.get("NAXIS2"),
        }

        height, width = array.shape
        stride = max(1, int(math.ceil(max(height, width) / 1024)))
        sample = np.asarray(array[::stride, ::stride], dtype=np.float64).ravel()
        finite = sample[np.isfinite(sample)]
        nonfinite_fraction = 1.0 - (finite.size / sample.size)

        if finite.size == 0:
            raise QCError("Image has no finite sampled pixels.")

        percentiles = np.percentile(
            finite,
            [0.1, 1.0, 10.0, 50.0, 90.0, 99.0, 99.9],
        )
        median = float(percentiles[3])
        mad = float(np.median(np.abs(finite - median)))
        robust_noise = 1.4826 * mad
        minimum = float(np.min(finite))
        maximum = float(np.max(finite))
        min_fraction = float(np.mean(finite == minimum))
        max_fraction = float(np.mean(finite == maximum))

        stats = {
            "shape": [int(height), int(width)],
            "sample_stride": stride,
            "sample_count": int(sample.size),
            "finite_sample_count": int(finite.size),
            "nonfinite_fraction": nonfinite_fraction,
            "minimum": minimum,
            "maximum": maximum,
            "p0_1": float(percentiles[0]),
            "p1": float(percentiles[1]),
            "p10": float(percentiles[2]),
            "median": median,
            "p90": float(percentiles[4]),
            "p99": float(percentiles[5]),
            "p99_9": float(percentiles[6]),
            "mad": mad,
            "robust_noise": robust_noise,
            "robust_range": float(percentiles[6] - percentiles[0]),
            "minimum_fraction": min_fraction,
            "maximum_fraction": max_fraction,
        }

    return metadata, stats


def parse_filename_hints(filename: str) -> dict[str, Any]:
    angle_match = re.search(r"_(\d+(?:\.\d+)?)deg_", filename, re.IGNORECASE)
    sequence_match = re.search(r"_(\d{4,})\.(?:fit|fits|fts)$", filename, re.IGNORECASE)
    timestamp_match = re.search(r"_(\d{8}-\d{6})_", filename)
    return {
        "rotator_angle_hint": (
            robust_float(angle_match.group(1)) if angle_match else None
        ),
        "sequence_number_hint": (
            int(sequence_match.group(1)) if sequence_match else None
        ),
        "filename_timestamp_hint": (
            timestamp_match.group(1) if timestamp_match else None
        ),
    }


def parse_date_obs(value: Any) -> dt.datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def assign_session_groups(records: list[dict[str, Any]]) -> None:
    sortable: list[tuple[dt.datetime, str, dict[str, Any]]] = []
    fallback = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    for record in records:
        observed = parse_date_obs(record.get("metadata", {}).get("DATE-OBS"))
        sortable.append((observed or fallback, record["filename"], record))
    sortable.sort(key=lambda item: (item[0], item[1]))

    session_number = 0
    previous_time: dt.datetime | None = None
    previous_sequence: int | None = None
    previous_angle: float | None = None

    for observed, _filename, record in sortable:
        hints = record.get("filename_hints", {})
        sequence_number = hints.get("sequence_number_hint")
        angle = hints.get("rotator_angle_hint")

        new_session = previous_time is None
        if previous_time is not None and observed != fallback:
            if (observed - previous_time).total_seconds() > 15 * 60:
                new_session = True
        if (
            previous_sequence is not None
            and sequence_number is not None
            and sequence_number <= previous_sequence
        ):
            new_session = True
        if (
            previous_angle is not None
            and angle is not None
            and abs(angle - previous_angle) > 30
        ):
            new_session = True

        if new_session:
            session_number += 1

        record["session_group"] = f"session-{session_number:02d}"
        if observed != fallback:
            previous_time = observed
        previous_sequence = sequence_number
        previous_angle = angle


def siril_command(siril_root: Path, *args: str) -> tuple[list[str], dict[str, str]]:
    app_run = siril_root / "AppRun"
    if not app_run.is_file():
        raise QCError(f"Siril AppRun not found: {app_run}")
    if not os.access(app_run, os.X_OK):
        raise QCError(f"Siril AppRun is not executable: {app_run}")
    command = [str(app_run), "siril-cli", *args]
    environment = os.environ.copy()
    environment["APPDIR"] = str(siril_root)
    return command, environment


def check_siril(siril_root: Path, timeout: int = 60) -> dict[str, Any]:
    command, environment = siril_command(siril_root, "--version")
    completed = subprocess.run(
        command,
        env=environment,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    combined = "\n".join(filter(None, (completed.stdout, completed.stderr))).strip()
    if completed.returncode != 0:
        raise QCError(
            f"Siril version check failed with exit status {completed.returncode}: "
            f"{combined}"
        )
    if REQUIRED_SIRIL_VERSION not in combined:
        raise QCError(
            f"Expected Siril {REQUIRED_SIRIL_VERSION}; version output was: {combined}"
        )
    return {
        "command": command,
        "exit_status": completed.returncode,
        "version_output": combined,
    }


def csv_numeric_values(rows: list[dict[str, str]], patterns: Sequence[str]) -> list[float]:
    if not rows:
        return []
    keys = list(rows[0].keys())
    chosen: str | None = None
    for key in keys:
        normalized = re.sub(r"[^a-z0-9]", "", key.lower())
        if any(pattern in normalized for pattern in patterns):
            chosen = key
            break
    if chosen is None:
        return []
    values: list[float] = []
    for row in rows:
        value = robust_float(row.get(chosen))
        if value is not None:
            values.append(value)
    return values


def parse_star_csv(path: Path, combined_log: str) -> dict[str, Any]:
    rows: list[dict[str, str]] = []
    parse_error: str | None = None
    if path.is_file() and path.stat().st_size > 0:
        try:
            with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
                sample = handle.read(4096)
                handle.seek(0)
                try:
                    dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
                except csv.Error:
                    dialect = csv.excel
                reader = csv.DictReader(handle, dialect=dialect)
                rows = [
                    {str(k): str(v) for k, v in row.items() if k is not None}
                    for row in reader
                    if row
                ]
        except Exception as exc:
            parse_error = str(exc)

    count = len(rows)
    if count == 0:
        matches = re.findall(
            r"Found\s+(\d+)\s+(?:Gaussian profile )?stars?",
            combined_log,
            flags=re.IGNORECASE,
        )
        if matches:
            count = int(matches[-1])

    fwhm_values = csv_numeric_values(rows, ("fwhm",))
    roundness_values = csv_numeric_values(rows, ("roundness", "round"))
    if not roundness_values and rows:
        fwhm_x = csv_numeric_values(rows, ("fwhmx",))
        fwhm_y = csv_numeric_values(rows, ("fwhmy",))
        if len(fwhm_x) == len(fwhm_y) and fwhm_x:
            for x_value, y_value in zip(fwhm_x, fwhm_y):
                maximum = max(abs(x_value), abs(y_value))
                if maximum > 0:
                    roundness_values.append(
                        min(abs(x_value), abs(y_value)) / maximum
                    )

    return {
        "star_count": count,
        "median_fwhm": (
            statistics.median(fwhm_values) if fwhm_values else None
        ),
        "median_roundness": (
            statistics.median(roundness_values) if roundness_values else None
        ),
        "csv_rows": len(rows),
        "csv_parse_error": parse_error,
    }


def run_siril_analysis(
    source: Path,
    attempt_dir: Path,
    siril_root: Path,
    timeout: int,
) -> dict[str, Any]:
    attempt_dir.mkdir(parents=True, exist_ok=False)
    script_path = attempt_dir / "analyze.ssf"
    star_csv = attempt_dir / "stars.csv"
    preview_base = attempt_dir / "preview"
    preview_png = attempt_dir / "preview.png"
    stdout_path = attempt_dir / "stdout.log"
    stderr_path = attempt_dir / "stderr.log"

    quoted_source = safe_siril_text(str(source))
    quoted_star_csv = safe_siril_text(str(star_csv))
    quoted_preview = safe_siril_text(str(preview_base))

    script = "\n".join(
        [
            f"requires {REQUIRED_SIRIL_VERSION}",
            "setfindstar reset",
            f'load "{quoted_source}"',
            f'findstar "-out={quoted_star_csv}"',
            "autostretch -linked",
            f'savepng "{quoted_preview}"',
            "close",
            "",
        ]
    )
    write_text_atomic(script_path, script)

    command, environment = siril_command(
        siril_root,
        "--directory",
        str(attempt_dir),
        "--script",
        str(script_path),
    )
    started = time.monotonic()
    timed_out = False
    try:
        completed = subprocess.run(
            command,
            env=environment,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        return_code = completed.returncode
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        return_code = None
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        stderr += f"\nTimed out after {timeout} seconds."
    duration = time.monotonic() - started

    write_text_atomic(stdout_path, stdout)
    write_text_atomic(stderr_path, stderr)

    if not preview_png.is_file():
        png_candidates = sorted(attempt_dir.glob("preview*.png"))
        if png_candidates:
            preview_png = png_candidates[0]

    combined_log = "\n".join((stdout, stderr))
    stars = parse_star_csv(star_csv, combined_log)
    fatal_patterns = (
        "Script execution failed",
        "command is not found",
        "cannot open",
        "No image is loaded",
    )
    fatal_log = any(pattern.lower() in combined_log.lower() for pattern in fatal_patterns)

    success = (
        not timed_out
        and return_code == 0
        and preview_png.is_file()
        and preview_png.stat().st_size > 0
        and not fatal_log
    )

    return {
        "success": success,
        "timed_out": timed_out,
        "exit_status": return_code,
        "duration_seconds": round(duration, 3),
        "command": command,
        "script_path": str(script_path),
        "script_sha256": sha256_file(script_path),
        "stdout_log": str(stdout_path),
        "stderr_log": str(stderr_path),
        "star_csv": str(star_csv) if star_csv.exists() else None,
        "preview_path": str(preview_png) if preview_png.exists() else None,
        "fatal_log_pattern": fatal_log,
        **stars,
    }


def median_and_mad(values: Iterable[float]) -> tuple[float | None, float | None]:
    cleaned = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    if not cleaned:
        return None, None
    median = statistics.median(cleaned)
    mad = statistics.median(abs(value - median) for value in cleaned)
    return median, mad


def add_fixed_flags(records: list[dict[str, Any]]) -> None:
    groups: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        key_parts = [
            str(record.get("metadata", {}).get("FILTER")),
            str(record.get("metadata", {}).get("NAXIS1")),
            str(record.get("metadata", {}).get("NAXIS2")),
            str(record.get("metadata", {}).get("XBINNING")),
            str(record.get("metadata", {}).get("YBINNING")),
            str(record.get("metadata", {}).get("EXPTIME")),
            str(record.get("metadata", {}).get("GAIN")),
            str(record.get("metadata", {}).get("OFFSET")),
        ]
        key = "|".join(key_parts)
        record["comparison_group"] = key
        groups.setdefault(key, []).append(record)

    for group_records in groups.values():
        star_median, star_mad = median_and_mad(
            record.get("siril", {}).get("star_count") for record in group_records
        )
        fwhm_median, fwhm_mad = median_and_mad(
            record.get("siril", {}).get("median_fwhm") for record in group_records
        )
        round_median, round_mad = median_and_mad(
            record.get("siril", {}).get("median_roundness") for record in group_records
        )

        for record in group_records:
            flags: list[str] = []
            if record.get("analysis_error"):
                flags.append("analysis_error")
            siril = record.get("siril", {})
            stats = record.get("statistics", {})
            if not siril.get("success"):
                flags.append("siril_analysis_failed")
            if stats.get("nonfinite_fraction", 0) > 0:
                flags.append("contains_nonfinite_pixels")
            robust_range = robust_float(stats.get("robust_range"))
            noise = robust_float(stats.get("robust_noise"))
            if robust_range is not None and noise is not None:
                if robust_range <= max(1e-12, 3.0 * noise):
                    flags.append("possible_blank_or_flat_frame")
            star_count = robust_float(siril.get("star_count"))
            if star_count is not None and star_median is not None and star_median > 0:
                low_limit = max(3.0, 0.15 * star_median)
                if star_mad is not None and star_mad > 0:
                    low_limit = min(low_limit, star_median - 4.0 * star_mad)
                if star_count < max(1.0, low_limit):
                    flags.append("very_low_star_count")
            fwhm = robust_float(siril.get("median_fwhm"))
            if (
                fwhm is not None
                and fwhm_median is not None
                and fwhm_mad is not None
                and fwhm_mad > 0
                and fwhm > fwhm_median + 4.0 * fwhm_mad
            ):
                flags.append("high_fwhm_outlier")
            roundness = robust_float(siril.get("median_roundness"))
            if roundness is not None:
                threshold = 0.25
                if (
                    round_median is not None
                    and round_mad is not None
                    and round_mad > 0
                ):
                    threshold = min(threshold, round_median - 4.0 * round_mad)
                if roundness < max(0.0, threshold):
                    flags.append("low_roundness_outlier")
            record["fixed_review_flags"] = sorted(set(flags))
            record["suggested_review"] = bool(flags)


def write_metrics_csv(path: Path, records: list[dict[str, Any]]) -> None:
    fields = [
        "filename",
        "sha256",
        "size",
        "session_group",
        "date_obs",
        "filter",
        "exptime",
        "gain",
        "offset",
        "temperature",
        "naxis1",
        "naxis2",
        "star_count",
        "median_fwhm",
        "median_roundness",
        "median",
        "robust_noise",
        "robust_range",
        "nonfinite_fraction",
        "preview_path",
        "analysis_success",
        "suggested_review",
        "fixed_review_flags",
        "analysis_error",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for record in records:
            metadata = record.get("metadata", {})
            stats = record.get("statistics", {})
            siril = record.get("siril", {})
            writer.writerow(
                {
                    "filename": record.get("filename"),
                    "sha256": record.get("sha256"),
                    "size": record.get("size"),
                    "session_group": record.get("session_group"),
                    "date_obs": metadata.get("DATE-OBS"),
                    "filter": metadata.get("FILTER"),
                    "exptime": metadata.get("EXPTIME"),
                    "gain": metadata.get("GAIN"),
                    "offset": metadata.get("OFFSET"),
                    "temperature": metadata.get("CCD-TEMP"),
                    "naxis1": metadata.get("NAXIS1"),
                    "naxis2": metadata.get("NAXIS2"),
                    "star_count": siril.get("star_count"),
                    "median_fwhm": siril.get("median_fwhm"),
                    "median_roundness": siril.get("median_roundness"),
                    "median": stats.get("median"),
                    "robust_noise": stats.get("robust_noise"),
                    "robust_range": stats.get("robust_range"),
                    "nonfinite_fraction": stats.get("nonfinite_fraction"),
                    "preview_path": siril.get("preview_path"),
                    "analysis_success": siril.get("success"),
                    "suggested_review": record.get("suggested_review"),
                    "fixed_review_flags": ";".join(record.get("fixed_review_flags", [])),
                    "analysis_error": record.get("analysis_error"),
                }
            )


def relative_href(base: Path, target: str | None) -> str | None:
    if not target:
        return None
    try:
        return os.path.relpath(target, start=base)
    except ValueError:
        return target


def write_review_html(path: Path, records: list[dict[str, Any]], title: str) -> None:
    cards: list[str] = []
    for record in records:
        siril = record.get("siril", {})
        preview = relative_href(path.parent, siril.get("preview_path"))
        image_html = (
            f'<a href="{html.escape(preview)}">'
            f'<img loading="lazy" src="{html.escape(preview)}" alt="">'
            f"</a>"
            if preview
            else '<div class="missing">Preview unavailable</div>'
        )
        flags = ", ".join(record.get("fixed_review_flags", [])) or "none"
        cards.append(
            f"""
            <article class="card">
              {image_html}
              <h3>{html.escape(record["filename"])}</h3>
              <dl>
                <dt>Session</dt><dd>{html.escape(str(record.get("session_group")))}</dd>
                <dt>DATE-OBS</dt><dd>{html.escape(str(record.get("metadata", {}).get("DATE-OBS")))}</dd>
                <dt>Stars</dt><dd>{html.escape(str(siril.get("star_count")))}</dd>
                <dt>Median FWHM</dt><dd>{html.escape(str(siril.get("median_fwhm")))}</dd>
                <dt>Median roundness</dt><dd>{html.escape(str(siril.get("median_roundness")))}</dd>
                <dt>Flags</dt><dd>{html.escape(flags)}</dd>
                <dt>SHA-256</dt><dd class="hash">{html.escape(record["sha256"])}</dd>
              </dl>
            </article>
            """
        )

    document = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{html.escape(title)}</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 1rem; background: #111; color: #eee; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 1rem; }}
.card {{ background: #222; padding: .75rem; border-radius: .5rem; overflow: hidden; }}
img {{ width: 100%; height: 300px; object-fit: contain; background: #000; }}
h3 {{ font-size: .82rem; overflow-wrap: anywhere; }}
dl {{ display: grid; grid-template-columns: 8rem 1fr; font-size: .78rem; }}
dt {{ font-weight: 700; }}
dd {{ margin: 0; overflow-wrap: anywhere; }}
.hash {{ font-family: monospace; font-size: .68rem; }}
.missing {{ height: 300px; display: grid; place-items: center; background: #400; }}
</style>
</head>
<body>
<h1>{html.escape(title)}</h1>
<p>Every card must be reviewed. Fixed flags are evidence prompts, not automatic rejection decisions.</p>
<section class="grid">
{''.join(cards)}
</section>
</body>
</html>
"""
    write_text_atomic(path, document)


def build_decision_template(
    analysis_manifest: Path,
    project: str,
    filter_name: str,
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "analysis_manifest": str(analysis_manifest),
        "project": project,
        "filter": filter_name,
        "review_completed": False,
        "reviewer": "",
        "review_notes": "",
        "decisions": [
            {
                "filename": record["filename"],
                "sha256": record["sha256"],
                "decision": "",
                "reason": "",
                "confidence": "",
            }
            for record in records
        ],
    }


def analyze_filter(
    workspace: Path,
    project_name: str,
    filter_name: str,
    siril_root: Path,
    timeout_per_frame: int,
) -> dict[str, Any]:
    paths = project_paths(workspace, project_name, filter_name)
    verify_prepared_lights(paths)
    candidates = direct_fits_files(paths["lights"])
    if not candidates:
        raise QCError(f"No direct-child FITS lights found: {paths['lights']}")

    identifier = run_id()
    run_dir = paths["qc_root"] / identifier
    attempts_dir = run_dir / "attempts"
    run_dir.mkdir(parents=True, exist_ok=False)
    attempts_dir.mkdir()

    siril_check = check_siril(siril_root)
    records: list[dict[str, Any]] = []

    for index, candidate in enumerate(candidates, start=1):
        record: dict[str, Any] = {
            "index": index,
            "filename": candidate.name,
            "path": str(candidate),
            "size": candidate.stat().st_size,
            "sha256": sha256_file(candidate),
            "filename_hints": parse_filename_hints(candidate.name),
            "analysis_error": None,
        }
        try:
            metadata, statistics_data = read_fits_evidence(candidate)
            record["metadata"] = metadata
            record["statistics"] = statistics_data
            attempt_name = f"{index:04d}-{record['sha256'][:12]}"
            record["siril"] = run_siril_analysis(
                source=candidate,
                attempt_dir=attempts_dir / attempt_name,
                siril_root=siril_root,
                timeout=timeout_per_frame,
            )
        except Exception as exc:
            record.setdefault("metadata", {})
            record.setdefault("statistics", {})
            record.setdefault(
                "siril",
                {
                    "success": False,
                    "star_count": None,
                    "median_fwhm": None,
                    "median_roundness": None,
                    "preview_path": None,
                },
            )
            record["analysis_error"] = f"{type(exc).__name__}: {exc}"
        records.append(record)

    assign_session_groups(records)
    add_fixed_flags(records)

    analysis_manifest_path = run_dir / "analysis-manifest.json"
    manifest = {
        "schema_version": 1,
        "helper_version": VERSION,
        "created_at": utc_now(),
        "run_id": identifier,
        "workspace": str(workspace),
        "project": project_name,
        "project_path": str(paths["project"]),
        "filter": filter_name,
        "prepared_lights_path": str(paths["lights"]),
        "resolved_lights_target_for_reporting": str(
            paths["lights"].resolve(strict=True)
        ),
        "candidate_count": len(records),
        "siril": {
            "root": str(siril_root),
            "required_version": REQUIRED_SIRIL_VERSION,
            "version_check": siril_check,
            "timeout_per_frame_seconds": timeout_per_frame,
        },
        "records": records,
    }
    json_dump_atomic(analysis_manifest_path, manifest)
    write_metrics_csv(run_dir / "metrics.csv", records)
    write_review_html(
        run_dir / "review.html",
        records,
        f"{project_name} — {filter_name} light-frame review",
    )
    template = build_decision_template(
        analysis_manifest_path,
        project_name,
        filter_name,
        records,
    )
    json_dump_atomic(run_dir / "decision-template.json", template)

    failed = [
        record
        for record in records
        if record.get("analysis_error") or not record.get("siril", {}).get("success")
    ]
    summary = {
        "status": "analysis_complete" if not failed else "blocked",
        "project": project_name,
        "filter": filter_name,
        "candidate_count": len(records),
        "successful_analysis_count": len(records) - len(failed),
        "failed_analysis_count": len(failed),
        "suggested_review_count": sum(
            1 for record in records if record.get("suggested_review")
        ),
        "run_directory": str(run_dir),
        "analysis_manifest": str(analysis_manifest_path),
        "metrics_csv": str(run_dir / "metrics.csv"),
        "review_html": str(run_dir / "review.html"),
        "decision_template": str(run_dir / "decision-template.json"),
    }
    json_dump_atomic(run_dir / "summary.json", summary)
    return summary


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_analysis_for_decisions(decisions_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    decisions = load_json(decisions_path)
    manifest_value = decisions.get("analysis_manifest")
    if not manifest_value:
        raise QCError("Decisions file does not name an analysis_manifest.")
    manifest_path = Path(manifest_value)
    if not manifest_path.is_absolute():
        manifest_path = (decisions_path.parent / manifest_path).resolve()
    if not manifest_path.is_file():
        raise QCError(f"Analysis manifest not found: {manifest_path}")
    analysis = load_json(manifest_path)
    return decisions, analysis


def validate_complete_decisions(
    decisions: dict[str, Any],
    analysis: dict[str, Any],
) -> list[dict[str, Any]]:
    if not decisions.get("review_completed"):
        raise QCError("Decisions file must set review_completed to true.")
    analysis_records = {
        (record["filename"], record["sha256"]): record
        for record in analysis.get("records", [])
    }
    supplied = decisions.get("decisions")
    if not isinstance(supplied, list):
        raise QCError("Decisions file must contain a decisions list.")
    supplied_map: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in supplied:
        key = (entry.get("filename"), entry.get("sha256"))
        if key in supplied_map:
            raise QCError(f"Duplicate decision entry: {key}")
        decision = entry.get("decision")
        if decision not in VALID_DECISIONS:
            raise QCError(
                f"Invalid or missing decision for {entry.get('filename')!r}: "
                f"{decision!r}"
            )
        if decision == "reject" and not str(entry.get("reason", "")).strip():
            raise QCError(
                f"Rejected frame requires a reason: {entry.get('filename')!r}"
            )
        supplied_map[key] = entry
    if set(supplied_map) != set(analysis_records):
        missing = sorted(set(analysis_records) - set(supplied_map))
        extra = sorted(set(supplied_map) - set(analysis_records))
        raise QCError(
            "Decisions must cover every analyzed frame exactly. "
            f"Missing={missing[:5]!r}; extra={extra[:5]!r}"
        )
    return [supplied_map[key] for key in analysis_records]


def load_rejection_index(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {
            "schema_version": 1,
            "updated_at": None,
            "entries": {},
        }
    payload = load_json(path)
    if not isinstance(payload.get("entries"), dict):
        raise QCError(f"Invalid rejection index: {path}")
    return payload


def collision_safe_destination(rejects: Path, source: Path, digest: str) -> Path:
    candidate = rejects / f"{source.stem}__sha256-{digest[:12]}{source.suffix}"
    counter = 1
    while candidate.exists():
        if sha256_file(candidate) == digest:
            return candidate
        candidate = rejects / (
            f"{source.stem}__sha256-{digest[:12]}-{counter}{source.suffix}"
        )
        counter += 1
    return candidate


def plan_or_apply_reject(
    lights: Path,
    rejects: Path,
    source: Path,
    digest: str,
    dry_run: bool,
) -> dict[str, Any]:
    if not safe_direct_child(lights, source):
        raise QCError(f"Source is not a direct child of lights: {source}")
    if not source.is_file():
        raise QCError(f"Direct-child light is missing: {source}")
    current_digest = sha256_file(source)
    if current_digest != digest:
        raise QCError(
            f"Checksum changed for {source.name}: expected {digest}, "
            f"found {current_digest}"
        )

    intended = rejects / source.name
    action: str
    destination: Path

    if not intended.exists():
        action = "would_move_to_rejects" if dry_run else "moved_to_rejects"
        destination = intended
        if not dry_run:
            rejects.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
            if not destination.is_file() or sha256_file(destination) != digest:
                raise QCError(f"Moved reject failed checksum verification: {destination}")
            if source.exists():
                raise QCError(f"Source remained after move: {source}")
    else:
        existing_digest = sha256_file(intended)
        if existing_digest == digest:
            action = (
                "would_delete_identical_direct_duplicate"
                if dry_run
                else "duplicate_direct_light_deleted"
            )
            destination = intended
            if not dry_run:
                if not intended.is_file() or sha256_file(intended) != digest:
                    raise QCError(f"Existing reject is not safely preserved: {intended}")
                source.unlink()
                if source.exists():
                    raise QCError(f"Identical direct duplicate could not be deleted: {source}")
                if not intended.is_file() or sha256_file(intended) != digest:
                    raise QCError(f"Reject copy changed after duplicate deletion: {intended}")
        else:
            destination = collision_safe_destination(rejects, source, digest)
            if destination.exists() and sha256_file(destination) == digest:
                action = (
                    "would_delete_identical_direct_duplicate"
                    if dry_run
                    else "duplicate_direct_light_deleted"
                )
                if not dry_run:
                    source.unlink()
                    if source.exists():
                        raise QCError(
                            f"Identical direct duplicate could not be deleted: {source}"
                        )
            else:
                action = (
                    "would_move_to_rejects_with_unique_name"
                    if dry_run
                    else "moved_to_rejects_with_unique_name"
                )
                if not dry_run:
                    rejects.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(source), str(destination))
                    if not destination.is_file() or sha256_file(destination) != digest:
                        raise QCError(
                            f"Collision-safe reject failed verification: {destination}"
                        )
                    if source.exists():
                        raise QCError(f"Source remained after collision-safe move: {source}")

    return {
        "filename": source.name,
        "sha256": digest,
        "source": str(source),
        "destination": str(destination),
        "action": action,
        "dry_run": dry_run,
    }


def apply_decisions(
    workspace: Path,
    decisions_path: Path,
    dry_run: bool,
) -> dict[str, Any]:
    decisions, analysis = load_analysis_for_decisions(decisions_path)
    validated = validate_complete_decisions(decisions, analysis)

    project_name = sanitize_project_name(str(analysis.get("project")))
    filter_name = str(analysis.get("filter"))
    if filter_name not in VALID_FILTERS:
        raise QCError(f"Invalid filter in analysis manifest: {filter_name!r}")
    paths = project_paths(workspace, project_name, filter_name)
    verify_prepared_lights(paths)

    if str(paths["lights"]) != analysis.get("prepared_lights_path"):
        raise QCError(
            "Prepared lights path differs from the analysis manifest. "
            "Run a new analysis."
        )

    analysis_by_key = {
        (record["filename"], record["sha256"]): record
        for record in analysis.get("records", [])
    }
    actions: list[dict[str, Any]] = []
    rejected_entries: list[dict[str, Any]] = []

    for entry in validated:
        key = (entry["filename"], entry["sha256"])
        source = paths["lights"] / entry["filename"]
        if entry["decision"] == "reject":
            record = analysis_by_key[key]
            action = plan_or_apply_reject(
                lights=paths["lights"],
                rejects=paths["rejects"],
                source=source,
                digest=entry["sha256"],
                dry_run=dry_run,
            )
            action.update(
                {
                    "decision": "reject",
                    "reason": entry.get("reason"),
                    "confidence": entry.get("confidence"),
                    "session_group": record.get("session_group"),
                    "date_obs": record.get("metadata", {}).get("DATE-OBS"),
                }
            )
            actions.append(action)
            rejected_entries.append(
                {
                    "sha256": entry["sha256"],
                    "original_filename": entry["filename"],
                    "final_path": action["destination"],
                    "reason": entry.get("reason"),
                    "confidence": entry.get("confidence"),
                    "date_obs": record.get("metadata", {}).get("DATE-OBS"),
                    "session_group": record.get("session_group"),
                    "recorded_at": utc_now(),
                    "analysis_manifest": decisions.get("analysis_manifest"),
                }
            )
        else:
            if not source.is_file():
                raise QCError(
                    f"Accepted/review frame is no longer directly in lights: {source}"
                )
            if sha256_file(source) != entry["sha256"]:
                raise QCError(f"Checksum changed before apply: {source}")

    identifier = run_id()
    apply_dir = paths["qc_root"] / f"{identifier}-apply"
    apply_dir.mkdir(parents=True, exist_ok=False)

    if not dry_run and rejected_entries:
        index_path = paths["rejects"] / "rejection-index.json"
        index = load_rejection_index(index_path)
        for rejected in rejected_entries:
            index["entries"][rejected["sha256"]] = rejected
        index["updated_at"] = utc_now()
        json_dump_atomic(index_path, index)

    result = {
        "schema_version": 1,
        "helper_version": VERSION,
        "created_at": utc_now(),
        "status": "dry_run" if dry_run else "success",
        "workspace": str(workspace),
        "project": project_name,
        "filter": filter_name,
        "decisions_file": str(decisions_path),
        "analysis_manifest": decisions.get("analysis_manifest"),
        "dry_run": dry_run,
        "actions": actions,
        "accepted_count": sum(
            1 for entry in validated if entry["decision"] == "accept"
        ),
        "needs_review_count": sum(
            1 for entry in validated if entry["decision"] == "needs_review"
        ),
        "rejected_count": sum(
            1 for entry in validated if entry["decision"] == "reject"
        ),
        "remaining_direct_fits_count": len(direct_fits_files(paths["lights"])),
    }
    json_dump_atomic(apply_dir / "apply-result.json", result)
    return {**result, "result_path": str(apply_dir / "apply-result.json")}


def reapply_previous(
    workspace: Path,
    project_name: str,
    filter_name: str,
    dry_run: bool,
) -> dict[str, Any]:
    paths = project_paths(workspace, project_name, filter_name)
    verify_prepared_lights(paths)
    index_path = paths["rejects"] / "rejection-index.json"
    index = load_rejection_index(index_path)
    entries = index.get("entries", {})
    candidates = direct_fits_files(paths["lights"])
    actions: list[dict[str, Any]] = []

    for source in candidates:
        digest = sha256_file(source)
        if digest not in entries:
            continue
        action = plan_or_apply_reject(
            lights=paths["lights"],
            rejects=paths["rejects"],
            source=source,
            digest=digest,
            dry_run=dry_run,
        )
        prior = entries[digest]
        action.update(
            {
                "decision": "previously_rejected",
                "reason": prior.get("reason"),
                "prior_index": str(index_path),
            }
        )
        actions.append(action)

    identifier = run_id()
    output_dir = paths["qc_root"] / f"{identifier}-reapply"
    output_dir.mkdir(parents=True, exist_ok=False)
    result = {
        "schema_version": 1,
        "helper_version": VERSION,
        "created_at": utc_now(),
        "status": "dry_run" if dry_run else "success",
        "workspace": str(workspace),
        "project": project_name,
        "filter": filter_name,
        "rejection_index": str(index_path),
        "indexed_reject_count": len(entries),
        "matched_direct_duplicate_count": len(actions),
        "dry_run": dry_run,
        "actions": actions,
        "remaining_direct_fits_count": len(direct_fits_files(paths["lights"])),
    }
    json_dump_atomic(output_dir / "reapply-result.json", result)
    return {**result, "result_path": str(output_dir / "reapply-result.json")}


def verify_applied_qc_state(
    paths: dict[str, Path],
) -> dict[str, Any] | None:
    apply_results = sorted(
        paths["qc_root"].glob("*-apply/apply-result.json"),
        key=lambda path: path.parent.name,
        reverse=True,
    )
    direct_by_name = {
        path.name: sha256_file(path)
        for path in direct_fits_files(paths["lights"])
    }
    index_path = paths["rejects"] / "rejection-index.json"
    index = load_rejection_index(index_path)
    indexed = index.get("entries", {})

    for result_path in apply_results:
        try:
            result = load_json(result_path)
            if result.get("status") != "success" or result.get("dry_run"):
                continue
            decisions_path = Path(str(result.get("decisions_file", "")))
            if not decisions_path.is_file():
                continue
            decisions, analysis = load_analysis_for_decisions(decisions_path)
            validated = validate_complete_decisions(decisions, analysis)
            if analysis.get("prepared_lights_path") != str(paths["lights"]):
                continue

            expected_direct: dict[str, str] = {}
            rejected_hashes: set[str] = set()
            needs_review_count = 0
            valid = True

            for entry in validated:
                decision = entry["decision"]
                if decision in {"accept", "needs_review"}:
                    expected_direct[entry["filename"]] = entry["sha256"]
                    if decision == "needs_review":
                        needs_review_count += 1
                elif decision == "reject":
                    digest = entry["sha256"]
                    rejected_hashes.add(digest)
                    indexed_entry = indexed.get(digest)
                    if not indexed_entry:
                        valid = False
                        break
                    final_path = Path(str(indexed_entry.get("final_path", "")))
                    if (
                        not final_path.is_file()
                        or sha256_file(final_path) != digest
                    ):
                        valid = False
                        break

            if not valid or expected_direct != direct_by_name:
                continue
            if any(digest not in indexed for digest in rejected_hashes):
                continue

            return {
                "apply_result": str(result_path),
                "decisions_file": str(decisions_path),
                "analysis_manifest": decisions.get("analysis_manifest"),
                "accepted_count": sum(
                    1 for entry in validated if entry["decision"] == "accept"
                ),
                "rejected_count": sum(
                    1 for entry in validated if entry["decision"] == "reject"
                ),
                "needs_review_count": needs_review_count,
            }
        except Exception:
            continue
    return None


def status_filter(
    workspace: Path,
    project_name: str,
    filter_name: str,
) -> dict[str, Any]:
    paths = project_paths(workspace, project_name, filter_name)
    verify_prepared_lights(paths)
    direct = direct_fits_files(paths["lights"])
    index_path = paths["rejects"] / "rejection-index.json"
    index = load_rejection_index(index_path)
    indexed = index.get("entries", {})
    violations: list[dict[str, str]] = []
    for path in direct:
        digest = sha256_file(path)
        if digest in indexed:
            violations.append(
                {
                    "filename": path.name,
                    "sha256": digest,
                    "reason": str(indexed[digest].get("reason")),
                }
            )

    rejected_fits: list[Path] = []
    if paths["rejects"].is_dir():
        rejected_fits = sorted(
            path
            for path in paths["rejects"].rglob("*")
            if path.is_file() and path.suffix.lower() in FITS_SUFFIXES
        )

    verified = verify_applied_qc_state(paths)
    if violations or not direct:
        status = "blocked"
    elif verified is None:
        status = "unreviewed"
    elif verified["needs_review_count"] > 0:
        status = "needs_review"
    else:
        status = "ready"

    return {
        "status": status,
        "workspace": str(workspace),
        "project": project_name,
        "filter": filter_name,
        "prepared_lights_path": str(paths["lights"]),
        "direct_fits_count": len(direct),
        "direct_accepted_count": (
            verified["accepted_count"] if verified else None
        ),
        "rejected_fits_count": len(rejected_fits),
        "needs_review_count": (
            verified["needs_review_count"] if verified else None
        ),
        "rejected_checksum_violations": violations,
        "rejection_index": str(index_path),
        "qc_completion_verified": verified is not None,
        "verified_apply_result": (
            verified["apply_result"] if verified else None
        ),
        "verified_decisions_file": (
            verified["decisions_file"] if verified else None
        ),
        "verified_analysis_manifest": (
            verified["analysis_manifest"] if verified else None
        ),
    }

def self_test(workspace: Path, siril_root: Path) -> dict[str, Any]:
    try:
        import numpy
        import astropy
    except ImportError as exc:
        raise QCError(f"Required Python dependency missing: {exc}") from exc
    siril = check_siril(siril_root)
    return {
        "status": "success",
        "helper_version": VERSION,
        "python": sys.executable,
        "numpy_version": numpy.__version__,
        "astropy_version": astropy.__version__,
        "workspace": str(workspace),
        "siril": siril,
    }



def numeric_evidence_summary(values: Iterable[Any]) -> dict[str, Any]:
    cleaned: list[float] = []
    missing = 0
    for value in values:
        number = robust_float(value)
        if number is None:
            missing += 1
        else:
            cleaned.append(number)
    return {
        "count": len(cleaned),
        "missing_count": missing,
        "minimum": min(cleaned) if cleaned else None,
        "median": statistics.median(cleaned) if cleaned else None,
        "maximum": max(cleaned) if cleaned else None,
    }


def record_sort_key(record: dict[str, Any]) -> tuple[dt.datetime, str]:
    observed = parse_date_obs(record.get("metadata", {}).get("DATE-OBS"))
    fallback = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    return observed or fallback, str(record.get("filename", ""))


def representative_records(
    records: list[dict[str, Any]],
    maximum: int = 5,
) -> list[dict[str, Any]]:
    ordered = sorted(records, key=record_sort_key)
    if len(ordered) <= maximum:
        return ordered

    selected_indexes: list[int] = [0, len(ordered) // 2, len(ordered) - 1]

    star_pairs: list[tuple[float, int]] = []
    for index, record in enumerate(ordered):
        value = robust_float(record.get("siril", {}).get("star_count"))
        if value is not None:
            star_pairs.append((value, index))
    if star_pairs:
        selected_indexes.append(min(star_pairs)[1])
        selected_indexes.append(max(star_pairs)[1])

    selected: list[dict[str, Any]] = []
    seen: set[int] = set()
    for index in selected_indexes:
        if index in seen:
            continue
        seen.add(index)
        selected.append(ordered[index])
        if len(selected) >= maximum:
            break

    if len(selected) < maximum:
        for index, record in enumerate(ordered):
            if index in seen:
                continue
            selected.append(record)
            seen.add(index)
            if len(selected) >= maximum:
                break

    return sorted(selected, key=record_sort_key)


def compact_record_evidence(record: dict[str, Any]) -> dict[str, Any]:
    siril = record.get("siril", {})
    metadata = record.get("metadata", {})
    return {
        "filename": record.get("filename"),
        "sha256": record.get("sha256"),
        "date_obs": metadata.get("DATE-OBS"),
        "star_count": siril.get("star_count"),
        "median_fwhm": siril.get("median_fwhm"),
        "median_roundness": siril.get("median_roundness"),
        "fixed_review_flags": record.get("fixed_review_flags", []),
        "preview_path": siril.get("preview_path"),
        "analysis_success": bool(siril.get("success"))
        and not bool(record.get("analysis_error")),
    }



def evidence_signature(record: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
    successful = bool(record.get("siril", {}).get("success")) and not bool(
        record.get("analysis_error")
    )
    flags = tuple(sorted(str(value) for value in record.get("fixed_review_flags", [])))
    state = "success" if successful else "failed"
    return state, flags


def evidence_signature_label(
    signature: tuple[str, tuple[str, ...]],
) -> str:
    state, flags = signature
    if state != "success":
        return "analysis-failed"
    if not flags:
        return "unflagged"
    return "+".join(
        re.sub(r"[^a-z0-9]+", "-", flag.lower()).strip("-")
        for flag in flags
    )


def evidence_group_sort_key(
    item: tuple[tuple[str, tuple[str, ...]], list[dict[str, Any]]],
) -> tuple[int, str]:
    signature, _records = item
    state, flags = signature
    if state != "success":
        rank = 2
    elif not flags:
        rank = 0
    else:
        rank = 1
    return rank, evidence_signature_label(signature)


def summarize_evidence_group(
    group_id: str,
    session_group: str,
    signature: tuple[str, tuple[str, ...]],
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    ordered = sorted(records, key=record_sort_key)
    state, flags = signature
    successful = state == "success"
    observed_times = [
        parse_date_obs(record.get("metadata", {}).get("DATE-OBS"))
        for record in ordered
    ]
    observed_times = [value for value in observed_times if value is not None]
    representatives = representative_records(ordered)

    return {
        "group_id": group_id,
        "session_group": session_group,
        "evidence_label": evidence_signature_label(signature),
        "analysis_state": state,
        "fixed_review_flags": list(flags),
        "frame_count": len(ordered),
        "all_frames_analyzed_successfully": successful,
        "group_level_accept_eligible": successful and not flags,
        "group_level_reject_eligible": successful and bool(flags),
        "star_count": numeric_evidence_summary(
            record.get("siril", {}).get("star_count") for record in ordered
        ),
        "median_fwhm": numeric_evidence_summary(
            record.get("siril", {}).get("median_fwhm") for record in ordered
        ),
        "median_roundness": numeric_evidence_summary(
            record.get("siril", {}).get("median_roundness") for record in ordered
        ),
        "date_obs_start": (
            min(observed_times).isoformat() if observed_times else None
        ),
        "date_obs_end": (
            max(observed_times).isoformat() if observed_times else None
        ),
        "representative_frames": [
            compact_record_evidence(record) for record in representatives
        ],
        "member_keys": [
            {
                "filename": record.get("filename"),
                "sha256": record.get("sha256"),
            }
            for record in ordered
        ],
    }


def build_evidence_groups(
    session_group: str,
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    buckets: dict[
        tuple[str, tuple[str, ...]],
        list[dict[str, Any]],
    ] = {}
    for record in records:
        buckets.setdefault(evidence_signature(record), []).append(record)

    groups: list[dict[str, Any]] = []
    for index, (signature, members) in enumerate(
        sorted(buckets.items(), key=evidence_group_sort_key),
        start=1,
    ):
        group_id = f"{session_group}/evidence-{index:02d}"
        groups.append(
            summarize_evidence_group(
                group_id,
                session_group,
                signature,
                members,
            )
        )
    return groups


def build_evidence_group_index(
    analysis: dict[str, Any],
) -> tuple[
    dict[str, dict[str, Any]],
    dict[tuple[str, str], str],
]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in analysis.get("records", []):
        session_group = str(record.get("session_group") or "unassigned")
        grouped.setdefault(session_group, []).append(record)

    groups_by_id: dict[str, dict[str, Any]] = {}
    record_to_group: dict[tuple[str, str], str] = {}
    for session_group in sorted(grouped):
        for group in build_evidence_groups(session_group, grouped[session_group]):
            group_id = group["group_id"]
            groups_by_id[group_id] = group
            for member in group["member_keys"]:
                key = (str(member["filename"]), str(member["sha256"]))
                record_to_group[key] = group_id
    return groups_by_id, record_to_group

def summarize_session_records(
    session_group: str,
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    ordered = sorted(records, key=record_sort_key)
    successes = [
        record
        for record in ordered
        if record.get("siril", {}).get("success")
        and not record.get("analysis_error")
    ]
    flagged = [
        record for record in ordered if record.get("fixed_review_flags")
    ]
    signatures = {
        tuple(sorted(record.get("fixed_review_flags", [])))
        for record in ordered
    }
    nonempty_signatures = {
        signature for signature in signatures if signature
    }
    flag_counts: dict[str, int] = {}
    for record in ordered:
        for flag in record.get("fixed_review_flags", []):
            flag_counts[flag] = flag_counts.get(flag, 0) + 1

    observed_times = [
        parse_date_obs(record.get("metadata", {}).get("DATE-OBS"))
        for record in ordered
    ]
    observed_times = [value for value in observed_times if value is not None]
    angles = sorted(
        {
            value
            for value in (
                robust_float(
                    record.get("filename_hints", {}).get("rotator_angle_hint")
                )
                for record in ordered
            )
            if value is not None
        }
    )

    all_successful = len(successes) == len(ordered)
    all_flagged = len(flagged) == len(ordered) and bool(ordered)
    no_flags = len(flagged) == 0
    homogeneous_nonempty_flags = (
        len(nonempty_signatures) == 1
        and len(signatures) == 1
        and all_flagged
    )

    representatives = representative_records(ordered)

    return {
        "session_group": session_group,
        "frame_count": len(ordered),
        "analysis_success_count": len(successes),
        "analysis_failure_count": len(ordered) - len(successes),
        "flagged_frame_count": len(flagged),
        "unflagged_frame_count": len(ordered) - len(flagged),
        "all_frames_analyzed_successfully": all_successful,
        "all_frames_flagged": all_flagged,
        "no_frames_flagged": no_flags,
        "homogeneous_nonempty_flag_signature": homogeneous_nonempty_flags,
        "session_level_reject_eligible": (
            all_successful
            and homogeneous_nonempty_flags
            and len(ordered) >= 2
        ),
        "session_level_accept_eligible": all_successful and no_flags,
        "flag_counts": dict(sorted(flag_counts.items())),
        "star_count": numeric_evidence_summary(
            record.get("siril", {}).get("star_count")
            for record in ordered
        ),
        "median_fwhm": numeric_evidence_summary(
            record.get("siril", {}).get("median_fwhm")
            for record in ordered
        ),
        "median_roundness": numeric_evidence_summary(
            record.get("siril", {}).get("median_roundness")
            for record in ordered
        ),
        "date_obs_start": (
            min(observed_times).isoformat() if observed_times else None
        ),
        "date_obs_end": (
            max(observed_times).isoformat() if observed_times else None
        ),
        "rotator_angle_hints": angles,
        "representative_frames": [
            compact_record_evidence(record) for record in representatives
        ],
        "evidence_groups": build_evidence_groups(session_group, ordered),
    }


def write_flagged_files_csv(
    path: Path,
    records: list[dict[str, Any]],
) -> None:
    fields = [
        "session_group",
        "filename",
        "sha256",
        "date_obs",
        "star_count",
        "median_fwhm",
        "median_roundness",
        "fixed_review_flags",
        "preview_path",
        "analysis_success",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for record in sorted(records, key=record_sort_key):
            if not record.get("fixed_review_flags"):
                continue
            evidence = compact_record_evidence(record)
            writer.writerow(
                {
                    "session_group": record.get("session_group"),
                    "filename": evidence.get("filename"),
                    "sha256": evidence.get("sha256"),
                    "date_obs": evidence.get("date_obs"),
                    "star_count": evidence.get("star_count"),
                    "median_fwhm": evidence.get("median_fwhm"),
                    "median_roundness": evidence.get("median_roundness"),
                    "fixed_review_flags": ";".join(
                        evidence.get("fixed_review_flags", [])
                    ),
                    "preview_path": evidence.get("preview_path"),
                    "analysis_success": evidence.get("analysis_success"),
                }
            )


def build_review_plan_template(
    analysis_manifest: Path,
    analysis: dict[str, Any],
    sessions: list[dict[str, Any]],
) -> dict[str, Any]:
    group_decisions: list[dict[str, Any]] = []
    for session in sessions:
        for group in session.get("evidence_groups", []):
            group_decisions.append(
                {
                    "group_id": group["group_id"],
                    "session_group": group["session_group"],
                    "evidence_label": group["evidence_label"],
                    "frame_count": group["frame_count"],
                    "fixed_review_flags": group["fixed_review_flags"],
                    "decision": "",
                    "reason": "",
                    "confidence": "",
                    "user_confirmed": False,
                }
            )

    return {
        "schema_version": 2,
        "analysis_manifest": str(analysis_manifest),
        "project": analysis.get("project"),
        "filter": analysis.get("filter"),
        "review_completed": False,
        "reviewer": "",
        "review_notes": "",
        "group_decisions": group_decisions,
        "file_overrides": [],
    }

def create_review_summary(
    analysis_manifest: Path,
) -> dict[str, Any]:
    if not analysis_manifest.is_file():
        raise QCError(f"Analysis manifest not found: {analysis_manifest}")
    analysis = load_json(analysis_manifest)
    records = analysis.get("records")
    if not isinstance(records, list) or not records:
        raise QCError("Analysis manifest contains no frame records.")

    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        session_group = str(record.get("session_group") or "unassigned")
        grouped.setdefault(session_group, []).append(record)

    sessions = [
        summarize_session_records(session_group, grouped[session_group])
        for session_group in sorted(grouped)
    ]

    run_dir = analysis_manifest.parent
    compact_path = run_dir / "compact-review.json"
    plan_template_path = run_dir / "review-plan-template.json"
    representative_html_path = run_dir / "representative-review.html"
    flagged_csv_path = run_dir / "flagged-files.csv"

    representative_source_records: list[dict[str, Any]] = []
    for session_group in sorted(grouped):
        session_records = grouped[session_group]
        for group in build_evidence_groups(session_group, session_records):
            member_keys = {
                (str(member["filename"]), str(member["sha256"]))
                for member in group["member_keys"]
            }
            members = [
                record
                for record in session_records
                if (str(record.get("filename")), str(record.get("sha256")))
                in member_keys
            ]
            representative_source_records.extend(
                representative_records(members)
            )

    compact = {
        "schema_version": 1,
        "helper_version": VERSION,
        "created_at": utc_now(),
        "analysis_manifest": str(analysis_manifest),
        "project": analysis.get("project"),
        "filter": analysis.get("filter"),
        "candidate_count": len(records),
        "session_count": len(sessions),
        "evidence_group_count": sum(
            len(session.get("evidence_groups", [])) for session in sessions
        ),
        "analysis_failure_count": sum(
            session["analysis_failure_count"] for session in sessions
        ),
        "flagged_frame_count": sum(
            session["flagged_frame_count"] for session in sessions
        ),
        "sessions": sessions,
        "files": {
            "compact_review": str(compact_path),
            "review_plan_template": str(plan_template_path),
            "representative_review_html": str(representative_html_path),
            "flagged_files_csv": str(flagged_csv_path),
        },
    }
    json_dump_atomic(compact_path, compact)
    json_dump_atomic(
        plan_template_path,
        build_review_plan_template(analysis_manifest, analysis, sessions),
    )
    write_flagged_files_csv(flagged_csv_path, records)
    write_review_html(
        representative_html_path,
        representative_source_records,
        (
            f"{analysis.get('project')} — {analysis.get('filter')} "
            "representative session review"
        ),
    )

    return {
        "status": "review_summary_complete",
        "helper_version": VERSION,
        "analysis_manifest": str(analysis_manifest),
        "candidate_count": len(records),
        "session_count": len(sessions),
        "evidence_group_count": compact["evidence_group_count"],
        "analysis_failure_count": compact["analysis_failure_count"],
        "flagged_frame_count": compact["flagged_frame_count"],
        "sessions": sessions,
        "compact_review": str(compact_path),
        "review_plan_template": str(plan_template_path),
        "representative_review_html": str(representative_html_path),
        "flagged_files_csv": str(flagged_csv_path),
    }


def resolve_analysis_path_from_plan(
    plan_path: Path,
    plan: dict[str, Any],
) -> Path:
    value = plan.get("analysis_manifest")
    if not value:
        raise QCError("Review plan does not name an analysis_manifest.")
    analysis_path = Path(str(value))
    if not analysis_path.is_absolute():
        analysis_path = (plan_path.parent / analysis_path).resolve()
    if not analysis_path.is_file():
        raise QCError(f"Analysis manifest not found: {analysis_path}")
    return analysis_path



def validate_group_review_plan(
    plan: dict[str, Any],
    analysis: dict[str, Any],
) -> tuple[
    dict[str, dict[str, Any]],
    dict[tuple[str, str], dict[str, Any]],
    dict[tuple[str, str], str],
]:
    if not plan.get("review_completed"):
        raise QCError("Review plan must set review_completed to true.")
    if plan.get("project") != analysis.get("project"):
        raise QCError("Review-plan project does not match the analysis.")
    if plan.get("filter") != analysis.get("filter"):
        raise QCError("Review-plan filter does not match the analysis.")

    groups_by_id, record_to_group = build_evidence_group_index(analysis)
    supplied_groups = plan.get("group_decisions")
    if not isinstance(supplied_groups, list):
        raise QCError("Schema-2 review plan must contain group_decisions.")

    decisions: dict[str, dict[str, Any]] = {}
    for entry in supplied_groups:
        group_id = str(entry.get("group_id") or "")
        if not group_id:
            raise QCError("An evidence-group decision is missing group_id.")
        if group_id not in groups_by_id:
            raise QCError(f"Unknown evidence group: {group_id}")
        if group_id in decisions:
            raise QCError(f"Duplicate evidence-group decision: {group_id}")

        group = groups_by_id[group_id]
        decision = entry.get("decision")
        if decision not in VALID_DECISIONS:
            raise QCError(
                f"Invalid or missing decision for {group_id}: {decision!r}"
            )
        reason = str(entry.get("reason", "")).strip()
        confidence = str(entry.get("confidence", "")).strip().lower()
        user_confirmed = bool(entry.get("user_confirmed"))

        if decision in {"reject", "needs_review"} and not reason:
            raise QCError(
                f"{decision} evidence group requires a reason: {group_id}"
            )
        if decision == "reject":
            if confidence not in {"high", "confirmed"}:
                raise QCError(
                    f"Evidence-group reject requires confidence high or "
                    f"confirmed: {group_id}"
                )
            if not (
                user_confirmed
                or group["group_level_reject_eligible"]
            ):
                raise QCError(
                    f"Evidence-group reject is unsupported: {group_id}. "
                    f"Use needs_review or an exact file override."
                )
        if decision == "accept" and not group["group_level_accept_eligible"]:
            raise QCError(
                f"Evidence-group accept is unsupported because the group is "
                f"flagged or contains failed analysis: {group_id}."
            )

        decisions[group_id] = {
            **entry,
            "reason": reason,
            "confidence": confidence,
        }

    if set(decisions) != set(groups_by_id):
        missing = sorted(set(groups_by_id) - set(decisions))
        extra = sorted(set(decisions) - set(groups_by_id))
        raise QCError(
            "Review plan must cover every evidence group exactly. "
            f"Missing={missing!r}; extra={extra!r}"
        )

    records = analysis.get("records", [])
    record_by_key = {
        (str(record.get("filename")), str(record.get("sha256"))): record
        for record in records
    }
    supplied_overrides = plan.get("file_overrides", [])
    if not isinstance(supplied_overrides, list):
        raise QCError("file_overrides must be a list.")

    overrides: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in supplied_overrides:
        key = (str(entry.get("filename")), str(entry.get("sha256")))
        if key not in record_by_key:
            raise QCError(f"Unknown file override: {key!r}")
        if key in overrides:
            raise QCError(f"Duplicate file override: {key!r}")
        decision = entry.get("decision")
        if decision not in VALID_DECISIONS:
            raise QCError(f"Invalid file override decision: {key!r}")
        reason = str(entry.get("reason", "")).strip()
        confidence = str(entry.get("confidence", "")).strip().lower()
        if decision in {"reject", "needs_review"} and not reason:
            raise QCError(f"{decision} override requires a reason: {key!r}")
        if decision == "reject" and confidence not in {"high", "confirmed"}:
            raise QCError(
                f"Reject override requires confidence high or confirmed: "
                f"{key!r}"
            )
        overrides[key] = {
            **entry,
            "reason": reason,
            "confidence": confidence,
        }

    return decisions, overrides, record_to_group

def validate_review_plan(
    plan_path: Path,
    plan: dict[str, Any],
    analysis: dict[str, Any],
) -> tuple[
    dict[str, dict[str, Any]],
    dict[tuple[str, str], dict[str, Any]],
]:
    if not plan.get("review_completed"):
        raise QCError("Review plan must set review_completed to true.")
    if plan.get("project") != analysis.get("project"):
        raise QCError("Review-plan project does not match the analysis.")
    if plan.get("filter") != analysis.get("filter"):
        raise QCError("Review-plan filter does not match the analysis.")

    records = analysis.get("records", [])
    grouped: dict[str, list[dict[str, Any]]] = {}
    record_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        session_group = str(record.get("session_group") or "unassigned")
        grouped.setdefault(session_group, []).append(record)
        key = (str(record.get("filename")), str(record.get("sha256")))
        record_by_key[key] = record

    supplied_sessions = plan.get("session_decisions")
    if not isinstance(supplied_sessions, list):
        raise QCError("Review plan must contain session_decisions.")

    session_decisions: dict[str, dict[str, Any]] = {}
    for entry in supplied_sessions:
        session_group = str(entry.get("session_group") or "")
        if not session_group:
            raise QCError("A session decision is missing session_group.")
        if session_group in session_decisions:
            raise QCError(f"Duplicate session decision: {session_group}")
        decision = entry.get("decision")
        if decision not in VALID_DECISIONS:
            raise QCError(
                f"Invalid or missing decision for {session_group}: "
                f"{decision!r}"
            )
        reason = str(entry.get("reason", "")).strip()
        confidence = str(entry.get("confidence", "")).strip().lower()
        user_confirmed = bool(entry.get("user_confirmed"))

        summary = summarize_session_records(
            session_group,
            grouped.get(session_group, []),
        )
        if not grouped.get(session_group):
            raise QCError(f"Unknown session in review plan: {session_group}")

        if decision in {"reject", "needs_review"} and not reason:
            raise QCError(
                f"{decision} session requires a reason: {session_group}"
            )
        if decision == "reject":
            if confidence not in {"high", "confirmed"}:
                raise QCError(
                    f"Session-level reject requires confidence high or "
                    f"confirmed: {session_group}"
                )
            if not (
                user_confirmed
                or summary["session_level_reject_eligible"]
            ):
                raise QCError(
                    f"Session-level reject is not supported by homogeneous "
                    f"analysis evidence: {session_group}. Use per-file "
                    f"overrides or mark needs_review."
                )
        if decision == "accept" and not summary["session_level_accept_eligible"]:
            raise QCError(
                f"Session-level accept is not supported because the session "
                f"contains flags or failed analysis: {session_group}. Use "
                f"per-file overrides or mark needs_review."
            )

        session_decisions[session_group] = {
            **entry,
            "reason": reason,
            "confidence": confidence,
        }

    if set(session_decisions) != set(grouped):
        missing = sorted(set(grouped) - set(session_decisions))
        extra = sorted(set(session_decisions) - set(grouped))
        raise QCError(
            "Review plan must cover every session exactly. "
            f"Missing={missing!r}; extra={extra!r}"
        )

    supplied_overrides = plan.get("file_overrides", [])
    if not isinstance(supplied_overrides, list):
        raise QCError("file_overrides must be a list.")
    overrides: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in supplied_overrides:
        key = (str(entry.get("filename")), str(entry.get("sha256")))
        if key not in record_by_key:
            raise QCError(f"Unknown file override: {key!r}")
        if key in overrides:
            raise QCError(f"Duplicate file override: {key!r}")
        decision = entry.get("decision")
        if decision not in VALID_DECISIONS:
            raise QCError(f"Invalid file override decision: {key!r}")
        reason = str(entry.get("reason", "")).strip()
        confidence = str(entry.get("confidence", "")).strip().lower()
        if decision in {"reject", "needs_review"} and not reason:
            raise QCError(f"{decision} override requires a reason: {key!r}")
        if decision == "reject" and confidence not in {"high", "confirmed"}:
            raise QCError(
                f"Reject override requires confidence high or confirmed: "
                f"{key!r}"
            )
        overrides[key] = {
            **entry,
            "reason": reason,
            "confidence": confidence,
        }

    return session_decisions, overrides


def build_decisions_from_review_plan(
    plan_path: Path,
    output_path: Path | None,
) -> dict[str, Any]:
    if not plan_path.is_file():
        raise QCError(f"Review plan not found: {plan_path}")
    plan = load_json(plan_path)
    analysis_path = resolve_analysis_path_from_plan(plan_path, plan)
    analysis = load_json(analysis_path)

    group_mode = isinstance(plan.get("group_decisions"), list)
    if group_mode:
        group_decisions, overrides, record_to_group = (
            validate_group_review_plan(plan, analysis)
        )
        session_decisions: dict[str, dict[str, Any]] = {}
    else:
        session_decisions, overrides = validate_review_plan(
            plan_path,
            plan,
            analysis,
        )
        group_decisions = {}
        record_to_group = {}

    decisions: list[dict[str, Any]] = []
    decision_counts = {value: 0 for value in VALID_DECISIONS}
    override_count = 0

    for record in analysis.get("records", []):
        key = (str(record.get("filename")), str(record.get("sha256")))
        session_group = str(record.get("session_group") or "unassigned")
        if group_mode:
            source = group_decisions[record_to_group[key]]
        else:
            source = session_decisions[session_group]
        if key in overrides:
            source = overrides[key]
            override_count += 1
        decision = source["decision"]
        decision_counts[decision] += 1
        decisions.append(
            {
                "filename": record["filename"],
                "sha256": record["sha256"],
                "decision": decision,
                "reason": source.get("reason", ""),
                "confidence": source.get("confidence", ""),
            }
        )

    run_directory = analysis_path.parent
    if output_path is None:
        output_path = run_directory / f"decisions-{run_id()}.json"
    else:
        output_path = output_path.expanduser().resolve()
    if output_path.exists():
        raise QCError(f"Refusing to overwrite decisions file: {output_path}")
    if output_path.parent != run_directory:
        raise QCError(
            "Decisions output must remain in the original analysis run "
            f"directory: {run_directory}"
        )

    payload = {
        "schema_version": 1,
        "analysis_manifest": str(analysis_path),
        "project": analysis.get("project"),
        "filter": analysis.get("filter"),
        "review_completed": True,
        "reviewer": plan.get("reviewer", ""),
        "review_notes": plan.get("review_notes", ""),
        "generated_from_review_plan": str(plan_path),
        "review_plan_schema": 2 if group_mode else 1,
        "decisions": decisions,
    }
    json_dump_atomic(output_path, payload)
    validate_complete_decisions(payload, analysis)

    result_path = run_directory / f"decision-build-result-{run_id()}.json"
    result = {
        "status": "decisions_built",
        "helper_version": VERSION,
        "analysis_manifest": str(analysis_path),
        "review_plan": str(plan_path),
        "review_plan_schema": 2 if group_mode else 1,
        "decisions_file": str(output_path),
        "candidate_count": len(decisions),
        "session_count": len(
            {
                str(record.get("session_group") or "unassigned")
                for record in analysis.get("records", [])
            }
        ),
        "evidence_group_count": (
            len(group_decisions) if group_mode else None
        ),
        "file_override_count": override_count,
        "decision_counts": dict(sorted(decision_counts.items())),
        "result_path": str(result_path),
    }
    json_dump_atomic(result_path, result)
    return result

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deterministic astrophotography light-frame quality control."
    )
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument(
        "--workspace",
        help="Owning agent workspace. Normally derived from the installed skill path.",
    )
    parser.add_argument(
        "--siril-root",
        default=str(DEFAULT_SIRIL_ROOT),
        help="Extracted Siril AppImage root.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("self-test", help="Validate Python and Siril dependencies.")

    review_parser = subparsers.add_parser(
        "review-summary",
        help="Create compact session-level review evidence from an existing analysis.",
    )
    review_parser.add_argument("--analysis", required=True)

    build_decisions_parser = subparsers.add_parser(
        "build-decisions",
        help="Expand a completed session review plan into full frame decisions.",
    )
    build_decisions_parser.add_argument("--plan", required=True)
    build_decisions_parser.add_argument("--output")

    analyze_parser = subparsers.add_parser(
        "analyze",
        help="Analyze every direct-child light without moving anything.",
    )
    analyze_parser.add_argument("--project", required=True)
    analyze_parser.add_argument("--filter", required=True)
    analyze_parser.add_argument(
        "--timeout-per-frame",
        type=int,
        default=180,
        help="Finite Siril timeout per frame in seconds.",
    )

    apply_parser = subparsers.add_parser(
        "apply",
        help="Apply a completed decisions JSON with checksum verification.",
    )
    apply_parser.add_argument("--decisions", required=True)
    apply_parser.add_argument("--dry-run", action="store_true")

    reapply_parser = subparsers.add_parser(
        "reapply",
        help="Return recopied, previously rejected checksums to rejects.",
    )
    reapply_parser.add_argument("--project", required=True)
    reapply_parser.add_argument("--filter", required=True)
    reapply_parser.add_argument("--dry-run", action="store_true")

    status_parser = subparsers.add_parser(
        "status",
        help="Verify no indexed rejected checksum remains directly in lights.",
    )
    status_parser.add_argument("--project", required=True)
    status_parser.add_argument("--filter", required=True)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        workspace = derive_workspace(Path(__file__), args.workspace)
        siril_root = Path(args.siril_root).expanduser().resolve()

        if args.command == "self-test":
            result = self_test(workspace, siril_root)
        elif args.command == "review-summary":
            analysis_path = Path(args.analysis).expanduser().resolve()
            result = create_review_summary(analysis_path)
        elif args.command == "build-decisions":
            plan_path = Path(args.plan).expanduser().resolve()
            output_path = (
                Path(args.output).expanduser().resolve()
                if args.output
                else None
            )
            result = build_decisions_from_review_plan(
                plan_path,
                output_path,
            )
        elif args.command == "analyze":
            project_name = sanitize_project_name(args.project)
            filters = normalize_filter(args.filter)
            summaries = [
                analyze_filter(
                    workspace=workspace,
                    project_name=project_name,
                    filter_name=filter_name,
                    siril_root=siril_root,
                    timeout_per_frame=args.timeout_per_frame,
                )
                for filter_name in filters
            ]
            result = {
                "status": (
                    "analysis_complete"
                    if all(item["status"] == "analysis_complete" for item in summaries)
                    else "blocked"
                ),
                "summaries": summaries,
            }
        elif args.command == "apply":
            decisions_path = Path(args.decisions).expanduser().resolve()
            if not decisions_path.is_file():
                raise QCError(f"Decisions file not found: {decisions_path}")
            result = apply_decisions(workspace, decisions_path, args.dry_run)
        elif args.command == "reapply":
            project_name = sanitize_project_name(args.project)
            filters = normalize_filter(args.filter)
            results = [
                reapply_previous(
                    workspace=workspace,
                    project_name=project_name,
                    filter_name=filter_name,
                    dry_run=args.dry_run,
                )
                for filter_name in filters
            ]
            result = {
                "status": "dry_run" if args.dry_run else "success",
                "results": results,
            }
        elif args.command == "status":
            project_name = sanitize_project_name(args.project)
            filters = normalize_filter(args.filter)
            results = [
                status_filter(workspace, project_name, filter_name)
                for filter_name in filters
            ]
            statuses = {item["status"] for item in results}
            if statuses == {"ready"}:
                overall_status = "ready"
            elif "blocked" in statuses:
                overall_status = "blocked"
            elif "needs_review" in statuses:
                overall_status = "needs_review"
            else:
                overall_status = "unreviewed"
            result = {
                "status": overall_status,
                "results": results,
            }
        else:
            raise QCError(f"Unsupported command: {args.command}")

        print(json.dumps(result, indent=2, sort_keys=True))
        if result.get("status") in {"blocked", "failed"}:
            return 2
        return 0
    except QCError as exc:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "error": str(exc),
                    "helper_version": VERSION,
                },
                indent=2,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "helper_version": VERSION,
                },
                indent=2,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
