#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import subprocess
import sys
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VERSION = "1.5.6"
ENGINE_VERSION = "1.5.2"
SOURCE_CONTRACT_REVISION = "native-starnet-channel-balance-v1"

WORKSPACE = Path("/home/peter/.openclaw/workspace/agents/codewarrior")
PROJECTS_ROOT = WORKSPACE / "Projects"
PYTHON = WORKSPACE / "AstroProcessor" / ".venv" / "bin" / "python"
ENGINE = WORKSPACE / "skills" / "siril-starnet-removal" / "scripts" / "starnet_workflow.py"

STATE_DIR_NAME = ".siril-starnet-v1.5.6"
CANONICAL_REL = Path("processing/starnet")
SOURCE_REL = Path("processing/background-neutralization/SHO-linear-neutralized.fit")

PANEL_ORDER = [
    "starless_linear_linked",
    "starmask_linked",
    "starmask_unlinked",
    "unscreen_linked",
]

class StarNetOrchestrationError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def uid() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise StarNetOrchestrationError(f"Cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise StarNetOrchestrationError(f"Expected JSON object: {path}")
    return value


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uid()}.partial")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def paths_for(project_name: str) -> dict[str, Path]:
    project = PROJECTS_ROOT / project_name
    canonical = project / CANONICAL_REL
    state = project / STATE_DIR_NAME
    return {
        "project": project,
        "source": project / SOURCE_REL,
        "canonical": canonical,
        "manifest": canonical / "starnet-manifest.json",
        "starless": canonical / "SHO-starless-linear.fit",
        "starmask": canonical / "SHO-starmask.fit",
        "unscreen": canonical / "SHO-stars-unscreen.fit",
        "review": canonical / "visual-review-record.json",
        "orchestration_review": canonical / "orchestration-review-v1.5.6.json",
        "state": state,
        "active": state / "active.json",
        "intents": state / "fresh-intents",
        "completed": state / "completed",
    }


def validate_project(project_name: str) -> dict[str, Path]:
    p = paths_for(project_name)
    if not p["project"].is_dir():
        raise StarNetOrchestrationError(f"Project does not exist: {p['project']}")
    if not p["source"].is_file():
        raise StarNetOrchestrationError(f"Background-neutralized StarNet source is missing: {p['source']}")
    return p


def canonical_snapshot(project_name: str, *, verify_hashes: bool = False) -> dict[str, Any]:
    """Inspect the canonical StarNet checkpoint.

    Fast-path status checks deliberately inspect only file existence plus the
    small manifest. Full FITS/source hashing is deferred until an explicitly
    confirmed fresh rerun or post-publication verification.
    """
    p = validate_project(project_name)
    required = ("manifest", "starless", "starmask", "unscreen", "review")
    exists = all(p[k].is_file() for k in required)
    if not exists:
        payload = {
            "exists": False,
            "status": "missing",
            "hashes_verified": False,
            "hash_verification_deferred": True,
        }
        if verify_hashes:
            payload["source_sha256"] = sha256_file(p["source"])
            payload["hashes_verified"] = True
            payload["hash_verification_deferred"] = False
        return payload

    m = load_json(p["manifest"])
    native = (
        m.get("status") == "ready"
        and m.get("helper_version") == ENGINE_VERSION
        and m.get("source_contract_revision") == SOURCE_CONTRACT_REVISION
        and m.get("next_stage") == "siril-sho-channel-balance"
        and m.get("sho_channel_balance_permitted") is True
        and m.get("ghs_pass1_permitted") is False
        and m.get("stage_order") == {
            "upstream": "siril-background-neutralization",
            "current": "siril-starnet-removal",
            "downstream": "siril-sho-channel-balance",
        }
    )
    payload = {
        "exists": True,
        "status": "ready" if native else "legacy_or_obsolete",
        "native_contract_valid": native,
        "manifest_sha256": sha256_file(p["manifest"]),
        "hashes_verified": False,
        "hash_verification_deferred": True,
    }
    if verify_hashes:
        payload.update({
            "starless_sha256": sha256_file(p["starless"]),
            "starmask_sha256": sha256_file(p["starmask"]),
            "unscreen_sha256": sha256_file(p["unscreen"]),
            "review_sha256": sha256_file(p["review"]),
            "source_sha256": sha256_file(p["source"]),
            "hashes_verified": True,
            "hash_verification_deferred": False,
        })
    return payload


