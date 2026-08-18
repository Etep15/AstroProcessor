#!/usr/bin/env python3
"""Copy-based implementation for astroproc -p/--prepare.

Installed beside the canonical ``astroproc`` launcher and invoked by a small
guarded launcher hook. Other astroproc commands continue to use the original
implementation.

Safety:
- Source lights, flats, darks, and biases are never deleted or moved.
- Existing directory symlinks are unlinked only after nested reject data is
  copied to safe staging.
- Real destination directories are never removed.
- Existing destination files are never overwritten with different content.
- Rejected checksums in rejection-index.json are not recopied to direct lights.
- Existing reject files and indexes survive legacy symlink conversion.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, Any, Callable

VERSION = "1.1.0"
FITS_SUFFIXES = {".fit", ".fits", ".fts"}
DATE_DIRECTORY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class PrepareCopyError(RuntimeError):
    pass


@dataclass
class CopyStats:
    category: str
    filter_name: str
    source: str
    destination: str
    discovered: int = 0
    copied: int = 0
    identical_existing: int = 0
    skipped_rejected: int = 0
    preserved_extra_direct_files: int = 0
    legacy_reject_files_preserved: int = 0


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def direct_fits(path: Path) -> list[Path]:
    if not path.is_dir():
        raise PrepareCopyError(f"Required source directory is missing: {path}")
    return sorted(
        item
        for item in path.iterdir()
        if item.is_file()
        and not item.name.startswith("._")
        and item.suffix.lower() in FITS_SUFFIXES
    )


def all_files(path: Path) -> list[Path]:
    if not path.is_dir():
        return []
    return sorted(item for item in path.rglob("*") if item.is_file())


def copy_file_no_overwrite(source: Path, destination: Path) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if not destination.is_file():
            raise PrepareCopyError(
                f"Destination collision is not a regular file: {destination}"
            )
        if source.stat().st_size != destination.stat().st_size:
            raise PrepareCopyError(
                f"Refusing to overwrite different existing file: {destination}"
            )
        if sha256_file(source) != sha256_file(destination):
            raise PrepareCopyError(
                f"Refusing to overwrite checksum-mismatched file: {destination}"
            )
        return "identical"

    temporary = destination.with_name(
        f".{destination.name}.astroproc-copy-{os.getpid()}.partial"
    )
    if temporary.exists():
        raise PrepareCopyError(f"Temporary copy path already exists: {temporary}")
    try:
        shutil.copy2(source, temporary)
        if sha256_file(source) != sha256_file(temporary):
            raise PrepareCopyError(
                f"Checksum verification failed while copying {source}"
            )
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return "copied"


def merge_tree_no_overwrite(source: Path, destination: Path) -> int:
    preserved = 0
    if not source.is_dir():
        return preserved
    for source_file in all_files(source):
        relative = source_file.relative_to(source)
        copy_file_no_overwrite(source_file, destination / relative)
        preserved += 1
    return preserved


def load_rejected_checksums(rejects_directory: Path) -> set[str]:
    index_path = rejects_directory / "rejection-index.json"
    if not index_path.is_file():
        return set()
    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise PrepareCopyError(
            f"Cannot read rejection index {index_path}: {exc}"
        ) from exc
    entries = payload.get("entries", {})
    if not isinstance(entries, dict):
        raise PrepareCopyError(f"Invalid rejection index entries in {index_path}")
    return {str(value) for value in entries.keys()}


CALIBRATION_SELECTION_POLICY_VERSION = 2
DARK_TEMP_TOLERANCE_C = 1.0
BIAS_TEMP_TOLERANCE_C = 1.0
MIN_DARK_FRAMES = 1
MIN_BIAS_FRAMES = 1
IGNORED_CALIBRATION_DIRECTORY_NAMES = {
    "process", "processing", "master", "masters", ".astroproc"
}


def _header_first(header: Any, *keys: str) -> Any:
    for key in keys:
        value = header.get(key)
        if value is not None and str(value).strip() != "":
            return value
    return None


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _normalize_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _parse_binning(header: Any) -> tuple[int | None, int | None]:
    xbin = _as_int(header.get("XBINNING"))
    ybin = _as_int(header.get("YBINNING"))
    if xbin is not None and ybin is not None:
        return xbin, ybin
    raw = _header_first(header, "BINNING", "CCDBINX")
    if raw is None:
        return xbin, ybin
    text = str(raw).strip().lower().replace(" ", "")
    match = re.fullmatch(r"(\d+)(?:x|,)(\d+)", text)
    if match:
        return int(match.group(1)), int(match.group(2))
    single = _as_int(raw)
    if single is not None:
        return single, single
    return xbin, ybin


def read_fits_metadata(path: Path) -> dict[str, Any]:
    try:
        from astropy.io import fits
    except Exception as exc:
        raise PrepareCopyError(
            "Astropy is required in the AstroProcessor virtual environment."
        ) from exc

    try:
        header = fits.getheader(path, 0)
    except Exception as exc:
        raise PrepareCopyError(f"Cannot read FITS header {path}: {exc}") from exc

    xbin, ybin = _parse_binning(header)
    return {
        "path": path,
        "imagetype": _normalize_text(_header_first(header, "IMAGETYP", "FRAME")),
        "camera": _normalize_text(_header_first(header, "INSTRUME", "CAMERA", "DETECTOR")),
        "xbin": xbin,
        "ybin": ybin,
        "gain": _as_float(header.get("GAIN")),
        "offset": _as_float(header.get("OFFSET")),
        "exptime": _as_float(_header_first(header, "EXPTIME", "EXPOSURE")),
        "temperature": _as_float(
            _header_first(header, "CCD-TEMP", "CCD_TEMP", "SET-TEMP", "SET_TEMP")
        ),
        "filter": _normalize_text(header.get("FILTER")),
        "date_obs": _normalize_text(_header_first(header, "DATE-OBS", "DATE")),
        "width": _as_int(header.get("NAXIS1")),
        "height": _as_int(header.get("NAXIS2")),
    }


def _median(values: list[float]) -> float:
    ordered = sorted(float(value) for value in values)
    count = len(ordered)
    if not count:
        raise PrepareCopyError("Cannot calculate median of an empty value set")
    middle = count // 2
    if count % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _same_number(left: float | int | None, right: float | int | None, tolerance: float = 1e-6) -> bool:
    if left is None or right is None:
        return False
    return abs(float(left) - float(right)) <= tolerance


def _same_text(left: str | None, right: str | None) -> bool:
    if left is None or right is None:
        return False
    return left.strip().casefold() == right.strip().casefold()


def _require_light_metadata(metadata: dict[str, Any]) -> None:
    missing = [
        key for key in (
            "imagetype", "camera", "xbin", "ybin", "gain", "offset",
            "exptime", "temperature", "width", "height"
        )
        if metadata.get(key) is None
    ]
    if missing:
        raise PrepareCopyError(
            f"Light FITS is missing calibration-critical header fields {missing}: "
            f"{metadata['path']}"
        )
    if "light" not in str(metadata["imagetype"]).casefold():
        raise PrepareCopyError(
            f"Expected a Light frame but IMAGETYP/FRAME is {metadata['imagetype']!r}: "
            f"{metadata['path']}"
        )


def light_profile(light_directory: Path) -> dict[str, Any]:
    files = direct_fits(light_directory)
    if not files:
        raise PrepareCopyError(f"No direct FITS light files found in {light_directory}")
    frames = [read_fits_metadata(path) for path in files]
    for frame in frames:
        _require_light_metadata(frame)

    baseline = frames[0]
    for frame in frames[1:]:
        mismatches: list[str] = []
        if not _same_text(frame["camera"], baseline["camera"]): mismatches.append("camera")
        if frame["xbin"] != baseline["xbin"]: mismatches.append("xbin")
        if frame["ybin"] != baseline["ybin"]: mismatches.append("ybin")
        if not _same_number(frame["gain"], baseline["gain"]): mismatches.append("gain")
        if not _same_number(frame["offset"], baseline["offset"]): mismatches.append("offset")
        if not _same_number(frame["exptime"], baseline["exptime"], tolerance=max(0.001, float(baseline["exptime"]) * 1e-4)):
            mismatches.append("exptime")
        if frame["width"] != baseline["width"]: mismatches.append("width")
        if frame["height"] != baseline["height"]: mismatches.append("height")
        if mismatches:
            raise PrepareCopyError(
                "Light set contains incompatible calibration profiles. "
                f"Reference={baseline['path']} conflicting={frame['path']} fields={mismatches}"
            )

    temperatures = [float(frame["temperature"]) for frame in frames]
    temp_min = min(temperatures)
    temp_max = max(temperatures)
    if temp_max - temp_min > 2.0:
        raise PrepareCopyError(
            f"Light temperature spread is too large for one calibration set: "
            f"{temp_min:.3f}C to {temp_max:.3f}C in {light_directory}"
        )

    dates = sorted(str(frame["date_obs"]) for frame in frames if frame.get("date_obs"))
    return {
        "count": len(frames),
        "camera": baseline["camera"],
        "xbin": baseline["xbin"],
        "ybin": baseline["ybin"],
        "gain": baseline["gain"],
        "offset": baseline["offset"],
        "exptime": baseline["exptime"],
        "width": baseline["width"],
        "height": baseline["height"],
        "temperature_min_c": temp_min,
        "temperature_max_c": temp_max,
        "temperature_median_c": _median(temperatures),
        "date_obs_first": dates[0] if dates else None,
        "date_obs_last": dates[-1] if dates else None,
        "source_directory": str(light_directory),
    }


def recursive_calibration_fits(root: Path) -> list[Path]:
    if not root.is_dir():
        raise PrepareCopyError(f"Calibration root is missing: {root}")
    result: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.name.startswith("._") or path.suffix.lower() not in FITS_SUFFIXES:
            continue
        relative_parts = path.relative_to(root).parts[:-1]
        if any(
            part.startswith(".") or part.casefold() in IGNORED_CALIBRATION_DIRECTORY_NAMES
            for part in relative_parts
        ):
            continue
        result.append(path)
    return sorted(result)


def _calibration_rejection_reasons(
    metadata: dict[str, Any], profile: dict[str, Any], kind: str
) -> list[str]:
    reasons: list[str] = []
    image_type = str(metadata.get("imagetype") or "").casefold()
    expected = "dark" if kind == "dark" else "bias"
    if expected not in image_type:
        reasons.append(f"imagetype_not_{expected}")
    if not _same_text(metadata.get("camera"), profile.get("camera")):
        reasons.append("camera_mismatch")
    if metadata.get("xbin") != profile.get("xbin"):
        reasons.append("xbin_mismatch")
    if metadata.get("ybin") != profile.get("ybin"):
        reasons.append("ybin_mismatch")
    if not _same_number(metadata.get("gain"), profile.get("gain")):
        reasons.append("gain_mismatch")
    if not _same_number(metadata.get("offset"), profile.get("offset")):
        reasons.append("offset_mismatch")
    if metadata.get("width") != profile.get("width"):
        reasons.append("width_mismatch")
    if metadata.get("height") != profile.get("height"):
        reasons.append("height_mismatch")

    temperature = metadata.get("temperature")
    if temperature is None:
        reasons.append("temperature_missing")
    else:
        tolerance = DARK_TEMP_TOLERANCE_C if kind == "dark" else BIAS_TEMP_TOLERANCE_C
        if abs(float(temperature) - float(profile["temperature_median_c"])) > tolerance:
            reasons.append("temperature_mismatch")

    exposure = metadata.get("exptime")
    if exposure is None:
        reasons.append("exptime_missing")
    elif kind == "dark":
        tolerance = max(0.001, float(profile["exptime"]) * 1e-4)
        if not _same_number(exposure, profile.get("exptime"), tolerance=tolerance):
            reasons.append("exptime_mismatch")
    elif float(exposure) <= 0:
        reasons.append("invalid_bias_exptime")

    # FILTER and capture date are intentionally NOT compatibility criteria for
    # darks/biases. Calibration date remains organizational metadata only.
    return reasons


def select_calibration_frames(
    root: Path, profile: dict[str, Any], kind: str
) -> dict[str, Any]:
    if kind not in {"dark", "bias"}:
        raise PrepareCopyError(f"Unsupported calibration kind: {kind}")
    candidates = recursive_calibration_fits(root)
    if not candidates:
        raise PrepareCopyError(f"No calibration FITS files found under {root}")

    eligible: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for path in candidates:
        metadata = read_fits_metadata(path)
        reasons = _calibration_rejection_reasons(metadata, profile, kind)
        if reasons:
            rejected.append({"path": str(path), "reasons": reasons})
        else:
            eligible.append(metadata)

    if kind == "bias" and eligible:
        exposure_counts: dict[float, int] = {}
        for metadata in eligible:
            key = round(float(metadata["exptime"]), 6)
            exposure_counts[key] = exposure_counts.get(key, 0) + 1
        chosen_exposure = sorted(exposure_counts, key=lambda key: (-exposure_counts[key], key))[0]
        still_eligible: list[dict[str, Any]] = []
        for metadata in eligible:
            key = round(float(metadata["exptime"]), 6)
            if key == chosen_exposure:
                still_eligible.append(metadata)
            else:
                rejected.append({
                    "path": str(metadata["path"]),
                    "reasons": ["bias_exposure_group_not_selected"],
                })
        eligible = still_eligible
    else:
        chosen_exposure = None

    selected = sorted(Path(metadata["path"]) for metadata in eligible)
    minimum = MIN_DARK_FRAMES if kind == "dark" else MIN_BIAS_FRAMES
    if len(selected) < minimum:
        reason_counts: dict[str, int] = {}
        for item in rejected:
            for reason in item["reasons"]:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
        raise PrepareCopyError(
            f"Only {len(selected)} compatible {kind} frames found under {root}; "
            f"minimum is {minimum}. Rejection summary: {reason_counts}"
        )

    temperatures = [float(metadata["temperature"]) for metadata in eligible]
    return {
        "kind": kind,
        "root": str(root),
        "candidate_count": len(candidates),
        "selected_count": len(selected),
        "selected_files": [str(path) for path in selected],
        "rejected_count": len(rejected),
        "rejected_files": rejected,
        "selected_temperature_min_c": min(temperatures),
        "selected_temperature_max_c": max(temperatures),
        "selected_temperature_median_c": _median(temperatures),
        "selected_bias_exptime_s": chosen_exposure,
        "temperature_tolerance_c": (
            DARK_TEMP_TOLERANCE_C if kind == "dark" else BIAS_TEMP_TOLERANCE_C
        ),
        "capture_date_used_for_selection": False,
        "filter_header_used_for_selection": False,
    }


def plan_project_calibration(workspace: Path, project_name: str) -> dict[str, Any]:
    workspace = workspace.resolve()
    project = workspace / "Projects" / project_name
    calibration = workspace / "calibration"
    source_lights_root = project / "source" / "lights"
    source_flats_root = project / "source" / "flats"
    if not project.is_dir():
        raise PrepareCopyError(f"Project does not exist: {project}")
    if not source_lights_root.is_dir():
        raise PrepareCopyError(f"Project source lights directory is missing: {source_lights_root}")
    if not source_flats_root.is_dir():
        raise PrepareCopyError(f"Project source flats directory is missing: {source_flats_root}")
    if not calibration.is_dir():
        raise PrepareCopyError(f"Workspace calibration directory is missing: {calibration}")

    dark_root = calibration / "darks"
    if not dark_root.is_dir() and (calibration / "dark").is_dir():
        dark_root = calibration / "dark"
    bias_root = calibration / "bias"
    if not bias_root.is_dir() and (calibration / "biases").is_dir():
        bias_root = calibration / "biases"

    filters = sorted(
        item.name for item in source_lights_root.iterdir()
        if item.is_dir() and not item.name.startswith(".") and bool(direct_fits(item))
    )
    if not filters:
        raise PrepareCopyError(f"No filter directories found under {source_lights_root}")

    report_filters: list[dict[str, Any]] = []
    for filter_name in filters:
        lights = source_lights_root / filter_name
        flats = source_flats_root / filter_name
        if not flats.is_dir() or not direct_fits(flats):
            raise PrepareCopyError(f"No direct FITS flats found for filter {filter_name}: {flats}")
        profile = light_profile(lights)
        dark_selection = select_calibration_frames(dark_root, profile, "dark")
        bias_selection = select_calibration_frames(bias_root, profile, "bias")
        report_filters.append({
            "filter": filter_name,
            "light_profile": profile,
            "dark_selection": dark_selection,
            "bias_selection": bias_selection,
        })

    return {
        "policy_version": CALIBRATION_SELECTION_POLICY_VERSION,
        "date_used_for_compatibility": False,
        "workspace": str(workspace),
        "project": str(project),
        "calibration_root": str(calibration),
        "dark_temperature_tolerance_c": DARK_TEMP_TOLERANCE_C,
        "bias_temperature_tolerance_c": BIAS_TEMP_TOLERANCE_C,
        "filters": report_filters,
    }


def _selected_source_map(sources: list[Path]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for source in sources:
        previous = result.get(source.name)
        if previous is not None and sha256_file(previous) != sha256_file(source):
            raise PrepareCopyError(
                f"Compatible calibration selection contains a basename collision with "
                f"different content: {previous} vs {source}"
            )
        result[source.name] = source
    return result


def preflight_existing_calibration_destination(
    destination: Path, selected_sources: list[Path], *, category: str, filter_name: str
) -> None:
    if destination.is_symlink() or not destination.exists():
        return
    if not destination.is_dir():
        raise PrepareCopyError(f"Prepared calibration destination is not a directory: {destination}")
    selected = _selected_source_map(selected_sources)
    stale: list[str] = []
    for existing in direct_fits(destination):
        source = selected.get(existing.name)
        if source is None:
            stale.append(f"{existing.name}:not_in_current_selection")
            continue
        if existing.stat().st_size != source.stat().st_size or sha256_file(existing) != sha256_file(source):
            stale.append(f"{existing.name}:content_mismatch")
    if stale:
        raise PrepareCopyError(
            f"Prepared {category} for filter {filter_name} contains {len(stale)} stale or "
            f"incompatible FITS frame(s); refusing to mix calibration populations. "
            f"A preservation-safe calibration refresh is required. Examples: {stale[:10]}"
        )


def stage_selected_files(sources: list[Path], destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    selected = _selected_source_map(sources)
    for name, source in sorted(selected.items()):
        target = destination / name
        if target.exists():
            if target.stat().st_size != source.stat().st_size or sha256_file(target) != sha256_file(source):
                raise PrepareCopyError(f"Staging collision for selected calibration file: {target}")
            continue
        try:
            os.link(source, target)
        except OSError:
            shutil.copy2(source, target)
            if target.stat().st_size != source.stat().st_size or sha256_file(target) != sha256_file(source):
                raise PrepareCopyError(f"Selected calibration staging verification failed: {source}")


def verify_prepared_calibration(
    destination: Path, selected_sources: list[Path], *, category: str, filter_name: str
) -> None:
    selected = _selected_source_map(selected_sources)
    actual = {path.name: path for path in direct_fits(destination)}
    if set(actual) != set(selected):
        missing = sorted(set(selected) - set(actual))
        extra = sorted(set(actual) - set(selected))
        raise PrepareCopyError(
            f"Prepared {category} verification failed for filter {filter_name}: "
            f"missing={missing[:10]} extra={extra[:10]}"
        )
    for name, source in selected.items():
        prepared = actual[name]
        if prepared.stat().st_size != source.stat().st_size or sha256_file(prepared) != sha256_file(source):
            raise PrepareCopyError(
                f"Prepared {category} checksum verification failed for filter {filter_name}: {prepared}"
            )



def ensure_real_directory(
    destination: Path,
    *,
    preserve_rejects_from: Path | None = None,
    staging_root: Path,
) -> int:
    preserved = 0
    staged_rejects: Path | None = None

    if destination.is_symlink():
        if preserve_rejects_from and preserve_rejects_from.is_dir():
            staged_rejects = staging_root / destination.parent.name / "rejects"
            preserved = merge_tree_no_overwrite(
                preserve_rejects_from, staged_rejects
            )
        # Removes only the directory link, never its target.
        destination.unlink()
        destination.mkdir(parents=True, exist_ok=False)
    elif destination.exists():
        if not destination.is_dir():
            raise PrepareCopyError(
                f"Prepared destination is neither a directory nor symlink: "
                f"{destination}"
            )
    else:
        destination.mkdir(parents=True, exist_ok=False)

    if destination.is_symlink():
        raise PrepareCopyError(
            f"Destination remained a symlink unexpectedly: {destination}"
        )

    if staged_rejects and staged_rejects.is_dir():
        merge_tree_no_overwrite(staged_rejects, destination / "rejects")
    return preserved


def print_progress(
    label: str, total: int, actions: Iterable[Callable[[], None]]
) -> None:
    print(f"Copying {total} {label} ", end="", flush=True)
    for action in actions:
        action()
        print(".", end="", flush=True)
    print()


def sync_category(
    *,
    source: Path,
    destination: Path,
    category: str,
    filter_name: str,
    staging_root: Path,
    preserve_legacy_rejects: bool = False,
) -> CopyStats:
    stats = CopyStats(
        category=category,
        filter_name=filter_name,
        source=str(source),
        destination=str(destination),
    )

    legacy_rejects = source / "rejects" if preserve_legacy_rejects else None
    stats.legacy_reject_files_preserved = ensure_real_directory(
        destination,
        preserve_rejects_from=legacy_rejects,
        staging_root=staging_root,
    )

    rejected_checksums = (
        load_rejected_checksums(destination / "rejects")
        if category == "lights"
        else set()
    )
    sources = direct_fits(source)
    stats.discovered = len(sources)
    source_names = {item.name for item in sources}

    destination_direct = direct_fits(destination)
    stats.preserved_extra_direct_files = sum(
        1 for item in destination_direct if item.name not in source_names
    )

    actions: list[Callable[[], None]] = []
    for source_file in sources:
        def operation(source_file: Path = source_file) -> None:
            if rejected_checksums:
                digest = sha256_file(source_file)
                if digest in rejected_checksums:
                    stats.skipped_rejected += 1
                    return
            result = copy_file_no_overwrite(
                source_file, destination / source_file.name
            )
            if result == "copied":
                stats.copied += 1
            else:
                stats.identical_existing += 1
        actions.append(operation)

    print_progress(f"{category.capitalize()} – {filter_name}", len(sources), actions)
    return stats


def write_manifest(project: Path, payload: dict[str, Any]) -> Path:
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = project / ".astroproc" / "prepare-copy"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{timestamp}.json"
    temporary = path.with_suffix(".json.partial")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return path


def prepare_project_copy(workspace: Path, project_name: str) -> dict[str, Any]:
    workspace = workspace.resolve()
    project = workspace / "Projects" / project_name
    source_lights_root = project / "source" / "lights"
    source_flats_root = project / "source" / "flats"
    processing_root = project / "processing"

    plan = plan_project_calibration(workspace, project_name)

    # Fail closed BEFORE copying anything if a real prepared dark/bias directory
    # contains frames outside the new compatibility selection. This prevents a
    # rerun from silently mixing stale calibration populations with the new one.
    for item in plan["filters"]:
        filter_name = item["filter"]
        filter_root = processing_root / filter_name
        dark_sources = [Path(path) for path in item["dark_selection"]["selected_files"]]
        bias_sources = [Path(path) for path in item["bias_selection"]["selected_files"]]
        preflight_existing_calibration_destination(
            filter_root / "darks", dark_sources, category="darks", filter_name=filter_name
        )
        preflight_existing_calibration_destination(
            filter_root / "biases", bias_sources, category="biases", filter_name=filter_name
        )

    processing_root.mkdir(parents=True, exist_ok=True)
    staging_parent = project / ".astroproc" / "prepare-copy-staging"
    staging_parent.mkdir(parents=True, exist_ok=True)

    report_filters: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="run-", dir=staging_parent) as staging_name:
        staging_root = Path(staging_name)

        for item in plan["filters"]:
            filter_name = item["filter"]
            source_lights = source_lights_root / filter_name
            source_flats = source_flats_root / filter_name
            dark_sources = [Path(path) for path in item["dark_selection"]["selected_files"]]
            bias_sources = [Path(path) for path in item["bias_selection"]["selected_files"]]

            dark_stage = staging_root / filter_name / "selected-darks"
            bias_stage = staging_root / filter_name / "selected-biases"
            stage_selected_files(dark_sources, dark_stage)
            stage_selected_files(bias_sources, bias_stage)

            filter_root = processing_root / filter_name
            filter_root.mkdir(exist_ok=True)
            stats = [
                sync_category(
                    source=source_lights,
                    destination=filter_root / "lights",
                    category="lights",
                    filter_name=filter_name,
                    staging_root=staging_root,
                ),
                sync_category(
                    source=source_flats,
                    destination=filter_root / "flats",
                    category="flats",
                    filter_name=filter_name,
                    staging_root=staging_root,
                ),
                sync_category(
                    source=dark_stage,
                    destination=filter_root / "darks",
                    category="darks",
                    filter_name=filter_name,
                    staging_root=staging_root,
                ),
                sync_category(
                    source=bias_stage,
                    destination=filter_root / "biases",
                    category="biases",
                    filter_name=filter_name,
                    staging_root=staging_root,
                ),
            ]

            for category in ("lights", "flats", "darks", "biases"):
                prepared = filter_root / category
                if prepared.is_symlink() or not prepared.is_dir():
                    raise PrepareCopyError(f"Prepared category is not a real directory: {prepared}")
                if not direct_fits(prepared):
                    raise PrepareCopyError(f"Prepared category contains no direct FITS files: {prepared}")

            verify_prepared_calibration(
                filter_root / "darks", dark_sources, category="darks", filter_name=filter_name
            )
            verify_prepared_calibration(
                filter_root / "biases", bias_sources, category="biases", filter_name=filter_name
            )

            report_filters.append({
                "filter": filter_name,
                "light_profile": item["light_profile"],
                "dark_selection": item["dark_selection"],
                "bias_selection": item["bias_selection"],
                "copy_stats": [asdict(value) for value in stats],
            })

    payload = {
        "schema_version": 2,
        "prepare_version": VERSION,
        "calibration_selection_policy_version": CALIBRATION_SELECTION_POLICY_VERSION,
        "capture_date_used_for_calibration_selection": False,
        "project": str(project),
        "workspace": str(workspace),
        "calibration_root": plan["calibration_root"],
        "mode": "copy",
        "filters": report_filters,
    }
    manifest = write_manifest(project, payload)
    payload["manifest"] = str(manifest)

    print(f"Project '{project_name}' prepared successfully using real directories.")
    print("----------------------------------------")
    print("Calibration selection policy: FITS compatibility v2 (capture date ignored)")
    for item in report_filters:
        profile = item["light_profile"]
        dark = item["dark_selection"]
        bias = item["bias_selection"]
        print(
            f"Filter {item['filter']}: lights={profile['count']} camera={profile['camera']} "
            f"bin={profile['xbin']}x{profile['ybin']} gain={profile['gain']:g} "
            f"offset={profile['offset']:g} exp={profile['exptime']:g}s "
            f"temp={profile['temperature_median_c']:.3f}C"
        )
        print(
            f"  darks: selected={dark['selected_count']}/{dark['candidate_count']} "
            f"temp={dark['selected_temperature_min_c']:.3f}..{dark['selected_temperature_max_c']:.3f}C "
            f"tolerance=±{dark['temperature_tolerance_c']:.1f}C"
        )
        print(
            f"  biases: selected={bias['selected_count']}/{bias['candidate_count']} "
            f"exp={bias['selected_bias_exptime_s']:g}s "
            f"temp={bias['selected_temperature_min_c']:.3f}..{bias['selected_temperature_max_c']:.3f}C "
            f"tolerance=±{bias['temperature_tolerance_c']:.1f}C"
        )
        for value in item["copy_stats"]:
            print(
                f"  {value['category']}: discovered={value['discovered']} "
                f"copied={value['copied']} identical={value['identical_existing']} "
                f"skipped_rejected={value['skipped_rejected']}"
            )
    print(f"Prepare manifest: {manifest}")
    return payload



def parse_prepare_arguments(argv: list[str]) -> str | None:
    if len(argv) == 2 and argv[0] in {"-p", "--prepare"}:
        return argv[1]
    if len(argv) == 1 and argv[0].startswith("--prepare="):
        value = argv[0].split("=", 1)[1]
        return value or None
    return None



def resolve_workspace_root() -> Path:
    explicit = os.environ.get("ASTROPROC_WORKSPACE_ROOT")
    if explicit:
        return Path(explicit).expanduser().resolve()
    projects = os.environ.get("ASTROPROC_PROJECTS_ROOT")
    if projects:
        return Path(projects).expanduser().resolve().parent
    return Path(__file__).resolve().parent.parent

def maybe_handle_prepare(argv: list[str]) -> int | None:
    project_name = parse_prepare_arguments(argv)
    if project_name is None:
        return None
    try:
        prepare_project_copy(resolve_workspace_root(), project_name)
    except PrepareCopyError as exc:
        eprint(f"Prepare failed: {exc}")
        return 2
    except Exception as exc:
        eprint(f"Prepare failed unexpectedly: {type(exc).__name__}: {exc}")
        return 2
    return 0


def self_test() -> None:
    from astropy.io import fits
    import numpy as np

    with tempfile.TemporaryDirectory(prefix="astroproc-prepare-copy-test-") as tmp:
        workspace = Path(tmp)
        project = workspace / "Projects" / "Synthetic"
        source_lights = project / "source" / "lights" / "Ha"
        source_flats = project / "source" / "flats" / "Ha"
        good_darks = workspace / "calibration" / "darks" / "2025-05-27"
        bad_darks = workspace / "calibration" / "darks" / "2025-06-25"
        good_biases = workspace / "calibration" / "bias" / "2025-05-27"
        bad_biases = workspace / "calibration" / "bias" / "2025-06-25"
        for path in (source_lights, source_flats, good_darks, bad_darks, good_biases, bad_biases):
            path.mkdir(parents=True, exist_ok=True)

        def write_fits(
            path: Path, value: int, *, image_type: str, date_obs: str,
            exptime: float, temperature: float, filter_name: str = "Ha"
        ) -> None:
            header = fits.Header()
            header["DATE-OBS"] = date_obs
            header["IMAGETYP"] = image_type
            header["INSTRUME"] = "ZWO ASI533MM Pro"
            header["XBINNING"] = 1
            header["YBINNING"] = 1
            header["GAIN"] = 102
            header["OFFSET"] = 70
            header["EXPTIME"] = exptime
            header["CCD-TEMP"] = temperature
            header["FILTER"] = filter_name
            fits.PrimaryHDU(np.full((4, 4), value, dtype=np.uint16), header=header).writeto(path)

        good_light = source_lights / "good.fit"
        rejected_light = source_lights / "rejected.fit"
        write_fits(good_light, 100, image_type="Light", date_obs="2026-07-17T08:00:00", exptime=30.0, temperature=-20.0)
        write_fits(rejected_light, 101, image_type="Light", date_obs="2026-07-17T08:01:00", exptime=30.0, temperature=-20.1)
        write_fits(source_flats / "flat.fit", 200, image_type="Flat", date_obs="2026-07-17T07:00:00", exptime=0.2, temperature=-20.0)

        for index in range(12):
            write_fits(
                good_darks / f"good-dark-{index:02d}.fit", 300 + index,
                image_type="Dark", date_obs=f"2025-05-27T01:{index:02d}:00",
                exptime=30.0, temperature=-20.0 + (index % 3) * 0.1,
                filter_name="SII",
            )
            write_fits(
                good_biases / f"good-bias-{index:02d}.fit", 400 + index,
                image_type="Bias", date_obs=f"2025-05-27T02:{index:02d}:00",
                exptime=0.001, temperature=-19.8 + (index % 3) * 0.1,
                filter_name="SII",
            )
        for index in range(10):
            write_fits(
                bad_darks / f"bad-dark-{index:02d}.fit", 500 + index,
                image_type="Dark", date_obs=f"2025-06-25T01:{index:02d}:00",
                exptime=30.0, temperature=0.0,
                filter_name="SII",
            )
            write_fits(
                bad_biases / f"bad-bias-{index:02d}.fit", 600 + index,
                image_type="Bias", date_obs=f"2025-06-25T02:{index:02d}:00",
                exptime=0.001, temperature=0.0,
                filter_name="SII",
            )

        # Preserve the prior rejection-index/rerun behavior test.
        rejects = source_lights / "rejects"
        rejects.mkdir()
        rejected_hash = sha256_file(rejected_light)
        rejected_destination = rejects / rejected_light.name
        rejected_light.rename(rejected_destination)
        index = {"entries": {rejected_hash: {"reason": "synthetic"}}}
        (rejects / "rejection-index.json").write_text(json.dumps(index))

        # Keep lights as a real prepared directory with existing rejection evidence.
        # Calibration v2 does not need to retest the unrelated legacy lights-symlink
        # migration path here; the installed copy-mode implementation already owns
        # that behavior. We DO retain wrong warm dark/bias symlinks so this test
        # still proves v2 replaces calibration selection rather than following date.
        processing = project / "processing" / "Ha"
        processing.mkdir(parents=True)
        prepared_lights = processing / "lights"
        prepared_lights.mkdir()
        prepared_rejects = prepared_lights / "rejects"
        prepared_rejects.mkdir()
        shutil.copy2(rejects / "rejection-index.json", prepared_rejects / "rejection-index.json")
        shutil.copy2(rejected_destination, prepared_rejects / rejected_destination.name)
        (processing / "flats").symlink_to(source_flats)
        (processing / "darks").symlink_to(bad_darks)
        (processing / "biases").symlink_to(bad_biases)

        plan = plan_project_calibration(workspace, "Synthetic")
        planned = plan["filters"][0]
        assert planned["dark_selection"]["selected_count"] == 12
        assert planned["bias_selection"]["selected_count"] == 12
        assert all("good-dark" in path for path in planned["dark_selection"]["selected_files"])
        assert all("good-bias" in path for path in planned["bias_selection"]["selected_files"])
        assert plan["date_used_for_compatibility"] is False

        prepare_project_copy(workspace, "Synthetic")
        for category in ("lights", "flats", "darks", "biases"):
            path = processing / category
            assert path.is_dir()
            assert not path.is_symlink()
        assert len(direct_fits(processing / "darks")) == 12
        assert len(direct_fits(processing / "biases")) == 12
        assert not (processing / "lights" / "rejected.fit").exists()
        assert (processing / "lights" / "rejects" / "rejection-index.json").is_file()

        # Exact-selection rerun is allowed and remains stable.
        prepare_project_copy(workspace, "Synthetic")
        assert len(direct_fits(processing / "darks")) == 12
        assert len(direct_fits(processing / "biases")) == 12

        # A stale incompatible real calibration file causes a fail-closed result;
        # the file is preserved rather than deleted or silently mixed.
        stale = processing / "darks" / "stale-warm-dark.fit"
        write_fits(
            stale, 999, image_type="Dark", date_obs="2025-06-25T03:00:00",
            exptime=30.0, temperature=0.0, filter_name="SII"
        )
        try:
            prepare_project_copy(workspace, "Synthetic")
        except PrepareCopyError as exc:
            assert "refusing to mix calibration populations" in str(exc)
        else:
            raise AssertionError("Expected stale prepared calibration preflight to fail closed")
        assert stale.is_file()

    print(json.dumps({
        "status": "ok",
        "version": VERSION,
        "calibration_selection_policy_version": CALIBRATION_SELECTION_POLICY_VERSION,
        "date_used_for_compatibility": False,
        "dark_temperature_tolerance_c": DARK_TEMP_TOLERANCE_C,
        "bias_temperature_tolerance_c": BIAS_TEMP_TOLERANCE_C,
        "stale_prepared_calibration_fails_closed": True,
        "real_directory_copy_mode_preserved": True,
    }, sort_keys=True))



def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--workspace")
    parser.add_argument("--project")
    args = parser.parse_args()

    if args.version:
        print(VERSION)
        return 0
    if args.self_test:
        self_test()
        return 0
    if args.workspace and args.project:
        prepare_project_copy(Path(args.workspace), args.project)
        return 0
    parser.error("Use --self-test, --version, or --workspace with --project.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
