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

VERSION = "1.0.0"
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


def parse_fits_observation_date(path: Path) -> dt.date:
    try:
        from astropy.io import fits
    except Exception as exc:
        raise PrepareCopyError(
            "Astropy is required in the AstroProcessor virtual environment."
        ) from exc

    try:
        with fits.open(path, memmap=False, do_not_scale_image_data=False) as hdul:
            raw = hdul[0].header.get("DATE-OBS")
    except Exception as exc:
        raise PrepareCopyError(f"Cannot read FITS header {path}: {exc}") from exc

    if raw is None:
        raise PrepareCopyError(f"DATE-OBS is missing from {path}")
    text = str(raw).strip().replace("Z", "+00:00")
    try:
        return dt.datetime.fromisoformat(text).date()
    except ValueError:
        try:
            return dt.date.fromisoformat(text[:10])
        except ValueError as exc:
            raise PrepareCopyError(
                f"Unparseable DATE-OBS {raw!r} in {path}"
            ) from exc


def first_valid_light_date(light_directory: Path) -> tuple[dt.date, Path]:
    errors: list[str] = []
    candidates = direct_fits(light_directory)
    if not candidates:
        raise PrepareCopyError(
            f"No direct FITS light files found in {light_directory}"
        )
    for candidate in candidates:
        try:
            return parse_fits_observation_date(candidate), candidate
        except PrepareCopyError as exc:
            errors.append(str(exc))
    raise PrepareCopyError(
        "No light frame contained a readable DATE-OBS. "
        + " | ".join(errors[:5])
    )


def dated_directories(root: Path) -> tuple[list[tuple[dt.date, Path]], list[Path]]:
    if not root.is_dir():
        raise PrepareCopyError(f"Calibration root is missing: {root}")
    valid: list[tuple[dt.date, Path]] = []
    ignored: list[Path] = []
    for item in sorted(root.iterdir()):
        if not item.is_dir():
            continue
        if not DATE_DIRECTORY_RE.fullmatch(item.name):
            ignored.append(item)
            continue
        try:
            parsed = dt.date.fromisoformat(item.name)
        except ValueError:
            ignored.append(item)
            continue
        valid.append((parsed, item))
    if not valid:
        raise PrepareCopyError(
            f"No YYYY-MM-DD calibration directories found under {root}"
        )
    return valid, ignored