def run_engine(args: list[str], timeout: int = 9000) -> dict[str, Any]:
    if not PYTHON.is_file():
        raise StarNetOrchestrationError(f"Canonical Python is missing: {PYTHON}")
    if not ENGINE.is_file():
        raise StarNetOrchestrationError(f"StarNet processing engine is missing: {ENGINE}")
    cp = subprocess.run(
        [str(PYTHON), str(ENGINE), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    stdout = cp.stdout.strip()
    stderr = cp.stderr.strip()
    try:
        payload = json.loads(stdout)
    except Exception as exc:
        raise StarNetOrchestrationError(
            f"StarNet engine returned non-JSON. exit={cp.returncode}; "
            f"stderr={stderr[:500]!r}; stdout={stdout[:500]!r}"
        ) from exc
    if cp.returncode != 0:
        raise StarNetOrchestrationError(
            f"StarNet engine blocked/failed: {payload.get('error', payload)}"
        )
    if not isinstance(payload, dict):
        raise StarNetOrchestrationError("StarNet engine returned non-object JSON.")
    return payload


# ---------- Minimal PNG decoder/encoder for context-safe contact sheets ----------

def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def read_png_rgb8(path: Path):
    import numpy as np

    data = path.read_bytes()
    sig = b"\x89PNG\r\n\x1a\n"
    if not data.startswith(sig):
        raise StarNetOrchestrationError(f"Not a PNG file: {path}")

    pos = len(sig)
    width = height = bit_depth = color_type = interlace = None
    compressed = bytearray()
    while pos < len(data):
        if pos + 12 > len(data):
            raise StarNetOrchestrationError(f"Truncated PNG: {path}")
        length = struct.unpack(">I", data[pos:pos+4])[0]
        ctype = data[pos+4:pos+8]
        chunk = data[pos+8:pos+8+length]
        pos += 12 + length
        if ctype == b"IHDR":
            width, height, bit_depth, color_type, comp, filt, interlace = struct.unpack(">IIBBBBB", chunk)
            if comp != 0 or filt != 0 or interlace != 0:
                raise StarNetOrchestrationError(f"Unsupported PNG encoding in {path}")
        elif ctype == b"IDAT":
            compressed.extend(chunk)
        elif ctype == b"IEND":
            break

    if None in (width, height, bit_depth, color_type, interlace):
        raise StarNetOrchestrationError(f"PNG has no valid IHDR: {path}")
    if bit_depth not in (8, 16):
        raise StarNetOrchestrationError(f"Unsupported PNG bit depth {bit_depth}: {path}")
    channels_by_type = {0: 1, 2: 3, 4: 2, 6: 4}
    if color_type not in channels_by_type:
        raise StarNetOrchestrationError(f"Unsupported PNG color type {color_type}: {path}")

    channels = channels_by_type[color_type]
    sample_bytes = bit_depth // 8
    bpp = channels * sample_bytes
    row_bytes = width * bpp
    raw = zlib.decompress(bytes(compressed))
    expected = height * (row_bytes + 1)
    if len(raw) != expected:
        raise StarNetOrchestrationError(
            f"Unexpected decompressed PNG size for {path}: {len(raw)} != {expected}"
        )

    rows = bytearray(height * row_bytes)
    prev = bytearray(row_bytes)
    src = 0
    for y in range(height):
        filter_type = raw[src]
        src += 1
        scan = bytearray(raw[src:src+row_bytes])
        src += row_bytes
        recon = bytearray(row_bytes)
        for i, x in enumerate(scan):
            left = recon[i-bpp] if i >= bpp else 0
            up = prev[i]
            up_left = prev[i-bpp] if i >= bpp else 0
            if filter_type == 0:
                val = x
            elif filter_type == 1:
                val = (x + left) & 255
            elif filter_type == 2:
                val = (x + up) & 255
            elif filter_type == 3:
                val = (x + ((left + up) >> 1)) & 255
            elif filter_type == 4:
                val = (x + _paeth(left, up, up_left)) & 255
            else:
                raise StarNetOrchestrationError(f"Unsupported PNG filter {filter_type}: {path}")
            recon[i] = val
        rows[y*row_bytes:(y+1)*row_bytes] = recon
        prev = recon

    arr = np.frombuffer(rows, dtype=np.uint8).reshape(height, width, bpp)
    if bit_depth == 16:
        # PNG stores 16-bit samples big-endian; retain the high byte for review panels.
        arr = arr[:, :, ::2]

    if color_type == 0:
        gray = arr[:, :, 0]
        rgb = np.stack([gray, gray, gray], axis=2)
    elif color_type == 2:
        rgb = arr[:, :, :3]
    elif color_type == 4:
        gray = arr[:, :, 0]
        rgb = np.stack([gray, gray, gray], axis=2)
    else:
        rgb = arr[:, :, :3]
    return np.ascontiguousarray(rgb, dtype=np.uint8)


def resize_nearest(rgb, max_width: int = 900, max_height: int = 900):
    import numpy as np
    h, w, _ = rgb.shape
    scale = min(max_width / w, max_height / h, 1.0)
    nw = max(1, int(round(w * scale)))
    nh = max(1, int(round(h * scale)))
    yi = np.linspace(0, h - 1, nh).astype(np.int64)
    xi = np.linspace(0, w - 1, nw).astype(np.int64)
    return np.ascontiguousarray(rgb[yi][:, xi], dtype=np.uint8)


def write_png_rgb8(path: Path, rgb) -> None:
    import numpy as np
    rgb = np.ascontiguousarray(rgb, dtype=np.uint8)
    h, w, c = rgb.shape
    if c != 3:
        raise StarNetOrchestrationError("Contact-sheet writer expects RGB data.")
    scanlines = bytearray()
    for y in range(h):
        scanlines.append(0)
        scanlines.extend(rgb[y].tobytes())

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xffffffff)

    png = bytearray(b"\x89PNG\r\n\x1a\n")
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(scanlines), 6))
    png += chunk(b"IEND", b"")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


def make_contact_sheet(preview_paths: list[Path], output: Path) -> dict[str, Any]:
    import numpy as np
    if len(preview_paths) != 4:
        raise StarNetOrchestrationError("StarNet contact sheet requires exactly four preview images.")
    imgs = []
    for path in preview_paths:
        if not path.is_file():
            raise StarNetOrchestrationError(f"Required candidate preview is missing: {path}")
        imgs.append(resize_nearest(read_png_rgb8(path)))

    tile_h = max(x.shape[0] for x in imgs)
    tile_w = max(x.shape[1] for x in imgs)
    sheet = np.zeros((tile_h * 2 + 4, tile_w * 2 + 4, 3), dtype=np.uint8)
    positions = [(0, 0), (0, tile_w + 4), (tile_h + 4, 0), (tile_h + 4, tile_w + 4)]
    for img, (y, x) in zip(imgs, positions):
        h, w, _ = img.shape
        sheet[y:y+h, x:x+w] = img
    write_png_rgb8(output, sheet)
    return {
        "path": str(output),
        "sha256": sha256_file(output),
        "panel_order": list(PANEL_ORDER),
        "source_previews": [str(x) for x in preview_paths],
    }


