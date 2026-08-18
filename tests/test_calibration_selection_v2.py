from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
from astropy.io import fits

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("astroproc_prepare_copy_v2_test", ROOT / "astroproc_prepare_copy.py")
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def write_fit(path: Path, *, kind: str, temp: float, exp: float, date: str, gain: int = 102) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    h = fits.Header()
    h["DATE-OBS"] = date
    h["IMAGETYP"] = kind
    h["INSTRUME"] = "ZWO ASI533MM Pro"
    h["XBINNING"] = 1
    h["YBINNING"] = 1
    h["GAIN"] = gain
    h["OFFSET"] = 70
    h["EXPTIME"] = exp
    h["CCD-TEMP"] = temp
    h["FILTER"] = "SII" if kind in {"Dark", "Bias"} else "Ha"
    fits.PrimaryHDU(np.zeros((2, 2), dtype=np.uint16), header=h).writeto(path)


def make_project(workspace: Path) -> None:
    lights = workspace / "Projects" / "Regression" / "source" / "lights" / "Ha"
    flats = workspace / "Projects" / "Regression" / "source" / "flats" / "Ha"
    for i in range(3):
        write_fit(lights / f"light-{i}.fit", kind="Light", temp=-20.0, exp=30.0, date=f"2026-07-17T08:0{i}:00")
    write_fit(flats / "flat.fit", kind="Flat", temp=-20.0, exp=0.2, date="2026-07-17T07:00:00")

    # Older by capture date but physically compatible: MUST win.
    for i in range(90):
        write_fit(
            workspace / "calibration" / "darks" / "2025-05-27" / f"good-dark-{i:03d}.fit",
            kind="Dark", temp=-20.0 + (i % 5) * 0.1, exp=30.0,
            date=f"2025-05-27T01:{i % 60:02d}:{i % 60:02d}",
        )
        write_fit(
            workspace / "calibration" / "bias" / "2025-05-27" / f"good-bias-{i:03d}.fit",
            kind="Bias", temp=-19.9 + (i % 5) * 0.1, exp=0.001,
            date=f"2025-05-27T02:{i % 60:02d}:{i % 60:02d}",
        )

    # Newer/closer by date but ~20 C wrong: MUST be rejected.
    for i in range(10):
        write_fit(
            workspace / "calibration" / "darks" / "2025-06-25" / f"bad-dark-{i:03d}.fit",
            kind="Dark", temp=0.0, exp=30.0,
            date=f"2025-06-25T01:{i:02d}:00",
        )
        write_fit(
            workspace / "calibration" / "bias" / "2025-06-25" / f"bad-bias-{i:03d}.fit",
            kind="Bias", temp=0.0, exp=0.001,
            date=f"2025-06-25T02:{i:02d}:00",
        )


def test_date_is_not_a_calibration_compatibility_criterion(tmp_path: Path) -> None:
    make_project(tmp_path)
    plan = MODULE.plan_project_calibration(tmp_path, "Regression")
    item = plan["filters"][0]
    assert plan["date_used_for_compatibility"] is False
    assert item["dark_selection"]["selected_count"] == 90
    assert item["bias_selection"]["selected_count"] == 90
    assert all("2025-05-27" in path for path in item["dark_selection"]["selected_files"])
    assert all("2025-05-27" in path for path in item["bias_selection"]["selected_files"])
    assert not any("2025-06-25" in path for path in item["dark_selection"]["selected_files"])
    assert not any("2025-06-25" in path for path in item["bias_selection"]["selected_files"])


def test_gain_mismatch_is_rejected_even_when_temperature_matches(tmp_path: Path) -> None:
    make_project(tmp_path)
    write_fit(
        tmp_path / "calibration" / "darks" / "2026-07-17" / "wrong-gain.fit",
        kind="Dark", temp=-20.0, exp=30.0, date="2026-07-17T08:00:00", gain=100,
    )
    plan = MODULE.plan_project_calibration(tmp_path, "Regression")
    dark = plan["filters"][0]["dark_selection"]
    assert dark["selected_count"] == 90
    wrong = [item for item in dark["rejected_files"] if item["path"].endswith("wrong-gain.fit")]
    assert wrong and "gain_mismatch" in wrong[0]["reasons"]