def choose_closest_calibration(
    root: Path, observation_date: dt.date
) -> tuple[dt.date, Path, list[Path]]:
    valid, ignored = dated_directories(root)
    selected_date, selected_path = min(
        valid,
        key=lambda item: (abs((item[0] - observation_date).days), item[0]),
    )
    return selected_date, selected_path, ignored


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
    calibration = workspace / "calibration"
    source_lights_root = project / "source" / "lights"
    source_flats_root = project / "source" / "flats"
    processing_root = project / "processing"

    if not project.is_dir():
        raise PrepareCopyError(f"Project does not exist: {project}")
    if not source_lights_root.is_dir():
        raise PrepareCopyError(
            f"Project source lights directory is missing: {source_lights_root}"
        )
    if not source_flats_root.is_dir():
        raise PrepareCopyError(
            f"Project source flats directory is missing: {source_flats_root}"
        )
    if not calibration.is_dir():
        raise PrepareCopyError(
            f"Workspace calibration directory is missing: {calibration}"
        )

    dark_root = calibration / "darks"
    if not dark_root.is_dir() and (calibration / "dark").is_dir():
        dark_root = calibration / "dark"
    bias_root = calibration / "bias"
    if not bias_root.is_dir() and (calibration / "biases").is_dir():
        bias_root = calibration / "biases"

    filters = sorted(
        item.name
        for item in source_lights_root.iterdir()
        if item.is_dir()
        and not item.name.startswith(".")
        and bool(direct_fits(item))
    )
    if not filters:
        raise PrepareCopyError(
            f"No filter directories found under {source_lights_root}"
        )

    processing_root.mkdir(parents=True, exist_ok=True)
    staging_parent = project / ".astroproc" / "prepare-copy-staging"
    staging_parent.mkdir(parents=True, exist_ok=True)

    report_filters: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(
        prefix="run-", dir=staging_parent
    ) as staging_name:
        staging_root = Path(staging_name)

        for filter_name in filters:
            source_lights = source_lights_root / filter_name
            source_flats = source_flats_root / filter_name
            observation_date, date_source = first_valid_light_date(source_lights)
            dark_date, dark_source, ignored_dark = choose_closest_calibration(
                dark_root, observation_date
            )
            bias_date, bias_source, ignored_bias = choose_closest_calibration(
                bias_root, observation_date
            )

            filter_root = processing_root / filter_name
            filter_root.mkdir(parents=True, exist_ok=True)

            stats = [
                sync_category(
                    source=source_lights,
                    destination=filter_root / "lights",
                    category="lights",
                    filter_name=filter_name,
                    staging_root=staging_root,
                    preserve_legacy_rejects=True,
                ),
                sync_category(
                    source=source_flats,
                    destination=filter_root / "flats",
                    category="flats",
                    filter_name=filter_name,
                    staging_root=staging_root,
                ),
                sync_category(
                    source=dark_source,
                    destination=filter_root / "darks",
                    category="darks",
                    filter_name=filter_name,
                    staging_root=staging_root,
                ),
                sync_category(
                    source=bias_source,
                    destination=filter_root / "biases",
                    category="biases",
                    filter_name=filter_name,
                    staging_root=staging_root,
                ),
            ]

            for category in ("lights", "flats", "darks", "biases"):
                prepared = filter_root / category
                if prepared.is_symlink() or not prepared.is_dir():
                    raise PrepareCopyError(
                        f"Prepared category is not a real directory: {prepared}"
                    )
                if not direct_fits(prepared):
                    raise PrepareCopyError(
                        f"Prepared category contains no direct FITS files: "
                        f"{prepared}"
                    )

            report_filters.append(
                {
                    "filter": filter_name,
                    "observation_date": observation_date.isoformat(),
                    "date_source": str(date_source),
                    "selected_dark_date": dark_date.isoformat(),
                    "selected_dark_directory": str(dark_source),
                    "selected_bias_date": bias_date.isoformat(),
                    "selected_bias_directory": str(bias_source),
                    "ignored_non_date_dark_directories": [
                        str(path) for path in ignored_dark
                    ],
                    "ignored_non_date_bias_directories": [
                        str(path) for path in ignored_bias
                    ],
                    "copy_stats": [asdict(item) for item in stats],
                }
            )

    payload = {
        "schema_version": 1,
        "patch_version": VERSION,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "workspace": str(workspace),
        "project": project_name,
        "project_path": str(project),
        "mode": "copy",
        "filters": report_filters,
    }
    manifest = write_manifest(project, payload)
    payload["manifest"] = str(manifest)

    print(f"Project '{project_name}' prepared successfully using real directories.")
    print("----------------------------------------")
    for item in report_filters:
        print(
            f"Filter {item['filter']}: Obs Date {item['observation_date']} "
            f"-> Darks {item['selected_dark_date']}, "
            f"Bias {item['selected_bias_date']}"
        )
        for stats in item["copy_stats"]:
            print(
                f"  {stats['category']}: discovered={stats['discovered']} "
                f"copied={stats['copied']} "
                f"identical={stats['identical_existing']} "
                f"skipped_rejected={stats['skipped_rejected']}"
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
        darks = workspace / "calibration" / "darks" / "2025-05-27"
        biases = workspace / "calibration" / "bias" / "2025-05-27"
        for path in (source_lights, source_flats, darks, biases):
            path.mkdir(parents=True, exist_ok=True)

        def write_fits(path: Path, value: int, date_obs: str) -> None:
            header = fits.Header()
            header["DATE-OBS"] = date_obs
            fits.PrimaryHDU(
                data=np.full((8, 8), value, dtype=np.uint16),
                header=header,
            ).writeto(path)

        good = source_lights / "good.fit"
        rejected = source_lights / "rejected.fit"
        write_fits(good, 100, "2026-07-17T08:00:00")
        write_fits(rejected, 101, "2026-07-17T08:01:00")
        write_fits(source_flats / "flat.fit", 200, "2026-07-17T07:00:00")
        write_fits(darks / "dark.fit", 300, "2025-05-27T01:00:00")
        write_fits(biases / "bias.fit", 400, "2025-05-27T01:00:00")

        processing = project / "processing" / "Ha"
        processing.mkdir(parents=True)
        (processing / "lights").symlink_to(source_lights)
        (processing / "flats").symlink_to(source_flats)
        (processing / "darks").symlink_to(darks)
        (processing / "biases").symlink_to(biases)

        rejects = source_lights / "rejects"
        rejects.mkdir()
        rejected_hash = sha256_file(rejected)
        rejected_destination = rejects / rejected.name
        rejected.rename(rejected_destination)
        index = {
            "schema_version": 1,
            "entries": {
                rejected_hash: {
                    "sha256": rejected_hash,
                    "original_filename": rejected.name,
                    "final_path": str(
                        processing / "lights" / "rejects" / rejected.name
                    ),
                    "reason": "synthetic",
                }
            },
        }
        (rejects / "rejection-index.json").write_text(json.dumps(index))

        prepare_project_copy(workspace, "Synthetic")

        for category in ("lights", "flats", "darks", "biases"):
            path = processing / category
            assert path.is_dir()
            assert not path.is_symlink()

        assert (processing / "lights" / "good.fit").is_file()
        assert not (processing / "lights" / "rejected.fit").exists()
        assert (processing / "lights" / "rejects" / "rejected.fit").is_file()
        assert (
            processing / "lights" / "rejects" / "rejection-index.json"
        ).is_file()

        source_good_hash = sha256_file(good)
        assert sha256_file(processing / "lights" / "good.fit") == source_good_hash

        prepare_project_copy(workspace, "Synthetic")
        assert not (processing / "lights" / "rejected.fit").exists()
        assert sha256_file(processing / "lights" / "good.fit") == source_good_hash

    print(
        json.dumps(
            {
                "status": "success",
                "patch_version": VERSION,
                "tests": [
                    "legacy symlink conversion",
                    "real destination directories",
                    "source preservation",
                    "reject migration",
                    "rejected checksum exclusion",
                    "idempotent rerun",
                    "copy checksum verification",
                ],
            },
            indent=2,
        )
    )


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