# ---------- Orchestration ----------

def locate_intent(p: dict[str, Path], snap: dict[str, Any]) -> Path | None:
    if not p["intents"].is_dir():
        return None
    rows = []
    for f in p["intents"].glob("fresh-run-*.json"):
        try:
            j = load_json(f)
        except Exception:
            continue
        if (
            j.get("status") == "authorized"
            and j.get("canonical_manifest_sha256") == snap.get("manifest_sha256")
            and j.get("canonical_starless_sha256") == snap.get("starless_sha256")
            and j.get("source_sha256") == snap.get("source_sha256")
        ):
            rows.append((f.stat().st_mtime, f))
    return sorted(rows, key=lambda x: x[0], reverse=True)[0][1] if rows else None



def authorized_intent_may_exist(p: dict[str, Path]) -> bool:
    """Cheaply detect whether a confirmed rerun authorization may exist.

    This scans only the small orchestration intent JSON files. If no currently
    authorized intent exists, advance can return the confirmation question
    without hashing any large FITS products.
    """
    if not p["intents"].is_dir():
        return False
    for f in p["intents"].glob("fresh-run-*.json"):
        try:
            j = load_json(f)
        except Exception:
            continue
        if (
            j.get("status") == "authorized"
            and j.get("orchestration_version") in {"1.5.3", VERSION}
            and j.get("project_name") == p["project"].name
        ):
            return True
    return False


def confirm_fresh(project_name: str) -> dict[str, Any]:
    p = validate_project(project_name)
    snap = canonical_snapshot(project_name, verify_hashes=True)
    if not snap.get("exists"):
        raise StarNetOrchestrationError("No completed StarNet canonical result exists to rerun.")
    p["intents"].mkdir(parents=True, exist_ok=True)
    f = p["intents"] / f"fresh-run-{uid()}.json"
    write_json_atomic(f, {
        "schema_version": 1,
        "orchestration_version": VERSION,
        "processing_engine_version": ENGINE_VERSION,
        "status": "authorized",
        "authorized_at": utc_now(),
        "project_name": project_name,
        "canonical_manifest_sha256": snap["manifest_sha256"],
        "canonical_starless_sha256": snap["starless_sha256"],
        "source_sha256": snap["source_sha256"],
    })
    return {
        "status": "fresh_run_confirmed",
        "orchestration_version": VERSION,
        "processing_engine_version": ENGINE_VERSION,
        "project": str(p["project"]),
        "fresh_intent": str(f),
        "canonical_output_preserved": True,
    }


def build_state(project_name: str, run: dict[str, Any], intent: Path) -> dict[str, Any]:
    p = validate_project(project_name)
    if run.get("status") != "awaiting_visual_selection":
        raise StarNetOrchestrationError(
            f"Expected awaiting_visual_selection from StarNet engine, got {run.get('status')!r}"
        )
    run_root = Path(str(run.get("run_root", "")))
    if not run_root.is_dir():
        raise StarNetOrchestrationError("StarNet engine did not return a valid run_root.")
    template_path = run_root / "compact-review" / "visual-review-template.json"
    if not template_path.is_file():
        raise StarNetOrchestrationError(f"Visual-review template is missing: {template_path}")
    template = load_json(template_path)
    candidates = template.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise StarNetOrchestrationError("Visual-review template contains no candidates.")

    contact_dir = run_root / "compact-review" / "v1.5.6-panels"
    contact_dir.mkdir(parents=True, exist_ok=True)

    candidate_state = {}
    for c in candidates:
        if not isinstance(c, dict) or not c.get("candidate"):
            raise StarNetOrchestrationError("Malformed candidate in StarNet visual-review template.")
        name = str(c["candidate"])
        previews = c.get("previews", {})
        try:
            ordered = [Path(str(previews[key]["path"])) for key in PANEL_ORDER]
        except Exception as exc:
            raise StarNetOrchestrationError(f"{name} preview template is incomplete.") from exc
        panel = make_contact_sheet(ordered, contact_dir / f"{name}-review-panel.png")
        candidate_state[name] = {
            "candidate": name,
            "technical_status": c.get("technical_status"),
            "contact_panel": panel,
            "preview_records": previews,
        }

    source = template.get("source_preview", {})
    source_path = Path(str(source.get("path", "")))
    if not source_path.is_file():
        raise StarNetOrchestrationError(f"Source preview is missing: {source_path}")

    state = {
        "schema_version": 1,
        "orchestration_version": VERSION,
        "processing_engine_version": ENGINE_VERSION,
        "status": "awaiting_visual_selection",
        "created_at": utc_now(),
        "project_name": project_name,
        "project": str(p["project"]),
        "run_root": str(run_root),
        "fresh_run": True,
        "source_sha256": sha256_file(p["source"]),
        "source_preview": {
            "path": str(source_path),
            "sha256": sha256_file(source_path),
        },
        "candidates": candidate_state,
        "generated_candidates": [str(c["candidate"]) for c in candidates],
        "satisfactory_candidates": [str(x) for x in run.get("satisfactory_candidates", [])],
        "recommended_candidate": run.get("recommended_candidate"),
        "visual_review_template": str(template_path),
        "canonical_output_changed": False,
        "sho_channel_balance_permitted": False,
    }
    write_json_atomic(p["active"], state)

    j = load_json(intent)
    j["status"] = "consumed"
    j["consumed_at"] = utc_now()
    j["run_root"] = str(run_root)
    write_json_atomic(intent, j)
    return state


