from __future__ import annotations
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
import numpy as np
from astropy.io import fits

ROOT = Path(__file__).resolve().parents[1]

def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # Python 3.14 dataclasses resolving postponed annotations expect the
    # executing module to already be registered in sys.modules.
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return mod

def write_fit(path: Path, *, filt: str | None, date: str | None, value: float = 1.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    h = fits.Header()
    if filt is not None: h["FILTER"] = filt
    if date is not None: h["DATE-OBS"] = date
    fits.PrimaryHDU(data=np.full((8, 8), value, dtype=np.float32), header=h).writeto(path)

def make_source(root: Path):
    base = root / "Autorun"
    write_fit(base / "Light" / "Exact Source" / "ha.fit", filt="Ha", date="2026-07-17T01:00:00")
    write_fit(base / "Light" / "Exact Source" / "odd.fit", filt="Custom Filter", date="2026-07-17T01:05:00", value=2)
    (base / "Light" / "Exact Source" / "._fake.fit").write_bytes(b"AppleDouble-not-FITS")
    write_fit(base / "Flat" / "flat-ha.fit", filt="Ha", date="2026-07-17T02:00:00")
    write_fit(base / "Flat" / "flat-custom.fit", filt="Custom Filter", date="2026-07-17T02:02:00")
    (base / "Flat" / "._flat.fit").write_bytes(b"junk")
    write_fit(base / "Dark" / "dark.fit", filt=None, date="2026-07-17T03:00:00")
    write_fit(base / "Bias" / "bias.fit", filt=None, date="2026-07-17T03:01:00")

def test_copy_feeds_prepare_and_ignores_appledouble(tmp_path, monkeypatch):
    workspace = tmp_path / "agent"
    project = workspace / "Projects" / "Destination"
    project.mkdir(parents=True)
    source = tmp_path / "asiair"
    make_source(source)
    monkeypatch.setenv("ASTROPROC_WORKSPACE_ROOT", str(workspace))
    copy_mod = load_module(ROOT / "astroproc_copy_source.py", "copy_source_test")
    prepare_mod = load_module(ROOT / "astroproc_prepare_copy.py", "prepare_source_test")
    before = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    result = copy_mod.run_copy("Destination", str(source), "Exact Source", ["autorun"])
    assert result["results"]["lights"]["filters"] == {"Custom_Filter": 1, "Ha": 1}
    assert not any(p.name.startswith("._") for p in (project / "source").rglob("*"))
    assert not (project / "lights").exists() and not (project / "flats").exists()
    assert (project / "source/lights/Ha/ha.fit").is_file()
    assert (project / "source/lights/Custom_Filter/odd.fit").is_file()
    assert (workspace / "calibration/darks/2026-07-17/dark.fit").is_file()
    assert (workspace / "calibration/bias/2026-07-17/bias.fit").is_file()
    after = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    assert before == after
    prepared = prepare_mod.prepare_project_copy(workspace, "Destination")
    assert {x["filter"] for x in prepared["filters"]} == {"Custom_Filter", "Ha"}
    assert (project / "processing/Ha/lights/ha.fit").is_file()
    again = copy_mod.run_copy("Destination", str(source), "Exact Source", ["AUTORUN"])
    assert again["results"]["lights"]["copied"] == 0
    assert again["results"]["lights"]["identical"] == 2

def test_prepare_workspace_resolution_does_not_depend_on_cwd(tmp_path, monkeypatch):
    workspace = tmp_path / "agent"
    monkeypatch.setenv("ASTROPROC_WORKSPACE_ROOT", str(workspace))
    mod = load_module(ROOT / "astroproc_prepare_copy.py", "prepare_resolver_test")
    other = tmp_path / "elsewhere"; other.mkdir()
    old = Path.cwd()
    try:
        os.chdir(other)
        assert mod.resolve_workspace_root() == workspace.resolve()
    finally:
        os.chdir(old)

def test_launcher_copy_uses_canonical_workspace_root(tmp_path):
    workspace = tmp_path / "agent"
    project = workspace / "Projects" / "Destination"; project.mkdir(parents=True)
    source = tmp_path / "asiair"; make_source(source)
    env = os.environ.copy(); env["ASTROPROC_WORKSPACE_ROOT"] = str(workspace)
    r = subprocess.run(
        [str(ROOT / "astroproc"), "-c", "Destination", "-sp", "Exact Source", "-sd", str(source), "-t", "autorun"],
        cwd=tmp_path, env=env, capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert (project / "source/lights/Ha/ha.fit").is_file()
    assert not (project / "lights").exists()
