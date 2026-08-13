#!/usr/bin/env python3
import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from astropy.io import fits

VERSION = "1.0.3"
SKILL_NAME = "siril-star-processing"
AGENT_ROOT = Path("/home/peter/.openclaw/workspace/agents/codewarrior")
PROJECTS_ROOT = AGENT_ROOT / "Projects"
SIRIL_BIN = Path("/home/peter/.openclaw/runtime/siril-processor/toolchain/.toolchain/siril/1.4.4/squashfs-root/AppRun")

CANDIDATES = [
    {
        "name": "candidate-00",
        "label": "control",
        "neutralization_mode": "control",
        "threshold_quantile": 0.990,
        "knee_strength": 0.80,
        "base_scale": 0.92,
    },
    {
        "name": "candidate-01",
        "label": "targeted-halo-cleanup",
        "neutralization_mode": "targeted",
        "threshold_quantile": 0.990,
        "knee_strength": 0.80,
        "base_scale": 0.92,
    },
    {
        "name": "candidate-02",
        "label": "full-neutral",
        "neutralization_mode": "full-neutral",
        "threshold_quantile": 0.990,
        "knee_strength": 0.80,
        "base_scale": 0.92,
    },
]

@dataclass
class Paths:
    project_root: Path
    processing_root: Path
    upstream_fit: Path
    upstream_manifest: Path | None
    stage_root: Path
    canonical_fit: Path
    canonical_manifest: Path
    selection_record: Path
    preview_png: Path
    state_root: Path
    auth_file: Path
    current_run_file: Path


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sha256_file(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def json_dump(obj, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding='utf-8')


def json_load(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))


def find_project_paths(project_name: str) -> Paths:
    project_root = PROJECTS_ROOT / project_name
    processing_root = project_root / "processing"
    upstream_fit = processing_root / "starnet" / "SHO-stars-unscreen.fit"
    if not upstream_fit.exists():
        raise SystemExit(json.dumps({
            "status": "blocked",
            "error": f"Upstream Starnet star FITS not found: {upstream_fit}",
            "version": VERSION,
        }, indent=2))
    manifest_candidates = [
        processing_root / "starnet" / "starnet-manifest.json",
        processing_root / "starnet" / "starnet-removal-manifest.json",
        processing_root / "starnet-removal" / "starnet-removal-manifest.json",
    ]
    upstream_manifest = next((p for p in manifest_candidates if p.exists()), None)
    stage_root = processing_root / "star-processing"
    canonical_fit = stage_root / "SHO-stars-processed.fit"
    canonical_manifest = stage_root / "star-processing-manifest.json"
    selection_record = stage_root / "visual-selection-record.json"
    preview_png = stage_root / "SHO-stars-processed-before-recombination.png"
    state_root = project_root / ".siril-star-processing"
    auth_file = state_root / "confirm-fresh.json"
    current_run_file = state_root / "current-run.json"
    return Paths(project_root, processing_root, upstream_fit, upstream_manifest, stage_root,
                 canonical_fit, canonical_manifest, selection_record, preview_png,
                 state_root, auth_file, current_run_file)


def load_rgb(path: Path) -> np.ndarray:
    data = fits.getdata(path)
    arr = np.asarray(data, dtype=np.float32)
    if arr.ndim == 3 and arr.shape[0] == 3:
        arr = np.moveaxis(arr, 0, -1)
    if arr.ndim != 3 or arr.shape[-1] != 3:
        raise RuntimeError(f"Expected RGB FITS, got shape {arr.shape} from {path}")
    return arr


def image_metrics(rgb: np.ndarray) -> dict:
    lum = rgb.mean(axis=-1)
    maxc = rgb.max(axis=-1)
    minc = rgb.min(axis=-1)
    with np.errstate(divide='ignore', invalid='ignore'):
        sat = np.where(maxc > 0, (maxc - minc) / maxc, 0.0)
    return {
        "median": float(np.median(lum)),
        "maximum": float(np.max(rgb)),
        "minimum": float(np.min(rgb)),
        "saturation_median": float(np.median(sat)),
        "saturation_p90": float(np.quantile(sat, 0.90)),
        "saturation_p99": float(np.quantile(sat, 0.99)),
        "lum_p99": float(np.quantile(lum, 0.99)),
        "lum_p999": float(np.quantile(lum, 0.999)),
        "lum_p9999": float(np.quantile(lum, 0.9999)),
        "finite_fraction": float(np.isfinite(rgb).mean()),
    }