def load_active(project_name: str):
    p = validate_project(project_name)
    if not p["active"].is_file():
        return None
    s = load_json(p["active"])
    if s.get("orchestration_version") != VERSION:
        raise StarNetOrchestrationError("Active StarNet orchestration state uses another version.")
    if s.get("status") != "awaiting_visual_selection":
        raise StarNetOrchestrationError(f"Unexpected active StarNet state: {s.get('status')!r}")
    if s.get("source_sha256") != sha256_file(p["source"]):
        raise StarNetOrchestrationError("Background-neutralized source changed during active StarNet run.")
    return p, s


def review_plan(state: dict[str, Any]) -> dict[str, Any]:
    targets = [{
        "role": "source_before",
        "path": state["source_preview"]["path"],
        "sha256": state["source_preview"]["sha256"],
    }]
    for name in state["generated_candidates"]:
        panel = state["candidates"][name]["contact_panel"]
        targets.append({
            "role": "candidate_contact_panel",
            "candidate": name,
            "path": panel["path"],
            "sha256": panel["sha256"],
            "panel_order": panel["panel_order"],
        })

    note_examples = []
    for name in state["generated_candidates"]:
        accepted = "true" if name == state.get("recommended_candidate") else "false"
        note_examples.append(
            f'{name}=accepted:{accepted}; remaining_stars:none; '
            'broad_nebula:false; nebula_damage:none; halos:none; '
            'observation:Describe the actual visible candidate-specific differences '
            'in residual stars, nebular preservation, starmask leakage, halos, and '
            'the unscreen layer before using the recommendation only as a tie-breaker.'
        )

    return {
        "status": "visual_review_required",
        "action": "continue_autonomously_to_publication",
        "orchestration_version": VERSION,
        "processing_engine_version": ENGINE_VERSION,
        "project_name": state["project_name"],
        "run_root": state["run_root"],
        "recommended_candidate": state.get("recommended_candidate"),
        "satisfactory_candidates": state.get("satisfactory_candidates"),
        "read_targets": targets,
        "candidate_panel_order": {
            "top_left": "starless_linear_linked",
            "top_right": "starmask_linked",
            "bottom_left": "starmask_unlinked",
            "bottom_right": "unscreen_linked",
        },
        "read_target_policy": {
            "use": "OpenClaw Read",
            "path_handling": "verbatim",
            "directory_discovery_forbidden": True,
            "source_code_discovery_forbidden": True,
            "media_attachment_forbidden": True,
            "copying_or_listing_files_does_not_count_as_review": True,
            "on_read_failure": "stop_and_report_exact_failed_path",
        },
        "autonomous_completion_policy": {
            "user_candidate_selection_forbidden": True,
            "ask_user_after_processing": False,
            "attach_panels_to_user": False,
            "list_panel_directory": False,
            "continue_after_process_exit_zero": True,
            "read_every_exact_target": True,
            "select_candidate_autonomously": True,
            "publish_autonomously": True,
            "format_repair_retries_without_reprocessing": 3,
            "stop_only_on": [
                "published_ready",
                "exact_read_failure",
                "real_processing_or_contract_blocker",
            ],
        },
        "review_criteria": [
            "remaining stars in the starless image",
            "broad recognizable M16 nebulosity in either starmask view",
            "nebular knots/filaments removed or holes cut into the starless image",
            "halos, seams or other artifacts",
            "whether the unscreen star layer remains plausible and localized",
        ],
        "review_evidence_policy": {
            "recommendation_is_tiebreaker_only": True,
            "candidate_specific_observation_required": True,
            "generic_recommended_default_only_reasoning_forbidden": True,
            "compare_visible_candidate_differences_first": True,
        },
        "note_format": (
            "candidate-NN=accepted:<true|false>; "
            "remaining_stars:<none|minor|significant>; "
            "broad_nebula:<true|false>; "
            "nebula_damage:<none|minor|significant>; "
            "halos:<none|minor|significant>; "
            "observation:<at least 80 characters of candidate-specific visual comparison>"
        ),
        "note_parser": {
            "observation_minimum_characters": 80,
            "semicolons_inside_field_values_supported": True,
            "candidate_notes_required": len(state["generated_candidates"]),
            "severity_enum": ["none", "minor", "significant"],
            "severity_fields": ["remaining_stars", "nebula_damage", "halos"],
            "identical_candidate_observations_forbidden": True,
        },
        "review_publish_command_template": {
            "command": (
                "/home/peter/.openclaw/workspace/agents/codewarrior/skills/"
                "siril-starnet-removal/bin/starnet-removal review-publish"
            ),
            "required": [
                '--project "<project>"',
                '--candidate "<selected candidate>"',
                '--selection-rationale "<specific overall visual comparison; at least 80 characters>"',
                *[f'--note "{x}"' for x in note_examples],
            ],
        },
        "canonical_output_changed": False,
        "sho_channel_balance_permitted": False,
    }


