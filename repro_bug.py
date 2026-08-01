import os
import shutil
from pathlib import Path

# Mocking the module structure to avoid import issues in a single script
# But since we are testing the actual importer, we will use the real one.
# We just need to set up the environment.

from src.astroproc.importer import AstroImporter
import src.astroproc.fits_utils as fits_utils

# Mock fits_utils to avoid FITS header dependencies
fits_utils.get_fits_header = lambda p: {}
fits_utils.get_filter_name = lambda h: "Luminance"
fits_utils.get_observation_date = lambda h: None

def reproduce():
    base = Path("repro_bug_dir").absolute()
    if base.exists():
        shutil.rmtree(base)
    base.mkdir()

    projects_root = base / "Projects"
    calibration_root = base / "calibration"
    source_dir = base / "source"

    project_name = "TestProject"
    proj_path = projects_root / project_name
    
    # 1. Setup source files
    (source_dir / "Autorun/Light/TestProject").mkdir(parents=True, exist_ok=True)
    (source_dir / "Autorun/Flat/TestProject").mkdir(parents=True, exist_ok=True)
    (source_dir / "Autorun/Dark").mkdir(parents=True, exist_ok=True)
    (source_dir / "Autorun/Bias").mkdir(parents=True, exist_ok=True)

    light_file = source_dir / "Autorun/Light/TestProject/L1.fit"
    flat_file = source_dir / "Autorun/Flat/TestProject/F1.fit"
    dark_file = source_dir / "Autorun/Dark/D1.fit"
    bias_file = source_dir / "Autorun/Bias/B1.fit"

    light_file.write_text("light content")
    flat_file.write_text("flat content")
    dark_file.write_text("dark content")
    bias_file.write_text("bias content")

    # 2. Initial Import
    print("--- Run 1: Initial Import ---")
    importer = AstroImporter(project_name, source_dir, projects_root, calibration_root)
    report1 = importer.run_import(capture_types=["Autorun"])
    
    print(f"Lights copied: {report1['lights']['copied']}")
    print(f"Darks copied: {report1['darks']['copied']}")
    print(f"Bias copied: {report1['bias']['copied']}")
    print(f"Calibration exists: {calibration_root.exists()}")

    # 3. Delete Calibration
    print("\n--- Deleting Calibration Directory ---")
    if calibration_root.exists():
        shutil.rmtree(calibration_root)
    print(f"Calibration exists: {calibration_root.exists()}")

    # 4. Second Import
    print("\n--- Run 2: Rerun after deleting calibration ---")
    # We reuse the importer or create a new one.
    report2 = importer.run_import(capture_types=["Autorun"])
    
    print(f"Lights already_present: {report2['lights']['already_present']}")
    print(f"Darks copied: {report2['darks']['copied']}")
    print(f"Darks already_present: {report2['darks']['already_present']}")
    print(f"Bias copied: {report2['bias']['copied']}")
    print(f"Bias already_present: {report2['bias']['already_present']}")
    print(f"Calibration exists: {calibration_root.exists()}")
    
    # Verification
    if report2['darks']['already_present'] > 0 or report2['bias']['already_present'] > 0:
        print("\nBUG REPRODUCED: Darks/Bias reported as already present but calibration dir was deleted.")
    elif report2['darks']['copied'] == 0 or report2['bias']['copied'] == 0:
        print("\nBUG REPRODUCED: Darks/Bias not copied after calibration dir deletion.")
    else:
        print("\nBug NOT reproduced. Darks/Bias were copied as expected.")

if __name__ == "__main__":
    reproduce()
