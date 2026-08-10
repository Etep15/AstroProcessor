#!/usr/bin/env python3
"""Deterministic StarNet 2.5 removal through Siril.

Produces:
- linear starless RGB FITS
- exact linear stars-only difference FITS
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

VERSION = "1.0.4"
MINIMUM_SIRIL_VERSION = "1.4.4"
MINIMUM_STARNET_VERSION = (2, 5, 3)
DEFAULT_TIMEOUT_SECONDS = 7200
RECONSTRUCTION_TOLERANCE = 2e-6
CHANGE_TOLERANCE = 1e-7
MIN_CHANGED_FRACTION = 1e-5

SIRIL_ROOT = Path(
    "/home/peter/.openclaw/runtime/siril-processor/toolchain/"
    ".toolchain/siril/1.4.4/squashfs-root"
)
SIRIL_APPRUN = SIRIL_ROOT / "AppRun"

SOURCE_NAME = "SHO-linear.fit"
STABLE_STARLESS_NAME = "SHO-starless-linear.fit"
STABLE_STARS_NAME = "SHO-stars-linear.fit"
STABLE_MANIFEST_NAME = "starnet-removal-manifest.json"

SKILL_ROOT = Path(__file__).resolve().parents[1]
VENDOR_DIR = SKILL_ROOT / "vendor"
VENDORED_STARNET_SCRIPT = VENDOR_DIR / "StarNet.py"
UPSTREAM_RECORD = VENDOR_DIR / "UPSTREAM.json"
STARNET_RUNTIME_ROOT = Path("/home/peter/.openclaw/runtime/starnet2/2.5.4")


class StarNetRemovalError(RuntimeError):
    pass


@dataclass(frozen=True)
class FitsEvidence:
    path: str
    size: int
    sha256: str
    width: int
    height: int
    channels: int
    dtype: str
    bitpix: int
    finite_fraction: float
    minimum: float
    maximum: float
    median: float
    filter_header: str | None


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def unique_id() -> str:
    return (
        dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        + f"-p{os.getpid()}"
    )


def derive_workspace() -> Path:
    resolved = Path(__file__).resolve()
    for parent in resolved.parents:
        if parent.name == "skills":
            return parent.parent
    raise StarNetRemovalError(
        f"Cannot derive CodeWarrior workspace from helper path: {resolved}"
    )


def project_paths(workspace: Path, project_name: str) -> dict[str, Path]:
    project = workspace / "Projects" / project_name
    sho = project / "processing" / "sho"
    starnet = project / "processing" / "starnet"
    return {
        "workspace": workspace,
        "project": project,
        "sho": sho,
        "sho_input": sho / SOURCE_NAME,
        "sho_manifest": sho / "sho-combination-manifest.json",
        "starnet": starnet,
        "stable_starless": starnet / STABLE_STARLESS_NAME,
        "stable_stars": starnet / STABLE_STARS_NAME,
        "stable_manifest": starnet / STABLE_MANIFEST_NAME,
        "runs": project / ".siril-starnet-removal",
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_dump_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f".{path.name}.{unique_id()}.partial")
    partial.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(partial, path)


def inspect_fits(
    path: Path,
    *,
    expected_channels: int | None = None,
    require_float32: bool = False,
) -> FitsEvidence:
    try:
        import numpy as np
        from astropy.io import fits
    except Exception as exc:
        raise StarNetRemovalError(
            "Astropy and NumPy are required in the AstroProcessor "
            "virtual environment."
        ) from exc

    if not path.is_file():
        raise StarNetRemovalError(f"FITS file is missing: {path}")

    try:
        with fits.open(
            path,
            memmap=False,
            do_not_scale_image_data=False,
        ) as hdul:
            data = hdul[0].data
            header = hdul[0].header.copy()
            bitpix = int(header.get("BITPIX", 0))
            if data is None:
                raise StarNetRemovalError(
                    f"Primary FITS image has no data: {path}"
                )
            array = np.asarray(data)
    except StarNetRemovalError:
        raise
    except Exception as exc:
        raise StarNetRemovalError(
            f"Cannot read FITS image {path}: {exc}"
        ) from exc

    if array.ndim == 2:
        height, width = array.shape
        channels = 1
    elif array.ndim == 3 and array.shape[0] in {1, 3, 4}:
        channels, height, width = array.shape
    else:
        raise StarNetRemovalError(
            f"Unsupported FITS shape {tuple(array.shape)} in {path}"
        )

    if expected_channels is not None and channels != expected_channels:
        raise StarNetRemovalError(
            f"Expected {expected_channels} channels, found {channels}: {path}"
        )
    if width < 32 or height < 32:
        raise StarNetRemovalError(f"Implausibly small FITS image: {path}")
    if require_float32 and not (
        array.dtype.kind == "f" and array.dtype.itemsize == 4 and bitpix == -32
    ):
        raise StarNetRemovalError(
            f"Expected 32-bit floating-point FITS, found dtype={array.dtype}, "
            f"BITPIX={bitpix}: {path}"
        )

    finite = np.isfinite(array)
    finite_count = int(finite.sum())
    total = int(array.size)
    if finite_count == 0:
        raise StarNetRemovalError(
            f"FITS image contains no finite pixels: {path}"
        )

    values = array[finite]
    minimum = float(np.min(values))
    maximum = float(np.max(values))
    median = float(np.median(values))
    if maximum <= minimum:
        raise StarNetRemovalError(f"FITS image is constant: {path}")

    filter_value = header.get("FILTER")
    return FitsEvidence(
        path=str(path),
        size=path.stat().st_size,
        sha256=sha256_file(path),
        width=int(width),
        height=int(height),
        channels=int(channels),
        dtype=str(array.dtype),
        bitpix=bitpix,
        finite_fraction=finite_count / total,
        minimum=minimum,
        maximum=maximum,
        median=median,
        filter_header=(
            str(filter_value) if filter_value is not None else None
        ),
    )


def read_fits_array(path: Path):
    try:
        import numpy as np
        from astropy.io import fits
    except Exception as exc:
        raise StarNetRemovalError(
            "Astropy and NumPy are required."
        ) from exc
    with fits.open(
        path,
        memmap=False,
        do_not_scale_image_data=False,
    ) as hdul:
        return np.asarray(hdul[0].data, dtype=np.float32)


def write_stars_difference(
    original_path: Path,
    starless_path: Path,
    destination: Path,
) -> FitsEvidence:
    try:
        import numpy as np
        from astropy.io import fits
    except Exception as exc:
        raise StarNetRemovalError(
            "Astropy and NumPy are required."
        ) from exc

    if destination.exists():
        raise StarNetRemovalError(
            f"Refusing to overwrite stars layer: {destination}"
        )

    with fits.open(
        original_path,
        memmap=False,
        do_not_scale_image_data=False,
    ) as original_hdul:
        original = np.asarray(
            original_hdul[0].data,
            dtype=np.float32,
        )
        header = original_hdul[0].header.copy()

    with fits.open(
        starless_path,
        memmap=False,
        do_not_scale_image_data=False,
    ) as starless_hdul:
        starless = np.asarray(
            starless_hdul[0].data,
            dtype=np.float32,
        )

    if original.shape != starless.shape:
        raise StarNetRemovalError(
            "Cannot derive stars layer: original and starless shapes differ."
        )

    stars = np.subtract(original, starless, dtype=np.float32)
    if not np.all(np.isfinite(stars)):
        raise StarNetRemovalError(
            "Derived stars layer contains non-finite pixels."
        )

    header["FILTER"] = "mixed_Stars"
    header["HISTORY"] = (
        "Exact linear stars layer: original SHO minus StarNet starless SHO."
    )
    header["HISTORY"] = (
        "Generated by siril-starnet-removal helper version " + VERSION
    )

    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(
        f".{destination.name}.{unique_id()}.partial"
    )
    fits.PrimaryHDU(
        data=stars.astype(np.float32, copy=False),
        header=header,
    ).writeto(partial, overwrite=False, checksum=True)
    os.replace(partial, destination)
    return inspect_fits(
        destination,
        expected_channels=3,
        require_float32=True,
    )


def separation_metrics(
    original_path: Path,
    starless_path: Path,
    stars_path: Path,
) -> dict[str, Any]:
    try:
        import numpy as np
    except Exception as exc:
        raise StarNetRemovalError("NumPy is required.") from exc

    original = read_fits_array(original_path).astype(np.float64)
    starless = read_fits_array(starless_path).astype(np.float64)
    stars = read_fits_array(stars_path).astype(np.float64)

    if original.shape != starless.shape or original.shape != stars.shape:
        raise StarNetRemovalError(
            "Original, starless, and stars shapes do not match."
        )

    change = np.abs(original - starless)
    changed_fraction = float(np.mean(change > CHANGE_TOLERANCE))
    max_change = float(np.max(change))
    mean_change = float(np.mean(change))

    reconstructed = starless + stars
    reconstruction_error = np.abs(reconstructed - original)
    reconstruction_max = float(np.max(reconstruction_error))
    reconstruction_mean = float(np.mean(reconstruction_error))

    if max_change <= CHANGE_TOLERANCE:
        raise StarNetRemovalError(
            "StarNet output is indistinguishable from the source image."
        )
    if changed_fraction < MIN_CHANGED_FRACTION:
        raise StarNetRemovalError(
            "StarNet changed too few pixels to validate nontrivial separation: "
            f"{changed_fraction:.8f}"
        )
    if reconstruction_max > RECONSTRUCTION_TOLERANCE:
        raise StarNetRemovalError(
            "Starless plus stars does not reconstruct the source within "
            f"tolerance: max error={reconstruction_max}"
        )

    return {
        "maximum_source_to_starless_difference": max_change,
        "mean_source_to_starless_difference": mean_change,
        "changed_pixel_fraction": changed_fraction,
        "change_threshold": CHANGE_TOLERANCE,
        "reconstruction_maximum_absolute_error": reconstruction_max,
        "reconstruction_mean_absolute_error": reconstruction_mean,
        "reconstruction_tolerance": RECONSTRUCTION_TOLERANCE,
    }


def copy_verified(source: Path, destination: Path, expected_hash: str) -> None:
    if destination.exists():
        raise StarNetRemovalError(
            f"Refusing to overwrite existing path: {destination}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(
        f".{destination.name}.{unique_id()}.partial"
    )
    shutil.copy2(source, partial)
    if sha256_file(partial) != expected_hash:
        raise StarNetRemovalError(
            f"Checksum changed while copying {source}; "
            f"partial copy preserved at {partial}"
        )
    os.replace(partial, destination)


def load_vendored_starnet() -> dict[str, Any]:
    if not VENDORED_STARNET_SCRIPT.is_file():
        raise StarNetRemovalError(
            "Vendored StarNet.py is missing from the installed skill: "
            f"{VENDORED_STARNET_SCRIPT}"
        )
    if not UPSTREAM_RECORD.is_file():
        raise StarNetRemovalError(
            "StarNet upstream provenance record is missing: "
            f"{UPSTREAM_RECORD}"
        )
    try:
        record = json.loads(
            UPSTREAM_RECORD.read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise StarNetRemovalError(
            f"Cannot read StarNet upstream record: {exc}"
        ) from exc

    current_hash = sha256_file(VENDORED_STARNET_SCRIPT)
    if current_hash != record.get("sha256"):
        raise StarNetRemovalError(
            "Vendored StarNet.py checksum does not match UPSTREAM.json."
        )
    if record.get("repository") != (
        "https://gitlab.com/free-astro/siril-scripts.git"
    ):
        raise StarNetRemovalError(
            "Unexpected StarNet repository provenance."
        )
    commit = str(record.get("commit", ""))
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise StarNetRemovalError(
            "Invalid pinned siril-scripts commit in UPSTREAM.json."
        )
    source_path = str(record.get("source_path", ""))
    if not source_path.endswith("StarNet.py"):
        raise StarNetRemovalError(
            "Invalid StarNet source path in UPSTREAM.json."
        )

    cli_record = record.get("starnet_cli")
    if not isinstance(cli_record, dict):
        raise StarNetRemovalError(
            "UPSTREAM.json has no valid starnet_cli record."
        )
    if cli_record.get("distribution") != "official portable ZIP":
        raise StarNetRemovalError(
            "Unexpected StarNet2 distribution type in UPSTREAM.json."
        )
    if cli_record.get("package_url") != (
        "https://download.starnetastro.com/"
        "starnet2_linux_2.5.4-0214_ORT_x64_cli.zip"
    ):
        raise StarNetRemovalError(
            "Unexpected StarNet2 portable package URL."
        )
    if cli_record.get("package_sha256") != (
        "b7a95ae3e1a9745b09536c3686eed338690b8693d5f446092524e7be75d29052"
    ):
        raise StarNetRemovalError(
            "Unexpected StarNet2 portable package SHA-256."
        )

    try:
        compile(
            VENDORED_STARNET_SCRIPT.read_text(encoding="utf-8"),
            str(VENDORED_STARNET_SCRIPT),
            "exec",
        )
    except Exception as exc:
        raise StarNetRemovalError(
            f"Vendored StarNet.py does not compile: {exc}"
        ) from exc

    return {
        **record,
        "installed_path": str(VENDORED_STARNET_SCRIPT),
        "current_sha256": current_hash,
    }


def stage_vendored_starnet(workdir: Path) -> tuple[Path, dict[str, Any]]:
    provenance = load_vendored_starnet()
    destination = workdir / "StarNet.py"
    copy_verified(
        VENDORED_STARNET_SCRIPT,
        destination,
        provenance["sha256"],
    )
    return destination, provenance


def configured_starnet_executable() -> Path:
    if not UPSTREAM_RECORD.is_file():
        raise StarNetRemovalError(
            f"StarNet upstream provenance record is missing: {UPSTREAM_RECORD}"
        )
    try:
        record = json.loads(
            UPSTREAM_RECORD.read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise StarNetRemovalError(
            f"Cannot read StarNet upstream record: {exc}"
        ) from exc

    cli_record = record.get("starnet_cli")
    if not isinstance(cli_record, dict):
        raise StarNetRemovalError(
            "UPSTREAM.json has no valid starnet_cli record."
        )

    configured = str(cli_record.get("installed_executable", "")).strip()
    if not configured:
        raise StarNetRemovalError(
            "UPSTREAM.json does not identify the installed StarNet2 executable."
        )

    executable = Path(configured).resolve()
    runtime_root = STARNET_RUNTIME_ROOT.resolve()
    try:
        executable.relative_to(runtime_root)
    except ValueError as exc:
        raise StarNetRemovalError(
            "Configured StarNet2 executable is outside the approved "
            f"portable runtime: {executable}"
        ) from exc

    if not executable.is_file():
        raise StarNetRemovalError(
            f"StarNet2 executable is missing: {executable}"
        )
    if not os.access(executable, os.X_OK):
        raise StarNetRemovalError(
            f"StarNet2 executable is not executable: {executable}"
        )

    expected_hash = str(cli_record.get("executable_sha256", "")).strip()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        raise StarNetRemovalError(
            "UPSTREAM.json has no valid StarNet2 executable SHA-256."
        )
    current_hash = sha256_file(executable)
    if current_hash != expected_hash:
        raise StarNetRemovalError(
            "Portable StarNet2 executable checksum no longer matches "
            "UPSTREAM.json."
        )
    return executable


def starnet_cli_version() -> dict[str, Any]:
    executable = configured_starnet_executable()

    try:
        completed = subprocess.run(
            [str(executable), "--version"],
            cwd=executable.parent,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except Exception as exc:
        raise StarNetRemovalError(
            f"Cannot run StarNet2 --version: {exc}"
        ) from exc

    output = (completed.stdout + "\n" + completed.stderr).strip()
    if completed.returncode != 0:
        raise StarNetRemovalError(
            f"StarNet2 --version exited with status "
            f"{completed.returncode}: {output}"
        )

    match = re.search(
        r"starnet2[\s_]+v?(\d+)\.(\d+)(?:\.(\d+))?",
        output,
        flags=re.IGNORECASE,
    )
    if not match:
        match = re.search(
            r"version:\s*v?(\d+)\.(\d+)(?:\.(\d+))?",
            output,
            flags=re.IGNORECASE,
        )
    if not match:
        match = re.search(
            r"\b(\d+)\.(\d+)(?:\.(\d+))?\b",
            output,
        )
    if not match:
        raise StarNetRemovalError(
            f"Cannot parse StarNet2 version output: {output}"
        )

    version = (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3) or 0),
    )
    if version < MINIMUM_STARNET_VERSION:
        raise StarNetRemovalError(
            f"StarNet2 {version} is older than required "
            f"{MINIMUM_STARNET_VERSION}."
        )

    return {
        "path": str(executable),
        "runtime_root": str(STARNET_RUNTIME_ROOT),
        "version": ".".join(str(part) for part in version),
        "version_tuple": list(version),
        "version_output": output,
        "sha256": sha256_file(executable),
    }



def siril_version() -> str:
    if not SIRIL_APPRUN.is_file() or not os.access(SIRIL_APPRUN, os.X_OK):
        raise StarNetRemovalError(
            f"Siril AppRun is missing or not executable: {SIRIL_APPRUN}"
        )
    environment = os.environ.copy()
    environment["APPDIR"] = str(SIRIL_ROOT)
    completed = subprocess.run(
        [str(SIRIL_APPRUN), "siril-cli", "--version"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    output = (completed.stdout + "\n" + completed.stderr).strip()
    if completed.returncode != 0:
        raise StarNetRemovalError(
            f"Cannot verify Siril version "
            f"(exit {completed.returncode}): {output}"
        )
    if MINIMUM_SIRIL_VERSION not in output:
        raise StarNetRemovalError(
            f"Expected Siril {MINIMUM_SIRIL_VERSION}, received: {output}"
        )
    return output


def starnet_script(executable: Path) -> str:
    executable_text = str(executable)
    if '"' in executable_text or "\n" in executable_text:
        raise StarNetRemovalError(
            f"Unsafe StarNet2 executable path: {executable}"
        )
    return "\n".join(
        [
            f"requires {MINIMUM_SIRIL_VERSION}",
            "setext fit",
            'load "SHO_linear.fit"',
            (
                f'pyscript StarNet.py --exe "{executable_text}" '
                "--linear --stride 256 --no-upsample "
                "--protect-highlights --masks subtract"
            ),
            "save SHO_starless_linear -chksum",
            "close",
            "",
        ]
    )


def preview_script(
    starless_path: Path,
    stars_preview_input: Path,
    preview_dir: Path,
) -> str:
    starless_stem = preview_dir / "SHO-starless-autostretch-preview"
    stars_stem = preview_dir / "SHO-stars-autostretch-preview"
    return "\n".join(
        [
            f"requires {MINIMUM_SIRIL_VERSION}",
            f'load "{starless_path}"',
            "autostretch -linked",
            f'savepng "{starless_stem}"',
            "close",
            f'load "{stars_preview_input}"',
            "autostretch -linked",
            f'savepng "{stars_stem}"',
            "close",
            "",
        ]
    )


def siril_command(workdir: Path, script: Path) -> list[str]:
    return [
        str(SIRIL_APPRUN),
        "siril-cli",
        "--directory",
        str(workdir),
        "--script",
        str(script),
    ]


def run_siril(
    *,
    workdir: Path,
    script: Path,
    stdout_path: Path,
    stderr_path: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    command = siril_command(workdir, script)
    environment = os.environ.copy()
    environment["APPDIR"] = str(SIRIL_ROOT)
    portable_executable = configured_starnet_executable()
    environment["PATH"] = (
        str(portable_executable.parent)
        + os.pathsep
        + environment.get("PATH", "")
    )
    started = time.monotonic()

    try:
        completed = subprocess.run(
            command,
            cwd=workdir,
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        stdout = completed.stdout
        stderr = completed.stderr
        exit_status = completed.returncode
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        stderr += f"\nTimed out after {timeout_seconds} seconds.\n"
        exit_status = None
        timed_out = True

    duration = round(time.monotonic() - started, 3)
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")

    combined = stdout + "\n" + stderr
    lower = combined.lower()
    fatal_markers = [
        marker
        for marker in (
            "script execution failed",
            "could not connect to siril",
            "no image is loaded",
            "starnet did not complete",
            "starnet: could not",
            "starnet: no compatible",
            "starnet: unsupported",
            "was not found in a standard cli installer location",
            "download the starnet2 cli installer",
            "starNet executable not found".lower(),
            "fatal error",
            "traceback",
        )
        if marker in lower
    ]

    return {
        "command": command,
        "display_command": (
            f'env APPDIR="{SIRIL_ROOT}" "{SIRIL_APPRUN}" siril-cli '
            f'--directory "{workdir}" --script "{script}"'
        ),
        "exit_status": exit_status,
        "timed_out": timed_out,
        "timeout_seconds": timeout_seconds,
        "duration_seconds": duration,
        "stdout_log": str(stdout_path),
        "stderr_log": str(stderr_path),
        "fatal_log_markers": fatal_markers,
        "combined_log_text": combined,
    }


def parse_version_tuple(text: str) -> tuple[int, int, int] | None:
    candidates = re.findall(
        r"(?:StarNet2|StarNet)\D{0,40}(\d+)\.(\d+)(?:\.(\d+))?",
        text,
        flags=re.IGNORECASE,
    )
    if not candidates:
        return None
    major, minor, patch = candidates[-1]
    return int(major), int(minor), int(patch or 0)


def find_repository_mask(workdir: Path) -> Path | None:
    """Return the single subtraction mask produced by StarNet.py.

    The official Siril integration names this file from its internal image
    object. In the verified Siril 1.4.4 workflow it is
    ``subtract_mask_image.fit``, not a name derived from ``SHO_linear.fit``.

    Accept exactly one FITS-family file beginning with ``subtract_mask_``.
    Multiple candidates remain ambiguous and therefore block publication.
    """
    supported_suffixes = (
        ".fit",
        ".fits",
        ".fts",
        ".fit.fz",
        ".fits.fz",
        ".fts.fz",
    )
    candidates = sorted(
        path
        for path in workdir.iterdir()
        if path.is_file()
        and path.name.lower().startswith("subtract_mask_")
        and path.name.lower().endswith(supported_suffixes)
    )
    return candidates[0] if len(candidates) == 1 else None


def create_clipped_preview_stars(
    stars_path: Path,
    destination: Path,
) -> FitsEvidence:
    try:
        import numpy as np
        from astropy.io import fits
    except Exception as exc:
        raise StarNetRemovalError(
            "Astropy and NumPy are required."
        ) from exc

    if destination.exists():
        raise StarNetRemovalError(
            f"Refusing to overwrite preview input: {destination}"
        )
    with fits.open(
        stars_path,
        memmap=False,
        do_not_scale_image_data=False,
    ) as hdul:
        stars = np.asarray(hdul[0].data, dtype=np.float32)
        header = hdul[0].header.copy()

    clipped = np.clip(stars, 0.0, None).astype(np.float32, copy=False)
    header["HISTORY"] = (
        "Clipped-to-zero preview copy only; stable stars layer is unchanged."
    )
    fits.PrimaryHDU(data=clipped, header=header).writeto(
        destination,
        overwrite=False,
        checksum=True,
    )
    return inspect_fits(
        destination,
        expected_channels=3,
        require_float32=True,
    )


def load_sho_manifest(
    paths: dict[str, Path],
) -> tuple[dict[str, Any], str]:
    manifest_path = paths["sho_manifest"]
    if not manifest_path.is_file():
        raise StarNetRemovalError(
            f"SHO-combination manifest is missing: {manifest_path}"
        )
    manifest_hash = sha256_file(manifest_path)
    try:
        manifest = json.loads(
            manifest_path.read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise StarNetRemovalError(
            f"Cannot read SHO-combination manifest: {exc}"
        ) from exc

    if manifest.get("project") != paths["project"].name:
        raise StarNetRemovalError(
            "SHO-combination manifest project name does not match."
        )
    if Path(str(manifest.get("project_path", ""))).resolve() != paths[
        "project"
    ].resolve():
        raise StarNetRemovalError(
            "SHO-combination manifest project path does not match."
        )
    if manifest.get("star_removal_permitted") is not True:
        raise StarNetRemovalError(
            "SHO-combination manifest does not permit star removal."
        )
    mapping = manifest.get("mapping", {})
    if mapping != {"red": "SII", "green": "Ha", "blue": "OIII"}:
        raise StarNetRemovalError(
            f"Unexpected SHO mapping in manifest: {mapping}"
        )
    return manifest, manifest_hash


def validate_input(
    paths: dict[str, Path],
    manifest: dict[str, Any],
) -> FitsEvidence:
    record = manifest.get("output")
    if not isinstance(record, dict):
        raise StarNetRemovalError(
            "SHO-combination manifest has no valid output record."
        )
    input_path = Path(str(record.get("path", ""))).resolve()
    if input_path != paths["sho_input"].resolve():
        raise StarNetRemovalError(
            f"Manifest output path is not the expected stable SHO input: "
            f"{input_path}"
        )
    try:
        input_path.relative_to(paths["project"].resolve())
    except ValueError as exc:
        raise StarNetRemovalError(
            f"SHO input is outside the project: {input_path}"
        ) from exc
    if sha256_file(input_path) != record.get("sha256"):
        raise StarNetRemovalError(
            "SHO-linear checksum no longer matches its manifest."
        )

    evidence = inspect_fits(
        input_path,
        expected_channels=3,
        require_float32=True,
    )
    if evidence.finite_fraction != 1.0:
        raise StarNetRemovalError(
            "SHO-linear contains non-finite pixels."
        )
    for key in ("width", "height", "channels"):
        if evidence.__dict__[key] != record.get(key):
            raise StarNetRemovalError(
                f"SHO-linear {key} differs from its manifest."
            )
    return evidence


def ensure_no_stable_outputs(paths: dict[str, Path]) -> None:
    existing = [
        str(path)
        for path in (
            paths["starnet"],
            paths["stable_starless"],
            paths["stable_stars"],
            paths["stable_manifest"],
        )
        if path.exists()
    ]
    if existing:
        raise StarNetRemovalError(
            "Stable StarNet output location already exists and will not be "
            "overwritten: " + json.dumps(existing)
        )


def execute_starnet_attempt(
    *,
    source_path: Path,
    source_evidence: FitsEvidence,
    attempt: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    workdir = attempt / "work"
    logs = attempt / "logs"
    previews = attempt / "previews"
    for directory in (workdir, logs, previews):
        directory.mkdir(parents=True, exist_ok=False)

    work_input = workdir / "SHO_linear.fit"
    copy_verified(
        source_path,
        work_input,
        source_evidence.sha256,
    )

    staged_starnet_script, upstream_provenance = stage_vendored_starnet(
        workdir
    )
    cli_evidence = starnet_cli_version()

    script_path = attempt / "starnet-linear.ssf"
    script_path.write_text(
        starnet_script(Path(cli_evidence["path"])),
        encoding="utf-8",
    )
    script_hash = sha256_file(script_path)

    run_record = run_siril(
        workdir=workdir,
        script=script_path,
        stdout_path=logs / "stdout.log",
        stderr_path=logs / "stderr.log",
        timeout_seconds=timeout_seconds,
    )
    combined_log = run_record.pop("combined_log_text")

    if run_record["timed_out"]:
        raise StarNetRemovalError(
            f"StarNet timed out after {timeout_seconds} seconds. "
            f"Attempt preserved at {attempt}"
        )
    if run_record["exit_status"] != 0:
        raise StarNetRemovalError(
            f"Siril/StarNet exited with status "
            f"{run_record['exit_status']}. Attempt preserved at {attempt}"
        )
    if run_record["fatal_log_markers"]:
        raise StarNetRemovalError(
            f"Siril/StarNet logs contain fatal markers "
            f"{run_record['fatal_log_markers']}. "
            f"Attempt preserved at {attempt}"
        )

    detected_version = tuple(cli_evidence["version_tuple"])

    starless_path = workdir / "SHO_starless_linear.fit"
    starless_evidence = inspect_fits(
        starless_path,
        expected_channels=3,
        require_float32=True,
    )
    if (
        starless_evidence.width != source_evidence.width
        or starless_evidence.height != source_evidence.height
    ):
        raise StarNetRemovalError(
            "Starless dimensions differ from the source."
        )
    if starless_evidence.finite_fraction != 1.0:
        raise StarNetRemovalError(
            "Starless output contains non-finite pixels."
        )

    stars_path = workdir / "SHO_stars_linear.fit"
    stars_evidence = write_stars_difference(
        work_input,
        starless_path,
        stars_path,
    )

    metrics = separation_metrics(
        work_input,
        starless_path,
        stars_path,
    )

    repository_mask = find_repository_mask(workdir)
    if repository_mask is None:
        mask_candidates = sorted(
            str(path)
            for path in workdir.iterdir()
            if path.is_file()
            and path.name.lower().startswith("subtract_mask_")
        )
        raise StarNetRemovalError(
            "Expected exactly one StarNet repository subtraction mask, "
            f"found {len(mask_candidates)}: {mask_candidates}. "
            f"Attempt preserved at {attempt}"
        )
    repository_mask_evidence = asdict(
        inspect_fits(
            repository_mask,
            expected_channels=3,
        )
    )

    stars_preview_input = workdir / "SHO_stars_preview_clipped.fit"
    stars_preview_evidence = create_clipped_preview_stars(
        stars_path,
        stars_preview_input,
    )

    preview_path = attempt / "preview.ssf"
    preview_path.write_text(
        preview_script(
            starless_path,
            stars_preview_input,
            previews,
        ),
        encoding="utf-8",
    )
    preview_run = run_siril(
        workdir=attempt,
        script=preview_path,
        stdout_path=logs / "preview-stdout.log",
        stderr_path=logs / "preview-stderr.log",
        timeout_seconds=min(timeout_seconds, 600),
    )
    preview_run.pop("combined_log_text", None)

    return {
        "workdir": str(workdir),
        "logs": str(logs),
        "previews_directory": str(previews),
        "script": str(script_path),
        "script_sha256": script_hash,
        "staged_starnet_script": str(staged_starnet_script),
        "upstream_starnet": upstream_provenance,
        "starnet_cli": cli_evidence,
        "siril_run": run_record,
        "starnet_version": ".".join(str(value) for value in detected_version),
        "work_input": str(work_input),
        "starless_path": str(starless_path),
        "starless_evidence": asdict(starless_evidence),
        "stars_path": str(stars_path),
        "stars_evidence": asdict(stars_evidence),
        "repository_subtract_mask": (
            str(repository_mask) if repository_mask else None
        ),
        "repository_subtract_mask_evidence": repository_mask_evidence,
        "separation_metrics": metrics,
        "stars_preview_input": str(stars_preview_input),
        "stars_preview_input_evidence": asdict(stars_preview_evidence),
        "preview_script": str(preview_path),
        "preview_run": preview_run,
        "previews": {
            "starless": str(
                previews / "SHO-starless-autostretch-preview.png"
            )
            if (
                previews / "SHO-starless-autostretch-preview.png"
            ).is_file()
            else None,
            "stars": str(
                previews / "SHO-stars-autostretch-preview.png"
            )
            if (
                previews / "SHO-stars-autostretch-preview.png"
            ).is_file()
            else None,
        },
    }


def run_project(
    workspace: Path,
    project_name: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    paths = project_paths(workspace, project_name)
    if not paths["project"].is_dir():
        raise StarNetRemovalError(
            f"Project does not exist: {paths['project']}"
        )
    ensure_no_stable_outputs(paths)

    siril_version_output = siril_version()
    sho_manifest, sho_manifest_hash = load_sho_manifest(paths)
    source_evidence = validate_input(paths, sho_manifest)

    attempt = paths["runs"] / unique_id()
    attempt.mkdir(parents=True, exist_ok=False)
    result_path = attempt / "starnet-removal-result.json"

    preliminary = {
        "status": "running_starnet",
        "helper_version": VERSION,
        "created_at": utc_now(),
        "project": project_name,
        "project_path": str(paths["project"]),
        "attempt": str(attempt),
        "siril_version_output": siril_version_output,
        "sho_manifest": str(paths["sho_manifest"]),
        "sho_manifest_sha256": sho_manifest_hash,
        "source": asdict(source_evidence),
        "upstream_starnet": load_vendored_starnet(),
        "starnet_cli": starnet_cli_version(),
        "fixed_options": {
            "interface": "pyscript StarNet.py with explicit portable --exe path",
            "linear": True,
            "stride": 256,
            "upsample": False,
            "protect_highlights": True,
            "repository_mask": "subtract",
            "stable_stars_method": "exact linear original minus starless",
        },
    }
    json_dump_atomic(result_path, preliminary)

    attempt_result = execute_starnet_attempt(
        source_path=Path(source_evidence.path),
        source_evidence=source_evidence,
        attempt=attempt,
        timeout_seconds=timeout_seconds,
    )

    if sha256_file(paths["sho_manifest"]) != sho_manifest_hash:
        raise StarNetRemovalError(
            "SHO-combination manifest changed during StarNet execution."
        )
    if sha256_file(paths["sho_input"]) != source_evidence.sha256:
        raise StarNetRemovalError(
            "SHO-linear source changed during StarNet execution."
        )

    # Build and validate all stable deliverables together inside the attempt.
    # The completed directory is then atomically renamed into processing/starnet.
    publish_dir = attempt / "publish-staging"
    publish_dir.mkdir(parents=True, exist_ok=False)
    staged_starless_path = publish_dir / STABLE_STARLESS_NAME
    staged_stars_path = publish_dir / STABLE_STARS_NAME
    staged_manifest_path = publish_dir / STABLE_MANIFEST_NAME

    copy_verified(
        Path(attempt_result["starless_path"]),
        staged_starless_path,
        attempt_result["starless_evidence"]["sha256"],
    )
    copy_verified(
        Path(attempt_result["stars_path"]),
        staged_stars_path,
        attempt_result["stars_evidence"]["sha256"],
    )

    staged_starless = inspect_fits(
        staged_starless_path,
        expected_channels=3,
        require_float32=True,
    )
    staged_stars = inspect_fits(
        staged_stars_path,
        expected_channels=3,
        require_float32=True,
    )
    staged_metrics = separation_metrics(
        paths["sho_input"],
        staged_starless_path,
        staged_stars_path,
    )

    final_starless_record = asdict(staged_starless)
    final_starless_record["path"] = str(paths["stable_starless"])
    final_stars_record = asdict(staged_stars)
    final_stars_record["path"] = str(paths["stable_stars"])

    manifest = {
        "schema_version": 1,
        "helper_version": VERSION,
        "created_at": utc_now(),
        "project": project_name,
        "project_path": str(paths["project"]),
        "siril_version_output": siril_version_output,
        "starnet_repository_script": "StarNet.py",
        "upstream_starnet": attempt_result["upstream_starnet"],
        "starnet_cli": attempt_result["starnet_cli"],
        "starnet_version": attempt_result["starnet_version"],
        "sho_manifest": str(paths["sho_manifest"]),
        "sho_manifest_sha256": sho_manifest_hash,
        "source": asdict(source_evidence),
        "options": preliminary["fixed_options"],
        "starless": final_starless_record,
        "stars": final_stars_record,
        "separation_metrics": staged_metrics,
        "repository_subtract_mask": attempt_result[
            "repository_subtract_mask"
        ],
        "attempt": str(attempt),
        "script": attempt_result["script"],
        "script_sha256": attempt_result["script_sha256"],
        "siril_run": attempt_result["siril_run"],
        "preview_script": attempt_result["preview_script"],
        "preview_run": attempt_result["preview_run"],
        "previews": attempt_result["previews"],
        "publication_method": "atomic directory rename",
        "starless_background_processing_permitted": True,
    }
    json_dump_atomic(staged_manifest_path, manifest)

    if paths["starnet"].exists():
        raise StarNetRemovalError(
            f"Stable StarNet directory appeared before publication: "
            f"{paths['starnet']}"
        )
    os.replace(publish_dir, paths["starnet"])

    stable_starless = inspect_fits(
        paths["stable_starless"],
        expected_channels=3,
        require_float32=True,
    )
    stable_stars = inspect_fits(
        paths["stable_stars"],
        expected_channels=3,
        require_float32=True,
    )
    stable_metrics = separation_metrics(
        paths["sho_input"],
        paths["stable_starless"],
        paths["stable_stars"],
    )
    if stable_starless.sha256 != staged_starless.sha256:
        raise StarNetRemovalError(
            "Starless checksum changed during atomic publication."
        )
    if stable_stars.sha256 != staged_stars.sha256:
        raise StarNetRemovalError(
            "Stars checksum changed during atomic publication."
        )
    if not paths["stable_manifest"].is_file():
        raise StarNetRemovalError(
            "Stable manifest is missing after atomic publication."
        )

    final = {
        **preliminary,
        "status": "ready",
        "starnet_version": attempt_result["starnet_version"],
        "upstream_starnet": attempt_result["upstream_starnet"],
        "starnet_cli": attempt_result["starnet_cli"],
        "staged_starnet_script": attempt_result[
            "staged_starnet_script"
        ],
        "script": attempt_result["script"],
        "script_sha256": attempt_result["script_sha256"],
        "siril_run": attempt_result["siril_run"],
        "repository_subtract_mask": attempt_result[
            "repository_subtract_mask"
        ],
        "starless": asdict(stable_starless),
        "stars": asdict(stable_stars),
        "separation_metrics": stable_metrics,
        "previews": attempt_result["previews"],
        "stable_manifest": str(paths["stable_manifest"]),
        "publication_method": "atomic directory rename",
        "starless_background_processing_permitted": True,
    }
    json_dump_atomic(result_path, final)
    return final


def status_project(
    workspace: Path,
    project_name: str,
) -> dict[str, Any]:
    paths = project_paths(workspace, project_name)
    manifest_path = paths["stable_manifest"]
    if not manifest_path.is_file():
        return {
            "status": "not_run",
            "project": str(paths["project"]),
            "manifest": str(manifest_path),
            "starless_background_processing_permitted": False,
        }

    try:
        manifest = json.loads(
            manifest_path.read_text(encoding="utf-8")
        )
    except Exception as exc:
        return {
            "status": "blocked",
            "project": str(paths["project"]),
            "manifest": str(manifest_path),
            "errors": [f"Cannot read manifest: {exc}"],
            "starless_background_processing_permitted": False,
        }

    errors: list[str] = []
    source_evidence = None
    starless_evidence = None
    stars_evidence = None
    metrics = None

    try:
        if not paths["sho_manifest"].is_file():
            errors.append(
                f"SHO-combination manifest missing: {paths['sho_manifest']}"
            )
        elif sha256_file(paths["sho_manifest"]) != manifest.get(
            "sho_manifest_sha256"
        ):
            errors.append("SHO-combination manifest checksum changed.")

        source_record = manifest.get("source", {})
        source_path = Path(str(source_record.get("path", "")))
        if not source_path.is_file():
            errors.append(f"Source SHO missing: {source_path}")
        elif sha256_file(source_path) != source_record.get("sha256"):
            errors.append("Source SHO checksum changed.")
        else:
            source_evidence = asdict(
                inspect_fits(
                    source_path,
                    expected_channels=3,
                    require_float32=True,
                )
            )

        starless_record = manifest.get("starless", {})
        starless_path = Path(str(starless_record.get("path", "")))
        if not starless_path.is_file():
            errors.append(f"Starless output missing: {starless_path}")
        elif sha256_file(starless_path) != starless_record.get("sha256"):
            errors.append("Starless output checksum changed.")
        else:
            starless_evidence = asdict(
                inspect_fits(
                    starless_path,
                    expected_channels=3,
                    require_float32=True,
                )
            )

        stars_record = manifest.get("stars", {})
        stars_path = Path(str(stars_record.get("path", "")))
        if not stars_path.is_file():
            errors.append(f"Stars output missing: {stars_path}")
        elif sha256_file(stars_path) != stars_record.get("sha256"):
            errors.append("Stars output checksum changed.")
        else:
            stars_evidence = asdict(
                inspect_fits(
                    stars_path,
                    expected_channels=3,
                    require_float32=True,
                )
            )

        if not errors:
            metrics = separation_metrics(
                source_path,
                starless_path,
                stars_path,
            )
    except StarNetRemovalError as exc:
        errors.append(str(exc))

    return {
        "status": "ready" if not errors else "blocked",
        "project": str(paths["project"]),
        "manifest": str(manifest_path),
        "source": source_evidence,
        "starless": starless_evidence,
        "stars": stars_evidence,
        "separation_metrics": metrics,
        "errors": errors,
        "starless_background_processing_permitted": not errors,
    }


def make_synthetic_rgb(path: Path) -> FitsEvidence:
    try:
        import numpy as np
        from astropy.io import fits
    except Exception as exc:
        raise StarNetRemovalError(
            "Astropy and NumPy are required."
        ) from exc

    height = 512
    width = 512
    y, x = np.mgrid[0:height, 0:width]
    base = (
        0.008
        + 0.002 * (x / width)
        + 0.0015 * (y / height)
        + 0.012 * np.exp(
            -(
                ((x - 270.0) / 120.0) ** 2
                + ((y - 250.0) / 90.0) ** 2
            )
        )
    )
    rgb = np.stack(
        [
            base * 1.2,
            base * 0.95,
            base * 0.75,
        ],
        axis=0,
    ).astype(np.float32)

    stars = [
        (40, 55, 0.70, 2.3, (1.0, 0.85, 0.72)),
        (85, 390, 0.85, 3.1, (0.75, 0.85, 1.0)),
        (120, 145, 0.65, 2.7, (1.0, 0.95, 0.88)),
        (160, 320, 0.92, 4.0, (1.0, 0.78, 0.62)),
        (205, 475, 0.72, 2.5, (0.70, 0.82, 1.0)),
        (235, 80, 0.95, 4.8, (1.0, 0.92, 0.82)),
        (275, 265, 0.82, 3.0, (0.82, 0.90, 1.0)),
        (315, 420, 0.88, 3.7, (1.0, 0.80, 0.65)),
        (360, 115, 0.76, 2.4, (0.72, 0.85, 1.0)),
        (410, 350, 0.98, 5.2, (1.0, 0.90, 0.76)),
        (455, 205, 0.68, 2.8, (0.80, 0.90, 1.0)),
        (480, 480, 0.80, 3.4, (1.0, 0.84, 0.70)),
    ]
    for cy, cx, amplitude, sigma, color in stars:
        profile = amplitude * np.exp(
            -(
                (x - cx) ** 2
                + (y - cy) ** 2
            )
            / (2.0 * sigma * sigma)
        )
        for channel in range(3):
            rgb[channel] += (
                profile * float(color[channel])
            ).astype(np.float32)

    rgb = np.clip(rgb, 0.0, 1.0).astype(np.float32)
    header = fits.Header()
    header["OBJECT"] = "Synthetic StarNet self-test"
    header["FILTER"] = "mixed"
    header["HISTORY"] = (
        "Synthetic image generated by siril-starnet-removal self-test."
    )
    fits.PrimaryHDU(data=rgb, header=header).writeto(
        path,
        overwrite=False,
        checksum=True,
    )
    return inspect_fits(
        path,
        expected_channels=3,
        require_float32=True,
    )


def self_test(
    workspace: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    siril_version_output = siril_version()
    test_root = (
        workspace
        / ".skill-self-tests"
        / "siril-starnet-removal"
        / unique_id()
    )
    test_root.mkdir(parents=True, exist_ok=False)
    source = test_root / SOURCE_NAME
    source_evidence = make_synthetic_rgb(source)

    attempt = test_root / "attempt"
    attempt.mkdir(parents=True, exist_ok=False)

    result = execute_starnet_attempt(
        source_path=source,
        source_evidence=source_evidence,
        attempt=attempt,
        timeout_seconds=timeout_seconds,
    )

    payload = {
        "status": "success",
        "helper_version": VERSION,
        "created_at": utc_now(),
        "workspace": str(workspace),
        "self_test_directory": str(test_root),
        "siril_version_output": siril_version_output,
        "starnet_version": result["starnet_version"],
        "upstream_starnet": result["upstream_starnet"],
        "starnet_cli": result["starnet_cli"],
        "staged_starnet_script": result["staged_starnet_script"],
        "source": asdict(source_evidence),
        "script": result["script"],
        "siril_run": result["siril_run"],
        "starless": result["starless_evidence"],
        "stars": result["stars_evidence"],
        "repository_subtract_mask": result[
            "repository_subtract_mask"
        ],
        "separation_metrics": result["separation_metrics"],
        "previews": result["previews"],
        "tests": [
            "workspace derivation",
            "Siril 1.4.4 AppRun",
            "vendored official StarNet.py checksum and provenance",
            "portable StarNet2 direct executable version preflight",
            "StarNet.py copied into isolated working directory",
            "exact StarNet.py command construction",
            "StarNet 2.5.3 or newer",
            "linear mode",
            "standard stride 256",
            "no upsampling",
            "highlight protection",
            "synthetic 512x512 RGB StarNet execution",
            "repository subtraction mask discovery (subtract_mask_image.fit)",
            "32-bit RGB starless FITS validation",
            "exact linear stars-layer derivation",
            "starless plus stars reconstruction",
            "self-test evidence preservation",
        ],
    }
    json_dump_atomic(test_root / "self-test-result.json", payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Remove stars from a validated linear SHO FITS with StarNet 2.5 "
            "through Siril."
        )
    )
    parser.add_argument("--version", action="version", version=VERSION)
    subparsers = parser.add_subparsers(dest="command", required=True)

    self_parser = subparsers.add_parser("self-test")
    self_parser.add_argument(
        "--timeout",
        type=int,
        default=1800,
    )

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--project", required=True)
    run_parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
    )

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--project", required=True)

    args = parser.parse_args()

    try:
        workspace = derive_workspace()
        if args.command == "self-test":
            if args.timeout < 120 or args.timeout > 7200:
                raise StarNetRemovalError(
                    "Self-test timeout must be between 120 and 7200 seconds."
                )
            result = self_test(workspace, args.timeout)
        elif args.command == "run":
            if args.timeout < 300 or args.timeout > 14400:
                raise StarNetRemovalError(
                    "Run timeout must be between 300 and 14400 seconds."
                )
            result = run_project(
                workspace,
                args.project,
                args.timeout,
            )
        else:
            result = status_project(workspace, args.project)

        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result.get("status") in {
            "success",
            "ready",
            "not_run",
        } else 2
    except StarNetRemovalError as exc:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "helper_version": VERSION,
                    "error": str(exc),
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
                    "helper_version": VERSION,
                    "error": f"{type(exc).__name__}: {exc}",
                },
                indent=2,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