def advance(project_name: str) -> dict[str, Any]:
    p = validate_project(project_name)
    active = load_active(project_name)
    if active:
        return review_plan(active[1])

    # Manifest-first fast path: no large FITS hashing before confirmation.
    snap_fast = canonical_snapshot(project_name, verify_hashes=False)
    if snap_fast.get("exists"):
        if not authorized_intent_may_exist(p):
            return {
                "status": "confirmation_required",
                "action": "await_user_confirmation",
                "orchestration_version": VERSION,
                "processing_engine_version": ENGINE_VERSION,
                "project": str(p["project"]),
                "question": (
                    f"StarNet removal for {project_name} has already completed. "
                    "Do you want me to run it again as a fresh run?"
                ),
                "confirmation_required": True,
                "current_canonical_status": snap_fast.get("status"),
                "canonical_manifest_sha256": snap_fast.get("manifest_sha256"),
                "canonical_output_preserved": True,
                "hashes_verified": False,
                "hash_verification_deferred_until": "confirm-fresh",
            }

        # An authorization may exist. Only now pay the cost of strong hashes,
        # then require an exact match before consuming it.
        snap_verified = canonical_snapshot(project_name, verify_hashes=True)
        intent = locate_intent(p, snap_verified)
        if intent is None:
            return {
                "status": "confirmation_required",
                "action": "await_user_confirmation",
                "orchestration_version": VERSION,
                "processing_engine_version": ENGINE_VERSION,
                "project": str(p["project"]),
                "question": (
                    f"StarNet removal for {project_name} has already completed. "
                    "Do you want me to run it again as a fresh run?"
                ),
                "confirmation_required": True,
                "current_canonical_status": snap_fast.get("status"),
                "canonical_manifest_sha256": snap_fast.get("manifest_sha256"),
                "canonical_output_preserved": True,
                "hashes_verified": True,
                "authorization_match": False,
            }
        run = run_engine(["run", "--project", project_name, "--fresh-run"])
        return review_plan(build_state(project_name, run, intent))

    # First-time processing is allowed without fresh confirmation.
    run = run_engine(["run", "--project", project_name])
    p["intents"].mkdir(parents=True, exist_ok=True)
    marker = p["intents"] / f"initial-run-{uid()}.json"
    write_json_atomic(marker, {
        "status": "consumed",
        "project_name": project_name,
        "orchestration_version": VERSION,
        "initial_run": True,
        "run_root": run.get("run_root"),
    })
    return review_plan(build_state(project_name, run, marker))


def parse_notes(values: list[str], expected_candidates: list[str]):
    import re

    expected = set(expected_candidates)
    result = {}
    ordered_keys = (
        "accepted",
        "remaining_stars",
        "broad_nebula",
        "nebula_damage",
        "halos",
        "observation",
    )
    required = set(ordered_keys)
    severity_values = {"none", "minor", "significant"}
    field_boundary = re.compile(
        r";\s*(?=(?:accepted|remaining_stars|broad_nebula|"
        r"nebula_damage|halos|observation)\s*:)",
        flags=re.IGNORECASE,
    )

    for raw in values:
        if "=" not in raw:
            raise StarNetOrchestrationError("Each --note must start with candidate-NN=...")
        name, body = raw.split("=", 1)
        name = name.strip()
        if name not in expected:
            raise StarNetOrchestrationError(f"Unexpected candidate in --note: {name!r}")

        fields = {}
        for piece in [x.strip() for x in field_boundary.split(body) if x.strip()]:
            if ":" not in piece:
                raise StarNetOrchestrationError(f"{name} has an unlabeled review field.")
            key, value = piece.split(":", 1)
            key = key.strip().lower()
            if key not in required:
                raise StarNetOrchestrationError(f"{name} contains unknown review field {key!r}.")
            if key in fields:
                raise StarNetOrchestrationError(f"{name} repeats review field {key!r}.")
            fields[key] = " ".join(value.split())

        if set(fields) != required:
            raise StarNetOrchestrationError(
                f"{name} must contain exactly {list(ordered_keys)}; got {sorted(fields)}"
            )

        accepted_text = fields["accepted"].lower()
        broad_text = fields["broad_nebula"].lower()
        if accepted_text not in {"true", "false"}:
            raise StarNetOrchestrationError(f"{name} accepted must be true or false.")
        if broad_text not in {"true", "false"}:
            raise StarNetOrchestrationError(f"{name} broad_nebula must be true or false.")

        for key in ("remaining_stars", "nebula_damage", "halos"):
            value = fields[key].lower()
            if value not in severity_values:
                raise StarNetOrchestrationError(
                    f"{name} {key} must be exactly one of none, minor, or significant."
                )
            fields[key] = value

        if len(fields["observation"]) < 80:
            raise StarNetOrchestrationError(
                f"{name} observation must contain at least 80 characters."
            )

        result[name] = {
            "accepted": accepted_text == "true",
            "remaining_stars_in_starless": fields["remaining_stars"],
            "broad_nebula_in_starmask": broad_text == "true",
            "nebula_damage": fields["nebula_damage"],
            "halos_or_artifacts": fields["halos"],
            "observations": fields["observation"],
        }

    if set(result) != expected:
        raise StarNetOrchestrationError(
            "Review notes must cover every generated candidate exactly. "
            f"Expected {sorted(expected)}, got {sorted(result)}."
        )

    observations = [result[name]["observations"] for name in sorted(result)]
    if len(observations) > 1 and len(set(observations)) == 1:
        raise StarNetOrchestrationError(
            "Candidate observations must be candidate-specific; the same observation cannot be reused for every candidate."
        )

    return result


