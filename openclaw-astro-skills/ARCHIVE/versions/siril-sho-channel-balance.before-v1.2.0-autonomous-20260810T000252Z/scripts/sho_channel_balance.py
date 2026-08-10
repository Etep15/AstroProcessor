#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from astropy.io import fits

VERSION = "1.1.0"
REQUIRED_STARNET_SOURCE_CONTRACT_REVISION = "native-starnet-channel-balance-v1"
REQUIRED_SIRIL_VERSION = "1.4.4"

SIRIL_ROOT = Path(
    "/home/peter/.openclaw/runtime/siril-processor/toolchain/.toolchain/"
    "siril/1.4.4/squashfs-root"
)
SIRIL_APP = SIRIL_ROOT / "AppRun"

WORKSPACE = Path("/home/peter/.openclaw/workspace/agents/codewarrior")
PROJECTS_ROOT = WORKSPACE / "Projects"

BASELINE_COEFFICIENTS = {"r": 1.00, "g": 0.25, "b": 1.00}
COEFFICIENT_BOUNDS = {
    "r": (0.70, 1.30),
    "g": (0.15, 0.40),
    "b": (0.70, 1.30),
}
COEFFICIENT_STEPS = {"r": 0.15, "g": 0.05, "b": 0.15}
MAX_ATTEMPTS = 5

DOMINANT_PROBLEMS = (
    "excessive_green",
    "insufficient_green",
    "magenta_cast",
    "weak_red",
    "excessive_red",
    "weak_blue",
    "excessive_blue",
    "balanced",
    "no_improvement",
)

PROBLEM_MOVES = {
    "excessive_green": ("g", -1),
    "insufficient_green": ("g", +1),
    "magenta_cast": ("g", +1),
    "weak_red": ("r", +1),
    "excessive_red": ("r", -1),
    "weak_blue": ("b", +1),
    "excessive_blue": ("b", -1),
}

MAX_CONTEXT_SAFE_JSON_BYTES = 9000
FORMULA_TOLERANCE = 3e-5
MIN_CHANNEL_CORRELATION = 0.9995
NOTE_FIELDS = ("balance", "magenta", "structure", "noise")
CLI_SUCCESS_STATUSES = {
    "success",
    "missing",
    "ready",
    "would_generate_baseline",
    "confirmation_required",
    "visual_review_required",
    "selection_review_required",
    "fresh_run_confirmed",
}


class ChannelBalanceError(RuntimeError):
    pass


@dataclass
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
    return (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        + f"-p{os.getpid()}-{uuid.uuid4().hex[:8]}"
    )


def compact_text(value: Any, limit: int = 1000) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def json_dump_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{unique_id()}.partial")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ChannelBalanceError(f"Cannot read JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ChannelBalanceError(f"Expected JSON object: {path}")
    return payload


def assert_context_safe_payload(payload: dict[str, Any]) -> None:
    size = len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    if size > MAX_CONTEXT_SAFE_JSON_BYTES:
        raise ChannelBalanceError(
            f"Helper response exceeds context-safe limit: {size} bytes"
        )


def inspect_fits(path: Path) -> FitsEvidence:
    if not path.is_file():
        raise ChannelBalanceError(f"FITS file is missing: {path}")
    try:
        with fits.open(path, memmap=True) as hdul:
            data = np.asarray(hdul[0].data)
            header = hdul[0].header.copy()
            bitpix = int(header.get("BITPIX", 0))
    except Exception as exc:
        raise ChannelBalanceError(f"Cannot inspect FITS {path}: {exc}") from exc

    if data.ndim == 3 and data.shape[0] == 3:
        channels = 3
        height, width = int(data.shape[1]), int(data.shape[2])
    elif data.ndim == 2:
        channels = 1
        height, width = int(data.shape[0]), int(data.shape[1])
    else:
        raise ChannelBalanceError(f"Unsupported FITS shape {data.shape}: {path}")

    sample = np.asarray(data[..., ::8, ::8] if data.ndim == 3 else data[::8, ::8], dtype=np.float64)
    finite = np.isfinite(sample)
    finite_fraction = float(np.mean(finite))
    if finite_fraction != 1.0:
        raise ChannelBalanceError(f"Non-finite pixels found in {path}")

    return FitsEvidence(
        path=str(path),
        sha256=sha256_file(path),
        size=path.stat().st_size,
        bitpix=bitpix,
        dtype=str(data.dtype),
        channels=channels,
        width=width,
        height=height,
        finite_fraction=finite_fraction,
        minimum=float(np.min(sample)),
        maximum=float(np.max(sample)),
        median=float(np.median(sample)),
        filter_header=str(header["FILTER"]) if "FILTER" in header else None,
    )


def load_rgb(path: Path) -> np.ndarray:
    with fits.open(path, memmap=True) as hdul:
        data = np.asarray(hdul[0].data, dtype=np.float32)
    if data.ndim != 3 or data.shape[0] != 3:
        raise ChannelBalanceError(f"Expected 3-channel RGB FITS: {path}")
    return data


def project_paths(workspace: Path, project_name: str) -> dict[str, Path]:
    project = workspace / "Projects" / project_name
    processing = project / "processing"
    stable = processing / "sho-channel-balance"
    return {
        "project": project,
        "source": processing / "starnet" / "SHO-starless-linear.fit",
        "source_manifest": processing / "starnet" / "starnet-manifest.json",
        "source_review": processing / "starnet" / "visual-review-record.json",
        "runs": project / ".siril-sho-channel-balance",
        "intents": project / ".siril-sho-channel-balance" / "fresh-intents",
        "stable": stable,
        "stable_output": stable / "SHO-starless-linear-balanced.fit",
        "stable_before_preview": stable / "SHO-starless-linear-before-channel-balance.png",
        "stable_after_preview": stable / "SHO-starless-linear-balanced.png",
        "stable_manifest": stable / "sho-channel-balance-manifest.json",
    }



