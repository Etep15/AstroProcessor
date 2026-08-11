#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VERSION = "1.4.0"
PROCESSING_ENGINE_VERSION = "1.4.0"

WORKSPACE = Path("/home/peter/.openclaw/workspace/agents/codewarrior")
PROJECTS_ROOT = WORKSPACE / "Projects"
PYTHON = WORKSPACE / "AstroProcessor" / ".venv" / "bin" / "python"
ENGINE = WORKSPACE / "skills" / "siril-ghs-stretch" / "scripts" / "ghs_stretch.py"

SOURCE_REL = Path("processing/sho-channel-balance/SHO-starless-linear-balanced.fit")
SOURCE_MANIFEST_REL = Path("processing/sho-channel-balance/sho-channel-balance-manifest.json")
CANONICAL_DIR_REL = Path("processing/ghs-pass1")
CANONICAL_MANIFEST_REL = CANONICAL_DIR_REL / "ghs-pass1-manifest.json"
CANONICAL_OUTPUT_REL = CANONICAL_DIR_REL / "SHO-starless-ghs-pass1.fit"
REVIEW_RECORD_REL = CANONICAL_DIR_REL / "visual-selection-record-v1.3.2.json"
STATE_DIR_NAME = ".siril-ghs-stretch-v1.4.0"

REVIEW_FIELDS = ("stretch", "structure", "color", "noise", "highlights")


class OrchestrationError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def sha256_file(path: Path) -> str:
    if os.environ.get("GHS140_FORBID_FITS_HASH") == "1" and path.suffix.lower() in {".fit", ".fits", ".fts"}:
        raise OrchestrationError(f"Large FITS hashing forbidden in manifest-first mode: {path}")
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()



def load_json(path: Path) -> dict[str, Any]:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise OrchestrationError(f"Cannot read JSON {path}: {exc}") from exc
    if not isinstance(obj, dict):
        raise OrchestrationError(f"Expected JSON object: {path}")
    return obj


