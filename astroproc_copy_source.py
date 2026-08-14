#!/usr/bin/env python3
"""Canonical source-layout implementation for ``astroproc -c/--copy``.

The ASIAIR source is read-only. Project lights/flats are copied directly to
``<workspace>/Projects/<project>/source`` and shared calibration frames are
copied to ``<workspace>/calibration``. AppleDouble ``._*`` files are ignored
before FITS inspection.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Iterable

VERSION = "1.1.0"
SOURCE_FITS_SUFFIXES = {".fit"}
SUPPORTED_TYPES = ("Autorun", "Live", "Plan", "Preview", "Stacked", "Video")
FILTER_ALIASES = {
    "ha": "Ha", "h-alpha": "Ha", "halpha": "Ha",
    "sii": "SII", "s2": "SII",
    "oiii": "OIII", "o3": "OIII",
}

class CopySourceError(RuntimeError):
    pass

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()

def workspace_root() -> Path:
    explicit = os.environ.get("ASTROPROC_WORKSPACE_ROOT")
    if explicit:
        return Path(explicit).expanduser().resolve()
    projects = os.environ.get("ASTROPROC_PROJECTS_ROOT")
    if projects:
        return Path(projects).expanduser().resolve().parent
    return Path(__file__).resolve().parent.parent

def project_name_safe(name: str) -> str:
    name = name.strip()
    if not name or name in {".", ".."} or "/" in name or "\\" in name or "\x00" in name:
        raise CopySourceError("Invalid project name.")
    return re.sub(r"[^a-zA-Z0-9\s\-_]", "_", name)

def source_project_safe(name: str) -> str:
    name = name.strip()
    if not name or name in {".", ".."} or "/" in name or "\\" in name or "\x00" in name:
        raise CopySourceError("Invalid ASIAIR source-project name.")
    return name

def canonical_filter(value: Any) -> str:
    if value is None:
        return "Unknown"
    text = str(value).strip()
    if not text:
        return "Unknown"
    alias = FILTER_ALIASES.get(text.lower())
    if alias:
        return alias
    safe = re.sub(r"[^a-zA-Z0-9._+\-]", "_", text).strip("._")
    return safe or "Unknown"

def eligible_source_fits(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    out: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.name.startswith("._"):
            continue
        if path.suffix.lower() not in SOURCE_FITS_SUFFIXES:
            continue
        out.append(path)
    return sorted(out)

def read_header(path: Path) -> dict[str, Any]:
    from astropy.io import fits
    try:
        with fits.open(path, memmap=False, do_not_scale_image_data=True) as hdul:
            return dict(hdul[0].header)
    except Exception as exc:
        raise CopySourceError(f"Unreadable FITS {path}: {exc}") from exc

def observation_date(header: dict[str, Any]) -> dt.date | None:
    raw = header.get("DATE-OBS")
    if raw is None:
        return None
    text = str(raw).strip().replace("Z", "+00:00")
    try:
        return dt.datetime.fromisoformat(text).date()
    except ValueError:
        try:
            return dt.date.fromisoformat(text[:10])
        except ValueError:
            return None

def capture_directory(source_root: Path, capture_type: str) -> Path:
    wanted = capture_type.lower()
    if wanted not in {x.lower() for x in SUPPORTED_TYPES}:
        raise CopySourceError(
            f"Unsupported capture type {capture_type!r}; allowed: {', '.join(SUPPORTED_TYPES)}"
        )
    matches = [p for p in source_root.iterdir() if p.is_dir() and p.name.lower() == wanted]
    if len(matches) != 1:
        raise CopySourceError(f"Capture-type directory not found uniquely for {capture_type}: {source_root}")
    return matches[0]

def resolve_named_directory(parent: Path, name: str) -> Path | None:
    direct = parent / name
    if direct.is_dir():
        return direct
    if not parent.is_dir():
        return None
    matches = [p for p in parent.rglob("*") if p.is_dir() and p.name == name]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise CopySourceError(f"ASIAIR source project is ambiguous beneath {parent}: {name}")
    return None

def destination_for(source: Path, dest_dir: Path) -> tuple[Path, str]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / source.name
    if not target.exists():
        return target, "new"
    if target.is_file() and source.stat().st_size == target.stat().st_size and sha256_file(source) == sha256_file(target):
        return target, "identical"
    digest = sha256_file(source)[:16]
    target = dest_dir / f"{source.stem}__sha256-{digest}{source.suffix.lower()}"
    if target.exists():
        if target.is_file() and source.stat().st_size == target.stat().st_size and sha256_file(source) == sha256_file(target):
            return target, "identical_collision"
        raise CopySourceError(f"Checksum-suffixed destination collision: {target}")
    return target, "collision"

def copy_verified(source: Path, dest_dir: Path) -> tuple[str, Path]:
    target, disposition = destination_for(source, dest_dir)
    if disposition.startswith("identical"):
        return "identical", target
    partial = target.with_name(f".{target.name}.astroproc-copy-{os.getpid()}.partial")
    if partial.exists():
        raise CopySourceError(f"Temporary copy path already exists: {partial}")
    try:
        shutil.copy2(source, partial)
        if sha256_file(source) != sha256_file(partial):
            raise CopySourceError(f"Checksum mismatch while copying {source}")
        os.replace(partial, target)
    finally:
        if partial.exists():
            partial.unlink()
    return ("collision" if disposition == "collision" else "copied"), target

def dated_calibration_copy(source: Path, category_root: Path, header: dict[str, Any], warnings: list[str]) -> tuple[str, Path] | None:
    date = observation_date(header)
    if date is None:
        warnings.append(f"Skipped {source.name}: calibration FITS has no readable DATE-OBS")
        return None
    return copy_verified(source, category_root / date.isoformat())

def write_manifest(project: Path, payload: dict[str, Any]) -> Path:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = project / ".astroproc" / "import"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{stamp}.json"
    tmp = path.with_suffix(".json.partial")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path

def process_files(files: Iterable[Path], destination_root: Path, *, kind: str, warnings: list[str], light_dates: dict[str, set[dt.date]] | None = None, shared_flat: bool = False) -> dict[str, Any]:
    report: dict[str, Any] = {"discovered": 0, "copied": 0, "identical": 0, "collisions": 0, "skipped": 0, "filters": {}}
    for source in files:
        report["discovered"] += 1
        try:
            header = read_header(source)
        except CopySourceError as exc:
            report["skipped"] += 1
            warnings.append(str(exc))
            continue
        filter_name = canonical_filter(header.get("FILTER"))
        if shared_flat:
            date = observation_date(header)
            dates = (light_dates or {}).get(filter_name, set())
            if date is None:
                report["skipped"] += 1
                warnings.append(f"Skipped shared flat {source.name}: no readable DATE-OBS")
                continue
            if not dates or min(abs((date - d).days) for d in dates) > 1:
                report["skipped"] += 1
                continue
        dest = destination_root / filter_name
        result, _ = copy_verified(source, dest)
        if result == "copied": report["copied"] += 1
        elif result == "collision": report["copied"] += 1; report["collisions"] += 1
        else: report["identical"] += 1
        report["filters"][filter_name] = report["filters"].get(filter_name, 0) + 1
        if kind == "lights" and light_dates is not None:
            date = observation_date(header)
            if date is not None:
                light_dates.setdefault(filter_name, set()).add(date)
            else:
                warnings.append(f"Light frame {source.name} missing observation date; retained but not used for flat-date matching")
    return report

def run_copy(project_name: str, source_root_text: str, source_project_name: str | None, capture_types: list[str] | None) -> dict[str, Any]:
    workspace = workspace_root()
    project_name = project_name_safe(project_name)
    source_project = source_project_safe(source_project_name or project_name)
    source_root = Path(source_root_text).expanduser().resolve()
    if not source_root.is_dir():
        raise CopySourceError(f"Source directory does not exist: {source_root}")
    project = workspace / "Projects" / project_name
    if not project.is_dir():
        raise CopySourceError(f"Destination project does not exist: {project}")
    source_out = project / "source"
    lights_out = source_out / "lights"
    flats_out = source_out / "flats"
    calibration = workspace / "calibration"
    warnings: list[str] = []
    types = capture_types or list(SUPPORTED_TYPES)
    light_dates: dict[str, set[dt.date]] = {}
    total = {
        "lights": {"discovered": 0, "copied": 0, "identical": 0, "collisions": 0, "skipped": 0, "filters": {}},
        "flats": {"discovered": 0, "copied": 0, "identical": 0, "collisions": 0, "skipped": 0, "filters": {}},
        "darks": {"discovered": 0, "copied": 0, "identical": 0, "collisions": 0, "skipped": 0},
        "bias": {"discovered": 0, "copied": 0, "identical": 0, "collisions": 0, "skipped": 0},
    }

    def merge(category: str, part: dict[str, Any]) -> None:
        dst = total[category]
        for key in ("discovered", "copied", "identical", "collisions", "skipped"):
            dst[key] += int(part.get(key, 0))
        if "filters" in dst:
            for name, count in part.get("filters", {}).items():
                dst["filters"][name] = dst["filters"].get(name, 0) + count

    found_capture = 0
    for requested in types:
        try:
            capture = capture_directory(source_root, requested)
        except CopySourceError:
            if capture_types:
                raise
            continue
        light_parent = capture / "Light"
        light_dir = resolve_named_directory(light_parent, source_project)
        if light_dir is None:
            if capture_types:
                raise CopySourceError(f"Exact ASIAIR source project not found beneath {light_parent}: {source_project}")
            continue
        light_files = eligible_source_fits(light_dir)
        if not light_files:
            if capture_types:
                raise CopySourceError(f"No eligible light .fit files found beneath {light_dir}")
            continue
        found_capture += 1
        merge("lights", process_files(light_files, lights_out, kind="lights", warnings=warnings, light_dates=light_dates))

        flat_parent = capture / "Flat"
        named_flat = resolve_named_directory(flat_parent, source_project)
        if named_flat is not None:
            flat_files = eligible_source_fits(named_flat)
            merge("flats", process_files(flat_files, flats_out, kind="flats", warnings=warnings))
        else:
            flat_files = eligible_source_fits(flat_parent)
            merge("flats", process_files(flat_files, flats_out, kind="flats", warnings=warnings, light_dates=light_dates, shared_flat=True))

        for key, folder, dest_name in (("darks", "Dark", "darks"), ("bias", "Bias", "bias")):
            files = eligible_source_fits(capture / folder)
            for source in files:
                total[key]["discovered"] += 1
                try:
                    header = read_header(source)
                    result = dated_calibration_copy(source, calibration / dest_name, header, warnings)
                except CopySourceError as exc:
                    total[key]["skipped"] += 1
                    warnings.append(str(exc))
                    continue
                if result is None:
                    total[key]["skipped"] += 1
                    continue
                disposition, _ = result
                if disposition == "copied": total[key]["copied"] += 1
                elif disposition == "collision": total[key]["copied"] += 1; total[key]["collisions"] += 1
                else: total[key]["identical"] += 1

    if found_capture == 0 or total["lights"]["discovered"] == 0:
        raise CopySourceError("No matching light .fit files found in any selected capture type.")
    if sum(total["lights"]["filters"].values()) == 0:
        raise CopySourceError("No readable light FITS files were imported.")

    payload = {
        "schema_version": 1,
        "copy_helper_version": VERSION,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "workspace": str(workspace),
        "project": project_name,
        "project_path": str(project),
        "source_project": source_project,
        "source_root": str(source_root),
        "capture_types": types,
        "layout": "canonical-source-v1",
        "appledouble_ignored": True,
        "results": total,
        "warnings": warnings,
    }
    manifest = write_manifest(project, payload)
    payload["manifest"] = str(manifest)
    return payload

def parse_copy_arguments(argv: list[str]) -> argparse.Namespace | None:
    if not any(arg in {"-c", "--copy"} or arg.startswith("--copy=") for arg in argv):
        return None
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("-c", "--copy", dest="project", required=True)
    p.add_argument("-sp", "--source-project")
    p.add_argument("-sd", "--source-directory", required=True)
    p.add_argument("-t", "--type")
    return p.parse_args(argv)

def maybe_handle_copy(argv: list[str]) -> int | None:
    args = parse_copy_arguments(argv)
    if args is None:
        return None
    try:
        result = run_copy(args.project, args.source_directory, args.source_project, [args.type] if args.type else None)
    except CopySourceError as exc:
        print(f"Copy failed: {exc}", file=os.sys.stderr)
        return 2
    r = result["results"]
    print("Import Summary:")
    print(f"  Lights: {r['lights']['discovered']} discovered, {r['lights']['copied']} copied, {r['lights']['identical']} already present, {r['lights']['skipped']} skipped")
    print(f"  Flats: {r['flats']['discovered']} discovered, {r['flats']['copied']} copied, {r['flats']['identical']} already present, {r['flats']['skipped']} skipped")
    print(f"  Darks: {r['darks']['copied']} copied, {r['darks']['identical']} already present, {r['darks']['skipped']} skipped")
    print(f"  Bias: {r['bias']['copied']} copied, {r['bias']['identical']} already present, {r['bias']['skipped']} skipped")
    print(f"  Collisions: {sum(r[k]['collisions'] for k in ('lights','flats','darks','bias'))} files safely renamed")
    print("Lights sorted by filter:")
    for name, count in sorted(r["lights"]["filters"].items()): print(f"  - {name}: {count}")
    print("Flats sorted by filter:")
    for name, count in sorted(r["flats"]["filters"].items()): print(f"  - {name}: {count}")
    if result["warnings"]:
        print(f"Warnings ({len(result['warnings'])}):")
        for warning in result["warnings"]: print(f"  - {warning}")
    print(f"Final project path: {result['project_path']}")
    print(f"Import manifest: {result['manifest']}")
    print("Status: Succeeded with warnings" if result["warnings"] else "Status: Succeeded")
    return 0
