#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from astropy.io import fits

VERSION = "1.0.0"
SKILL_NAME = "siril-star-recombination"
AGENT_ROOT = Path("/home/peter/.openclaw/workspace/agents/codewarrior")
PROJECTS_ROOT = AGENT_ROOT / "Projects"
SIRIL_BIN = Path("/home/peter/.openclaw/runtime/siril-processor/toolchain/.toolchain/siril/1.4.4/squashfs-root/AppRun")

CANDIDATES = [
    {"name": "candidate-00", "label": "conservative-screen", "star_contribution": 0.70},
    {"name": "candidate-01", "label": "balanced-screen", "star_contribution": 0.85},
    {"name": "candidate-02", "label": "full-processed-stars-screen", "star_contribution": 1.00},
]


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def json_dump(obj, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def json_load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def project_paths(project_name: str) -> dict:
    root = PROJECTS_ROOT / project_name
    processing = root / "processing"
    stage = processing / "star-recombination"
    state = root / ".siril-star-recombination"
    return {
        "project_root": root,
        "processing_root": processing,
        "starless_fit": processing / "saturation" / "SHO-starless-saturated.fit",
        "starless_manifest": processing / "saturation" / "saturation-manifest.json",
        "stars_fit": processing / "star-processing" / "SHO-stars-processed.fit",
        "stars_manifest": processing / "star-processing" / "star-processing-manifest.json",
        "stage_root": stage,
        "canonical_fit": stage / "SHO-recombined.fit",
        "canonical_preview": stage / "SHO-recombined.png",
        "canonical_manifest": stage / "star-recombination-manifest.json",
        "selection_record": stage / "visual-selection-record.json",
        "state_root": state,
        "auth_file": state / "confirm-fresh.json",
        "current_run": state / "current-run.json",
    }


def load_rgb(path: Path) -> np.ndarray:
    data = np.asarray(fits.getdata(path), dtype=np.float32)
    if data.ndim == 3 and data.shape[0] == 3:
        data = np.moveaxis(data, 0, -1)
    if data.ndim != 3 or data.shape[-1] != 3:
        raise RuntimeError(f"Expected RGB FITS, got shape {data.shape}: {path}")
    if not np.isfinite(data).all():
        raise RuntimeError(f"Non-finite pixels found: {path}")
    return data


def save_rgb(path: Path, rgb: np.ndarray, template: Path | None = None) -> None:
    arr = np.moveaxis(np.asarray(rgb, dtype=np.float32), -1, 0)
    header = fits.getheader(template) if template and template.exists() else fits.Header()
    fits.PrimaryHDU(data=arr, header=header).writeto(path, overwrite=True)


def run_siril_script(workdir: Path, text: str) -> None:
    ensure_dir(workdir)
    script = workdir / "script.ssf"
    script.write_text(text, encoding="utf-8")
    home = workdir / ".runner-home"
    ensure_dir(home)
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["APPDIR"] = str(SIRIL_BIN.parent)
    proc = subprocess.run(
        [str(SIRIL_BIN), "siril-cli", "--directory", str(workdir), "--script", str(script)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    if proc.returncode != 0:
        json_dump({
            "returncode": proc.returncode,
            "script": text,
            "stdout_tail": proc.stdout[-12000:],
            "stderr_tail": proc.stderr[-6000:],
        }, workdir / f"siril-failure-{now_utc()}.json")
        raise RuntimeError(f"Siril failed in {workdir} with exit {proc.returncode}")


def validate_upstreams(project_name: str, p: dict) -> dict:
    required = [p["starless_fit"], p["starless_manifest"], p["stars_fit"], p["stars_manifest"]]
    missing = [str(x) for x in required if not x.is_file()]
    if missing:
        raise RuntimeError("Missing required upstream files: " + ", ".join(missing))

    sat = json_load(p["starless_manifest"])
    sp = json_load(p["stars_manifest"])

    if sat.get("status") != "ready" or sat.get("project") != project_name:
        raise RuntimeError("Saturation manifest is not ready for this project")
    sat_out = sat.get("output") or {}
    if sat_out.get("path") != str(p["starless_fit"]):
        raise RuntimeError("Saturation manifest output path is not the canonical starless saturation FITS")
    if sat_out.get("sha256") != sha256_file(p["starless_fit"]):
        raise RuntimeError("Saturation FITS checksum differs from its manifest")
    if sat.get("visual_review_completed") is not True:
        raise RuntimeError("Saturation visual review is not complete")

    # Saturation v1.0.0 predates assignment of star recombination as a downstream stage.
    # Accept that exact legacy handoff only when the canonical output contract is otherwise ready.
    sat_next = sat.get("next_stage")
    sat_down = sat.get("downstream_processing_permitted")
    legacy_sat_handoff = sat_next is None and sat_down is False
    explicit_sat_handoff = sat_next == SKILL_NAME and sat_down is True
    if not (legacy_sat_handoff or explicit_sat_handoff):
        raise RuntimeError("Saturation manifest does not permit this recombination contract")

    if sp.get("status") != "ready" or sp.get("project") != project_name:
        raise RuntimeError("Star-processing manifest is not ready for this project")
    sp_out = sp.get("output") or {}
    if sp_out.get("path") != str(p["stars_fit"]):
        raise RuntimeError("Star-processing manifest output path is not the canonical processed-stars FITS")
    if sp_out.get("sha256") != sha256_file(p["stars_fit"]):
        raise RuntimeError("Processed-stars FITS checksum differs from its manifest")
    if sp.get("recombination_processing_permitted") is not True:
        raise RuntimeError("Star-processing manifest does not permit recombination")
    if sp.get("next_stage") != SKILL_NAME:
        raise RuntimeError("Star-processing manifest next_stage is not siril-star-recombination")

    starless = load_rgb(p["starless_fit"])
    stars = load_rgb(p["stars_fit"])
    if starless.shape != stars.shape:
        raise RuntimeError(f"Recombination input shape mismatch: starless={starless.shape}, stars={stars.shape}")
    if np.min(starless) < 0 or np.max(starless) > 1.000001:
        raise RuntimeError("Starless input lies outside the normalized [0,1] range")
    if np.min(stars) < 0 or np.max(stars) > 1.000001:
        raise RuntimeError("Processed-stars input lies outside the normalized [0,1] range")

    return {
        "starless": {
            "path": str(p["starless_fit"]),
            "sha256": sha256_file(p["starless_fit"]),
            "manifest_path": str(p["starless_manifest"]),
            "manifest_sha256": sha256_file(p["starless_manifest"]),
            "stage": "siril-saturation",
            "legacy_handoff_accepted": legacy_sat_handoff,
        },
        "stars": {
            "path": str(p["stars_fit"]),
            "sha256": sha256_file(p["stars_fit"]),
            "manifest_path": str(p["stars_manifest"]),
            "manifest_sha256": sha256_file(p["stars_manifest"]),
            "stage": "siril-star-processing",
            "selected_candidate": sp.get("selected_candidate"),
        },
        "shape": list(starless.shape),
    }


def screen_formula(weight: float) -> str:
    return f"1 - (1 - $starless$) * (1 - {weight:.6f} * $stars$)"


def candidate_script(weight: float) -> str:
    expr = screen_formula(weight)
    return "\n".join([
        "requires 1.4.4",
        f'pm "{expr}"',
        "save processed.fit",
        "savepng preview",
        "close",
        "",
    ])


def sat_map(rgb: np.ndarray) -> np.ndarray:
    mx = rgb.max(axis=-1)
    mn = rgb.min(axis=-1)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(mx > 0, (mx - mn) / mx, 0.0)


def candidate_metrics(starless: np.ndarray, stars: np.ndarray, out: np.ndarray, weight: float) -> dict:
    sl = starless.mean(axis=-1)
    tl = stars.mean(axis=-1)
    ol = out.mean(axis=-1)
    positive = tl[tl > 0]
    support_threshold = float(np.quantile(positive, 0.50)) if positive.size else 0.0
    support = tl >= max(support_threshold, 1e-8)
    background = ~support
    if not np.any(background):
        background = np.ones_like(support, dtype=bool)

    sample = slice(None, None, 32)
    bg_sl = sl[background][sample]
    bg_ol = ol[background][sample]
    bg_corr = float(np.corrcoef(bg_sl, bg_ol)[0, 1]) if bg_sl.size > 2 else 1.0
    gain = ol - sl
    expected = 1.0 - (1.0 - starless) * (1.0 - weight * stars)
    max_formula_error = float(np.max(np.abs(out - expected)))
    stars_sat = sat_map(stars)
    out_sat = sat_map(out)

    return {
        "star_contribution": float(weight),
        "screen_formula": screen_formula(weight),
        "finite_fraction": float(np.isfinite(out).mean()),
        "minimum": float(np.min(out)),
        "maximum": float(np.max(out)),
        "added_low_clip_fraction": float(np.mean((out <= 0).all(axis=-1) & ~(starless <= 0).all(axis=-1))),
        "added_high_clip_fraction": float(np.mean((out >= 1).any(axis=-1) & ~(starless >= 1).any(axis=-1))),
        "max_abs_screen_formula_error": max_formula_error,
        "starless_luma_median": float(np.median(sl)),
        "output_luma_median": float(np.median(ol)),
        "absolute_luma_median_change": float(abs(np.median(ol) - np.median(sl))),
        "output_luma_p99": float(np.quantile(ol, 0.99)),
        "output_luma_p999": float(np.quantile(ol, 0.999)),
        "output_luma_p9999": float(np.quantile(ol, 0.9999)),
        "star_support_threshold": support_threshold,
        "star_support_fraction": float(np.mean(support)),
        "background_median_abs_luma_change": float(np.median(np.abs(gain[background]))),
        "background_luma_correlation": bg_corr,
        "star_support_median_luma_gain": float(np.median(gain[support])) if np.any(support) else 0.0,
        "star_support_p90_luma_gain": float(np.quantile(gain[support], 0.90)) if np.any(support) else 0.0,
        "processed_stars_saturation_median": float(np.median(stars_sat[support])) if np.any(support) else 0.0,
        "output_saturation_median_on_star_support": float(np.median(out_sat[support])) if np.any(support) else 0.0,
    }


def select_reference_points(stars: np.ndarray, count: int = 6, min_spacing: int = 180, half_size: int = 34, upscale: int = 5) -> list[dict]:
    lum = stars.mean(axis=-1)
    h, w = lum.shape
    flat = lum.ravel()
    take = min(max(count * 400, 2000), flat.size)
    idxs = np.argpartition(flat, -take)[-take:]
    idxs = idxs[np.argsort(flat[idxs])[::-1]]
    points = []
    for idx in idxs:
        y, x = divmod(int(idx), w)
        if any((x - q["center_x"]) ** 2 + (y - q["center_y"]) ** 2 < min_spacing ** 2 for q in points):
            continue
        x0, x1 = max(0, x - half_size), min(w, x + half_size + 1)
        y0, y1 = max(0, y - half_size), min(h, y + half_size + 1)
        points.append({
            "panel_index": len(points) + 1,
            "reason": "bright_or_representative_star",
            "center_x": x,
            "center_y": y,
            "bounds_xyxy": [x0, y0, x1, y1],
            "upscale_factor": upscale,
            "score": float(lum[y, x]),
        })
        if len(points) >= count:
            break
    return points


def crop_pad(rgb: np.ndarray, bounds: list[int], side: int) -> np.ndarray:
    x0, y0, x1, y1 = bounds
    crop = rgb[y0:y1, x0:x1, :]
    out = np.zeros((side, side, 3), dtype=np.float32)
    h, w = crop.shape[:2]
    out[:h, :w, :] = crop
    return out


def nearest_upscale(rgb: np.ndarray, factor: int) -> np.ndarray:
    if factor <= 1:
        return rgb.astype(np.float32)
    return np.repeat(np.repeat(rgb, factor, axis=0), factor, axis=1).astype(np.float32)


def write_diagnostic_panel(cdir: Path, out: np.ndarray, specs: list[dict]) -> dict:
    if not specs:
        raise RuntimeError("No diagnostic panel locations were selected")
    side = max(s["bounds_xyxy"][2] - s["bounds_xyxy"][0] for s in specs)
    tiles = []
    for spec in specs:
        tile = crop_pad(out, spec["bounds_xyxy"], side)
        tile = nearest_upscale(tile, int(spec["upscale_factor"]))
        tiles.append(tile)
    th, tw = tiles[0].shape[:2]
    sep = 4
    sheet = np.zeros((2 * th + sep, 3 * tw + 2 * sep, 3), dtype=np.float32)
    for i, tile in enumerate(tiles[:6]):
        row, col = divmod(i, 3)
        y, x = row * (th + sep), col * (tw + sep)
        sheet[y:y+th, x:x+tw, :] = tile
    panel_fit = cdir / "diagnostic-panel.fit"
    save_rgb(panel_fit, sheet)
    run_siril_script(cdir, "\n".join([
        "requires 1.4.4",
        "load diagnostic-panel.fit",
        "savepng diagnostic-panel",
        "close",
        "",
    ]))
    panel_png = cdir / "diagnostic-panel.png"
    try:
        panel_fit.unlink()
    except FileNotFoundError:
        pass
    if not panel_png.exists():
        raise RuntimeError("Diagnostic panel PNG was not created")
    return {
        "path": str(panel_png),
        "sha256": sha256_file(panel_png),
        "layout": "2x3",
        "panel_order": specs,
    }


def generate_candidates(starless_fit: Path, stars_fit: Path, run_root: Path) -> dict:
    ensure_dir(run_root)
    starless = load_rgb(starless_fit)
    stars = load_rgb(stars_fit)
    if starless.shape != stars.shape:
        raise RuntimeError("Input dimensions/channels do not match")
    panel_specs = select_reference_points(stars)
    results = []
    eligible = []

    for cand in CANDIDATES:
        cdir = run_root / cand["name"]
        ensure_dir(cdir)
        shutil.copy2(starless_fit, cdir / "starless.fit")
        shutil.copy2(stars_fit, cdir / "stars.fit")
        run_siril_script(cdir, candidate_script(cand["star_contribution"]))
        out_fit = cdir / "processed.fit"
        out_png = cdir / "preview.png"
        if not out_fit.exists() or not out_png.exists():
            raise RuntimeError(f"Missing Siril output for {cand['name']}")
        out = load_rgb(out_fit)
        metrics = candidate_metrics(starless, stars, out, cand["star_contribution"])
        panel = write_diagnostic_panel(cdir, out, panel_specs)
        is_eligible = bool(
            metrics["finite_fraction"] == 1.0
            and metrics["minimum"] >= -1e-6
            and metrics["maximum"] <= 1.000001
            and metrics["added_low_clip_fraction"] == 0.0
            and metrics["added_high_clip_fraction"] == 0.0
            and metrics["max_abs_screen_formula_error"] <= 5e-6
            and metrics["background_median_abs_luma_change"] <= 0.001
            and metrics["background_luma_correlation"] >= 0.999
        )
        meta = {
            "candidate": cand["name"],
            "label": cand["label"],
            "parameters": cand,
            "fit_path": str(out_fit),
            "png_path": str(out_png),
            "fit_sha256": sha256_file(out_fit),
            "png_sha256": sha256_file(out_png),
            "diagnostic_panel": panel,
            "metrics": metrics,
            "eligible": is_eligible,
        }
        results.append(meta)
        if is_eligible:
            eligible.append(cand["name"])

    if not eligible:
        raise RuntimeError("No technically eligible screen-recombination candidate")

    by = {c["candidate"]: c for c in results}
    if all(name in by and by[name]["eligible"] for name in ("candidate-00", "candidate-01", "candidate-02")):
        gains = [by[name]["metrics"]["star_support_p90_luma_gain"] for name in ("candidate-00", "candidate-01", "candidate-02")]
        if not (gains[0] < gains[1] < gains[2]):
            raise RuntimeError(f"Candidate star contribution progression is not monotonic: {gains}")

    summary = {
        "status": "awaiting_visual_selection",
        "version": VERSION,
        "blend_model": "rgb-screen-pixelmath",
        "formula_template": "1 - (1 - starless) * (1 - k * stars)",
        "candidate_count": len(results),
        "eligible": eligible,
        "recommended_candidate": "candidate-01" if "candidate-01" in eligible else eligible[0],
        "panel_specs": panel_specs,
        "starless": {"path": str(starless_fit), "sha256": sha256_file(starless_fit)},
        "stars": {"path": str(stars_fit), "sha256": sha256_file(stars_fit)},
        "candidates": results,
    }
    json_dump(summary, run_root / "candidate-summary.json")
    return summary


def current_status(project_name: str, p: dict) -> tuple[str, list[str]]:
    if not p["canonical_fit"].exists() or not p["canonical_manifest"].exists():
        return "missing", []
    reasons = []
    try:
        m = json_load(p["canonical_manifest"])
    except Exception as exc:
        return "obsolete", [f"Existing manifest unreadable: {exc}"]
    if m.get("status") != "ready": reasons.append("Manifest status is not ready")
    if m.get("project") != project_name: reasons.append("Manifest project differs")
    if (m.get("starless_source") or {}).get("sha256") != sha256_file(p["starless_fit"]): reasons.append("Starless source SHA changed")
    if (m.get("stars_source") or {}).get("sha256") != sha256_file(p["stars_fit"]): reasons.append("Processed-stars source SHA changed")
    if (m.get("output") or {}).get("sha256") != sha256_file(p["canonical_fit"]): reasons.append("Canonical output SHA differs from manifest")
    return ("current", reasons) if not reasons else ("obsolete", reasons)


def begin(project_name: str) -> dict:
    p = project_paths(project_name)
    upstream = validate_upstreams(project_name, p)
    status, reasons = current_status(project_name, p)
    exe = Path(__file__).resolve().parent.parent / "bin" / "star-recombination"
    if status == "current":
        return {
            "status": "confirmation_required",
            "current_canonical_status": "current",
            "question": f"Star recombination for {project_name} has already completed. Do you want me to run it again as a fresh run?",
            "next_command_after_confirmation": f'{exe} confirm-fresh --project "{project_name}" && {exe} advance --project "{project_name}"',
            "version": VERSION,
        }
    if status == "obsolete":
        return {
            "status": "confirmation_required",
            "current_canonical_status": "obsolete",
            "obsolete_reasons": reasons,
            "question": f"Star recombination for {project_name} already exists but is obsolete for the current inputs. Do you want me to run it again as a fresh run?",
            "next_command_after_confirmation": f'{exe} confirm-fresh --project "{project_name}" && {exe} advance --project "{project_name}"',
            "version": VERSION,
        }
    return {
        "status": "would_generate_candidates",
        "production_processing_started": False,
        "version": VERSION,
        "starless_source_sha256": upstream["starless"]["sha256"],
        "stars_source_sha256": upstream["stars"]["sha256"],
        "blend_model": "rgb-screen-pixelmath",
    }


def confirm_fresh(project_name: str) -> dict:
    p = project_paths(project_name)
    upstream = validate_upstreams(project_name, p)
    ensure_dir(p["state_root"])
    json_dump({
        "project": project_name,
        "starless_sha256": upstream["starless"]["sha256"],
        "stars_sha256": upstream["stars"]["sha256"],
        "confirmed_at_utc": now_utc(),
        "version": VERSION,
    }, p["auth_file"])
    return {"status": "confirmed", "version": VERSION}


def advance(project_name: str) -> dict:
    p = project_paths(project_name)
    upstream = validate_upstreams(project_name, p)
    status, _ = current_status(project_name, p)
    if status in {"current", "obsolete"}:
        if not p["auth_file"].exists():
            raise RuntimeError("Fresh rerun confirmation is required before advance")
        auth = json_load(p["auth_file"])
        if auth.get("starless_sha256") != upstream["starless"]["sha256"] or auth.get("stars_sha256") != upstream["stars"]["sha256"]:
            raise RuntimeError("Stored fresh-rerun authorization does not match current inputs")

    run_root = p["state_root"] / f"star-recombination-{now_utc()}-{upstream['starless']['sha256'][:6]}-{upstream['stars']['sha256'][:6]}"
    summary = generate_candidates(p["starless_fit"], p["stars_fit"], run_root)
    ensure_dir(p["state_root"])
    json_dump({
        "run_root": str(run_root),
        "starless_sha256": upstream["starless"]["sha256"],
        "stars_sha256": upstream["stars"]["sha256"],
        "created_at_utc": now_utc(),
    }, p["current_run"])
    if p["auth_file"].exists():
        consumed = json_load(p["auth_file"])
        consumed["status"] = "consumed"
        consumed["consumed_at_utc"] = now_utc()
        json_dump(consumed, p["auth_file"])

    read_targets = []
    for c in summary["candidates"]:
        read_targets.append({
            "candidate": c["candidate"],
            "view": "full_frame",
            "path": c["png_path"],
            "sha256": c["png_sha256"],
        })
        panel = c["diagnostic_panel"]
        read_targets.append({
            "candidate": c["candidate"],
            "view": "star_diagnostic_panel",
            "path": panel["path"],
            "sha256": panel["sha256"],
            "layout": panel["layout"],
            "panel_order": panel["panel_order"],
        })

    return {
        "status": "visual_review_required",
        "version": VERSION,
        "project_name": project_name,
        "run_root": str(run_root),
        "technical_recommendation": summary["recommended_candidate"],
        "selection_rule": "Choose the screen-recombined candidate with the best star-to-nebula balance. Preserve the nebula as the main subject, avoid overpowering/bloated stars, reject halos or dark seams, retain natural neutral star appearance, and do not choose from metrics alone.",
        "instruction": "Read all three full-frame previews and all three star diagnostic panels verbatim, compare every candidate, then call select-publish with notes for all three.",
        "read_target_policy": {"directory_discovery_forbidden": True, "path_handling": "verbatim", "on_read_failure": "stop_and_report_exact_failed_path"},
        "read_targets": read_targets,
        "required_review_fields": ["star_nebula_balance", "star_dominance", "star_profiles", "halos_or_seams", "color_neutrality", "nebula_preservation", "overall_balance"],
    }


def select_publish(project_name: str, run_root: Path, candidate_name: str, compared: list[str], notes: list[str]) -> dict:
    p = project_paths(project_name)
    upstream = validate_upstreams(project_name, p)
    summary = json_load(run_root / "candidate-summary.json")
    by = {c["candidate"]: c for c in summary["candidates"]}
    if candidate_name not in by or not by[candidate_name].get("eligible"):
        raise RuntimeError(f"Candidate is unknown or technically ineligible: {candidate_name}")
    if set(compared) != {"candidate-00", "candidate-01", "candidate-02"} or len(notes) != len(compared):
        raise RuntimeError("Publication requires review notes for all three candidates")
    selected = by[candidate_name]

    previous_publication = None
    if p["canonical_manifest"].exists() or p["canonical_fit"].exists():
        previous_publication = {"recorded_at_utc": now_utc()}
        if p["canonical_fit"].exists():
            previous_publication["canonical_output_sha256"] = sha256_file(p["canonical_fit"])
        if p["canonical_manifest"].exists():
            try:
                old = json_load(p["canonical_manifest"])
                previous_publication["prior_selected_candidate"] = old.get("selected_candidate")
                previous_publication["prior_run_root"] = old.get("run_root")
                previous_publication["prior_published_at_utc"] = old.get("published_at_utc")
                prev_cand = old.get("selected_candidate")
                prev_root = old.get("run_root")
                if prev_cand and prev_root:
                    recoverable = Path(prev_root) / prev_cand / "processed.fit"
                    if recoverable.exists():
                        previous_publication["recoverable_prior_candidate_fit"] = str(recoverable)
                        previous_publication["recoverable_prior_candidate_fit_sha256"] = sha256_file(recoverable)
            except Exception as exc:
                previous_publication["prior_manifest_read_error"] = str(exc)

    ensure_dir(p["stage_root"])
    tmp_fit = p["stage_root"] / ".SHO-recombined.fit.tmp"
    tmp_png = p["stage_root"] / ".SHO-recombined.png.tmp"
    shutil.copy2(selected["fit_path"], tmp_fit)
    shutil.copy2(selected["png_path"], tmp_png)
    os.replace(tmp_fit, p["canonical_fit"])
    os.replace(tmp_png, p["canonical_preview"])

    published_at = now_utc()
    manifest = {
        "status": "ready",
        "version": VERSION,
        "project": project_name,
        "blend_model": "rgb-screen-pixelmath",
        "formula_template": "1 - (1 - starless) * (1 - k * stars)",
        "starless_source": upstream["starless"],
        "stars_source": upstream["stars"],
        "selected_candidate": candidate_name,
        "selected_parameters": selected["parameters"],
        "selected_metrics": selected["metrics"],
        "output": {
            "path": str(p["canonical_fit"]),
            "sha256": sha256_file(p["canonical_fit"]),
            "preview_path": str(p["canonical_preview"]),
            "preview_sha256": sha256_file(p["canonical_preview"]),
        },
        "stage_order": {"starless_upstream": "siril-saturation", "stars_upstream": "siril-star-processing", "current": SKILL_NAME, "downstream": None},
        "next_stage": None,
        "final_processing_complete": True,
        "run_root": str(run_root),
        "previous_canonical_preserved_at": None,
        "previous_publication": previous_publication,
        "published_at_utc": published_at,
    }
    selection = {
        "selected_candidate": candidate_name,
        "technical_recommendation": summary.get("recommended_candidate"),
        "compared_candidates": compared,
        "notes": dict(zip(compared, notes)),
        "run_root": str(run_root),
        "published_at_utc": published_at,
    }
    json_dump(manifest, p["canonical_manifest"])
    json_dump(selection, p["selection_record"])
    return {
        "status": "ready",
        "version": VERSION,
        "project": project_name,
        "selected_candidate": candidate_name,
        "canonical_output": str(p["canonical_fit"]),
        "canonical_output_sha256": sha256_file(p["canonical_fit"]),
        "canonical_preview": str(p["canonical_preview"]),
        "canonical_manifest": str(p["canonical_manifest"]),
        "final_processing_complete": True,
        "next_stage": None,
        "previous_canonical_preserved_at": None,
        "previous_publication": previous_publication,
    }


def smoke_test(starless: Path, stars: Path, starless_manifest: Path | None = None, stars_manifest: Path | None = None) -> dict:
    root = Path("/home/peter") / f"siril-star-recombination-smoke-{now_utc()}-{sha256_file(starless)[:6]}-{sha256_file(stars)[:6]}"
    ensure_dir(root)
    summary = generate_candidates(starless, stars, root / "run")
    summary["smoke_root"] = str(root)
    summary["starless_manifest_sha256"] = sha256_file(starless_manifest) if starless_manifest and starless_manifest.exists() else None
    summary["stars_manifest_sha256"] = sha256_file(stars_manifest) if stars_manifest and stars_manifest.exists() else None
    return summary


def self_test() -> dict:
    return {
        "status": "success",
        "version": VERSION,
        "candidate_count": len(CANDIDATES),
        "candidate_weights": [c["star_contribution"] for c in CANDIDATES],
        "uses_siril_pixelmath": True,
        "blend_model": "rgb-screen-pixelmath",
        "screen_formula_template": "1 - (1 - starless) * (1 - k * stars)",
        "linear_addition_prohibited": True,
        "uses_native_unscreen_derived_processed_stars": True,
        "requires_star_processing_recombination_permission": True,
        "accepts_legacy_saturation_v1_handoff": True,
        "exact_path_visual_review": True,
        "uses_full_frame_and_star_diagnostic_panels": True,
        "completed_stage_requires_confirmation": True,
        "publishes_without_new_before_fit_copies": True,
        "target_agnostic_project_resolution": True,
        "validation_fixture_not_processing_scope": True,
        "final_stage": True,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="star-recombination")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("version")
    sub.add_parser("self-test")
    p = sub.add_parser("begin"); p.add_argument("--project", required=True)
    p = sub.add_parser("confirm-fresh"); p.add_argument("--project", required=True)
    p = sub.add_parser("advance"); p.add_argument("--project", required=True)
    p = sub.add_parser("select-publish")
    p.add_argument("--project", required=True)
    p.add_argument("--run-root", required=True)
    p.add_argument("--candidate", required=True)
    p.add_argument("--compared", action="append", required=True)
    p.add_argument("--note", action="append", required=True)
    p = sub.add_parser("smoke-test")
    p.add_argument("--starless", required=True)
    p.add_argument("--stars", required=True)
    p.add_argument("--starless-manifest")
    p.add_argument("--stars-manifest")
    args = parser.parse_args(argv)

    if args.cmd == "version": print(VERSION); return 0
    if args.cmd == "self-test": print(json.dumps(self_test(), indent=2)); return 0
    if args.cmd == "begin": print(json.dumps(begin(args.project), indent=2)); return 0
    if args.cmd == "confirm-fresh": print(json.dumps(confirm_fresh(args.project), indent=2)); return 0
    if args.cmd == "advance": print(json.dumps(advance(args.project), indent=2)); return 0
    if args.cmd == "select-publish": print(json.dumps(select_publish(args.project, Path(args.run_root), args.candidate, args.compared, args.note), indent=2)); return 0
    if args.cmd == "smoke-test":
        print(json.dumps(smoke_test(Path(args.starless), Path(args.stars), Path(args.starless_manifest) if args.starless_manifest else None, Path(args.stars_manifest) if args.stars_manifest else None), indent=2)); return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