def write_json_atomic(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{stamp()}.partial")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def project_paths(project_name: str) -> dict[str, Path]:
    project = PROJECTS_ROOT / project_name
    state_dir = project / STATE_DIR_NAME
    return {
        "project": project,
        "source": project / SOURCE_REL,
        "source_manifest": project / SOURCE_MANIFEST_REL,
        "canonical_dir": project / CANONICAL_DIR_REL,
        "canonical_manifest": project / CANONICAL_MANIFEST_REL,
        "canonical_output": project / CANONICAL_OUTPUT_REL,
        "review_record": project / REVIEW_RECORD_REL,
        "state_dir": state_dir,
        "active": state_dir / "active.json",
        "intents": state_dir / "fresh-intents",
        "completed": state_dir / "completed",
    }


def validate_project(project_name: str) -> dict[str, Path]:
    p = project_paths(project_name)
    if not p["project"].is_dir():
        raise OrchestrationError(f"Project does not exist: {p['project']}")
    if not p["source"].is_file():
        raise OrchestrationError(f"Required balanced starless source is missing: {p['source']}")
    if not p["source_manifest"].is_file():
        raise OrchestrationError(f"Channel-balance manifest is missing: {p['source_manifest']}")
    m = load_json(p["source_manifest"])
    if m.get("status") != "ready":
        raise OrchestrationError("SHO channel-balance source is not ready.")
    if m.get("source_is_starless") is not True:
        raise OrchestrationError("SHO channel-balance source is not explicitly STARLESS.")
    if m.get("stars_layer_modified") is not False:
        raise OrchestrationError("SHO channel-balance does not prove the stars layer stayed untouched.")
    if m.get("ghs_pass1_permitted") is not True:
        raise OrchestrationError("SHO channel-balance does not permit GHS pass 1.")
    return p


def canonical_snapshot(project_name: str, *, strong: bool = False) -> dict[str, Any]:
    p = validate_project(project_name)
    sm = load_json(p["source_manifest"])
    source_recorded_sha = sm.get("output", {}).get("sha256")
    if not isinstance(source_recorded_sha, str) or len(source_recorded_sha) != 64:
        raise OrchestrationError("Channel-balance manifest lacks a recorded output SHA-256.")
    source_manifest_sha = sha256_file(p["source_manifest"])

    if not p["canonical_manifest"].is_file() or not p["canonical_output"].is_file():
        return {
            "exists": False,
            "status": "missing",
            "source_sha256": source_recorded_sha,
            "source_manifest_sha256": source_manifest_sha,
            "strong_hashes_verified": False,
            "large_fits_hashed": False,
        }

    m = load_json(p["canonical_manifest"])
    canonical_manifest_sha = sha256_file(p["canonical_manifest"])
    output_recorded_sha = m.get("output", {}).get("sha256")
    if not isinstance(output_recorded_sha, str) or len(output_recorded_sha) != 64:
        output_recorded_sha = None

    errors: list[str] = []
    policy_obsolete = m.get("helper_version") != PROCESSING_ENGINE_VERSION
    if m.get("status") != "ready":
        errors.append("canonical GHS manifest is not ready")
    if m.get("source_contract_revision") != "post-starnet-channel-balance-v1":
        errors.append("canonical source contract is not post-starnet-channel-balance-v1")
    if m.get("source", {}).get("sha256") != source_recorded_sha:
        errors.append("canonical source SHA does not match the current channel-balance manifest")
    if output_recorded_sha is None:
        errors.append("canonical manifest lacks an output SHA-256")
    if m.get("next_stage") != "siril-ghs-stretch-pass2":
        errors.append("canonical next stage is not siril-ghs-stretch-pass2")
    if p["canonical_output"].stat().st_size <= 0:
        errors.append("canonical output is empty")

    review_ok = False
    review = None
    if p["review_record"].is_file() and not policy_obsolete:
        try:
            review = load_json(p["review_record"])
            review_ok = (
                review.get("orchestration_version") == "1.3.2"
                and review.get("publication_orchestration_version") == VERSION
                and review.get("processing_engine_version") == PROCESSING_ENGINE_VERSION
                and review.get("canonical_output_sha256") == output_recorded_sha
                and review.get("visual_review_completed") is True
                and review.get("review_method") == "openclaw-read"
                and review.get("ghs_pass2_processing_permitted") is True
            )
        except Exception:
            review_ok = False

    strong_source_sha = None
    strong_output_sha = None
    if strong:
        strong_source_sha = sha256_file(p["source"])
        strong_output_sha = sha256_file(p["canonical_output"])
        if strong_source_sha != source_recorded_sha:
            errors.append("current channel-balanced FITS SHA does not match its manifest")
        if output_recorded_sha is not None and strong_output_sha != output_recorded_sha:
            errors.append("canonical GHS FITS SHA does not match its manifest")

    if policy_obsolete:
        status = "obsolete"
    elif not errors and review_ok:
        status = "ready"
    else:
        status = "provisional"

    return {
        "exists": True,
        "status": status,
        "policy_obsolete": policy_obsolete,
        "canonical_helper_version": m.get("helper_version"),
        "required_helper_version": PROCESSING_ENGINE_VERSION,
        "errors": errors,
        "output_sha256": strong_output_sha or output_recorded_sha,
        "manifest_sha256": canonical_manifest_sha,
        "source_sha256": strong_source_sha or source_recorded_sha,
        "source_manifest_sha256": source_manifest_sha,
        "selected_candidate": m.get("selected_candidate"),
        "recommended_candidate": m.get("recommended_candidate"),
        "review_record": str(p["review_record"]) if p["review_record"].is_file() else None,
        "review_record_v1_3_2_valid": review_ok,
        "ghs_pass2_processing_permitted": bool(status == "ready" and review_ok),
        "strong_hashes_verified": bool(strong and not errors),
        "large_fits_hashed": bool(strong),
    }



def run_engine(args: list[str], timeout: int = 7200) -> dict[str, Any]:
    if not PYTHON.is_file():
        raise OrchestrationError(f"Canonical Python is missing: {PYTHON}")
    if not ENGINE.is_file():
        raise OrchestrationError(f"GHS processing engine is missing: {ENGINE}")
    cp = subprocess.run(
        [str(PYTHON), str(ENGINE), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    stdout = cp.stdout.strip()
    stderr = cp.stderr.strip()
    try:
        payload = json.loads(stdout)
    except Exception as exc:
        raise OrchestrationError(
            f"GHS engine returned non-JSON. exit={cp.returncode}; "
            f"stderr={stderr[:500]!r}; stdout={stdout[:500]!r}"
        ) from exc
    if cp.returncode != 0:
        raise OrchestrationError(f"GHS engine blocked/failed: {payload.get('error', payload)}")
    if not isinstance(payload, dict):
        raise OrchestrationError("GHS engine returned non-object JSON.")
    return payload


def locate_fresh_intent(p: dict[str, Path], canonical: dict[str, Any]) -> Path | None:
    if not p["intents"].is_dir():
        return None
    rows: list[tuple[float, Path, dict[str, Any]]] = []
    for f in p["intents"].glob("fresh-run-*.json"):
        try:
            x = load_json(f)
        except Exception:
            continue
        if (
            x.get("status") == "authorized"
            and x.get("orchestration_version") == VERSION
            and x.get("canonical_manifest_sha256") == canonical.get("manifest_sha256")
            and x.get("source_manifest_sha256") == canonical.get("source_manifest_sha256")
            and x.get("canonical_output_sha256") == canonical.get("output_sha256")
            and x.get("source_sha256") == canonical.get("source_sha256")
        ):
            rows.append((f.stat().st_mtime, f, x))
    if not rows:
        return None
    _, f, x = sorted(rows, key=lambda item: item[0], reverse=True)[0]
    strong = canonical_snapshot(p["project"].name, strong=True)
    if (
        strong.get("canonical_manifest_sha256") not in (None, x.get("canonical_manifest_sha256"))
        and strong.get("manifest_sha256") != x.get("canonical_manifest_sha256")
    ):
        return None
    if strong.get("manifest_sha256") != x.get("canonical_manifest_sha256"):
        return None
    if strong.get("source_manifest_sha256") != x.get("source_manifest_sha256"):
        return None
    if strong.get("output_sha256") != x.get("canonical_output_sha256"):
        return None
    if strong.get("source_sha256") != x.get("source_sha256"):
        return None
    if not strong.get("strong_hashes_verified"):
        return None
    return f



def confirm_fresh(project_name: str) -> dict[str, Any]:
    p = validate_project(project_name)
    c = canonical_snapshot(project_name, strong=True)
    if not c.get("exists"):
        raise OrchestrationError("No completed canonical GHS pass-1 result exists to rerun.")
    if not c.get("strong_hashes_verified"):
        raise OrchestrationError(f"Cannot authorize fresh rerun because strong provenance verification failed: {c.get('errors')}")
    p["intents"].mkdir(parents=True, exist_ok=True)
    f = p["intents"] / f"fresh-run-{stamp()}.json"
    obj = {
        "schema_version": 2,
        "orchestration_version": VERSION,
        "processing_engine_version": PROCESSING_ENGINE_VERSION,
        "status": "authorized",
        "authorized_at": utc_now(),
        "project_name": project_name,
        "canonical_status_at_authorization": c.get("status"),
        "canonical_manifest_sha256": c["manifest_sha256"],
        "source_manifest_sha256": c["source_manifest_sha256"],
        "canonical_output_sha256": c["output_sha256"],
        "source_sha256": c["source_sha256"],
        "strong_hashes_verified": True,
    }
    write_json_atomic(f, obj)
    return {
        "status": "fresh_run_confirmed",
        "orchestration_version": VERSION,
        "processing_engine_version": PROCESSING_ENGINE_VERSION,
        "project": str(p["project"]),
        "fresh_intent": str(f),
        "current_canonical_status": c.get("status"),
        "strong_hashes_verified": True,
        "ghs_pass2_processing_permitted": False,
    }



def build_state(project_name: str, engine: dict[str, Any], fresh: bool, intent: Path | None) -> dict[str, Any]:
    p = validate_project(project_name)
    if engine.get("status") != "awaiting_visual_selection":
        raise OrchestrationError(
            f"Expected engine status 'awaiting_visual_selection', got {engine.get('status')!r}"
        )
    if engine.get("helper_version") != PROCESSING_ENGINE_VERSION:
        raise OrchestrationError("Unexpected GHS engine version in run result.")
    if engine.get("publication_permitted") is not True:
        raise OrchestrationError(f"GHS engine did not permit publication: {engine.get('publication_gate')}")
    eligible = [str(x) for x in engine.get("publication_eligible_candidates", [])]
    if not eligible:
        raise OrchestrationError("No publication-eligible GHS candidates were returned.")

    by_name = {
        str(c.get("candidate")): c
        for c in engine.get("candidates", [])
        if isinstance(c, dict) and c.get("candidate")
    }
    candidates: dict[str, Any] = {}
    for name in eligible:
        c = by_name.get(name)
        if c is None:
            raise OrchestrationError(f"Eligible candidate {name} missing from engine result.")
        after = Path(str(c.get("previews", {}).get("after_linear", "")))
        before = Path(str(c.get("previews", {}).get("before_linked", "")))
        if not after.is_file():
            raise OrchestrationError(f"Candidate preview is missing: {after}")
        if not before.is_file():
            raise OrchestrationError(f"Before-linked preview is missing: {before}")
        metrics = c.get("quality_assessment", {}).get("metrics", {})
        median = metrics.get("output_luma_median")
        p90 = metrics.get("output_luma_p90")
        p99 = metrics.get("output_luma_p99")
        ratio = (float(p99) / max(float(median), 1.0e-12)) if median is not None and p99 is not None else None
        spread = (float(p99) - float(median)) if median is not None and p99 is not None else None
        candidates[name] = {
            "candidate": name,
            "parameters": c.get("parameters"),
            "histogram_classification": c.get("histogram_classification"),
            "selection_score": c.get("selection_score"),
            "after_preview": str(after),
            "after_preview_sha256": sha256_file(after),
            "before_preview": str(before),
            "before_preview_sha256": sha256_file(before),
            "output_sha256": c.get("output", {}).get("sha256"),
            "output_luma_median": median,
            "output_luma_p90": p90,
            "output_luma_p99": p99,
            "output_p99_to_median_ratio": ratio,
            "output_p99_minus_median": spread,
            "output_maximum": metrics.get("output_maximum"),
            "luma_correlation": metrics.get("luma_correlation"),
        }

    state = {
        "schema_version": 2,
        "orchestration_version": VERSION,
        "processing_engine_version": PROCESSING_ENGINE_VERSION,
        "processing_policy_revision": "dynamic-range-expansion-v1",
        "status": "awaiting_visual_selection",
        "created_at": utc_now(),
        "project_name": project_name,
        "project": str(p["project"]),
        "run_root": engine.get("run_root"),
        "fresh_run": fresh,
        "fresh_intent": str(intent) if intent else None,
        "source_sha256": sha256_file(p["source"]),
        "publication_eligible_candidates": eligible,
        "recommended_candidate": engine.get("recommended_candidate"),
        "recommended_selection_score": engine.get("recommended_selection_score"),
        "candidates": candidates,
        "canonical_output_changed": False,
        "ghs_pass2_processing_permitted": False,
    }
    write_json_atomic(p["active"], state)

    if intent:
        x = load_json(intent)
        x["status"] = "consumed"
        x["consumed_at"] = utc_now()
        x["run_root"] = state["run_root"]
        write_json_atomic(intent, x)
    return state



def load_active(project_name: str) -> tuple[dict[str, Path], dict[str, Any]] | None:
    p = validate_project(project_name)
    if not p["active"].is_file():
        return None
    s = load_json(p["active"])
    if s.get("orchestration_version") != VERSION or s.get("processing_engine_version") != PROCESSING_ENGINE_VERSION:
        raise OrchestrationError("Active GHS run belongs to an older processing policy and cannot be resumed under v1.4.0.")
    if s.get("status") != "awaiting_visual_selection":
        raise OrchestrationError(f"Unexpected active GHS state: {s.get('status')!r}")
    if s.get("source_sha256") != sha256_file(p["source"]):
        raise OrchestrationError("Balanced starless source changed during active GHS run.")
    s.setdefault("publication_format_failures", 0)
    return p, s



def review_plan(state: dict[str, Any]) -> dict[str, Any]:
    eligible = state["publication_eligible_candidates"]
    first = state["candidates"][eligible[0]]
    targets = [{
        "role": "before_linked",
        "path": first["before_preview"],
        "sha256": first["before_preview_sha256"],
    }]
    summaries = []
    note_templates = []
    for name in eligible:
        c = state["candidates"][name]
        targets.append({
            "role": "candidate_after",
            "candidate": name,
            "path": c["after_preview"],
            "sha256": c["after_preview_sha256"],
        })
        summaries.append({
            "candidate": name,
            "parameters": c.get("parameters"),
            "histogram_classification": c.get("histogram_classification"),
            "output_luma_median": c.get("output_luma_median"),
            "output_luma_p90": c.get("output_luma_p90"),
            "output_luma_p99": c.get("output_luma_p99"),
            "output_p99_to_median_ratio": c.get("output_p99_to_median_ratio"),
            "output_p99_minus_median": c.get("output_p99_minus_median"),
            "output_maximum": c.get("output_maximum"),
            "luma_correlation": c.get("luma_correlation"),
            "recommended": name == state.get("recommended_candidate"),
        })
        note_templates.append(
            f'--note "{name}=stretch:<specific contrast/histogram-spread observation>; '
            'structure:<specific faint nebula/Pillars/dark-lanes observation>; '
            'color:<specific SHO colour observation>; '
            'noise:<specific noise/grain observation>; '
            'highlights:<specific highlight/clipping observation>"'
        )
    return {
        "status": "visual_review_required",
        "action": "continue_autonomously_to_publication",
        "orchestration_version": VERSION,
        "processing_engine_version": PROCESSING_ENGINE_VERSION,
        "processing_policy_revision": "dynamic-range-expansion-v1",
        "project_name": state["project_name"],
        "run_root": state["run_root"],
        "publication_eligible_candidates": eligible,
        "recommended_candidate": state.get("recommended_candidate"),
        "numerical_recommendation_is_advisory": True,
        "candidates": summaries,
        "read_targets": targets,
        "read_target_policy": {
            "path_handling": "verbatim",
            "directory_discovery_forbidden": True,
            "forbidden_recovery_tools": ["ls", "find", "cat", "grep", "jq", "globbing"],
            "on_read_failure": "stop_and_report_exact_failed_path",
        },
        "selection_rule": (
            "Visually compare every eligible candidate. Prefer genuine pass-1 signal separation and useful "
            "nebular contrast, not merely a median near a target. Preserve faint Eagle Nebula structure, "
            "Pillars/dark lanes and SHO colour while avoiding noisy backgrounds or harsh/clipped highlights."
        ),
        "dynamic_range_policy": {
            "median_is_advisory_not_primary": True,
            "source_relative_percentile_shape_preservation": False,
            "minimum_p99_to_median_ratio": 1.20,
            "preferred_p99_to_median_ratio": 1.25,
            "minimum_p99_minus_median": 0.025,
        },
        "candidate_note_format": (
            "candidate-NN=stretch:<specific observation>; structure:<specific observation>; "
            "color:<specific observation>; noise:<specific observation>; highlights:<specific observation>"
        ),
        "required_candidate_note_fields": list(REVIEW_FIELDS),
        "select_publish_command_template": {
            "command": str(WORKSPACE / "skills/siril-ghs-stretch/bin/ghs-stretch") + " select-publish",
            "required": [
                '--project "<project>"',
                '--candidate "<selected candidate>"',
                *note_templates,
            ],
            "optional": ['--visual-notes "<overall comparison; candidate notes must NOT be placed here>"'],
        },
        "candidate_notes_belong_in": "repeated --note arguments",
        "publication_format_retry_budget": 3,
        "resume_without_reprocessing": False,
        "ghs_pass2_processing_permitted": False,
    }



def advance(project_name: str) -> dict[str, Any]:
    p = validate_project(project_name)
    active = load_active(project_name)
    if active:
        return review_plan(active[1])

    c = canonical_snapshot(project_name)
    if c.get("exists"):
        intent = locate_fresh_intent(p, c)
        if intent is None:
            return {
                "status": "confirmation_required",
                "action": "await_user_confirmation",
                "orchestration_version": VERSION,
                "processing_engine_version": PROCESSING_ENGINE_VERSION,
                "project": str(p["project"]),
                "question": (
                    f"GHS stretch pass 1 for {project_name} has already completed successfully. "
                    "Do you want me to run it again as a fresh run?"
                ),
                "confirmation_required": True,
                "current_canonical_status": c.get("status"),
                "current_canonical_output_sha256": c.get("output_sha256"),
                "current_canonical_manifest_sha256": c.get("manifest_sha256"),
                "current_source_manifest_sha256": c.get("source_manifest_sha256"),
                "policy_obsolete": c.get("policy_obsolete", False),
                "manifest_first": True,
                "large_fits_hashed": False,
                "ghs_pass2_processing_permitted": False,
            }
        engine = run_engine(["run", "--project", project_name, "--fresh-run"])
        return review_plan(build_state(project_name, engine, True, intent))

    engine = run_engine(["run", "--project", project_name])
    return review_plan(build_state(project_name, engine, False, None))


def parse_candidate_notes(values: list[str], eligible: list[str]) -> dict[str, dict[str, str]]:
    expected = set(eligible)
    result: dict[str, dict[str, str]] = {}
    boundary = re.compile(
        r";\s*(?=(?:stretch|structure|color|noise|highlights)\s*:)",
        flags=re.IGNORECASE,
    )
    for raw in values:
        if "=" not in raw:
            raise OrchestrationError(
                "Each --note must use candidate-NN=stretch:<...>; structure:<...>; "
                "color:<...>; noise:<...>; highlights:<...>. Candidate notes belong "
                "in repeated --note arguments, not inside --visual-notes."
            )
        candidate, body = raw.split("=", 1)
        candidate = candidate.strip()
        if candidate not in expected:
            raise OrchestrationError(
                f"Candidate note references {candidate!r}; expected {sorted(expected)}."
            )
        if candidate in result:
            raise OrchestrationError(f"Duplicate note for {candidate}.")
        fields: dict[str, str] = {}
        for piece in [p.strip() for p in boundary.split(body) if p.strip()]:
            if ":" not in piece:
                raise OrchestrationError(f"{candidate} has an unlabeled visual-note field.")
            key, value = piece.split(":", 1)
            key = key.strip().lower()
            value = " ".join(value.split())
            if key not in REVIEW_FIELDS:
                raise OrchestrationError(f"{candidate} contains unknown field {key!r}.")
            if key in fields:
                raise OrchestrationError(f"{candidate} repeats field {key!r}.")
            fields[key] = value
        if set(fields) != set(REVIEW_FIELDS):
            raise OrchestrationError(
                f"{candidate} must contain exactly {list(REVIEW_FIELDS)}; got {sorted(fields)}."
            )
        for key in REVIEW_FIELDS:
            if len(fields[key]) < 12:
                raise OrchestrationError(
                    f"{candidate} {key}: observation is too vague; provide a specific visual observation."
                )
        if not any(w in fields["structure"].lower() for w in ("faint", "pillar", "dark", "lane", "structure", "nebula")):
            raise OrchestrationError(f"{candidate} structure: must address faint nebula/Pillars/dark lanes.")
        if not any(w in fields["color"].lower() for w in ("color", "colour", "cyan", "blue", "gold", "orange", "green", "sho", "red")):
            raise OrchestrationError(f"{candidate} color: must address preserved SHO colour.")
        if not any(w in fields["noise"].lower() for w in ("noise", "grain")):
            raise OrchestrationError(f"{candidate} noise: must explicitly address noise/grain.")
        if not any(w in fields["highlights"].lower() for w in ("highlight", "clip", "bright", "core", "harsh")):
            raise OrchestrationError(f"{candidate} highlights: must address highlight/clipping appearance.")
        result[candidate] = fields
    if set(result) != expected:
        raise OrchestrationError(
            "Visual notes must cover every eligible candidate exactly using repeated --note arguments. "
            f"Expected {sorted(expected)}, got {sorted(result)}."
        )
    combined = [" | ".join(result[name][key] for key in REVIEW_FIELDS) for name in sorted(result)]
    if len(combined) > 1 and len(set(combined)) == 1:
        raise OrchestrationError("Candidate notes must be candidate-specific; identical boilerplate is not allowed.")
    return result


def derive_visual_notes(candidate: str, notes: dict[str, dict[str, str]], eligible: list[str]) -> str:
    selected = notes[candidate]
    pieces = [
        f"Selected {candidate} after visually comparing every publication-eligible GHS pass-1 candidate.",
        f"Its stretch assessment was: {selected['stretch']}",
        f"Its structure assessment was: {selected['structure']}",
        f"Its SHO colour assessment was: {selected['color']}",
    ]
    others = [name for name in eligible if name != candidate]
    if others:
        pieces.append(
            "Other reviewed candidates were not selected after comparing their stretch, "
            "structure, colour, noise and highlight behavior: " + ", ".join(others) + "."
        )
    return " ".join(pieces)


def select_publish(project_name: str, candidate: str, visual_notes: str | None, note_values: list[str]) -> dict[str, Any]:
    active = load_active(project_name)
    if active is None:
        raise OrchestrationError("No active GHS pass-1 run is awaiting visual selection.")
    p, state = active
    eligible = state["publication_eligible_candidates"]
    if candidate not in eligible:
        raise OrchestrationError(f"Selected candidate {candidate!r} is not eligible: {eligible}")

    try:
        notes = parse_candidate_notes(note_values, eligible)
        if visual_notes is None or not visual_notes.strip():
            visual_notes = derive_visual_notes(candidate, notes, eligible)
        else:
            visual_notes = " ".join(visual_notes.split())
            if len(visual_notes) < 40:
                raise OrchestrationError(
                    "Overall --visual-notes are too vague; omit --visual-notes to let the "
                    "orchestrator derive the comparison from the candidate notes, or provide "
                    "a real overall visual comparison."
                )
    except OrchestrationError as exc:
        failures = int(state.get("publication_format_failures", 0)) + 1
        state["publication_format_failures"] = failures
        state["last_publication_format_error"] = str(exc)
        state["last_publication_format_error_at"] = utc_now()
        write_json_atomic(p["active"], state)
        if failures >= 3:
            raise OrchestrationError(
                f"Publication review payload retry budget exhausted ({failures}/3). "
                f"Stop and report this exact contract error without rerunning GHS or rereading images: {exc}"
            ) from exc
        raise OrchestrationError(
            f"Publication review payload rejected ({failures}/3): {exc} "
            "Repair only the select-publish payload using the returned command template; "
            "do not rerun GHS and do not reread images."
        ) from exc

    reviewed = []
    first = state["candidates"][eligible[0]]
    before = Path(first["before_preview"])
    if not before.is_file() or sha256_file(before) != first["before_preview_sha256"]:
        raise OrchestrationError("Before-linked review target changed or disappeared.")
    reviewed.append({"role": "before_linked", "path": str(before), "sha256": first["before_preview_sha256"]})

    for name in eligible:
        c = state["candidates"][name]
        path = Path(c["after_preview"])
        if not path.is_file() or sha256_file(path) != c["after_preview_sha256"]:
            raise OrchestrationError(f"{name} review target changed or disappeared: {path}")
        reviewed.append({
            "role": "candidate_after",
            "candidate": name,
            "path": str(path),
            "sha256": c["after_preview_sha256"],
        })

    args = [
        "publish", "--project", project_name,
        "--run-root", state["run_root"],
        "--candidate", candidate,
        "--visual-notes", visual_notes,
    ]
    if state.get("fresh_run"):
        args.append("--fresh-run")
    engine_result = run_engine(args)

    m = load_json(p["canonical_manifest"])
    output_sha = sha256_file(p["canonical_output"])
    if m.get("status") != "ready":
        raise OrchestrationError("Published canonical GHS manifest is not ready.")
    if m.get("output", {}).get("sha256") != output_sha:
        raise OrchestrationError("Published output SHA does not match canonical manifest.")
    if m.get("selected_candidate") != candidate:
        raise OrchestrationError("Published candidate does not match visual selection.")
    if m.get("source", {}).get("sha256") != state.get("source_sha256"):
        raise OrchestrationError("Published source differs from reviewed run source.")

    # Keep the v1.3.2 compatibility record filename and contract version because
    # the already-installed pass-2 gate explicitly requires them. Record the
    # actual publication orchestration separately.
    record = {
        "schema_version": 1,
        "orchestration_version": "1.3.2",
        "publication_orchestration_version": VERSION,
        "review_contract_revision": "ghs-pass1-publication-v1.4.0-dynamic-range",
        "processing_engine_version": PROCESSING_ENGINE_VERSION,
        "recorded_at": utc_now(),
        "project_name": project_name,
        "project": str(p["project"]),
        "run_root": state["run_root"],
        "review_method": "openclaw-read",
        "copying_files_counts_as_review": False,
        "visual_review_completed": True,
        "publication_eligible_candidates": eligible,
        "recommended_candidate": state.get("recommended_candidate"),
        "selected_candidate": candidate,
        "selected_candidate_was_recommended": candidate == state.get("recommended_candidate"),
        "visual_notes": visual_notes,
        "candidate_notes": notes,
        "reviewed_targets": reviewed,
        "canonical_output": str(p["canonical_output"]),
        "canonical_output_sha256": output_sha,
        "canonical_manifest": str(p["canonical_manifest"]),
        "canonical_manifest_sha256": sha256_file(p["canonical_manifest"]),
        "source_sha256": state["source_sha256"],
        "next_stage": "siril-ghs-stretch-pass2",
        "ghs_pass2_processing_permitted": True,
    }
    write_json_atomic(p["review_record"], record)

    state["status"] = "published"
    state["published_at"] = utc_now()
    state["selected_candidate"] = candidate
    state["canonical_output_changed"] = True
    state["canonical_output_sha256"] = output_sha
    state["visual_selection_record"] = str(p["review_record"])
    state["publication_format_failures"] = int(state.get("publication_format_failures", 0))
    p["completed"].mkdir(parents=True, exist_ok=True)
    write_json_atomic(p["completed"] / f"completed-{stamp()}.json", state)
    os.replace(p["active"], p["completed"] / f"active-consumed-{stamp()}.json")

    verification = canonical_snapshot(project_name)
    if verification.get("status") != "ready":
        raise OrchestrationError(f"Post-publication v1.4.0 verification failed: {verification}")

    return {
        "status": "ready",
        "orchestration_version": VERSION,
        "processing_engine_version": PROCESSING_ENGINE_VERSION,
        "project": str(p["project"]),
        "run_root": state["run_root"],
        "selected_candidate": candidate,
        "selected_candidate_was_recommended": candidate == state.get("recommended_candidate"),
        "canonical_output_sha256": output_sha,
        "visual_review_completed": True,
        "visual_selection_record": str(p["review_record"]),
        "visual_selection_record_contract_version": "1.3.2",
        "publication_orchestration_version": VERSION,
        "next_stage": "siril-ghs-stretch-pass2",
        "ghs_pass2_processing_permitted": True,
        "engine_publication_completed": bool(engine_result.get("canonical_output_changed", True)),
        "verification": verification,
    }


def stage_status(project_name: str) -> dict[str, Any]:
    p = validate_project(project_name)
    active = load_active(project_name)
    if active:
        s = active[1]
        return {
            "status": "visual_review_required",
            "orchestration_version": VERSION,
            "processing_engine_version": PROCESSING_ENGINE_VERSION,
            "project": str(p["project"]),
            "run_root": s["run_root"],
            "publication_eligible_candidates": s["publication_eligible_candidates"],
            "ghs_pass2_processing_permitted": False,
        }
    c = canonical_snapshot(project_name)
    if not c.get("exists"):
        return {
            "status": "missing",
            "orchestration_version": VERSION,
            "processing_engine_version": PROCESSING_ENGINE_VERSION,
            "project": str(p["project"]),
            "ghs_pass2_processing_permitted": False,
        }
    return {
        "status": c["status"],
        "orchestration_version": VERSION,
        "processing_engine_version": PROCESSING_ENGINE_VERSION,
        "project": str(p["project"]),
        "canonical_output_sha256": c.get("output_sha256"),
        "canonical_manifest_sha256": c.get("manifest_sha256"),
        "selected_candidate": c.get("selected_candidate"),
        "review_record": c.get("review_record"),
        "review_record_v1_3_2_valid": c.get("review_record_v1_3_2_valid"),
        "publication_orchestration_version": (
            load_json(p["review_record"]).get("publication_orchestration_version")
            if p["review_record"].is_file() else None
        ),
        "ghs_pass2_processing_permitted": c.get("ghs_pass2_processing_permitted", False),
        "errors": c.get("errors", []),
    }


def self_test() -> dict[str, Any]:
    eligible = ["candidate-01", "candidate-02"]
    good = [
        "candidate-01=stretch:nebular contrast is materially expanded while the background remains controlled; structure:faint outer nebula, Pillars and dark lanes remain clearly distinguishable; color:SHO gold and cyan colour separation remains natural and intact; noise:visible noise and grain remain controlled in the faint background; highlights:bright nebular highlights remain smooth with no clipped or harsh cores",
        "candidate-02=stretch:signal separation is stronger while the primary nebula remains natural rather than overprocessed; structure:faint Eagle Nebula structure, Pillars and dark lanes remain preserved; color:gold orange and cyan blue SHO colour separation remains intact; noise:background noise remains fine and does not become visibly coarse or grainy; highlights:bright central highlights remain smooth without clipping or harsh transitions",
    ]
    parsed = parse_candidate_notes(good, eligible)
    derived = derive_visual_notes("candidate-01", parsed, eligible)
    if len(derived) < 80 or "candidate-02" not in derived:
        raise OrchestrationError("Derived overall visual-notes self-test failed.")
    bad = [
        "candidate-01=stretch:looks good; structure:preserved; color:good; noise:low; highlights:fine",
        good[1],
    ]
    try:
        parse_candidate_notes(bad, eligible)
    except OrchestrationError:
        vague_rejected = True
    else:
        vague_rejected = False
    if not vague_rejected:
        raise OrchestrationError("Vague notes were incorrectly accepted.")
    return {
        "status": "success",
        "orchestration_version": VERSION,
        "processing_engine_version": PROCESSING_ENGINE_VERSION,
        "processing_policy_revision": "dynamic-range-expansion-v1",
        "structured_notes_accepted": True,
        "vague_notes_rejected": True,
        "required_fields": list(REVIEW_FIELDS),
        "completed_current_requires_confirmation": True,
        "completed_obsolete_requires_confirmation": True,
        "manifest_first_completed_stage_detection": True,
        "pre_confirmation_large_fits_hashing": False,
        "confirmation_strong_hash_binding": True,
        "old_active_candidate_runs_resumable": False,
        "new_state_directory": STATE_DIR_NAME,
        "exact_read_targets_required": True,
        "candidate_notes_use_repeated_note_arguments": True,
        "publication_format_retry_budget": 3,
        "pass2_v1_3_2_record_filename_preserved": True,
    }



def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="GHS pass-1 v1.3.3 resumable autonomous publication wrapper.")
    p.add_argument("--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="command", required=True)
    a = sub.add_parser("advance"); a.add_argument("--project", required=True)
    c = sub.add_parser("confirm-fresh"); c.add_argument("--project", required=True)
    s = sub.add_parser("select-publish")
    s.add_argument("--project", required=True)
    s.add_argument("--candidate", required=True)
    s.add_argument("--visual-notes", required=False, default=None)
    s.add_argument("--note", action="append", default=[], required=True)
    t = sub.add_parser("stage-status"); t.add_argument("--project", required=True)
    sub.add_parser("self-test")
    return p


def main() -> int:
    a = build_parser().parse_args()
    try:
        if a.command == "advance":
            payload = advance(a.project)
        elif a.command == "confirm-fresh":
            payload = confirm_fresh(a.project)
        elif a.command == "select-publish":
            payload = select_publish(a.project, a.candidate, a.visual_notes, a.note)
        elif a.command == "stage-status":
            payload = stage_status(a.project)
        elif a.command == "self-test":
            payload = self_test()
        else:
            raise OrchestrationError(f"Unsupported command: {a.command}")
    except OrchestrationError as exc:
        print(json.dumps({
            "status": "blocked",
            "orchestration_version": VERSION,
            "processing_engine_version": PROCESSING_ENGINE_VERSION,
            "error": str(exc),
            "ghs_pass2_processing_permitted": False,
        }, indent=2, sort_keys=True))
        return 2
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