def compare_metrics(src: np.ndarray, out: np.ndarray) -> dict:
    src_lum = src.mean(axis=-1)
    out_lum = out.mean(axis=-1)
    src_max = src.max(axis=-1)
    src_min = src.min(axis=-1)
    out_max = out.max(axis=-1)
    out_min = out.min(axis=-1)
    with np.errstate(divide='ignore', invalid='ignore'):
        src_sat = np.where(src_max > 0, (src_max - src_min) / src_max, 0.0)
        out_sat = np.where(out_max > 0, (out_max - out_min) / out_max, 0.0)

    def hue_degrees(rgb: np.ndarray) -> np.ndarray:
        r = rgb[..., 0]
        g = rgb[..., 1]
        b = rgb[..., 2]
        mx = np.max(rgb, axis=-1)
        mn = np.min(rgb, axis=-1)
        delta = mx - mn
        hue = np.zeros_like(mx, dtype=np.float32)
        nz = delta > 1e-8
        rm = nz & (mx == r)
        gm = nz & (mx == g)
        bm = nz & (mx == b)
        hue[rm] = (60.0 * (((g[rm] - b[rm]) / delta[rm]) % 6.0)).astype(np.float32)
        hue[gm] = (60.0 * (((b[gm] - r[gm]) / delta[gm]) + 2.0)).astype(np.float32)
        hue[bm] = (60.0 * (((r[bm] - g[bm]) / delta[bm]) + 4.0)).astype(np.float32)
        return hue

    src_hue = hue_degrees(src)
    out_hue = hue_degrees(out)

    star_cut = float(np.quantile(src_lum, 0.99))
    bright_cut = float(np.quantile(src_lum, 0.999))
    halo_low_cut = float(np.quantile(src_lum, 0.95))
    star_mask = src_lum >= star_cut
    bright_mask = src_lum >= bright_cut
    # Halo / wing analysis deliberately uses a source-defined lower-luminance
    # band beneath the brightest cores. This is the region the previous metric
    # missed when it declared the bright cores neutral while colored halos
    # remained visibly present.
    halo_band = (src_lum >= halo_low_cut) & (src_lum < bright_cut)

    # Approximate the Siril hue ranges the user wants neutralized:
    # magenta->pink, pink->orange, and orange->yellow.  The metric uses HSV-like
    # hue only for validation; Siril still performs all image transforms.
    src_unwanted_hue = ((src_hue < 90.0) | (src_hue >= 285.0)) & (src_sat > 0.02)
    out_unwanted_hue = ((out_hue < 90.0) | (out_hue >= 285.0)) & (out_sat > 0.02)
    src_cool_hue = (src_hue >= 90.0) & (src_hue < 285.0) & (src_sat > 0.02)

    def sat_median(values: np.ndarray, mask: np.ndarray) -> float:
        if not np.any(mask):
            return 0.0
        return float(np.median(values[mask]))

    def frac(mask: np.ndarray, domain: np.ndarray) -> float:
        count = int(np.count_nonzero(domain))
        if count == 0:
            return 0.0
        return float(np.count_nonzero(mask & domain) / count)

    src_star_sat = sat_median(src_sat, star_mask)
    out_star_sat = sat_median(out_sat, star_mask)
    src_bright_sat = sat_median(src_sat, bright_mask)
    out_bright_sat = sat_median(out_sat, bright_mask)
    src_halo_sat = sat_median(src_sat, halo_band)
    out_halo_sat = sat_median(out_sat, halo_band)
    src_unwanted_frac = frac(src_unwanted_hue, halo_band)
    out_unwanted_frac = frac(out_unwanted_hue, halo_band)
    src_unwanted_sat = sat_median(src_sat, halo_band & src_unwanted_hue)
    out_unwanted_sat_same_pixels = sat_median(out_sat, halo_band & src_unwanted_hue)
    src_cool_sat = sat_median(src_sat, halo_band & src_cool_hue)
    out_cool_sat_same_pixels = sat_median(out_sat, halo_band & src_cool_hue)

    return {
        "absolute_luma_median_change": float(abs(np.median(out_lum) - np.median(src_lum))),
        "luma_correlation_diagnostic": float(np.corrcoef(src_lum.ravel()[::32], out_lum.ravel()[::32])[0,1]),
        "added_low_clip_fraction": float(np.mean((out <= 0).all(axis=-1) & ~(src <= 0).all(axis=-1))),
        "added_high_clip_fraction": float(np.mean((out >= 1).any(axis=-1) & ~(src >= 1).any(axis=-1))),
        "source_saturation_median": float(np.median(src_sat)),
        "output_saturation_median": float(np.median(out_sat)),
        "saturation_median_change": float(np.median(out_sat) - np.median(src_sat)),
        "source_star_saturation_median": src_star_sat,
        "output_star_saturation_median": out_star_sat,
        "star_saturation_reduction_fraction": float(1.0 - out_star_sat / max(src_star_sat, 1e-8)),
        "source_bright_star_saturation_median": src_bright_sat,
        "output_bright_star_saturation_median": out_bright_sat,
        "bright_star_saturation_reduction_fraction": float(1.0 - out_bright_sat / max(src_bright_sat, 1e-8)),
        "halo_band_low_quantile": 0.95,
        "halo_band_high_quantile": 0.999,
        "source_halo_band_saturation_median": src_halo_sat,
        "output_halo_band_saturation_median": out_halo_sat,
        "halo_band_saturation_reduction_fraction": float(1.0 - out_halo_sat / max(src_halo_sat, 1e-8)),
        "source_unwanted_warm_magenta_halo_fraction": src_unwanted_frac,
        "output_unwanted_warm_magenta_halo_fraction": out_unwanted_frac,
        "unwanted_warm_magenta_halo_reduction_fraction": float(1.0 - out_unwanted_frac / max(src_unwanted_frac, 1e-8)),
        "source_unwanted_warm_magenta_halo_saturation_median": src_unwanted_sat,
        "output_unwanted_warm_magenta_halo_saturation_median_on_source_pixels": out_unwanted_sat_same_pixels,
        "unwanted_warm_magenta_halo_saturation_reduction_fraction": float(1.0 - out_unwanted_sat_same_pixels / max(src_unwanted_sat, 1e-8)),
        "source_cool_halo_saturation_median": src_cool_sat,
        "output_cool_halo_saturation_median_on_source_pixels": out_cool_sat_same_pixels,
        "source_lum_p95": float(np.quantile(src_lum, 0.95)),
        "output_lum_p95": float(np.quantile(out_lum, 0.95)),
        "dim_star_reduction_fraction": float(1.0 - (np.quantile(out_lum, 0.95) / max(np.quantile(src_lum, 0.95), 1e-8))),
        "source_lum_p99": float(np.quantile(src_lum, 0.99)),
        "output_lum_p99": float(np.quantile(out_lum, 0.99)),
        "source_lum_p999": float(np.quantile(src_lum, 0.999)),
        "output_lum_p999": float(np.quantile(out_lum, 0.999)),
        "source_lum_p9999": float(np.quantile(src_lum, 0.9999)),
        "output_lum_p9999": float(np.quantile(out_lum, 0.9999)),
        "bright_star_reduction_fraction": float(1.0 - (np.quantile(out_lum, 0.9999) / max(np.quantile(src_lum, 0.9999), 1e-8))),
        "mid_bright_reduction_fraction": float(1.0 - (np.quantile(out_lum, 0.999) / max(np.quantile(src_lum, 0.999), 1e-8))),
    }