def review_publish(project_name: str, candidate: str, selection_rationale: str, note_values: list[str]) -> dict[str, Any]:
    active = load_active(project_name)
    if active is None:
        raise StarNetOrchestrationError("No active StarNet run is awaiting visual review.")
    p, state = active
    if candidate not in state.get("satisfactory_candidates", []):
        raise StarNetOrchestrationError(
            f"Selected candidate {candidate!r} is not technically satisfactory."
        )
    if len(" ".join(selection_rationale.split())) < 80:
        raise StarNetOrchestrationError("Selection rationale must contain at least 80 characters.")

    notes = parse_notes(note_values, state["generated_candidates"])
    accepted = [name for name, note in notes.items() if note["accepted"]]
    if accepted != [candidate]:
        raise StarNetOrchestrationError(
            f"Exactly the selected candidate must have accepted:true. "
            f"Selected={candidate!r}, accepted={accepted!r}"
        )

    # Verify that exact Read evidence targets have not changed.
    source = Path(state["source_preview"]["path"])
    if not source.is_file() or sha256_file(source) != state["source_preview"]["sha256"]:
        raise StarNetOrchestrationError("Source review target changed or disappeared.")
    for name in state["generated_candidates"]:
        panel = state["candidates"][name]["contact_panel"]
        path = Path(panel["path"])
        if not path.is_file() or sha256_file(path) != panel["sha256"]:
            raise StarNetOrchestrationError(f"{name} contact panel changed or disappeared.")

    template = load_json(Path(state["visual_review_template"]))
    template["reviewed_at"] = utc_now()
    template["reviewer"] = "CodeWarrior"
    template["selected_candidate"] = candidate
    template["selection_rationale"] = " ".join(selection_rationale.split())
    if not isinstance(template.get("source_preview"), dict):
        raise StarNetOrchestrationError("Visual-review template source_preview is malformed.")
    template["source_preview"]["inspected"] = True

    by_name = {str(x.get("candidate")): x for x in template.get("candidates", []) if isinstance(x, dict)}
    if set(by_name) != set(state["generated_candidates"]):
        raise StarNetOrchestrationError("Visual-review template candidate set changed.")
    for name in state["generated_candidates"]:
        row = by_name[name]
        note = notes[name]
        row["accepted"] = note["accepted"]
        row["remaining_stars_in_starless"] = note["remaining_stars_in_starless"]
        row["broad_nebula_in_starmask"] = note["broad_nebula_in_starmask"]
        row["nebula_damage"] = note["nebula_damage"]
        row["halos_or_artifacts"] = note["halos_or_artifacts"]
        row["observations"] = note["observations"]
        previews = row.get("previews", {})
        for key in PANEL_ORDER:
            if key not in previews or not isinstance(previews[key], dict):
                raise StarNetOrchestrationError(f"{name} template preview {key} is missing.")
            previews[key]["inspected"] = True

    completed_review = Path(state["run_root"]) / "compact-review" / "visual-review-v1.5.6-completed.json"
    write_json_atomic(completed_review, template)

    recorded = run_engine([
        "record-review",
        "--project", project_name,
        "--run-root", state["run_root"],
        "--review-json", str(completed_review),
    ])
    if recorded.get("status") != "visual_review_recorded":
        raise StarNetOrchestrationError(f"StarNet engine did not validate visual review: {recorded}")
    review_record = Path(str(recorded.get("visual_review_record", "")))
    if not review_record.is_file():
        raise StarNetOrchestrationError("Validated StarNet review record is missing.")

    published = run_engine([
        "publish",
        "--project", project_name,
        "--run-root", state["run_root"],
        "--review-record", str(review_record),
        "--fresh-run",
    ])
    if published.get("status") != "ready":
        raise StarNetOrchestrationError(f"StarNet publication did not become ready: {published}")

    manifest = load_json(p["manifest"])
    contract_errors = []
    if manifest.get("source_contract_revision") != SOURCE_CONTRACT_REVISION:
        contract_errors.append("wrong source_contract_revision")
    if manifest.get("next_stage") != "siril-sho-channel-balance":
        contract_errors.append("wrong next_stage")
    if manifest.get("sho_channel_balance_permitted") is not True:
        contract_errors.append("SHO channel balance not permitted")
    if manifest.get("ghs_pass1_permitted") is not False:
        contract_errors.append("direct GHS pass1 incorrectly permitted")
    if manifest.get("stage_order") != {
        "upstream": "siril-background-neutralization",
        "current": "siril-starnet-removal",
        "downstream": "siril-sho-channel-balance",
    }:
        contract_errors.append("wrong native stage_order")
    if contract_errors:
        raise StarNetOrchestrationError("Published StarNet native contract failed: " + "; ".join(contract_errors))

    orchestration_review = {
        "schema_version": 1,
        "orchestration_version": VERSION,
        "processing_engine_version": ENGINE_VERSION,
        "recorded_at": utc_now(),
        "project_name": project_name,
        "run_root": state["run_root"],
        "review_method": "openclaw-read-contact-panels",
        "copying_files_counts_as_review": False,
        "source_preview": state["source_preview"],
        "candidate_panel_order": list(PANEL_ORDER),
        "candidate_panels": {
            name: state["candidates"][name]["contact_panel"]
            for name in state["generated_candidates"]
        },
        "candidate_notes": notes,
        "recommended_candidate": state.get("recommended_candidate"),
        "selected_candidate": candidate,
        "selection_rationale": " ".join(selection_rationale.split()),
        "engine_visual_review_record": str(review_record),
        "engine_visual_review_record_sha256": sha256_file(review_record),
        "canonical_manifest_sha256": sha256_file(p["manifest"]),
        "canonical_starless_sha256": sha256_file(p["starless"]),
        "canonical_starmask_sha256": sha256_file(p["starmask"]),
        "canonical_unscreen_sha256": sha256_file(p["unscreen"]),
        "source_contract_revision": SOURCE_CONTRACT_REVISION,
        "next_stage": "siril-sho-channel-balance",
        "sho_channel_balance_permitted": True,
        "ghs_pass1_permitted": False,
        "visual_review_completed": True,
    }
    write_json_atomic(p["orchestration_review"], orchestration_review)

    state["status"] = "published"
    state["published_at"] = utc_now()
    state["selected_candidate"] = candidate
    state["canonical_output_changed"] = True
    p["completed"].mkdir(parents=True, exist_ok=True)
    completed = p["completed"] / f"completed-{uid()}.json"
    write_json_atomic(completed, state)
    consumed = p["completed"] / f"active-consumed-{uid()}.json"
    os.replace(p["active"], consumed)

    snap = canonical_snapshot(project_name, verify_hashes=True)
    if snap.get("status") != "ready":
        raise StarNetOrchestrationError(f"Post-publication canonical verification failed: {snap}")

    return {
        "status": "ready",
        "orchestration_version": VERSION,
        "processing_engine_version": ENGINE_VERSION,
        "project": str(p["project"]),
        "run_root": state["run_root"],
        "selected_candidate": candidate,
        "recommended_candidate": state.get("recommended_candidate"),
        "visual_review_completed": True,
        "review_method": "openclaw-read-contact-panels",
        "orchestration_review_record": str(p["orchestration_review"]),
        "canonical_starless_sha256": snap["starless_sha256"],
        "canonical_starmask_sha256": snap["starmask_sha256"],
        "canonical_unscreen_sha256": snap["unscreen_sha256"],
        "source_contract_revision": SOURCE_CONTRACT_REVISION,
        "next_stage": "siril-sho-channel-balance",
        "sho_channel_balance_permitted": True,
        "ghs_pass1_permitted": False,
        "canonical_output_changed": True,
    }