def siril_version() -> dict[str, Any]:
    if not SIRIL_APP.is_file() or not os.access(SIRIL_APP, os.X_OK):
        raise ChannelBalanceError(f"Siril AppRun is unavailable: {SIRIL_APP}")
    env = os.environ.copy()
    env["APPDIR"] = str(SIRIL_ROOT)
    completed = subprocess.run(
        [str(SIRIL_APP), "siril-cli", "--version"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    output = (completed.stdout + "\n" + completed.stderr).strip()
    if completed.returncode != 0 or REQUIRED_SIRIL_VERSION not in output:
        raise ChannelBalanceError(
            f"Expected Siril {REQUIRED_SIRIL_VERSION}; received: {output}"
        )
    return {
        "version": REQUIRED_SIRIL_VERSION,
        "version_output": output,
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
    command = [
        str(SIRIL_APP),
        "siril-cli",
        "--directory",
        str(directory),
        "--script",
        str(script),
    ]
    env = os.environ.copy()
    env["APPDIR"] = str(SIRIL_ROOT)

    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=directory,
            env=env,
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
        exit_status = 124
        timed_out = True

    stdout_log.parent.mkdir(parents=True, exist_ok=True)
    stdout_log.write_text(stdout, encoding="utf-8")
    stderr_log.write_text(stderr, encoding="utf-8")
    combined = (stdout + "\n" + stderr).lower()
    fatal_markers = [
        marker
        for marker in (
            "script execution failed",
            "cannot open",
            "could not open",
            "fatal error",
            "unknown command",
        )
        if marker in combined
    ]

    result = {
        "command": command,
        "display_command": (
            f'env APPDIR="{SIRIL_ROOT}" "{SIRIL_APP}" siril-cli '
            f'--directory "{directory}" --script "{script}"'
        ),
        "exit_status": int(exit_status),
        "timed_out": timed_out,
        "duration_seconds": round(time.monotonic() - started, 3),
        "timeout_seconds": int(timeout_seconds),
        "fatal_log_markers": fatal_markers,
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
    }
    if timed_out or exit_status != 0 or fatal_markers:
        raise ChannelBalanceError(
            "Siril processing failed: "
            + compact_text(
                {
                    "exit_status": exit_status,
                    "timed_out": timed_out,
                    "fatal_log_markers": fatal_markers,
                    "stdout_log": str(stdout_log),
                    "stderr_log": str(stderr_log),
                }
            )
        )
    return result



def validate_upstream(
    workspace: Path,
    project_name: str,
) -> tuple[dict[str, Any], FitsEvidence, dict[str, Any]]:
    paths = project_paths(workspace, project_name)
    if not paths["project"].is_dir():
        raise ChannelBalanceError(f"Project does not exist: {paths['project']}")
    if not paths["source_manifest"].is_file():
        raise ChannelBalanceError(f"StarNet manifest is missing: {paths['source_manifest']}")
    if not paths["source_review"].is_file():
        raise ChannelBalanceError(f"StarNet visual-review record is missing: {paths['source_review']}")

    manifest = load_json(paths["source_manifest"])
    source = inspect_fits(paths["source"])
    errors: list[str] = []
    if manifest.get("project") != project_name:
        errors.append("StarNet manifest project does not match")
    if Path(str(manifest.get("project_path", ""))).resolve() != paths["project"].resolve():
        errors.append("StarNet manifest project path does not match")
    if manifest.get("helper_version") != "1.5.2":
        errors.append("StarNet helper is not 1.5.2")
    if manifest.get("source_contract_revision") != REQUIRED_STARNET_SOURCE_CONTRACT_REVISION:
        errors.append("StarNet source-contract revision is not native-starnet-channel-balance-v1")
    if manifest.get("status") != "ready":
        errors.append("StarNet manifest is not ready")
    if manifest.get("visual_review_completed") is not True:
        errors.append("StarNet visual review is incomplete")
    if manifest.get("sho_channel_balance_permitted") is not True:
        errors.append("StarNet does not permit SHO channel balance")
    if manifest.get("ghs_pass1_permitted") is not False:
        errors.append("StarNet incorrectly permits direct GHS pass 1")
    if manifest.get("starless_processing_permitted") is not True:
        errors.append("StarNet does not permit starless processing")
    if manifest.get("starless_background_processing_permitted") is not False:
        errors.append("StarNet incorrectly permits another background stage")
    if manifest.get("next_stage") != "siril-sho-channel-balance":
        errors.append("StarNet next stage is not SHO channel balance")
    if manifest.get("stage_order") != {
        "upstream": "siril-background-neutralization",
        "current": "siril-starnet-removal",
        "downstream": "siril-sho-channel-balance",
    }:
        errors.append("StarNet stage order does not use the native channel-balance handoff")

    linear_starless = manifest.get("linear_starless", {})
    if Path(str(linear_starless.get("path", ""))).resolve() != paths["source"].resolve():
        errors.append("StarNet manifest does not reference canonical SHO-starless-linear.fit")
    if linear_starless.get("sha256") != source.sha256:
        errors.append("StarNet starless SHA-256 does not match current source")
    review = manifest.get("visual_review", {})
    if Path(str(review.get("record_path", ""))).resolve() != paths["source_review"].resolve():
        errors.append("StarNet visual-review path is not canonical")
    elif sha256_file(paths["source_review"]) != review.get("record_sha256"):
        errors.append("StarNet visual-review checksum does not match")
    if source.channels != 3 or source.bitpix != -32 or source.finite_fraction != 1.0:
        errors.append("StarNet starless source is not finite 32-bit floating-point RGB")
    if errors:
        raise ChannelBalanceError("Upstream StarNet contract failed: " + "; ".join(errors))
    summary = {
        "helper_version": manifest.get("helper_version"),
        "source_contract_revision": manifest.get("source_contract_revision"),
        "status": manifest.get("status"),
        "manifest": str(paths["source_manifest"]),
        "manifest_sha256": sha256_file(paths["source_manifest"]),
        "visual_review_record": str(paths["source_review"]),
        "visual_review_record_sha256": sha256_file(paths["source_review"]),
        "contract_mode": REQUIRED_STARNET_SOURCE_CONTRACT_REVISION,
        "source_sha256": source.sha256,
    }
    return manifest, source, summary




def split_source_script() -> str:
    return "\n".join(
        [
            f"requires {REQUIRED_SIRIL_VERSION}",
            "setext fit",
            'load "source.fit"',
            'split "R" "G" "B"',
            'autostretch -linked',
            'savepng "../common/SHO-starless-linear-source-linked"',
            "close",
            "",
        ]
    )



def candidate_script(coefficients: dict[str, float]) -> str:
    r, g, b = coefficients["r"], coefficients["g"], coefficients["b"]
    expressions = {
        "R_bal": f"med($R$) + {r:.6f} * ($R$ - med($R$))",
        "G_bal": f"med($R$) + {g:.6f} * ($G$ - med($G$))",
        "B_bal": f"med($R$) + {b:.6f} * ($B$ - med($B$))",
    }
    lines = [f"requires {REQUIRED_SIRIL_VERSION}", "setext fit"]
    for name in ("R_bal", "G_bal", "B_bal"):
        lines.extend([
            f'pm "{expressions[name]}" -nosum',
            f'save "{name}.fit"',
            "close",
        ])
    lines.extend([
        'rgbcomp "R_bal.fit" "G_bal.fit" "B_bal.fit" '
        '-out=SHO-starless-linear-balanced.fit -nosum',
        'load "SHO-starless-linear-balanced.fit"',
        'autostretch -linked',
        'savepng "../previews/SHO-starless-linear-balanced-linked"',
        "close",
        "",
    ])
    return "\n".join(lines)



def formula_quality(
    source_path: Path,
    output_path: Path,
    coefficients: dict[str, float],
) -> dict[str, Any]:
    src = load_rgb(source_path)
    out = load_rgb(output_path)
    if src.shape != out.shape:
        raise ChannelBalanceError(
            f"Candidate shape changed: source={src.shape}, output={out.shape}"
        )

    medians = [float(np.median(src[i])) for i in range(3)]
    stride = 8
    s = np.asarray(src[:, ::stride, ::stride], dtype=np.float64)
    o = np.asarray(out[:, ::stride, ::stride], dtype=np.float64)

    expected = np.stack(
        [
            medians[0] + coefficients["r"] * (s[0] - medians[0]),
            medians[0] + coefficients["g"] * (s[1] - medians[1]),
            medians[0] + coefficients["b"] * (s[2] - medians[2]),
        ]
    )

    difference = np.abs(o - expected)
    max_difference = float(np.max(difference))
    mean_difference = float(np.mean(difference))

    correlations: dict[str, float] = {}
    for idx, name in enumerate(("red_from_SII_derived_starless", "green_from_Ha_derived_starless", "blue_from_OIII_derived_starless")):
        a = s[idx].ravel()
        b = o[idx].ravel()
        corr = float(np.corrcoef(a, b)[0, 1])
        correlations[name] = corr

    output_medians = [float(np.median(o[i])) for i in range(3)]

    def mad(array: np.ndarray) -> float:
        m = np.median(array)
        return float(np.median(np.abs(array - m)))

    src_mads = [mad(s[i]) for i in range(3)]
    out_mads = [mad(o[i]) for i in range(3)]
    mad_ratios = [
        0.0 if src_mads[i] <= 1e-15 else out_mads[i] / src_mads[i]
        for i in range(3)
    ]

    failed: list[dict[str, Any]] = []
    if not np.all(np.isfinite(o)):
        failed.append(
            {
                "metric": "finite_output",
                "value": float(np.mean(np.isfinite(o))),
                "requirement": "1.0",
            }
        )
    if max_difference > FORMULA_TOLERANCE:
        failed.append(
            {
                "metric": "formula_maximum_absolute_difference",
                "value": max_difference,
                "requirement": f"<= {FORMULA_TOLERANCE}",
            }
        )
    for name, corr in correlations.items():
        if not math.isfinite(corr) or corr < MIN_CHANNEL_CORRELATION:
            failed.append(
                {
                    "metric": name + "_correlation",
                    "value": corr,
                    "requirement": f">= {MIN_CHANNEL_CORRELATION}",
                }
            )

    return {
        "status": "satisfactory" if not failed else "needs_review",
        "satisfactory": not failed,
        "failed_checks": failed,
        "metrics": {
            "formula_maximum_absolute_difference": max_difference,
            "formula_mean_absolute_difference": mean_difference,
            "channel_correlations": correlations,
            "source_channel_medians": medians,
            "output_channel_medians": output_medians,
            "source_channel_mad": src_mads,
            "output_channel_mad": out_mads,
            "channel_mad_ratios": {
                "red": mad_ratios[0],
                "green": mad_ratios[1],
                "blue": mad_ratios[2],
            },
            "below_zero_fraction_diagnostic": float(np.mean(o < 0.0)),
            "above_one_fraction_diagnostic": float(np.mean(o > 1.0)),
        },
        "thresholds": {
            "maximum_formula_difference": FORMULA_TOLERANCE,
            "minimum_channel_correlation": MIN_CHANNEL_CORRELATION,
            "linear_values_outside_0_1_are_diagnostic_only": True,
        },
    }


def create_run(
    workspace: Path,
    project_name: str,
    timeout_seconds: int,
    fresh_intent: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    paths = project_paths(workspace, project_name)
    _, source, upstream_summary = validate_upstream(workspace, project_name)

    run_root = paths["runs"] / f"channel-balance-{unique_id()}"
    common = run_root / "common"
    work = run_root / "work"
    logs = run_root / "logs"
    common.mkdir(parents=True, exist_ok=False)
    work.mkdir(parents=True, exist_ok=False)
    logs.mkdir(parents=True, exist_ok=False)

    shutil.copy2(paths["source"], work / "source.fit")
    split_script = run_root / "split-source.ssf"
    split_script.write_text(split_source_script(), encoding="utf-8")
    split_result = run_siril_script(
        directory=work,
        script=split_script,
        stdout_log=logs / "split-stdout.log",
        stderr_log=logs / "split-stderr.log",
        timeout_seconds=timeout_seconds,
    )
    for name in ("R.fit", "G.fit", "B.fit"):
        if not (work / name).is_file():
            raise ChannelBalanceError(
                f"Siril split did not create expected RGB channel: {work / name}"
            )

    source_preview = common / "SHO-starless-linear-source-linked.png"
    if not source_preview.is_file():
        raise ChannelBalanceError("Siril did not create starless source linked preview.")

    record = {
        "schema_version": 2,
        "helper_version": VERSION,
        "created_at": utc_now(),
        "project_name": project_name,
        "project": str(paths["project"]),
        "run_root": str(run_root),
        "source": asdict(source),
        "upstream_summary": upstream_summary,
        "source_preview": {"path": str(source_preview), "sha256": sha256_file(source_preview)},
        "split_siril_run": split_result,
        "baseline_coefficients": BASELINE_COEFFICIENTS,
        "coefficient_bounds": {k: [v[0], v[1]] for k, v in COEFFICIENT_BOUNDS.items()},
        "coefficient_steps": COEFFICIENT_STEPS,
        "maximum_attempts": MAX_ATTEMPTS,
        "one_coefficient_change_per_attempt": True,
        "source_is_starless": True,
        "stars_layer_modified": False,
        "candidates": [],
        "current_candidate": None,
        "status": "starting",
        "canonical_output_changed": False,
        "ghs_pass1_permitted": False,
        "background_neutralization_permitted": False,
        "star_removal_permitted": False,
        "fresh_intent": str(fresh_intent) if fresh_intent else None,
    }
    save_run_record(run_root, record)
    first = run_candidate(
        run_root=run_root,
        source_path=paths["source"],
        candidate_index=1,
        coefficients=BASELINE_COEFFICIENTS.copy(),
        change_from_previous=None,
        timeout_seconds=timeout_seconds,
    )
    record = load_run_record(run_root)
    record["candidates"].append(first)
    record["current_candidate"] = first["candidate"]
    record["status"] = "awaiting_review"
    save_run_record(run_root, record)
    if fresh_intent:
        intent = load_json(fresh_intent)
        intent["status"] = "consumed"
        intent["consumed_at"] = utc_now()
        intent["run_root"] = str(run_root)
        json_dump_atomic(fresh_intent, intent)
    return run_root, record



def run_candidate(
    *,
    run_root: Path,
    source_path: Path,
    candidate_index: int,
    coefficients: dict[str, float],
    change_from_previous: dict[str, Any] | None,
    timeout_seconds: int,
) -> dict[str, Any]:
    name = f"candidate-{candidate_index:02d}"
    candidate_root = run_root / name
    work = candidate_root / "work"
    previews = candidate_root / "previews"
    logs = candidate_root / "logs"
    work.mkdir(parents=True, exist_ok=False)
    previews.mkdir(parents=True, exist_ok=False)
    logs.mkdir(parents=True, exist_ok=False)
    common_work = run_root / "work"
    for channel in ("R.fit", "G.fit", "B.fit"):
        os.symlink(common_work / channel, work / channel)
    script_path = candidate_root / "channel-balance.ssf"
    script_path.write_text(candidate_script(coefficients), encoding="utf-8")
    siril_run = run_siril_script(
        directory=work,
        script=script_path,
        stdout_log=logs / "siril-stdout.log",
        stderr_log=logs / "siril-stderr.log",
        timeout_seconds=timeout_seconds,
    )
    output = work / "SHO-starless-linear-balanced.fit"
    preview = previews / "SHO-starless-linear-balanced-linked.png"
    if not output.is_file() or not preview.is_file():
        raise ChannelBalanceError(f"Candidate {name} did not produce required FITS/preview.")
    evidence = inspect_fits(output)
    if evidence.channels != 3 or evidence.bitpix != -32:
        raise ChannelBalanceError(f"{name} is not 32-bit floating-point RGB.")
    quality = formula_quality(source_path, output, coefficients)
    if quality["satisfactory"] is not True:
        raise ChannelBalanceError(
            f"{name} failed formula/format quality checks: {quality['failed_checks']}"
        )
    return {
        "candidate": name,
        "attempt": candidate_index,
        "created_at": utc_now(),
        "helper_version": VERSION,
        "coefficients": coefficients,
        "change_from_previous": change_from_previous,
        "output": asdict(evidence),
        "preview": {"path": str(preview), "sha256": sha256_file(preview)},
        "quality_assessment": quality,
        "siril_run": siril_run,
        "script": str(script_path),
        "script_sha256": sha256_file(script_path),
        "review": None,
    }



def run_manifest_path(run_root: Path) -> Path:
    return run_root / "run-manifest.json"


def save_run_record(run_root: Path, record: dict[str, Any]) -> None:
    json_dump_atomic(run_manifest_path(run_root), record)


def load_run_record(run_root: Path) -> dict[str, Any]:
    path = run_manifest_path(run_root)
    if not path.is_file():
        raise ChannelBalanceError(f"Run manifest is missing: {path}")
    record = load_json(path)
    if record.get("helper_version") != VERSION:
        raise ChannelBalanceError(
            f"Incompatible run helper version: {record.get('helper_version')}"
        )
    return record


def latest_active_run(paths: dict[str, Path]) -> tuple[Path, dict[str, Any]] | None:
    if not paths["runs"].is_dir():
        return None
    rows: list[tuple[float, Path, dict[str, Any]]] = []
    for root in paths["runs"].iterdir():
        if not root.is_dir() or not root.name.startswith("channel-balance-"):
            continue
        manifest = run_manifest_path(root)
        if not manifest.is_file():
            continue
        try:
            record = load_json(manifest)
        except Exception:
            continue
        if record.get("helper_version") != VERSION:
            continue
        if record.get("canonical_output_changed") is True:
            continue
        if record.get("status") in (
            "awaiting_review",
            "selection_review_required",
            "ready_to_publish",
        ):
            rows.append((manifest.stat().st_mtime, root, record))
    if not rows:
        return None
    _, root, record = sorted(rows, key=lambda row: row[0], reverse=True)[0]
    return root, record


def canonical_status(workspace: Path, project_name: str) -> dict[str, Any]:
    paths = project_paths(workspace, project_name)
    if not paths["stable_manifest"].is_file():
        return {
            "status": "missing", "helper_version": VERSION,
            "project": str(paths["project"]),
            "errors": ["No canonical SHO channel-balance manifest exists."],
            "next_stage": "siril-sho-channel-balance",
            "ghs_pass1_permitted": False,
            "background_neutralization_permitted": False,
            "star_removal_permitted": False,
        }
    errors: list[str] = []
    manifest = load_json(paths["stable_manifest"])
    if manifest.get("helper_version") != VERSION:
        errors.append(f"canonical helper version is {manifest.get('helper_version')!r}")
    if manifest.get("status") != "ready": errors.append("canonical manifest is not ready")
    if manifest.get("visual_review_completed") is not True: errors.append("visual review is not complete")
    if manifest.get("next_stage") != "siril-ghs-stretch-pass1": errors.append("next stage is not GHS pass 1")
    if manifest.get("ghs_pass1_permitted") is not True: errors.append("GHS pass 1 is not permitted")
    if manifest.get("background_neutralization_permitted") is not False: errors.append("background neutralization is incorrectly permitted")
    if manifest.get("star_removal_permitted") is not False: errors.append("StarNet is incorrectly permitted again")
    if manifest.get("stage_order") != {
        "upstream": "siril-starnet-removal",
        "current": "siril-sho-channel-balance",
        "downstream": "siril-ghs-stretch-pass1",
    }:
        errors.append("stage order is not post-StarNet/pre-GHS")
    if not paths["stable_output"].is_file():
        errors.append("canonical starless balanced FITS is missing"); output_sha = None
    else:
        output_sha = sha256_file(paths["stable_output"])
        if output_sha != manifest.get("output", {}).get("sha256"):
            errors.append("canonical starless balanced FITS SHA-256 mismatch")
    try:
        _, source, upstream = validate_upstream(workspace, project_name)
        if source.sha256 != manifest.get("source", {}).get("sha256"):
            errors.append("StarNet starless upstream source changed")
        if upstream["manifest_sha256"] != manifest.get("upstream_summary", {}).get("manifest_sha256"):
            errors.append("StarNet upstream manifest changed")
    except Exception as exc:
        errors.append(f"upstream validation failed: {compact_text(exc)}")
    return {
        "status": "ready" if not errors else "obsolete",
        "helper_version": VERSION,
        "manifest_helper_version": manifest.get("helper_version"),
        "project": str(paths["project"]),
        "canonical_output_sha256": output_sha,
        "selected_candidate": manifest.get("selected_candidate"),
        "selected_coefficients": manifest.get("selected_coefficients"),
        "attempt_count": manifest.get("attempt_count"),
        "visual_review_completed": manifest.get("visual_review_completed"),
        "next_stage": "siril-ghs-stretch-pass1" if not errors else None,
        "ghs_pass1_permitted": False if errors else True,
        "background_neutralization_permitted": False,
        "star_removal_permitted": False,
        "errors": errors,
    }



def fresh_intent(
    paths: dict[str, Path],
    project_name: str,
    source_sha: str,
    canonical_sha: str,
) -> tuple[Path, dict[str, Any]] | None:
    if not paths["intents"].is_dir():
        return None
    matches: list[tuple[float, Path, dict[str, Any]]] = []
    for path in paths["intents"].glob("fresh-run-*.json"):
        try:
            row = load_json(path)
        except Exception:
            continue
        if (
            row.get("status") == "authorized"
            and row.get("project_name") == project_name
            and row.get("source_sha256") == source_sha
            and row.get("canonical_sha256") == canonical_sha
        ):
            matches.append((path.stat().st_mtime, path, row))
    if not matches:
        return None
    _, path, row = sorted(matches, key=lambda item: item[0], reverse=True)[0]
    return path, row


def confirm_fresh_run(workspace: Path, project_name: str) -> dict[str, Any]:
    status = canonical_status(workspace, project_name)
    if status.get("status") != "ready":
        raise ChannelBalanceError(
            "Fresh-run confirmation requires a valid completed canonical result."
        )
    paths = project_paths(workspace, project_name)
    _, source, _ = validate_upstream(workspace, project_name)
    canonical_sha = str(status.get("canonical_output_sha256"))
    paths["intents"].mkdir(parents=True, exist_ok=True)
    intent_path = paths["intents"] / f"fresh-run-{unique_id()}.json"
    payload = {
        "schema_version": 1,
        "helper_version": VERSION,
        "status": "authorized",
        "authorized_at": utc_now(),
        "project_name": project_name,
        "source_sha256": source.sha256,
        "canonical_sha256": canonical_sha,
    }
    json_dump_atomic(intent_path, payload)
    return {
        "status": "fresh_run_confirmed",
        "helper_version": VERSION,
        "project": str(paths["project"]),
        "fresh_intent": str(intent_path),
        "source_sha256": source.sha256,
        "canonical_sha256": canonical_sha,
        "ghs_pass1_permitted": False,
        "background_neutralization_permitted": False,
        "star_removal_permitted": False,
    }


def begin_stage(workspace: Path, project_name: str) -> dict[str, Any]:
    paths = project_paths(workspace, project_name)
    _, source, _ = validate_upstream(workspace, project_name)
    active = latest_active_run(paths)
    if active:
        root, record = active
        return {"status": record["status"], "action": "resume", "run_root": str(root)}
    stable = canonical_status(workspace, project_name)
    if stable.get("status") == "ready":
        canonical_sha = str(stable.get("canonical_output_sha256"))
        intent = fresh_intent(paths, project_name, source.sha256, canonical_sha)
        if intent is None:
            return {
                "status": "confirmation_required", "action": "await_user_confirmation",
                "question": f"SHO channel balance for {project_name} has already completed successfully. Do you want me to run it again as a fresh run?",
                "confirmation_required": True,
            }
        path, _ = intent
        return {"status": "start_new_run", "action": "start_fresh_run", "fresh_intent": str(path)}
    if stable.get("status") == "obsolete":
        # v1.0.1's pre-StarNet canonical is a known migratable predecessor.
        try:
            old = load_json(paths["stable_manifest"])
            old_output = paths["stable"] / "SHO-linear-balanced.fit"
            migratable = (
                old.get("helper_version") == "1.0.1"
                and old.get("status") == "ready"
                and old.get("stage_order") == {
                    "upstream": "siril-sho-combination",
                    "current": "siril-sho-channel-balance",
                    "downstream": "siril-background-neutralization",
                }
                and old_output.is_file()
                and sha256_file(old_output) == old.get("output", {}).get("sha256")
            )
        except Exception:
            migratable = False
        if migratable:
            return {"status": "start_new_run", "action": "migrate_from_pre_starnet_v1.0.1"}
        return {
            "status": "blocked", "action": "stop",
            "reason": "Existing canonical SHO channel-balance state is invalid or obsolete and is not the validated v1.0.1 migration predecessor; preserve it and repair before starting a new run.",
        }
    return {"status": "start_new_run", "action": "start_new_run"}



def current_candidate(record: dict[str, Any]) -> dict[str, Any]:
    name = record.get("current_candidate")
    for candidate in record.get("candidates", []):
        if candidate.get("candidate") == name:
            return candidate
    raise ChannelBalanceError(f"Current candidate {name!r} is unavailable.")


def visual_review_plan(run_root: Path, record: dict[str, Any]) -> dict[str, Any]:
    candidate = current_candidate(record)
    source_preview = record["source_preview"]
    payload = {
        "status": "visual_review_required",
        "action": "read_exact_targets_then_review_refine",
        "helper_version": VERSION,
        "project_name": record["project_name"],
        "candidate": candidate["candidate"],
        "attempt": candidate["attempt"],
        "coefficients": candidate["coefficients"],
        "maximum_attempts": MAX_ATTEMPTS,
        "source_is_starless": True,
        "stars_layer_modified": False,
        "read_targets": [
            {"role": "starless_source", "path": source_preview["path"], "sha256": source_preview["sha256"]},
            {"role": "starless_candidate", "candidate": candidate["candidate"], "path": candidate["preview"]["path"], "sha256": candidate["preview"]["sha256"]},
        ],
        "read_target_policy": {
            "path_handling": "verbatim", "directory_discovery_forbidden": True,
            "on_read_failure": "stop_and_report_exact_failed_path",
            "forbidden_recovery_tools": ["ls", "find", "cat", "grep", "jq", "globbing"],
        },
        "allowed_dominant_problems": list(DOMINANT_PROBLEMS),
        "required_review_fields": ["green", "magenta", "red", "blue", "structure", "noise"],
        "instruction": (
            "Read both paths verbatim. Both images are STARLESS. Evaluate only nebular/background colour balance and structure: green dominance, magenta/purple nebular over-correction, SII-derived red/gold, OIII-derived blue/cyan, faint emission/Pillars/dark lanes, and weak-channel noise. Do not evaluate or modify star colours. Choose exactly one dominant_problem. The helper, not CodeWarrior, chooses the next numeric coefficient."
        ),
        "ghs_pass1_permitted": False,
        "background_neutralization_permitted": False,
        "star_removal_permitted": False,
    }
    assert_context_safe_payload(payload)
    return payload



def propose_coefficients(
    candidate: dict[str, Any],
    dominant_problem: str,
    overshoot_observed: bool,
) -> tuple[dict[str, float] | None, dict[str, Any] | None]:
    if dominant_problem in ("balanced", "no_improvement"):
        return None, None
    if dominant_problem not in PROBLEM_MOVES:
        raise ChannelBalanceError(f"Unsupported dominant problem: {dominant_problem}")

    coefficient, direction = PROBLEM_MOVES[dominant_problem]
    current = {k: float(v) for k, v in candidate["coefficients"].items()}

    prior = candidate.get("change_from_previous")
    if isinstance(prior, dict) and prior.get("coefficient") == coefficient:
        prior_delta = float(prior.get("delta", 0.0))
        proposed_delta = direction * COEFFICIENT_STEPS[coefficient]
        if prior_delta * proposed_delta < 0 and not overshoot_observed:
            raise ChannelBalanceError(
                "The requested refinement reverses the immediately previous "
                f"{coefficient} movement. Reversal is allowed only when "
                "--overshoot-observed is supplied because the prior visual "
                "review explicitly found an overshoot."
            )

    raw = current[coefficient] + direction * COEFFICIENT_STEPS[coefficient]
    low, high = COEFFICIENT_BOUNDS[coefficient]
    new_value = round(max(low, min(high, raw)), 6)
    if math.isclose(new_value, current[coefficient], abs_tol=1e-12):
        return None, {
            "coefficient": coefficient,
            "reason": "coefficient_bound_reached",
            "current_value": current[coefficient],
            "requested_direction": direction,
        }

    proposed = current.copy()
    proposed[coefficient] = new_value
    change = {
        "coefficient": coefficient,
        "previous_value": current[coefficient],
        "new_value": new_value,
        "delta": round(new_value - current[coefficient], 6),
        "dominant_problem": dominant_problem,
        "overshoot_observed": bool(overshoot_observed),
    }
    return proposed, change


def validate_review_note(name: str, value: str) -> str:
    cleaned = compact_text(value, 600)
    if len(cleaned) < 12:
        raise ChannelBalanceError(
            f"{name} visual observation is too vague; provide a specific observation."
        )
    return cleaned


def review_refine_stage(
    workspace: Path,
    project_name: str,
    candidate_name: str,
    dominant_problem: str,
    green_note: str,
    magenta_note: str,
    red_note: str,
    blue_note: str,
    structure_note: str,
    noise_note: str,
    overshoot_observed: bool,
    timeout_seconds: int,
) -> dict[str, Any]:
    paths = project_paths(workspace, project_name)
    active = latest_active_run(paths)
    if active is None:
        raise ChannelBalanceError("No active channel-balance run exists.")
    run_root, record = active

    candidate = current_candidate(record)
    if candidate["candidate"] != candidate_name:
        raise ChannelBalanceError(
            f"Expected current candidate {candidate['candidate']}, got {candidate_name}."
        )
    if candidate.get("review") is not None:
        raise ChannelBalanceError(
            f"{candidate_name} already has a recorded visual review."
        )

    review = {
        "reviewed_at": utc_now(),
        "reviewer": "CodeWarrior",
        "method": "openclaw-read",
        "dominant_problem": dominant_problem,
        "green": validate_review_note("green", green_note),
        "magenta": validate_review_note("magenta", magenta_note),
        "red": validate_review_note("red", red_note),
        "blue": validate_review_note("blue", blue_note),
        "structure": validate_review_note("structure", structure_note),
        "noise": validate_review_note("noise", noise_note),
        "overshoot_observed": bool(overshoot_observed),
    }
    candidate["review"] = review

    _, source, _ = validate_upstream(workspace, project_name)
    if source.sha256 != record["source"]["sha256"]:
        raise ChannelBalanceError("StarNet starless source changed during refinement.")

    if dominant_problem in ("balanced", "no_improvement"):
        record["status"] = "selection_review_required"
        save_run_record(run_root, record)
        return selection_review_plan(run_root, record)

    if len(record["candidates"]) >= MAX_ATTEMPTS:
        record["status"] = "selection_review_required"
        record["stopped_reason"] = "maximum_attempts_reached"
        save_run_record(run_root, record)
        return selection_review_plan(run_root, record)

    proposed, change = propose_coefficients(
        candidate, dominant_problem, overshoot_observed
    )
    if proposed is None:
        record["status"] = "selection_review_required"
        record["stopped_reason"] = (
            change["reason"] if change else dominant_problem
        )
        save_run_record(run_root, record)
        return selection_review_plan(run_root, record)

    next_index = len(record["candidates"]) + 1
    next_candidate = run_candidate(
        run_root=run_root,
        source_path=paths["source"],
        candidate_index=next_index,
        coefficients=proposed,
        change_from_previous=change,
        timeout_seconds=timeout_seconds,
    )
    record["candidates"].append(next_candidate)
    record["current_candidate"] = next_candidate["candidate"]
    record["status"] = "awaiting_review"
    save_run_record(run_root, record)
    return visual_review_plan(run_root, record)


def selection_review_plan(run_root: Path, record: dict[str, Any]) -> dict[str, Any]:
    incomplete = [c["candidate"] for c in record.get("candidates", []) if not isinstance(c.get("review"), dict)]
    if incomplete:
        raise ChannelBalanceError(f"Selection review cannot start until every generated candidate has a visual review: {incomplete}")
    read_targets = [{"role": "starless_source", "path": record["source_preview"]["path"], "sha256": record["source_preview"]["sha256"]}]
    summaries=[]
    for candidate in record["candidates"]:
        read_targets.append({"role": "starless_candidate", "candidate": candidate["candidate"], "path": candidate["preview"]["path"], "sha256": candidate["preview"]["sha256"]})
        summaries.append({"candidate": candidate["candidate"], "attempt": candidate["attempt"], "coefficients": candidate["coefficients"], "dominant_problem": candidate["review"]["dominant_problem"], "blue_mad_ratio": candidate["quality_assessment"]["metrics"]["channel_mad_ratios"]["blue"]})
    payload = {
        "status": "selection_review_required", "action": "read_all_exact_targets_then_select_publish",
        "helper_version": VERSION, "project_name": record["project_name"],
        "generated_candidate_count": len(record["candidates"]), "maximum_attempts": MAX_ATTEMPTS,
        "source_is_starless": True, "stars_layer_modified": False,
        "read_targets": read_targets, "candidates": summaries,
        "selection_rule": "Choose the best acceptable reviewed STARLESS attempt, not necessarily the latest. Prefer the least aggressive coefficients that give convincing SHO nebular separation without magenta/purple nebular bias, weak-OIII noise amplification, or faint-structure loss. Star colour is intentionally outside this stage.",
        "selection_note_format": "candidate-NN=balance:<...>; magenta:<...>; structure:<...>; noise:<...>",
        "read_target_policy": {"path_handling": "verbatim", "directory_discovery_forbidden": True, "on_read_failure": "stop_and_report_exact_failed_path"},
        "ghs_pass1_permitted": False, "background_neutralization_permitted": False, "star_removal_permitted": False,
    }
    assert_context_safe_payload(payload)
    return payload



def parse_selection_notes(
    values: list[str],
    expected_candidates: list[str],
) -> dict[str, str]:
    expected = set(expected_candidates)
    notes: dict[str, str] = {}
    for raw in values:
        if "=" not in raw:
            raise ChannelBalanceError(
                "Each --note must use candidate-NN=balance:<...>; "
                "magenta:<...>; structure:<...>; noise:<...>."
            )
        candidate, body = raw.split("=", 1)
        candidate = candidate.strip()
        if candidate not in expected:
            raise ChannelBalanceError(
                f"Selection note references {candidate!r}; expected {sorted(expected)}."
            )
        if candidate in notes:
            raise ChannelBalanceError(f"Duplicate selection note for {candidate}.")
        fields: dict[str, str] = {}
        for part in [p.strip() for p in body.split(";") if p.strip()]:
            if ":" not in part:
                raise ChannelBalanceError(
                    f"{candidate} note is missing a labeled field."
                )
            key, value = part.split(":", 1)
            key = key.strip().lower()
            value = compact_text(value, 500)
            if key in fields:
                raise ChannelBalanceError(
                    f"{candidate} repeats selection field {key!r}."
                )
            fields[key] = value

        required = set(NOTE_FIELDS)
        if set(fields) != required:
            raise ChannelBalanceError(
                f"{candidate} must contain exactly balance:, magenta:, "
                f"structure:, and noise:. Got {sorted(fields)}."
            )
        for field in NOTE_FIELDS:
            if len(fields[field]) < 12:
                raise ChannelBalanceError(
                    f"{candidate} {field}: observation is too vague."
                )
        notes[candidate] = body.strip()

    if set(notes) != expected:
        raise ChannelBalanceError(
            f"Selection notes must cover every generated candidate exactly. "
            f"Expected {sorted(expected)}, got {sorted(notes)}."
        )
    return notes


def select_publish_stage(
    workspace: Path,
    project_name: str,
    candidate_name: str,
    visual_notes: str,
    candidate_notes: list[str],
) -> dict[str, Any]:
    paths = project_paths(workspace, project_name)
    active = latest_active_run(paths)
    if active is None: raise ChannelBalanceError("No active channel-balance run exists.")
    run_root, record = active
    if record.get("status") != "selection_review_required":
        raise ChannelBalanceError(f"Run is not ready for final selection: {record.get('status')!r}")
    candidates = record.get("candidates", [])
    by_name = {c["candidate"]: c for c in candidates}
    if candidate_name not in by_name: raise ChannelBalanceError(f"Unknown selected candidate {candidate_name!r}.")
    selected = by_name[candidate_name]
    if selected["quality_assessment"]["satisfactory"] is not True: raise ChannelBalanceError("Selected candidate failed technical quality.")
    if not isinstance(selected.get("review"), dict): raise ChannelBalanceError("Selected candidate lacks visual review.")
    expected_names = [c["candidate"] for c in candidates]
    notes = parse_selection_notes(candidate_notes, expected_names)
    overall = validate_review_note("overall visual comparison", visual_notes)
    for candidate in candidates:
        preview = Path(candidate["preview"]["path"])
        if not preview.is_file(): raise ChannelBalanceError(f"Reviewed preview is missing: {preview}")
        if sha256_file(preview) != candidate["preview"]["sha256"]: raise ChannelBalanceError(f"Reviewed preview changed after review: {preview}")
    _, source, upstream_summary = validate_upstream(workspace, project_name)
    if source.sha256 != record["source"]["sha256"]: raise ChannelBalanceError("StarNet starless source changed before publication.")
    output_path = Path(selected["output"]["path"])
    if sha256_file(output_path) != selected["output"]["sha256"]: raise ChannelBalanceError("Selected candidate FITS changed before publication.")
    quality = formula_quality(paths["source"], output_path, selected["coefficients"])
    if quality["satisfactory"] is not True: raise ChannelBalanceError("Selected candidate failed full publication revalidation.")
    staging = run_root / "publish-staging"
    if staging.exists():
        preserved = run_root / f"failed-publish-staging-{unique_id()}"; staging.rename(preserved)
    else: preserved = None
    staging.mkdir(parents=True, exist_ok=False)
    staged_output = staging / "SHO-starless-linear-balanced.fit"
    staged_before = staging / "SHO-starless-linear-before-channel-balance.png"
    staged_after = staging / "SHO-starless-linear-balanced.png"
    staged_manifest = staging / "sho-channel-balance-manifest.json"
    shutil.copy2(output_path, staged_output)
    shutil.copy2(Path(record["source_preview"]["path"]), staged_before)
    shutil.copy2(Path(selected["preview"]["path"]), staged_after)
    selection_record = {
        "completed": True, "recorded_at": utc_now(), "reviewer": "CodeWarrior",
        "selected_candidate": candidate_name, "selected_coefficients": selected["coefficients"],
        "visual_notes": overall, "candidate_notes": notes, "review_method": "openclaw-read",
        "generated_candidates_compared": expected_names,
    }
    final_output_evidence = inspect_fits(staged_output)
    payload = {
        "schema_version": 2, "helper_version": VERSION, "created_at": utc_now(),
        "project": project_name, "project_path": str(paths["project"]),
        "stage_order": {"upstream": "siril-starnet-removal", "current": "siril-sho-channel-balance", "downstream": "siril-ghs-stretch-pass1"},
        "status": "ready", "visual_review_completed": True,
        "source_is_starless": True, "stars_layer_modified": False,
        "source": asdict(source), "upstream_summary": upstream_summary,
        "baseline_coefficients": BASELINE_COEFFICIENTS,
        "coefficient_bounds": {k: [v[0], v[1]] for k, v in COEFFICIENT_BOUNDS.items()},
        "coefficient_steps": COEFFICIENT_STEPS, "maximum_attempts": MAX_ATTEMPTS,
        "attempt_count": len(candidates), "selected_candidate": candidate_name,
        "selected_coefficients": selected["coefficients"], "selected_quality_assessment": quality,
        "selection": selection_record,
        "attempts": [{"candidate": c["candidate"], "attempt": c["attempt"], "coefficients": c["coefficients"], "change_from_previous": c.get("change_from_previous"), "review": c.get("review"), "output_sha256": c["output"]["sha256"], "preview_sha256": c["preview"]["sha256"]} for c in candidates],
        "output": {**asdict(final_output_evidence), "path": str(paths["stable_output"])},
        "previews": {"before": str(paths["stable_before_preview"]), "after": str(paths["stable_after_preview"])},
        "run_root": str(run_root), "next_stage": "siril-ghs-stretch-pass1",
        "ghs_pass1_permitted": True, "background_neutralization_permitted": False, "star_removal_permitted": False,
        "failed_publish_staging_preserved_at": str(preserved) if preserved else None,
    }
    json_dump_atomic(staged_manifest, payload)
    previous = None
    if paths["stable"].exists():
        previous = run_root / f"previous-processing-sho-channel-balance-{unique_id()}"
        paths["stable"].rename(previous)
    try: staging.rename(paths["stable"])
    except Exception:
        if previous is not None and not paths["stable"].exists(): previous.rename(paths["stable"])
        raise
    canonical_sha = sha256_file(paths["stable_output"])
    if canonical_sha != final_output_evidence.sha256: raise ChannelBalanceError("Published canonical output SHA does not match selected candidate.")
    record["status"] = "ready"; record["canonical_output_changed"] = True; record["published_at"] = utc_now()
    record["selected_candidate"] = candidate_name; record["selected_coefficients"] = selected["coefficients"]
    record["previous_processing_sho_channel_balance_preserved_at"] = str(previous) if previous else None
    record["ghs_pass1_permitted"] = True
    save_run_record(run_root, record)
    verification = canonical_status(workspace, project_name)
    if verification.get("status") != "ready": raise ChannelBalanceError(f"Post-publication verification failed: {verification}")
    return {
        "status": "ready", "helper_version": VERSION, "project": str(paths["project"]), "run_root": str(run_root),
        "selected_candidate": candidate_name, "selected_coefficients": selected["coefficients"], "attempt_count": len(candidates),
        "canonical_output_sha256": canonical_sha, "previous_processing_sho_channel_balance_preserved_at": str(previous) if previous else None,
        "visual_review_completed": True, "source_is_starless": True, "stars_layer_modified": False,
        "next_stage": "siril-ghs-stretch-pass1", "ghs_pass1_permitted": True,
        "background_neutralization_permitted": False, "star_removal_permitted": False, "verification": verification,
    }



def advance_stage(*, workspace: Path, project_name: str, timeout_seconds: int, plan_only: bool = False) -> dict[str, Any]:
    entry = begin_stage(workspace, project_name)
    if entry.get("status") == "confirmation_required":
        payload = {"status": "confirmation_required", "action": "await_user_confirmation", "helper_version": VERSION, "project": str(project_paths(workspace, project_name)["project"]), "question": entry["question"], "confirmation_required": True, "ghs_pass1_permitted": False, "background_neutralization_permitted": False, "star_removal_permitted": False}
        assert_context_safe_payload(payload); return payload
    if entry.get("status") == "blocked":
        payload = {"status": "blocked", "action": "stop", "helper_version": VERSION, "reason": compact_text(entry.get("reason", "")), "ghs_pass1_permitted": False, "background_neutralization_permitted": False, "star_removal_permitted": False}
        assert_context_safe_payload(payload); return payload
    if entry.get("action") == "resume":
        run_root=Path(entry["run_root"]); record=load_run_record(run_root)
        if record["status"] == "awaiting_review": return visual_review_plan(run_root, record)
        if record["status"] == "selection_review_required": return selection_review_plan(run_root, record)
        raise ChannelBalanceError(f"Unsupported resumable run status: {record['status']}")
    if plan_only:
        _, source, upstream = validate_upstream(workspace, project_name)
        payload = {
            "status": "would_generate_baseline", "action": "generate_baseline_then_review", "helper_version": VERSION,
            "project": str(project_paths(workspace, project_name)["project"]), "source_sha256": source.sha256,
            "source_path": str(project_paths(workspace, project_name)["source"]), "source_is_starless": True, "stars_layer_modified": False,
            "upstream_contract_mode": upstream["contract_mode"], "baseline_coefficients": BASELINE_COEFFICIENTS,
            "baseline_formula": {
                "red": "med(R) + 1.00 * (R - med(R))",
                "green": "med(R) + 0.25 * (G - med(G))",
                "blue": "med(R) + 1.00 * (B - med(B))",
                "channel_semantics": {"R": "SII-derived starless", "G": "Ha-derived starless", "B": "OIII-derived starless"},
                "rescale": False, "sum_exposure_time": False,
            },
            "coefficient_bounds": {k: [v[0], v[1]] for k, v in COEFFICIENT_BOUNDS.items()}, "coefficient_steps": COEFFICIENT_STEPS,
            "maximum_attempts": MAX_ATTEMPTS, "one_coefficient_change_per_attempt": True, "confirmation_required": False,
            "next_stage": "siril-ghs-stretch-pass1", "ghs_pass1_permitted": False,
            "background_neutralization_permitted": False, "star_removal_permitted": False,
        }
        assert_context_safe_payload(payload); return payload
    fresh_path = Path(entry["fresh_intent"]) if entry.get("fresh_intent") else None
    run_root, record = create_run(workspace, project_name, timeout_seconds, fresh_intent=fresh_path)
    return visual_review_plan(run_root, record)



def status_project(workspace: Path, project_name: str) -> dict[str, Any]:
    stable = canonical_status(workspace, project_name)
    if stable.get("status") == "ready":
        return stable
    paths = project_paths(workspace, project_name)
    active = latest_active_run(paths)
    if active:
        run_root, record = active
        return {
            "status": record["status"],
            "helper_version": VERSION,
            "project": str(paths["project"]),
            "run_root": str(run_root),
            "current_candidate": record.get("current_candidate"),
            "generated_candidate_count": len(record.get("candidates", [])),
            "maximum_attempts": MAX_ATTEMPTS,
            "ghs_pass1_permitted": False,
            "background_neutralization_permitted": False,
            "star_removal_permitted": False,
        }
    return stable



def write_synthetic_upstream(workspace: Path, project_name: str) -> None:
    paths = project_paths(workspace, project_name)
    paths["source"].parent.mkdir(parents=True, exist_ok=False)
    h, w = 96, 112
    y, x = np.mgrid[0:h, 0:w]
    radial = np.exp(-(((x - w * 0.52) / 25.0) ** 2 + ((y - h * 0.49) / 21.0) ** 2))
    ridge = np.exp(-(((x - w * 0.43) / 11.0) ** 2 + ((y - h * 0.57) / 24.0) ** 2))
    r = 0.035 + 0.075 * radial + 0.025 * ridge
    g = 0.042 + 0.280 * radial + 0.050 * ridge
    b = 0.030 + 0.105 * radial + 0.035 * np.roll(ridge, 8, axis=1)
    data = np.stack([r, g, b]).astype(np.float32)
    header = fits.Header(); header["FILTER"] = "mixed_Starless"
    fits.PrimaryHDU(data=data, header=header).writeto(paths["source"], overwrite=False, output_verify="fix")
    source = inspect_fits(paths["source"])
    review_payload = {"schema_version": 1, "project": project_name, "reviewer": "Synthetic", "visual_review_completed": True}
    json_dump_atomic(paths["source_review"], review_payload)
    manifest = {
        "schema_version": 3, "helper_version": "1.5.2", "status": "ready",
        "source_contract_revision": REQUIRED_STARNET_SOURCE_CONTRACT_REVISION,
        "project": project_name, "project_path": str(paths["project"]),
        "stage_order": {"upstream": "siril-background-neutralization", "current": "siril-starnet-removal", "downstream": "siril-sho-channel-balance"},
        "next_stage": "siril-sho-channel-balance", "sho_channel_balance_permitted": True,
        "ghs_pass1_permitted": False, "starless_processing_permitted": True,
        "starless_background_processing_permitted": False, "visual_review_completed": True,
        "linear_starless": asdict(source),
        "visual_review": {"record_path": str(paths["source_review"]), "record_sha256": sha256_file(paths["source_review"])},
    }
    json_dump_atomic(paths["source_manifest"], manifest)




def self_test(timeout_seconds: int) -> dict[str, Any]:
    siril = siril_version()
    root = WORKSPACE / ".skill-self-tests" / "siril-sho-channel-balance" / unique_id()
    workspace = root / "workspace"; workspace.mkdir(parents=True, exist_ok=False)
    project_name = "Synthetic SHO Channel Balance 1.1.0"
    write_synthetic_upstream(workspace, project_name)
    first = advance_stage(workspace=workspace, project_name=project_name, timeout_seconds=timeout_seconds, plan_only=False)
    if first.get("status") != "visual_review_required" or first.get("source_is_starless") is not True:
        raise ChannelBalanceError("Self-test did not generate starless baseline review.")
    second = review_refine_stage(workspace, project_name, "candidate-01", "excessive_green",
        "green contribution is visibly stronger than desired in the synthetic starless nebula",
        "no magenta or purple nebular over-correction is visible in the baseline",
        "SII-derived red structure remains visible and not dominant",
        "OIII-derived blue structure remains visible but weaker than green",
        "faint synthetic emission and central dark structure remain preserved",
        "weak-channel noise remains smooth with no visible amplification", False, timeout_seconds)
    if second.get("status") != "visual_review_required" or second.get("coefficients", {}).get("g") != 0.20:
        raise ChannelBalanceError(f"Self-test expected g=0.20 second starless candidate, got {second}")
    selection = review_refine_stage(workspace, project_name, "candidate-02", "balanced",
        "green contribution is now balanced against synthetic red and blue starless structures",
        "no magenta or purple nebular cast is visible after the bounded green reduction",
        "SII-derived red structure remains distinct and naturally represented",
        "OIII-derived blue structure remains distinct without excessive cyan dominance",
        "faint synthetic outer emission and central dark structure remain preserved",
        "weak-channel noise remains smooth without obvious amplification", False, timeout_seconds)
    if selection.get("status") != "selection_review_required": raise ChannelBalanceError("Self-test did not enter selection review.")
    result = select_publish_stage(workspace, project_name, "candidate-02",
        "Candidate-02 gives the best synthetic STARLESS SHO balance after one bounded green correction while retaining structure and noise quality; no star layer is present or modified.",
        [
            "candidate-01=balance:green remains visibly too dominant in the synthetic starless nebula; magenta:no magenta or purple nebular cast is visible in the baseline candidate; structure:faint outer emission and dark structure remain preserved; noise:blue weak-channel noise remains smooth without visible amplification",
            "candidate-02=balance:red green and blue starless structures are convincingly separated; magenta:no magenta or purple nebular over-correction is visible after refinement; structure:faint outer emission and dark structure remain clearly preserved; noise:blue weak-channel noise remains smooth without visible amplification",
        ])
    if result.get("status") != "ready" or result.get("ghs_pass1_permitted") is not True or result.get("stars_layer_modified") is not False:
        raise ChannelBalanceError("Self-test publication did not become ready for GHS pass 1.")
    return {
        "status": "success", "helper_version": VERSION, "siril": siril,
        "pipeline": ["siril-starnet-removal", "siril-sho-channel-balance", "siril-ghs-stretch-pass1"],
        "source_is_starless": True, "stars_layer_modified": False,
        "baseline_coefficients": BASELINE_COEFFICIENTS, "maximum_attempts": MAX_ATTEMPTS,
        "coefficient_bounds": {k: [v[0], v[1]] for k, v in COEFFICIENT_BOUNDS.items()},
        "coefficient_steps": COEFFICIENT_STEPS, "second_candidate_coefficients": second.get("coefficients"),
        "selected_candidate": result.get("selected_candidate"), "selected_coefficients": result.get("selected_coefficients"),
        "final_status": result.get("verification", {}).get("status"), "ghs_pass1_permitted": result.get("ghs_pass1_permitted"),
        "test_root": str(root),
    }



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bounded autonomous Siril SHO channel balancing."
    )
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("self-test")
    p.add_argument("--timeout", type=int, default=1800)

    p = sub.add_parser("advance")
    p.add_argument("--project", required=True)
    p.add_argument("--timeout", type=int, default=1800)
    p.add_argument("--plan-only", action="store_true")

    p = sub.add_parser("confirm-fresh")
    p.add_argument("--project", required=True)

    p = sub.add_parser("review-refine")
    p.add_argument("--project", required=True)
    p.add_argument("--candidate", required=True)
    p.add_argument(
        "--dominant-problem",
        required=True,
        choices=DOMINANT_PROBLEMS,
    )
    p.add_argument("--green-note", required=True)
    p.add_argument("--magenta-note", required=True)
    p.add_argument("--red-note", required=True)
    p.add_argument("--blue-note", required=True)
    p.add_argument("--structure-note", required=True)
    p.add_argument("--noise-note", required=True)
    p.add_argument("--overshoot-observed", action="store_true")
    p.add_argument("--timeout", type=int, default=1800)

    p = sub.add_parser("select-publish")
    p.add_argument("--project", required=True)
    p.add_argument("--candidate", required=True)
    p.add_argument("--visual-notes", required=True)
    p.add_argument("--note", dest="candidate_notes", action="append", default=[])

    p = sub.add_parser("stage-status")
    p.add_argument("--project", required=True)

    p = sub.add_parser("status")
    p.add_argument("--project", required=True)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "self-test":
            payload = self_test(args.timeout)
        elif args.command == "advance":
            payload = advance_stage(
                workspace=WORKSPACE,
                project_name=args.project,
                timeout_seconds=args.timeout,
                plan_only=args.plan_only,
            )
        elif args.command == "confirm-fresh":
            payload = confirm_fresh_run(WORKSPACE, args.project)
        elif args.command == "review-refine":
            payload = review_refine_stage(
                WORKSPACE,
                args.project,
                args.candidate,
                args.dominant_problem,
                args.green_note,
                args.magenta_note,
                args.red_note,
                args.blue_note,
                args.structure_note,
                args.noise_note,
                args.overshoot_observed,
                args.timeout,
            )
        elif args.command == "select-publish":
            payload = select_publish_stage(
                WORKSPACE,
                args.project,
                args.candidate,
                args.visual_notes,
                args.candidate_notes,
            )
        elif args.command == "stage-status":
            payload = canonical_status(WORKSPACE, args.project)
        elif args.command == "status":
            payload = status_project(WORKSPACE, args.project)
        else:
            raise ChannelBalanceError(f"Unsupported command: {args.command}")

        if args.command != "self-test":
            assert_context_safe_payload(payload)

    except ChannelBalanceError as exc:
        payload = {
            "status": "blocked",
            "helper_version": VERSION,
            "error": compact_text(exc),
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 2

    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload.get("status") in CLI_SUCCESS_STATUSES else 2


if __name__ == "__main__":
    raise SystemExit(main())