def run_siril_script(workdir: Path, script_text: str) -> None:
    script = workdir / "script.ssf"
    script.write_text(script_text, encoding='utf-8')
    env = os.environ.copy()
    runner_home = workdir / ".runner-home"
    ensure_dir(runner_home)
    env["HOME"] = str(runner_home)
    env["APPDIR"] = str(SIRIL_BIN.parent)
    proc = subprocess.run(
        [str(SIRIL_BIN), "siril-cli", "--directory", str(workdir), "--script", str(script)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    if proc.returncode != 0:
        failure = {
            "script": script_text,
            "stdout_tail": proc.stdout[-8000:],
            "stderr_tail": proc.stderr[-4000:],
            "workdir": str(workdir),
        }
        json_dump(failure, workdir / f"siril-failure-{int(datetime.now().timestamp()*1000)}.json")
        raise RuntimeError(f"Siril failed ({proc.returncode}) in {workdir}")


def build_script(candidate: dict, threshold: float) -> str:
    t = min(max(float(threshold), 1e-6), 0.98)
    k = float(candidate["knee_strength"])
    scale = float(candidate["base_scale"])
    expr = (
        f'iif($T <= {t:.9f}, $T * {scale:.6f}, '
        f'({t:.9f} + ($T - {t:.9f}) / '
        f'(1 + {k:.6f} * ($T - {t:.9f}) / (1 - {t:.9f}))) * {scale:.6f})'
    )
    mode = candidate["neutralization_mode"]
    if mode == "control":
        neutralization_commands = ["satu -1.00 2.00 6"]
    elif mode == "targeted":
        # Keep the current thresholded global neutralization for bright cores,
        # then explicitly reach faint warm/magenta halo pixels by disabling the
        # background threshold only for the unwanted hue families.
        neutralization_commands = [
            "satu -1.00 2.00 6",
            "satu -1.00 0.00 5",  # magenta -> pink
            "satu -1.00 0.00 0",  # pink -> orange
            "satu -1.00 0.00 1",  # orange -> yellow
        ]
    elif mode == "full-neutral":
        neutralization_commands = ["satu -1.00 0.00 6"]
    else:
        raise RuntimeError(f"Unknown neutralization mode: {mode}")
    return "\n".join([
        "requires 1.4.4",
        "load input.fit",
        f'pm "{expr}" -nosum',
        *neutralization_commands,
        "save processed.fit",
        "savepng preview",
        "close",
        "",
    ])


def perform_candidate_generation(input_fit: Path, run_root: Path) -> dict:
    ensure_dir(run_root)
    src = load_rgb(input_fit)
    src_stats = image_metrics(src)
    source_luma = src.mean(axis=-1)
    candidates_out = []
    eligible = []
    for candidate in CANDIDATES:
        cdir = run_root / candidate["name"]
        ensure_dir(cdir)
        shutil.copy2(input_fit, cdir / "input.fit")
        threshold = float(np.quantile(source_luma, candidate["threshold_quantile"]))
        run_siril_script(cdir, build_script(candidate, threshold))
        out_fit = cdir / "processed.fit"
        out_png = cdir / "preview.png"
        out_rgb = load_rgb(out_fit)
        metrics = compare_metrics(src, out_rgb)
        meta = {
            "candidate": candidate["name"],
            "label": candidate["label"],
            "fit_path": str(out_fit),
            "png_path": str(out_png),
            "fit_sha256": sha256_file(out_fit),
            "png_sha256": sha256_file(out_png),
            "parameters": {**candidate, "resolved_threshold": threshold},
            "metrics": metrics,
            "eligible": bool(
                metrics["added_low_clip_fraction"] == 0.0 and
                metrics["added_high_clip_fraction"] == 0.0 and
                metrics["luma_correlation_diagnostic"] > 0.97 and
                metrics["dim_star_reduction_fraction"] <= 0.15 and
                metrics["bright_star_reduction_fraction"] >= 0.20 and
                metrics["bright_star_reduction_fraction"] >= metrics["dim_star_reduction_fraction"] + 0.10 and
                metrics["star_saturation_reduction_fraction"] >= 0.20 and
                np.isfinite(list(metrics.values())).all()
            ),
        }
        candidates_out.append(meta)
        if meta["eligible"]:
            eligible.append(candidate["name"])
    if not eligible:
        raise RuntimeError("No technically safe candidate produced")
    recommended = "candidate-01" if "candidate-01" in eligible else eligible[0]
    summary = {
        "status": "awaiting_visual_selection",
        "version": VERSION,
        "source": {
            "path": str(input_fit),
            "sha256": sha256_file(input_fit),
            **src_stats,
        },
        "candidate_count": len(candidates_out),
        "eligible": eligible,
        "recommended_candidate": recommended,
        "candidates": candidates_out,
    }
    json_dump(summary, run_root / "candidate-summary.json")
    return summary


def current_status(paths: Paths) -> tuple[str, list[str]]:
    if not paths.canonical_manifest.exists() or not paths.canonical_fit.exists():
        return "missing", []
    try:
        manifest = json_load(paths.canonical_manifest)
    except Exception:
        return "obsolete", ["Existing manifest is unreadable."]
    reasons = []
    output_sha = sha256_file(paths.canonical_fit)
    if manifest.get("output", {}).get("sha256") != output_sha:
        reasons.append("Canonical output checksum differs from manifest.")
    if manifest.get("source", {}).get("path") != str(paths.upstream_fit):
        reasons.append("Recorded source path is not the current Starnet star FITS.")
    if manifest.get("source", {}).get("sha256") != sha256_file(paths.upstream_fit):
        reasons.append("Recorded source checksum differs from the current Starnet star FITS.")
    if manifest.get("stage_order", {}).get("upstream") != "siril-starnet-removal":
        reasons.append("Recorded upstream stage is not siril-starnet-removal.")
    return ("current", reasons) if not reasons else ("obsolete", reasons)


def begin(project_name: str) -> dict:
    paths = find_project_paths(project_name)
    status, reasons = current_status(paths)
    if status == "current":
        return {
            "status": "confirmation_required",
            "current_canonical_status": "current",
            "question": f"Star processing for {project_name} has already completed. Do you want me to run it again as a fresh run?",
            "next_command_after_confirmation": f"{Path(__file__).resolve().parent.parent / 'bin' / 'star-processing'} confirm-fresh --project \"{project_name}\" && {Path(__file__).resolve().parent.parent / 'bin' / 'star-processing'} advance --project \"{project_name}\"",
            "version": VERSION,
        }
    if status == "obsolete":
        return {
            "status": "confirmation_required",
            "current_canonical_status": "obsolete",
            "obsolete_reasons": reasons,
            "question": f"Star processing for {project_name} has already completed but is obsolete for the current Starnet star branch. Do you want me to run it again as a fresh run?",
            "next_command_after_confirmation": f"{Path(__file__).resolve().parent.parent / 'bin' / 'star-processing'} confirm-fresh --project \"{project_name}\" && {Path(__file__).resolve().parent.parent / 'bin' / 'star-processing'} advance --project \"{project_name}\"",
            "version": VERSION,
        }
    return {
        "status": "would_generate_candidates",
        "production_processing_started": False,
        "source_sha256": sha256_file(paths.upstream_fit),
        "version": VERSION,
    }


def confirm_fresh(project_name: str) -> dict:
    paths = find_project_paths(project_name)
    ensure_dir(paths.state_root)
    payload = {
        "project": project_name,
        "source_path": str(paths.upstream_fit),
        "source_sha256": sha256_file(paths.upstream_fit),
        "version": VERSION,
        "confirmed_at_utc": now_utc(),
    }
    json_dump(payload, paths.auth_file)
    return {"status": "confirmed", "version": VERSION}


def advance(project_name: str) -> dict:
    paths = find_project_paths(project_name)
    status, _ = current_status(paths)
    if status in {"current", "obsolete"}:
        if not paths.auth_file.exists():
            raise SystemExit(json.dumps({
                "status": "blocked",
                "error": "Fresh rerun confirmation is required before advance.",
                "version": VERSION,
            }, indent=2))
        auth = json_load(paths.auth_file)
        if auth.get("source_sha256") != sha256_file(paths.upstream_fit):
            raise SystemExit(json.dumps({
                "status": "blocked",
                "error": "Stored confirmation does not match the current upstream source SHA.",
                "version": VERSION,
            }, indent=2))
    ensure_dir(paths.state_root)
    run_root = paths.project_root / ".siril-star-processing" / f"star-processing-{now_utc()}-{sha256_file(paths.upstream_fit)[:8]}"
    summary = perform_candidate_generation(paths.upstream_fit, run_root)
    json_dump({
        "run_root": str(run_root),
        "source_sha256": sha256_file(paths.upstream_fit),
        "created_at_utc": now_utc(),
    }, paths.current_run_file)
    if paths.auth_file.exists():
        consumed = json_load(paths.auth_file)
        consumed["status"] = "consumed"
        consumed["consumed_at_utc"] = now_utc()
        json_dump(consumed, paths.auth_file)
    return {
        "instruction": "Read all three candidate PNG targets verbatim, compare them, then call select-publish with the selected candidate and notes for all three.",
        "version": VERSION,
        "status": "visual_review_required",
        "project_name": project_name,
        "run_root": str(run_root),
        "selection_rule": "Prefer removal of yellow/orange/magenta halos while retaining the validated star dimming. Faint blue/green stars are acceptable and should not be penalized. Compare halo cleanup, cool-color preservation, profiles and overall balance; visual review is authoritative.",
        "read_target_policy": {
            "directory_discovery_forbidden": True,
            "path_handling": "verbatim",
            "on_read_failure": "stop_and_report_exact_failed_path",
        },
        "read_targets": [
            {
                "candidate": c["candidate"],
                "path": c["png_path"],
                "sha256": c["png_sha256"],
            }
            for c in summary["candidates"]
        ],
        "required_review_fields": [
            "warm_magenta_halos",
            "faint_blue_green_preservation",
            "bright_star_dominance",
            "dim_star_retention",
            "profiles",
            "overall_balance",
        ],
    }


def select_publish(project_name: str, run_root: Path, candidate_name: str, compared: list[str], notes: list[str]) -> dict:
    paths = find_project_paths(project_name)
    summary = json_load(run_root / "candidate-summary.json")
    candidates = {c["candidate"]: c for c in summary["candidates"]}
    if candidate_name not in candidates:
        raise SystemExit(json.dumps({"status": "blocked", "error": f"Unknown candidate: {candidate_name}", "version": VERSION}, indent=2))
    selected = candidates[candidate_name]
    ensure_dir(paths.stage_root)
    previous_preserved = None
    if paths.canonical_fit.exists():
        previous_preserved = str(paths.stage_root / f"SHO-stars-processed.before-{now_utc()}.fit")
        shutil.copy2(paths.canonical_fit, previous_preserved)
    shutil.copy2(selected["fit_path"], paths.canonical_fit)
    shutil.copy2(selected["png_path"], paths.preview_png)
    comparison_notes = dict(zip(compared, notes))
    manifest = {
        "status": "ready",
        "version": VERSION,
        "project": project_name,
        "source": {
            "stage": "siril-starnet-removal",
            "path": str(paths.upstream_fit),
            "sha256": sha256_file(paths.upstream_fit),
            "manifest_path": str(paths.upstream_manifest) if paths.upstream_manifest else None,
            "manifest_sha256": sha256_file(paths.upstream_manifest) if paths.upstream_manifest and paths.upstream_manifest.exists() else None,
        },
        "output": {
            "path": str(paths.canonical_fit),
            "sha256": sha256_file(paths.canonical_fit),
            "preview_path": str(paths.preview_png),
            "preview_sha256": sha256_file(paths.preview_png),
        },
        "selected_candidate": candidate_name,
        "selected_parameters": selected["parameters"],
        "selected_metrics": selected["metrics"],
        "stage_order": {
            "upstream": "siril-starnet-removal",
            "current": "siril-star-processing",
            "downstream": "siril-star-recombination",
        },
        "recombination_processing_permitted": True,
        "next_stage": "siril-star-recombination",
        "run_root": str(run_root),
        "previous_canonical_preserved_at": previous_preserved,
        "published_at_utc": now_utc(),
    }
    selection = {
        "selected_candidate": candidate_name,
        "compared_candidates": compared,
        "notes": comparison_notes,
        "run_root": str(run_root),
        "published_at_utc": manifest["published_at_utc"],
    }
    json_dump(manifest, paths.canonical_manifest)
    json_dump(selection, paths.selection_record)
    return {
        "status": "ready",
        "version": VERSION,
        "project": project_name,
        "selected_candidate": candidate_name,
        "canonical_output": str(paths.canonical_fit),
        "canonical_output_sha256": sha256_file(paths.canonical_fit),
        "canonical_manifest": str(paths.canonical_manifest),
        "next_stage": "siril-star-recombination",
        "recombination_permitted": True,
        "previous_canonical_preserved_at": previous_preserved,
    }


def smoke_test(input_fit: Path, manifest_path: Path | None) -> dict:
    work = Path("/home/peter") / f"siril-star-processing-smoke-{now_utc()}-{sha256_file(input_fit)[:8]}"
    ensure_dir(work)
    project_processing = work / "Projects" / "M16 July 2026" / "processing" / "starnet"
    ensure_dir(project_processing)
    copied_fit = project_processing / "SHO-stars-unscreen.fit"
    shutil.copy2(input_fit, copied_fit)
    if manifest_path and manifest_path.exists():
        copied_manifest = project_processing / manifest_path.name
        shutil.copy2(manifest_path, copied_manifest)
    run_root = work / "run"
    summary = perform_candidate_generation(copied_fit, run_root)
    material = [
        c for c in summary["candidates"]
        if c["metrics"]["saturation_median_change"] < -0.02 and
           c["metrics"]["bright_star_reduction_fraction"] > 0.20 and
           c["metrics"]["bright_star_reduction_fraction"] > c["metrics"]["dim_star_reduction_fraction"] + 0.10
    ]
    return {
        "status": "awaiting_visual_selection" if material else "blocked",
        "smoke_root": str(work),
        "material_candidates": [c["candidate"] for c in material],
        **summary,
    }


def self_test() -> dict:
    return {
        "status": "success",
        "version": VERSION,
        "upstream": "siril-starnet-removal",
        "candidate_count": len(CANDIDATES),
        "candidate_labels": [c["label"] for c in CANDIDATES],
        "uses_negative_satu_for_neutralization": True,
        "uses_pixelmath_soft_knee_for_brightness_control": True,
        "target_adaptive_brightness_threshold": True,
        "bright_stars_reduced_more_than_dim_stars": True,
        "focuses_on_bright_star_halo_neutralization": True,
        "halo_aware_lower_luminance_validation": True,
        "targeted_warm_magenta_hue_cleanup": True,
        "candidate_brightness_policy_identical": True,
        "completed_stage_requires_confirmation": True,
        "exact_path_visual_review": True,
        "downstream": "siril-star-recombination",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(prog="star_processing.py")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("version")
    sub.add_parser("self-test")

    p_begin = sub.add_parser("begin")
    p_begin.add_argument("--project", required=True)

    p_confirm = sub.add_parser("confirm-fresh")
    p_confirm.add_argument("--project", required=True)

    p_adv = sub.add_parser("advance")
    p_adv.add_argument("--project", required=True)

    p_sel = sub.add_parser("select-publish")
    p_sel.add_argument("--project", required=True)
    p_sel.add_argument("--run-root", required=True)
    p_sel.add_argument("--candidate", required=True)
    p_sel.add_argument("--compared", action="append", required=True)
    p_sel.add_argument("--note", action="append", required=True)

    p_smoke = sub.add_parser("smoke-test")
    p_smoke.add_argument("--input", required=True)
    p_smoke.add_argument("--manifest", required=False)

    args = parser.parse_args(argv)
    if args.cmd == "version":
        print(VERSION)
        return 0
    if args.cmd == "self-test":
        print(json.dumps(self_test(), indent=2))
        return 0
    if args.cmd == "begin":
        print(json.dumps(begin(args.project), indent=2))
        return 0
    if args.cmd == "confirm-fresh":
        print(json.dumps(confirm_fresh(args.project), indent=2))
        return 0
    if args.cmd == "advance":
        print(json.dumps(advance(args.project), indent=2))
        return 0
    if args.cmd == "select-publish":
        print(json.dumps(select_publish(args.project, Path(args.run_root), args.candidate, args.compared, args.note), indent=2))
        return 0
    if args.cmd == "smoke-test":
        manifest = Path(args.manifest) if args.manifest else None
        print(json.dumps(smoke_test(Path(args.input), manifest), indent=2))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