def stage_status(project_name: str) -> dict[str, Any]:
    p = validate_project(project_name)
    active = load_active(project_name)
    if active:
        s = active[1]
        return {
            "status": "visual_review_required",
            "orchestration_version": VERSION,
            "processing_engine_version": ENGINE_VERSION,
            "project": str(p["project"]),
            "run_root": s["run_root"],
            "generated_candidates": s["generated_candidates"],
            "sho_channel_balance_permitted": False,
        }
    snap = canonical_snapshot(project_name, verify_hashes=False)
    return {
        "status": snap.get("status"),
        "orchestration_version": VERSION,
        "processing_engine_version": ENGINE_VERSION,
        "project": str(p["project"]),
        "canonical_exists": snap.get("exists"),
        "native_contract_valid": snap.get("native_contract_valid", False),
        "canonical_manifest_sha256": snap.get("manifest_sha256"),
        "hashes_verified": False,
        "hash_verification_deferred_until": "confirm-fresh",
        "source_contract_revision": SOURCE_CONTRACT_REVISION,
        "sho_channel_balance_permitted": bool(snap.get("status") == "ready"),
        "ghs_pass1_permitted": False,
    }


def self_test() -> dict[str, Any]:
    import numpy as np
    global PROJECTS_ROOT

    root = Path("/tmp") / f"starnet-orchestrator-self-test-{uid()}"
    root.mkdir(parents=True, exist_ok=False)
    imgs = []
    for idx in range(4):
        arr = np.zeros((32, 48, 3), dtype=np.uint8)
        arr[:, :, idx % 3] = 80 + idx * 40
        arr[8:24, 12:36, :] = 180
        p = root / f"in-{idx}.png"
        write_png_rgb8(p, arr)
        reread = read_png_rgb8(p)
        if reread.shape != arr.shape:
            raise StarNetOrchestrationError("PNG roundtrip shape self-test failed.")
        imgs.append(p)
    panel = make_contact_sheet(imgs, root / "panel.png")
    if not Path(panel["path"]).is_file():
        raise StarNetOrchestrationError("Contact-panel self-test did not produce output.")

    notes = parse_notes([
        "candidate-00=accepted:true; remaining_stars:none; broad_nebula:false; "
        "nebula_damage:none; halos:none; "
        "observation:Starless structure is preserved and the mask contains localized stellar signal; the M16 nebulosity remains intact without obvious holes or broad leakage.",
        "candidate-01=accepted:false; remaining_stars:minor; broad_nebula:false; "
        "nebula_damage:none; halos:none; "
        "observation:Several faint stellar residuals remain compared with candidate-00; the nebular structure otherwise remains intact without obvious holes, seams, or broad mask leakage.",
    ], ["candidate-00", "candidate-01"])
    if not notes["candidate-00"]["accepted"] or notes["candidate-01"]["accepted"]:
        raise StarNetOrchestrationError("Structured StarNet review-note self-test failed.")

    semicolon_notes = parse_notes([
        "candidate-00=accepted:true; remaining_stars:none; broad_nebula:false; "
        "nebula_damage:none; halos:none; "
        "observation:The starless result is clean and preserves the Pillars; "
        "this semicolon remains part of the observation and must not be parsed "
        "as an unlabeled review field."
    ], ["candidate-00"])
    if not semicolon_notes["candidate-00"]["accepted"]:
        raise StarNetOrchestrationError(
            "Semicolon-safe StarNet review-note self-test failed."
        )

    # v1.5.6 enum alignment: catch descriptive prose before the v1.5.2 engine.
    try:
        parse_notes([
            "candidate-00=accepted:true; remaining_stars:none visible; broad_nebula:false; "
            "nebula_damage:no obvious damage; halos:no significant halos; "
            "observation:This deliberately uses descriptive severity values that the "
            "underlying v1.5.2 engine rejects and must be blocked by v1.5.6 first."
        ], ["candidate-00"])
    except StarNetOrchestrationError as exc:
        if "must be exactly one of none, minor, or significant" not in str(exc):
            raise
    else:
        raise StarNetOrchestrationError("Severity enum self-test failed to reject descriptive values.")

    duplicate_observation = (
        "This candidate preserves the main M16 structure and appears satisfactory; "
        "this deliberately duplicated visual note must be rejected when copied "
        "unchanged to every candidate."
    )
    try:
        parse_notes([
            "candidate-00=accepted:true; remaining_stars:none; broad_nebula:false; "
            f"nebula_damage:none; halos:none; observation:{duplicate_observation}",
            "candidate-01=accepted:false; remaining_stars:none; broad_nebula:false; "
            f"nebula_damage:none; halos:none; observation:{duplicate_observation}",
        ], ["candidate-00", "candidate-01"])
    except StarNetOrchestrationError as exc:
        if "candidate-specific" not in str(exc):
            raise
    else:
        raise StarNetOrchestrationError("Candidate-specific observation self-test failed.")

    # Verify manifest-first status versus strong confirm-fresh hashing.
    original_projects_root = PROJECTS_ROOT
    synthetic_root = root / "Projects"
    PROJECTS_ROOT = synthetic_root
    project_name = "Synthetic Fast Path"
    project = synthetic_root / project_name
    source = project / SOURCE_REL
    canonical = project / CANONICAL_REL
    canonical.mkdir(parents=True, exist_ok=True)
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"synthetic-source")
    (canonical / "SHO-starless-linear.fit").write_bytes(b"synthetic-starless")
    (canonical / "SHO-starmask.fit").write_bytes(b"synthetic-mask")
    (canonical / "SHO-stars-unscreen.fit").write_bytes(b"synthetic-unscreen")
    (canonical / "visual-review-record.json").write_text("{}\n", encoding="utf-8")
    write_json_atomic(canonical / "starnet-manifest.json", {
        "status": "ready",
        "helper_version": ENGINE_VERSION,
        "source_contract_revision": SOURCE_CONTRACT_REVISION,
        "next_stage": "siril-sho-channel-balance",
        "sho_channel_balance_permitted": True,
        "ghs_pass1_permitted": False,
        "stage_order": {
            "upstream": "siril-background-neutralization",
            "current": "siril-starnet-removal",
            "downstream": "siril-sho-channel-balance",
        },
    })
    try:
        fast = canonical_snapshot(project_name, verify_hashes=False)
        if fast.get("hashes_verified") is not False:
            raise StarNetOrchestrationError("Fast snapshot unexpectedly verified large hashes.")
        for forbidden in ("starless_sha256", "starmask_sha256", "unscreen_sha256", "source_sha256"):
            if forbidden in fast:
                raise StarNetOrchestrationError(
                    f"Fast snapshot unexpectedly produced {forbidden}."
                )
        strong = canonical_snapshot(project_name, verify_hashes=True)
        if strong.get("hashes_verified") is not True:
            raise StarNetOrchestrationError("Strong snapshot did not verify hashes.")
        for required in ("starless_sha256", "starmask_sha256", "unscreen_sha256", "source_sha256"):
            if required not in strong:
                raise StarNetOrchestrationError(
                    f"Strong snapshot omitted {required}."
                )
        confirmed = confirm_fresh(project_name)
        if confirmed.get("status") != "fresh_run_confirmed":
            raise StarNetOrchestrationError("Synthetic confirm-fresh self-test failed.")
        p = paths_for(project_name)
        if not authorized_intent_may_exist(p):
            raise StarNetOrchestrationError("Synthetic durable authorization was not visible.")
        matched = locate_intent(p, strong)
        if matched is None:
            raise StarNetOrchestrationError("Synthetic durable authorization did not bind to strong hashes.")
    finally:
        PROJECTS_ROOT = original_projects_root

    return {
        "status": "success",
        "orchestration_version": VERSION,
        "processing_engine_version": ENGINE_VERSION,
        "contact_panel_generation": True,
        "panel_order": list(PANEL_ORDER),
        "structured_candidate_notes": True,
        "completed_stage_requires_confirmation": True,
        "exact_read_targets_required": True,
        "source_code_discovery_forbidden": True,
        "manifest_first_fast_path": True,
        "pre_confirmation_large_fits_hashing": False,
        "confirm_fresh_strong_hash_binding": True,
        "autonomous_completion_after_processing": True,
        "user_candidate_selection_forbidden": True,
        "media_attachment_forbidden": True,
        "directory_listing_after_advance_forbidden": True,
        "observation_minimum_characters": 80,
        "semicolons_inside_observation_supported": True,
        "severity_enum_validation": True,
        "severity_enum_values": ["none", "minor", "significant"],
        "generic_identical_candidate_observations_forbidden": True,
        "recommendation_is_tiebreaker_only": True,
    }


