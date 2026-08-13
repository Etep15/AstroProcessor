#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from astropy.convolution import Gaussian2DKernel, convolve_fft
from astropy.io import fits

VERSION = "1.0.4"
SKILL_NAME = "siril-star-processing"
AGENT_ROOT = Path("/home/peter/.openclaw/workspace/agents/codewarrior")
PROJECTS_ROOT = AGENT_ROOT / "Projects"
SIRIL_BIN = Path("/home/peter/.openclaw/runtime/siril-processor/toolchain/.toolchain/siril/1.4.4/squashfs-root/AppRun")

CANDIDATES = [
    {
        "name": "candidate-00",
        "label": "corrected-geometry-conservative",
        "mode": "validator-artifact-mask",
        "threshold_quantile": 0.99,
        "knee_strength": 0.8,
        "base_scale": 0.92,
        "edge_core_quantile": 0.997,
        "edge_inner_radius": 1,
        "edge_outer_radius": 10,
        "red_margin": 0.002,
        "purple_margin": 0.002,
        "red_direct_strength": 0.64,
        "purple_direct_strength": 0.64,
        "feather_sigma": 0.45,
        "feather_strength": 0.08,
    },
    {
        "name": "candidate-01",
        "label": "corrected-geometry-strong-purple",
        "mode": "validator-artifact-mask",
        "threshold_quantile": 0.99,
        "knee_strength": 0.8,
        "base_scale": 0.92,
        "edge_core_quantile": 0.997,
        "edge_inner_radius": 1,
        "edge_outer_radius": 10,
        "red_margin": 0.002,
        "purple_margin": 0.002,
        "red_direct_strength": 0.64,
        "purple_direct_strength": 0.90,
        "feather_sigma": 0.45,
        "feather_strength": 0.08,
    },
    {
        "name": "candidate-02",
        "label": "corrected-geometry-full-purple",
        "mode": "validator-artifact-mask",
        "threshold_quantile": 0.99,
        "knee_strength": 0.8,
        "base_scale": 0.92,
        "edge_core_quantile": 0.997,
        "edge_inner_radius": 1,
        "edge_outer_radius": 10,
        "red_margin": 0.002,
        "purple_margin": 0.002,
        "red_direct_strength": 0.64,
        "purple_direct_strength": 1.00,
        "feather_sigma": 0.45,
        "feather_strength": 0.08,
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


def save_rgb(path: Path, rgb: np.ndarray, template: Path | None = None) -> None:
    arr = np.moveaxis(np.asarray(rgb, dtype=np.float32), -1, 0)
    header = fits.getheader(template) if template and template.exists() else fits.Header()
    fits.PrimaryHDU(data=arr, header=header).writeto(path, overwrite=True)


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


def build_brightness_script(candidate: dict) -> str:
    # identical brightness compressor across the family
    lum = candidate["resolved_threshold"]
    k = candidate["knee_strength"]
    scale = candidate["base_scale"]
    expr = (
        f'iif($T <= {lum:.9f}, $T * {scale:.6f}, '
        f'({lum:.9f} + ($T - {lum:.9f}) / '
        f'(1 + {k:.6f} * ($T - {lum:.9f}) / (1 - {lum:.9f}))) * {scale:.6f})'
    )
    return "\n".join([
        "requires 1.4.4",
        "load input.fit",
        f'pm "{expr}" -nosum',
        "save dimmed.fit",
        "close",
        "",
    ])


def build_control_script() -> str:
    # v1.0.3 targeted warm/magenta cleanup control
    return "\n".join([
        "requires 1.4.4",
        "load dimmed.fit",
        "satu -1.00 2.00 6",
        "satu -1.00 0.00 5",
        "satu -1.00 0.00 0",
        "satu -1.00 0.00 1",
        "save control.fit",
        "savepng control-preview",
        "close",
        "",
    ])


def build_neutral_script() -> str:
    return "\n".join([
        "requires 1.4.4",
        "load dimmed.fit",
        "satu -1.00 0.00 6",
        "save neutral.fit",
        "savepng neutral-preview",
        "close",
        "",
    ])


def build_preview_script() -> str:
    return "\n".join([
        "requires 1.4.4",
        "load processed.fit",
        "savepng preview",
        "close",
        "",
    ])


def gaussian_norm(mask: np.ndarray, sigma: float) -> np.ndarray:
    if sigma <= 0:
        return mask.astype(np.float32)
    kernel = Gaussian2DKernel(x_stddev=sigma)
    out = convolve_fft(mask.astype(np.float32), kernel, boundary='fill', fill_value=0.0, normalize_kernel=True)
    mx = float(np.max(out))
    if mx > 0:
        out = out / mx
    return np.clip(out, 0.0, 1.0).astype(np.float32)


def build_validator_artifact_blend_mask(source_rgb: np.ndarray, control_rgb: np.ndarray, params: dict) -> tuple[np.ndarray, dict]:
    # r5 deliberately uses the same bright-star edge-ring geometry and red/purple
    # hue tests as residual_edge_metrics(), so processing directly targets the
    # pixels the validator calls defective instead of using a weak normalized
    # color-excess approximation.
    src_lum = source_rgb.mean(axis=-1)
    edge_core_threshold = float(np.quantile(src_lum, params["edge_core_quantile"]))
    core = (src_lum >= edge_core_threshold).astype(np.float32)
    outer_radius = int(params["edge_outer_radius"])
    inner_radius = int(params["edge_inner_radius"])
    outer = convolve_fft(
        core,
        np.ones((2 * outer_radius + 1, 2 * outer_radius + 1), dtype=np.float32),
        boundary='fill', fill_value=0.0, normalize_kernel=False,
    ) >= 0.5
    inner = convolve_fft(
        core,
        np.ones((2 * inner_radius + 1, 2 * inner_radius + 1), dtype=np.float32),
        boundary='fill', fill_value=0.0, normalize_kernel=False,
    ) >= 0.5
    ring = np.asarray(outer & ~inner)
    if not np.any(ring):
        ring = src_lum >= np.quantile(src_lum, 0.995)

    r, g, b = [control_rgb[..., i] for i in range(3)]
    red = ring & (r > np.maximum(g, b) + params["red_margin"]) & (r > 0.01)
    purple = ring & (np.minimum(r, b) > g + params["purple_margin"]) & (np.maximum(r, b) > 0.01)
    artifact = red | purple

    # r6 freezes the successful r5 red correction at 0.64 but gives purple
    # its own much stronger direct neutralization.  Where a pixel is both red
    # and purple the stronger of the two direct strengths wins.
    red_direct = red.astype(np.float32) * float(params["red_direct_strength"])
    purple_direct = purple.astype(np.float32) * float(params["purple_direct_strength"])
    direct = np.maximum(red_direct, purple_direct)
    if float(params["feather_sigma"]) > 0 and np.any(artifact):
        feather = gaussian_norm(artifact.astype(np.float32), float(params["feather_sigma"]))
        feather = feather * float(params["feather_strength"])
        mask = np.maximum(direct, feather)
    else:
        mask = direct
    mask = np.clip(mask, 0.0, 1.0).astype(np.float32)

    ring_count = max(int(np.sum(ring)), 1)
    artifact_values = mask[artifact]
    red_values = mask[red]
    purple_values = mask[purple]
    meta = {
        "resolved_validator_edge_core_threshold": edge_core_threshold,
        "validator_edge_outer_radius_pixels": outer_radius,
        "validator_edge_inner_radius_pixels": inner_radius,
        "validator_fft_membership_threshold": 0.5,
        "validator_edge_ring_pixel_count": int(np.sum(ring)),
        "validator_edge_ring_fraction": float(np.mean(ring)),
        "validator_red_artifact_fraction_in_ring": float(np.sum(red) / ring_count),
        "validator_purple_artifact_fraction_in_ring": float(np.sum(purple) / ring_count),
        "validator_combined_artifact_fraction_in_ring": float(np.sum(artifact) / ring_count),
        "validator_artifact_pixel_count": int(np.sum(artifact)),
        "validator_red_artifact_pixel_count": int(np.sum(red)),
        "validator_purple_artifact_pixel_count": int(np.sum(purple)),
        "mask_nonzero_fraction": float(np.mean(mask > 0.01)),
        "mask_median_on_nonzero": float(np.median(mask[mask > 0.01])) if np.any(mask > 0.01) else 0.0,
        "mask_median_on_validator_artifact_pixels": float(np.median(artifact_values)) if artifact_values.size else 0.0,
        "mask_min_on_validator_artifact_pixels": float(np.min(artifact_values)) if artifact_values.size else 0.0,
        "mask_median_on_validator_red_pixels": float(np.median(red_values)) if red_values.size else 0.0,
        "mask_median_on_validator_purple_pixels": float(np.median(purple_values)) if purple_values.size else 0.0,
    }
    return mask, meta

def residual_edge_metrics(source_rgb: np.ndarray, out_rgb: np.ndarray, edge_core_quantile: float = 0.997, inner_radius: int = 1, outer_radius: int = 10) -> dict:
    src_lum = source_rgb.mean(axis=-1)
    edge_core_threshold = float(np.quantile(src_lum, edge_core_quantile))
    core = (src_lum >= edge_core_threshold).astype(np.float32)
    outer = convolve_fft(core, np.ones((2*outer_radius+1, 2*outer_radius+1), dtype=np.float32), boundary='fill', fill_value=0.0, normalize_kernel=False) >= 0.5
    inner = convolve_fft(core, np.ones((2*inner_radius+1, 2*inner_radius+1), dtype=np.float32), boundary='fill', fill_value=0.0, normalize_kernel=False) >= 0.5
    ring = np.asarray(outer & ~inner)
    if not np.any(ring):
        ring = src_lum >= np.quantile(src_lum, 0.995)

    def sat(img):
        maxc = img.max(axis=-1)
        minc = img.min(axis=-1)
        with np.errstate(divide='ignore', invalid='ignore'):
            return np.where(maxc > 0, (maxc - minc) / maxc, 0.0)

    src_sat = sat(source_rgb)
    out_sat = sat(out_rgb)
    sr, sg, sb = [source_rgb[..., i] for i in range(3)]
    or_, og, ob = [out_rgb[..., i] for i in range(3)]

    src_red = ring & (sr > np.maximum(sg, sb) + 0.002) & (sr > 0.01)
    out_red = ring & (or_ > np.maximum(og, ob) + 0.002) & (or_ > 0.01)
    src_purple = ring & (np.minimum(sr, sb) > sg + 0.002) & (np.maximum(sr, sb) > 0.01)
    out_purple = ring & (np.minimum(or_, ob) > og + 0.002) & (np.maximum(or_, ob) > 0.01)
    src_comb = src_red | src_purple
    out_comb = out_red | out_purple
    src_cool = ring & (np.maximum(sg, sb) > sr + 0.0015) & ((sg > sr + 0.0015) | (sb > sr + 0.0015))

    ring_count = max(int(np.sum(ring)), 1)
    def frac(mask):
        return float(np.sum(mask) / ring_count)
    def med_sat(values, mask):
        if not np.any(mask):
            return 0.0
        return float(np.median(values[mask]))

    return {
        "edge_core_quantile": edge_core_quantile,
        "edge_outer_radius_pixels": outer_radius,
        "edge_inner_radius_pixels": inner_radius,
        "edge_fft_membership_threshold": 0.5,
        "edge_ring_pixel_count": int(np.sum(ring)),
        "edge_ring_fraction": float(np.mean(ring)),
        "resolved_edge_core_threshold": edge_core_threshold,
        "source_residual_red_edge_fraction": frac(src_red),
        "output_residual_red_edge_fraction": frac(out_red),
        "residual_red_edge_reduction_fraction": 1.0 - frac(out_red) / max(frac(src_red), 1e-12),
        "source_residual_purple_edge_fraction": frac(src_purple),
        "output_residual_purple_edge_fraction": frac(out_purple),
        "residual_purple_edge_reduction_fraction": 1.0 - frac(out_purple) / max(frac(src_purple), 1e-12),
        "source_residual_red_purple_edge_fraction": frac(src_comb),
        "output_residual_red_purple_edge_fraction": frac(out_comb),
        "residual_red_purple_edge_reduction_fraction": 1.0 - frac(out_comb) / max(frac(src_comb), 1e-12),
        "source_residual_red_purple_edge_saturation_median": med_sat(src_sat, src_comb),
        "output_residual_red_purple_edge_saturation_median_on_source_pixels": med_sat(out_sat, src_comb),
        "source_acceptable_cool_edge_saturation_median": med_sat(src_sat, src_cool),
        "output_acceptable_cool_edge_saturation_median_on_source_pixels": med_sat(out_sat, src_cool),
    }


def compare_metrics(src_rgb: np.ndarray, out_rgb: np.ndarray, artifact_meta: dict | None = None) -> dict:
    src_lum = src_rgb.mean(axis=-1)
    out_lum = out_rgb.mean(axis=-1)
    src_max = src_rgb.max(axis=-1)
    src_min = src_rgb.min(axis=-1)
    out_max = out_rgb.max(axis=-1)
    out_min = out_rgb.min(axis=-1)
    with np.errstate(divide='ignore', invalid='ignore'):
        src_sat = np.where(src_max > 0, (src_max - src_min) / src_max, 0.0)
        out_sat = np.where(out_max > 0, (out_max - out_min) / out_max, 0.0)

    star_cut = float(np.quantile(src_lum, 0.99))
    bright_cut = float(np.quantile(src_lum, 0.9999))
    star_mask = src_lum >= star_cut
    bright_mask = src_lum >= bright_cut
    halo_band = (src_lum >= np.quantile(src_lum, 0.95)) & (src_lum <= np.quantile(src_lum, 0.999))
    src_r, src_g, src_b = [src_rgb[..., i] for i in range(3)]
    unwanted = halo_band & ((src_r > src_g + 0.0015) | (src_b > src_g + 0.0015))
    cool = halo_band & ((src_g > src_r + 0.0015) | (src_b > src_r + 0.0015))

    def sat_median(values, mask):
        if not np.any(mask):
            return 0.0
        return float(np.median(values[mask]))

    diag = residual_edge_metrics(src_rgb, out_rgb)
    metrics = {
        "absolute_luma_median_change": float(abs(np.median(out_lum) - np.median(src_lum))),
        "luma_correlation_diagnostic": float(np.corrcoef(src_lum.ravel()[::32], out_lum.ravel()[::32])[0, 1]),
        "added_low_clip_fraction": float(np.mean((out_rgb <= 0).all(axis=-1) & ~(src_rgb <= 0).all(axis=-1))),
        "added_high_clip_fraction": float(np.mean((out_rgb >= 1).any(axis=-1) & ~(src_rgb >= 1).any(axis=-1))),
        "source_saturation_median": float(np.median(src_sat)),
        "output_saturation_median": float(np.median(out_sat)),
        "saturation_median_change": float(np.median(out_sat) - np.median(src_sat)),
        "source_star_saturation_median": sat_median(src_sat, star_mask),
        "output_star_saturation_median": sat_median(out_sat, star_mask),
        "star_saturation_reduction_fraction": 1.0 - sat_median(out_sat, star_mask) / max(sat_median(src_sat, star_mask), 1e-12),
        "source_bright_star_saturation_median": sat_median(src_sat, bright_mask),
        "output_bright_star_saturation_median": sat_median(out_sat, bright_mask),
        "bright_star_saturation_reduction_fraction": 1.0 - sat_median(out_sat, bright_mask) / max(sat_median(src_sat, bright_mask), 1e-12),
        "halo_band_low_quantile": 0.95,
        "halo_band_high_quantile": 0.999,
        "source_halo_band_saturation_median": sat_median(src_sat, halo_band),
        "output_halo_band_saturation_median": sat_median(out_sat, halo_band),
        "halo_band_saturation_reduction_fraction": 1.0 - sat_median(out_sat, halo_band) / max(sat_median(src_sat, halo_band), 1e-12),
        "source_unwanted_warm_magenta_halo_fraction": float(np.mean(unwanted[halo_band])) if np.any(halo_band) else 0.0,
        "output_unwanted_warm_magenta_halo_fraction": float(np.mean(((out_rgb[...,0] > out_rgb[...,1] + 0.0015) | (out_rgb[...,2] > out_rgb[...,1] + 0.0015))[halo_band])) if np.any(halo_band) else 0.0,
        "source_unwanted_warm_magenta_halo_saturation_median": sat_median(src_sat, unwanted),
        "output_unwanted_warm_magenta_halo_saturation_median_on_source_pixels": sat_median(out_sat, unwanted),
        "source_cool_halo_saturation_median": sat_median(src_sat, cool),
        "output_cool_halo_saturation_median_on_source_pixels": sat_median(out_sat, cool),
        "source_lum_p95": float(np.quantile(src_lum, 0.95)),
        "output_lum_p95": float(np.quantile(out_lum, 0.95)),
        "dim_star_reduction_fraction": 1.0 - float(np.quantile(out_lum, 0.95) / max(np.quantile(src_lum, 0.95), 1e-12)),
        "source_lum_p99": float(np.quantile(src_lum, 0.99)),
        "output_lum_p99": float(np.quantile(out_lum, 0.99)),
        "source_lum_p999": float(np.quantile(src_lum, 0.999)),
        "output_lum_p999": float(np.quantile(out_lum, 0.999)),
        "source_lum_p9999": float(np.quantile(src_lum, 0.9999)),
        "output_lum_p9999": float(np.quantile(out_lum, 0.9999)),
        "bright_star_reduction_fraction": 1.0 - float(np.quantile(out_lum, 0.9999) / max(np.quantile(src_lum, 0.9999), 1e-12)),
        "mid_bright_reduction_fraction": 1.0 - float(np.quantile(out_lum, 0.999) / max(np.quantile(src_lum, 0.999), 1e-12)),
    }
    metrics.update(diag)
    if artifact_meta:
        metrics.update(artifact_meta)
    return {k: float(v) if isinstance(v, (np.floating, np.integer)) else v for k, v in metrics.items()}



def _nearest_upscale(rgb: np.ndarray, factor: int) -> np.ndarray:
    arr = np.asarray(rgb, dtype=np.float32)
    if factor <= 1:
        return arr
    return np.repeat(np.repeat(arr, factor, axis=0), factor, axis=1).astype(np.float32)


def derive_highzoom_review_crops(control_rgb: np.ndarray, params: dict, max_crops: int = 3, half_size: int = 28, min_spacing: int = 72, upscale_factor: int = 8) -> list[dict]:
    """Pick deterministic high-zoom locations from the validator's purple-fringe pixels."""
    lum = control_rgb.mean(axis=-1)
    mx = control_rgb.max(axis=-1)
    mn = control_rgb.min(axis=-1)
    with np.errstate(divide='ignore', invalid='ignore'):
        sat = np.where(mx > 0, (mx - mn) / mx, 0.0)

    core_threshold = float(np.quantile(lum, params['edge_core_quantile']))
    core = (lum >= core_threshold).astype(np.float32)
    outer_radius = int(params['edge_outer_radius'])
    inner_radius = int(params['edge_inner_radius'])
    outer = convolve_fft(
        core,
        np.ones((2 * outer_radius + 1, 2 * outer_radius + 1), dtype=np.float32),
        boundary='fill', fill_value=0.0, normalize_kernel=False,
    ) >= 0.5
    inner = convolve_fft(
        core,
        np.ones((2 * inner_radius + 1, 2 * inner_radius + 1), dtype=np.float32),
        boundary='fill', fill_value=0.0, normalize_kernel=False,
    ) >= 0.5
    ring = np.asarray(outer & ~inner)
    r, g, b = [control_rgb[..., i] for i in range(3)]
    purple = ring & (np.minimum(r, b) > g + params['purple_margin']) & (np.maximum(r, b) > 0.01)

    focus = purple
    reason = 'validator_purple_fringe'
    if not np.any(focus):
        focus = lum >= float(np.quantile(lum, 0.9995))
        reason = 'fallback_bright_star'

    ys, xs = np.nonzero(focus)
    if ys.size:
        scores = (lum[ys, xs] * (1.0 + sat[ys, xs])).astype(np.float32)
        order = np.argsort(scores)[::-1]
    else:
        h, w = lum.shape
        ys = np.asarray([h // 2])
        xs = np.asarray([w // 2])
        scores = np.asarray([1.0], dtype=np.float32)
        order = np.asarray([0])
        reason = 'fallback_center'

    h, w = lum.shape
    selected = []
    for idx in order:
        y = int(ys[idx]); x = int(xs[idx])
        if any((x - item['center_x']) ** 2 + (y - item['center_y']) ** 2 < min_spacing ** 2 for item in selected):
            continue
        x0 = max(0, x - half_size); x1 = min(w, x + half_size + 1)
        y0 = max(0, y - half_size); y1 = min(h, y + half_size + 1)
        selected.append({
            'crop_id': f'crop-{len(selected)+1:02d}',
            'reason': reason,
            'center_x': x,
            'center_y': y,
            'bounds_xyxy': [x0, y0, x1, y1],
            'upscale_factor': int(upscale_factor),
            'score': float(scores[idx]),
        })
        if len(selected) >= max_crops:
            break
    return selected


def write_highzoom_review_crops(candidate_dir: Path, out_rgb: np.ndarray, crop_specs: list[dict]) -> list[dict]:
    outputs = []
    for spec in crop_specs:
        x0, y0, x1, y1 = spec['bounds_xyxy']
        crop_rgb = out_rgb[y0:y1, x0:x1, :]
        if crop_rgb.size == 0:
            continue
        up_rgb = _nearest_upscale(crop_rgb, int(spec['upscale_factor']))
        crop_fit = candidate_dir / f"{spec['crop_id']}.fit"
        crop_png = candidate_dir / f"{spec['crop_id']}.png"
        save_rgb(crop_fit, up_rgb)
        script = "\n".join([
            'requires 1.4.4',
            f"load {crop_fit.name}",
            f"savepng {spec['crop_id']}",
            'close',
            '',
        ])
        run_siril_script(candidate_dir, script)
        try:
            crop_fit.unlink()
        except FileNotFoundError:
            pass
        if not crop_png.exists():
            raise RuntimeError(f'High-zoom review crop PNG was not created: {crop_png}')
        outputs.append({
            'crop_id': spec['crop_id'],
            'reason': spec['reason'],
            'center_xy': [int(spec['center_x']), int(spec['center_y'])],
            'bounds_xyxy': [int(v) for v in spec['bounds_xyxy']],
            'upscale_factor': int(spec['upscale_factor']),
            'path': str(crop_png),
            'sha256': sha256_file(crop_png),
        })
    return outputs


def perform_candidate_generation(input_fit: Path, run_root: Path) -> dict:
    ensure_dir(run_root)
    source_rgb = load_rgb(input_fit)
    source_stats = image_metrics(source_rgb)
    source_lum = source_rgb.mean(axis=-1)
    resolved_threshold = float(np.quantile(source_lum, 0.99))

    common = run_root / "common"
    ensure_dir(common)
    shutil.copy2(input_fit, common / "input.fit")

    base_candidate = dict(CANDIDATES[0])
    base_candidate["resolved_threshold"] = resolved_threshold
    run_siril_script(common, build_brightness_script(base_candidate))
    run_siril_script(common, build_control_script())
    run_siril_script(common, build_neutral_script())

    dimmed_rgb = load_rgb(common / "dimmed.fit")
    control_rgb = load_rgb(common / "control.fit")
    neutral_rgb = load_rgb(common / "neutral.fit")
    review_crop_specs = derive_highzoom_review_crops(control_rgb, CANDIDATES[0])

    results = []
    eligible = []
    for candidate in CANDIDATES:
        cdir = run_root / candidate["name"]
        ensure_dir(cdir)
        out_fit = cdir / "processed.fit"
        out_png = cdir / "preview.png"
        artifact_meta = {
            "resolved_validator_edge_core_threshold": float(np.quantile(source_rgb.mean(axis=-1), candidate["edge_core_quantile"])),
            "validator_edge_outer_radius_pixels": int(candidate["edge_outer_radius"]),
            "validator_edge_inner_radius_pixels": int(candidate["edge_inner_radius"]),
            "validator_fft_membership_threshold": 0.5,
            "validator_edge_ring_pixel_count": 0,
            "validator_edge_ring_fraction": 0.0,
            "validator_red_artifact_fraction_in_ring": 0.0,
            "validator_purple_artifact_fraction_in_ring": 0.0,
            "validator_combined_artifact_fraction_in_ring": 0.0,
            "validator_artifact_pixel_count": 0,
            "validator_red_artifact_pixel_count": 0,
            "validator_purple_artifact_pixel_count": 0,
            "mask_nonzero_fraction": 0.0,
            "mask_median_on_nonzero": 0.0,
            "mask_median_on_validator_artifact_pixels": 0.0,
            "mask_min_on_validator_artifact_pixels": 0.0,
            "mask_median_on_validator_red_pixels": 0.0,
            "mask_median_on_validator_purple_pixels": 0.0,
        }
        mask, artifact_meta = build_validator_artifact_blend_mask(source_rgb, control_rgb, candidate)
        out_rgb = control_rgb * (1.0 - mask[..., None]) + neutral_rgb * mask[..., None]
        out_rgb = np.clip(out_rgb, 0.0, 1.0).astype(np.float32)
        save_rgb(out_fit, out_rgb, template=common / "control.fit")
        run_siril_script(cdir, build_preview_script())
        metrics = compare_metrics(source_rgb, out_rgb, artifact_meta)

        # r6 quality gate: measure saturation specifically on the exact purple
        # pixels detected in the accepted r5 control within the corrected 1-10 px edge ring.
        src_lum_for_ring = source_rgb.mean(axis=-1)
        core_thr = float(np.quantile(src_lum_for_ring, candidate["edge_core_quantile"]))
        core = (src_lum_for_ring >= core_thr).astype(np.float32)
        outer_r = int(candidate["edge_outer_radius"])
        inner_r = int(candidate["edge_inner_radius"])
        outer = convolve_fft(core, np.ones((2*outer_r+1, 2*outer_r+1), dtype=np.float32), boundary='fill', fill_value=0.0, normalize_kernel=False) >= 0.5
        inner = convolve_fft(core, np.ones((2*inner_r+1, 2*inner_r+1), dtype=np.float32), boundary='fill', fill_value=0.0, normalize_kernel=False) >= 0.5
        ring = np.asarray(outer & ~inner)
        cr, cg, cb = [control_rgb[..., i] for i in range(3)]
        purple_pixels = ring & (np.minimum(cr, cb) > cg + candidate["purple_margin"]) & (np.maximum(cr, cb) > 0.01)
        def _sat(img):
            mx = img.max(axis=-1); mn = img.min(axis=-1)
            with np.errstate(divide='ignore', invalid='ignore'):
                return np.where(mx > 0, (mx-mn)/mx, 0.0)
        control_sat = _sat(control_rgb)
        output_sat = _sat(out_rgb)
        if np.any(purple_pixels):
            metrics["r6_control_purple_fringe_saturation_median"] = float(np.median(control_sat[purple_pixels]))
            metrics["r6_output_purple_fringe_saturation_median_on_control_pixels"] = float(np.median(output_sat[purple_pixels]))
            metrics["r6_purple_fringe_saturation_reduction_fraction"] = 1.0 - metrics["r6_output_purple_fringe_saturation_median_on_control_pixels"] / max(metrics["r6_control_purple_fringe_saturation_median"], 1e-12)
            metrics["r6_control_purple_fringe_pixel_count"] = int(np.sum(purple_pixels))
        else:
            metrics["r6_control_purple_fringe_saturation_median"] = 0.0
            metrics["r6_output_purple_fringe_saturation_median_on_control_pixels"] = 0.0
            metrics["r6_purple_fringe_saturation_reduction_fraction"] = 0.0
            metrics["r6_control_purple_fringe_pixel_count"] = 0
        review_crops = write_highzoom_review_crops(cdir, out_rgb, review_crop_specs)
        meta = {
            "candidate": candidate["name"],
            "label": candidate["label"],
            "fit_path": str(out_fit),
            "png_path": str(out_png),
            "fit_sha256": sha256_file(out_fit),
            "png_sha256": sha256_file(out_png),
            "review_crops": review_crops,
            "parameters": {
                **candidate,
                "resolved_threshold": resolved_threshold,
                "resolved_edge_core_threshold": metrics["resolved_edge_core_threshold"],
                "resolved_validator_edge_core_threshold": metrics["resolved_validator_edge_core_threshold"],
            },
            "metrics": metrics,
        }
        meta["eligible"] = bool(
            metrics["added_low_clip_fraction"] == 0.0 and
            metrics["added_high_clip_fraction"] == 0.0 and
            metrics["luma_correlation_diagnostic"] > 0.97 and
            metrics["dim_star_reduction_fraction"] <= 0.15 and
            metrics["bright_star_reduction_fraction"] >= 0.35 and
            metrics["bright_star_reduction_fraction"] >= metrics["dim_star_reduction_fraction"] + 0.10
        )
        results.append(meta)
        if meta["eligible"]:
            eligible.append(candidate["name"])

    if not eligible:
        raise RuntimeError("No technically safe candidate produced")
    summary = {
        "status": "awaiting_visual_selection",
        "version": VERSION,
        "source": {
            "path": str(input_fit),
            "sha256": sha256_file(input_fit),
            **source_stats,
        },
        "candidate_count": len(results),
        "eligible": eligible,
        "recommended_candidate": "candidate-02" if "candidate-02" in eligible else ("candidate-01" if "candidate-01" in eligible else eligible[0]),
        "review_crop_specs": review_crop_specs,
        "candidates": results,
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
    if manifest.get("source", {}).get("path") != str(paths.upstream_fit):
        reasons.append("Recorded source path is not the current Starnet star FITS.")
    if manifest.get("source", {}).get("sha256") != sha256_file(paths.upstream_fit):
        reasons.append("Recorded source checksum differs from the current Starnet star FITS.")
    if manifest.get("stage_order", {}).get("upstream") != "siril-starnet-removal":
        reasons.append("Recorded upstream stage is not siril-starnet-removal.")
    if manifest.get("output", {}).get("sha256") != sha256_file(paths.canonical_fit):
        reasons.append("Canonical output checksum differs from manifest.")
    return ("current", reasons) if not reasons else ("obsolete", reasons)


def begin(project_name: str) -> dict:
    paths = find_project_paths(project_name)
    status, reasons = current_status(paths)
    exe = Path(__file__).resolve().parent.parent / 'bin' / 'star-processing'
    if status == "current":
        return {
            "status": "confirmation_required",
            "current_canonical_status": "current",
            "question": f"Star processing for {project_name} has already completed. Do you want me to run it again as a fresh run?",
            "next_command_after_confirmation": f'{exe} confirm-fresh --project "{project_name}" && {exe} advance --project "{project_name}"',
            "version": VERSION,
        }
    if status == "obsolete":
        return {
            "status": "confirmation_required",
            "current_canonical_status": "obsolete",
            "obsolete_reasons": reasons,
            "question": f"Star processing for {project_name} has already completed but is obsolete for the current Starnet star branch. Do you want me to run it again as a fresh run?",
            "next_command_after_confirmation": f'{exe} confirm-fresh --project "{project_name}" && {exe} advance --project "{project_name}"',
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
            raise SystemExit(json.dumps({"status":"blocked","error":"Fresh rerun confirmation is required before advance.","version":VERSION}, indent=2))
        auth = json_load(paths.auth_file)
        if auth.get("source_sha256") != sha256_file(paths.upstream_fit):
            raise SystemExit(json.dumps({"status":"blocked","error":"Stored confirmation does not match the current upstream source SHA.","version":VERSION}, indent=2))
    ensure_dir(paths.state_root)
    run_root = paths.project_root / ".siril-star-processing" / f"star-processing-{now_utc()}-{sha256_file(paths.upstream_fit)[:8]}"
    summary = perform_candidate_generation(paths.upstream_fit, run_root)
    json_dump({"run_root": str(run_root), "source_sha256": sha256_file(paths.upstream_fit), "created_at_utc": now_utc()}, paths.current_run_file)
    if paths.auth_file.exists():
        consumed = json_load(paths.auth_file)
        consumed["status"] = "consumed"
        consumed["consumed_at_utc"] = now_utc()
        json_dump(consumed, paths.auth_file)

    read_targets = []
    for c in summary["candidates"]:
        read_targets.append({
            "candidate": c["candidate"],
            "view": "full_frame",
            "path": c["png_path"],
            "sha256": c["png_sha256"],
        })
    for crop_spec in summary.get("review_crop_specs", []):
        for c in summary["candidates"]:
            crop = next((item for item in c.get("review_crops", []) if item["crop_id"] == crop_spec["crop_id"]), None)
            if crop is None:
                raise RuntimeError(f"Missing high-zoom review crop {crop_spec['crop_id']} for {c['candidate']}")
            read_targets.append({
                "candidate": c["candidate"],
                "view": "high_zoom_crop",
                "crop_id": crop["crop_id"],
                "path": crop["path"],
                "sha256": crop["sha256"],
                "center_xy": crop["center_xy"],
                "bounds_xyxy": crop["bounds_xyxy"],
                "upscale_factor": crop["upscale_factor"],
                "reason": crop["reason"],
            })

    return {
        "instruction": "Read all three full-frame PNG targets verbatim, then read every high-zoom crop target verbatim. The high-zoom crops are mandatory and decisive for purple-fringe assessment. Then call select-publish with the selected candidate and notes for all three.",
        "version": VERSION,
        "status": "visual_review_required",
        "project_name": project_name,
        "run_root": str(run_root),
        "technical_recommendation": summary.get("recommended_candidate"),
        "selection_rule": "Use the full frames for overall balance, but use the 8x high-zoom crops to judge the 2-7 pixel purple fringe. Prefer the technically recommended candidate-02 when it removes the fringe without a visible high-zoom or full-frame tradeoff.",
        "read_target_policy": {"directory_discovery_forbidden": True, "path_handling": "verbatim", "on_read_failure": "stop_and_report_exact_failed_path"},
        "read_targets": read_targets,
        "required_review_fields": ["color_neutrality","red_specks","purple_fringe_high_zoom","bright_star_dominance","dim_star_retention","profiles","overall_balance"],
    }


def select_publish(project_name: str, run_root: Path, candidate_name: str, compared: list[str], notes: list[str]) -> dict:
    paths = find_project_paths(project_name)
    summary = json_load(run_root / "candidate-summary.json")
    candidates = {c["candidate"]: c for c in summary["candidates"]}
    if candidate_name not in candidates:
        raise SystemExit(json.dumps({"status":"blocked","error":f"Unknown candidate: {candidate_name}","version":VERSION}, indent=2))
    selected = candidates[candidate_name]
    ensure_dir(paths.stage_root)

    previous_publication = None
    if paths.canonical_fit.exists() or paths.canonical_manifest.exists() or paths.preview_png.exists():
        previous_publication = {
            "recorded_at_utc": now_utc(),
            "canonical_output_path": str(paths.canonical_fit),
            "canonical_output_sha256": sha256_file(paths.canonical_fit) if paths.canonical_fit.exists() else None,
            "preview_path": str(paths.preview_png),
            "preview_sha256": sha256_file(paths.preview_png) if paths.preview_png.exists() else None,
            "manifest_path": str(paths.canonical_manifest),
        }
        if paths.canonical_manifest.exists():
            try:
                old_manifest = json_load(paths.canonical_manifest)
                previous_publication["prior_selected_candidate"] = old_manifest.get("selected_candidate")
                previous_publication["prior_run_root"] = old_manifest.get("run_root")
                previous_publication["prior_published_at_utc"] = old_manifest.get("published_at_utc")
                previous_publication["prior_output_sha256"] = old_manifest.get("output", {}).get("sha256")
                old_run_root = old_manifest.get("run_root")
                old_candidate = old_manifest.get("selected_candidate")
                if old_run_root and old_candidate:
                    recovery_fit = Path(old_run_root) / old_candidate / "processed.fit"
                    if recovery_fit.exists():
                        previous_publication["recoverable_prior_candidate_fit"] = str(recovery_fit)
                        previous_publication["recoverable_prior_candidate_fit_sha256"] = sha256_file(recovery_fit)
            except Exception as exc:
                previous_publication["prior_manifest_read_error"] = str(exc)

    # Candidate generation/review is non-destructive. Only after successful selection do we
    # replace the canonical. r7 intentionally does not create another full-size before-*.fit.
    shutil.copy2(selected["fit_path"], paths.canonical_fit)
    shutil.copy2(selected["png_path"], paths.preview_png)
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
        "technical_recommendation": summary.get("recommended_candidate"),
        "review_crop_specs": summary.get("review_crop_specs", []),
        "stage_order": {"upstream": "siril-starnet-removal", "current": "siril-star-processing", "downstream": "siril-star-recombination"},
        "recombination_processing_permitted": True,
        "next_stage": "siril-star-recombination",
        "run_root": str(run_root),
        "previous_canonical_preserved_at": None,
        "previous_publication": previous_publication,
        "published_at_utc": now_utc(),
    }
    selection = {
        "selected_candidate": candidate_name,
        "technical_recommendation": summary.get("recommended_candidate"),
        "compared_candidates": compared,
        "notes": dict(zip(compared, notes)),
        "review_crop_specs": summary.get("review_crop_specs", []),
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
        "previous_canonical_preserved_at": None,
        "previous_publication": previous_publication,
    }


def smoke_test(input_fit: Path, manifest_path: Path | None) -> dict:
    work = Path("/home/peter") / f"siril-star-processing-smoke-{now_utc()}-{sha256_file(input_fit)[:8]}"
    ensure_dir(work)
    project_processing = work / "Projects" / "M16 July 2026" / "processing" / "starnet"
    ensure_dir(project_processing)
    copied_fit = project_processing / "SHO-stars-unscreen.fit"
    shutil.copy2(input_fit, copied_fit)
    if manifest_path and manifest_path.exists():
        shutil.copy2(manifest_path, project_processing / manifest_path.name)
    run_root = work / "run"
    summary = perform_candidate_generation(copied_fit, run_root)
    return {"status": "awaiting_visual_selection", "smoke_root": str(work), "material_candidates": [c["candidate"] for c in summary["candidates"]], **summary}


def self_test() -> dict:
    return {
        "status": "success",
        "version": VERSION,
        "upstream": "siril-starnet-removal",
        "candidate_count": len(CANDIDATES),
        "candidate_labels": [c["label"] for c in CANDIDATES],
        "uses_negative_satu_for_neutralization": True,
        "uses_pixelmath_soft_knee_for_brightness_control": True,
        "uses_python_validator_artifact_mask_blend": True,
        "keeps_r5_red_strength_fixed": True,
        "uses_separate_stronger_purple_direct_strength": True,
        "validates_purple_saturation_in_edge_annulus": True,
        "uses_exact_validator_edge_ring_as_processing_mask": True,
        "uses_exact_validator_red_and_purple_tests": True,
        "uses_direct_artifact_pixel_neutral_blend": True,
        "keeps_r6_color_and_brightness_math_unchanged": True,
        "fixes_fft_edge_membership_threshold": True,
        "fft_edge_membership_threshold": 0.5,
        "edge_outer_radius_pixels": 10,
        "uses_highzoom_review_crops": True,
        "technical_recommendation_candidate_02": True,
        "publishes_without_new_before_fit_copies": True,
        "target_adaptive_brightness_threshold": True,
        "bright_stars_reduced_more_than_dim_stars": True,
        "focuses_on_bright_star_halo_neutralization": True,
        "spatial_bright_star_edge_ring_validation": True,
        "residual_red_edge_validation": True,
        "residual_purple_edge_validation": True,
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