def build_parser():
    ap = argparse.ArgumentParser(description="StarNet v1.5.6 autonomous-completion orchestration wrapper.")
    ap.add_argument("--version", action="version", version=VERSION)
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("advance")
    p.add_argument("--project", required=True)
    p = sub.add_parser("confirm-fresh")
    p.add_argument("--project", required=True)
    p = sub.add_parser("review-publish")
    p.add_argument("--project", required=True)
    p.add_argument("--candidate", required=True)
    p.add_argument("--selection-rationale", required=True)
    p.add_argument("--note", action="append", default=[])
    p = sub.add_parser("stage-status")
    p.add_argument("--project", required=True)
    sub.add_parser("self-test")
    return ap


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "advance":
            payload = advance(args.project)
        elif args.command == "confirm-fresh":
            payload = confirm_fresh(args.project)
        elif args.command == "review-publish":
            payload = review_publish(args.project, args.candidate, args.selection_rationale, args.note)
        elif args.command == "stage-status":
            payload = stage_status(args.project)
        elif args.command == "self-test":
            payload = self_test()
        else:
            raise StarNetOrchestrationError(f"Unsupported command {args.command!r}")
    except StarNetOrchestrationError as exc:
        print(json.dumps({
            "status": "blocked",
            "orchestration_version": VERSION,
            "processing_engine_version": ENGINE_VERSION,
            "error": str(exc),
            "sho_channel_balance_permitted": False,
        }, indent=2, sort_keys=True))
        return 2
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
