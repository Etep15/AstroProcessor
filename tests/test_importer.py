import unittest
import os
import shutil
from pathlib import Path
import tempfile
from datetime import datetime, timedelta
from astropy.io import fits
import numpy as np

# We import the logic from the src directory
import sys
# Add src to path to allow importing astroproc as a package
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from src.astroproc.importer import AstroImporter
from src.astroproc.fits_utils import get_filter_name, get_observation_date

class TestAstroImporter(unittest.TestCase):
    def setUp(self):
        self.test_root = Path(tempfile.mkdtemp())
        self.source_root = self.test_root / "source"
        self.projects_root = self.test_root / "Projects"
        self.calib_root = self.test_root / "calibration"
        
        self.source_root.mkdir()
        self.projects_root.mkdir()
        self.calib_root.mkdir()
        
        # Create a test project
        self.project_name = "M16"
        self.project_path = self.projects_root / self.project_name
        self.project_path.mkdir()

    def tearDown(self):
        shutil.rmtree(self.test_root)

    def create_fits(self, path, filter_val=None, date_val=None):
        """Creates a synthetic FITS file with specific headers."""
        data = np.zeros((10, 10), dtype=np.float32)
        hdu = fits.PrimaryHDU(data)
        header = hdu.header
        if filter_val:
            header['FILTER'] = filter_val
        if date_val:
            header['DATE-OBS'] = date_val
        hdu.writeto(path, overwrite=True)

    def test_basic_light_import(self):
        # Setup source: /source/Autorun/Light/M16/img1.fit
        light_dir = self.source_root / "Autorun" / "Light" / self.project_name
        light_dir.mkdir(parents=True)
        self.create_fits(light_dir / "img1.fit", filter_val="Ha", date_val="2026-07-20")
        
        importer = AstroImporter(self.project_name, self.source_root, self.projects_root, self.calib_root)
        report = importer.run_import(capture_types=["Autorun"])
        
        self.assertEqual(report['lights']['discovered'], 1)
        self.assertEqual(report['lights']['copied'], 1)
        self.assertTrue((self.project_path / "lights" / "Ha" / "img1.fit").exists())

    def test_ignore_non_fit(self):
        light_dir = self.source_root / "Autorun" / "Light" / self.project_name
        light_dir.mkdir(parents=True)
        self.create_fits(light_dir / "img1.fit")
        (light_dir / "img1.jpg").write_text("not a fit")
        (light_dir / "img1.fits").write_text("not a fit") # prompt says .fits is ignored
        
        importer = AstroImporter(self.project_name, self.source_root, self.projects_root, self.calib_root)
        report = importer.run_import(capture_types=["Autorun"])
        
        self.assertEqual(report['lights']['discovered'], 1)

    def test_named_flat_import(self):
        # Setup source: /source/Autorun/Flat/M16/flat1.fit
        flat_dir = self.source_root / "Autorun" / "Flat" / self.project_name
        flat_dir.mkdir(parents=True)
        self.create_fits(flat_dir / "flat1.fit", filter_val="Ha")
        
        # Need some lights to trigger import (since lights are first in order)
        light_dir = self.source_root / "Autorun" / "Light" / self.project_name
        light_dir.mkdir(parents=True)
        self.create_fits(light_dir / "img1.fit")
        
        importer = AstroImporter(self.project_name, self.source_root, self.projects_root, self.calib_root)
        report = importer.run_import(capture_types=["Autorun"])
        
        self.assertTrue((self.project_path / "flats" / "Ha" / "flat1.fit").exists())

    def test_shared_flat_date_matching(self):
        # Light dated 2026-07-20
        light_dir = self.source_root / "Autorun" / "Light" / self.project_name
        light_dir.mkdir(parents=True)
        self.create_fits(light_dir / "img1.fit", date_val="2026-07-20")
        
        # Shared flats
        flat_dir = self.source_root / "Autorun" / "Flat"
        flat_dir.mkdir(parents=True)
        self.create_fits(flat_dir / "match_prev.fit", date_val="2026-07-19") # -1 day
        self.create_fits(flat_dir / "match_same.fit", date_val="2026-07-20") # 0 day
        self.create_fits(flat_dir / "match_next.fit", date_val="2026-07-21") # +1 day
        self.create_fits(flat_dir / "no_match.fit", date_val="2026-07-25")   # far
        
        importer = AstroImporter(self.project_name, self.source_root, self.projects_root, self.calib_root)
        report = importer.run_import(capture_types=["Autorun"])
        
        self.assertEqual(report['flats']['matched_by_date'], 3)
        self.assertTrue((self.project_path / "flats" / "Unknown" / "match_prev.fit").exists())
        self.assertFalse((self.project_path / "flats" / "Unknown" / "no_match.fit").exists())

    def test_dark_bias_copy(self):
        dark_dir = self.source_root / "Autorun" / "Dark"
        dark_dir.mkdir(parents=True)
        self.create_fits(dark_dir / "dark1.fit")
        
        bias_dir = self.source_root / "Autorun" / "Bias"
        bias_dir.mkdir(parents=True)
        self.create_fits(bias_dir / "bias1.fit")
        
        # Need a light to proceed
        light_dir = self.source_root / "Autorun" / "Light" / self.project_name
        light_dir.mkdir(parents=True)
        self.create_fits(light_dir / "img1.fit")
        
        importer = AstroImporter(self.project_name, self.source_root, self.projects_root, self.calib_root)
        report = importer.run_import(capture_types=["Autorun"])
        
        self.assertEqual(report['darks']['copied'], 1)
        self.assertEqual(report['bias']['copied'], 1)
        self.assertTrue((self.calib_root / "darks" / "dark1.fit").exists())
        self.assertTrue((self.calib_root / "bias" / "bias1.fit").exists())

    def test_filter_canonicalization(self):
        light_dir = self.source_root / "Autorun" / "Light" / self.project_name
        light_dir.mkdir(parents=True)
        self.create_fits(light_dir / "ha.fit", filter_val="ha") # lowercase
        self.create_fits(light_dir / "oiii.fit", filter_val="OIII") # uppercase
        self.create_fits(light_dir / "custom.fit", filter_val="MyFilter") # custom
        self.create_fits(light_dir / "none.fit") # missing filter
        
        importer = AstroImporter(self.project_name, self.source_root, self.projects_root, self.calib_root)
        report = importer.run_import(capture_types=["Autorun"])
        
        self.assertTrue((self.project_path / "lights" / "Ha" / "ha.fit").exists())
        self.assertTrue((self.project_path / "lights" / "OIII" / "oiii.fit").exists())
        self.assertTrue((self.project_path / "lights" / "MyFilter" / "custom.fit").exists())
        self.assertTrue((self.project_path / "lights" / "Unknown" / "none.fit").exists())

    def test_duplicate_handling_identical(self):
        light_dir = self.source_root / "Autorun" / "Light" / self.project_name
        light_dir.mkdir(parents=True)
        self.create_fits(light_dir / "img1.fit")
        
        importer = AstroImporter(self.project_name, self.source_root, self.projects_root, self.calib_root)
        
        # First run
        report1 = importer.run_import(capture_types=["Autorun"])
        self.assertEqual(report1['lights']['copied'], 1)
        
        # Second run
        report2 = importer.run_import(capture_types=["Autorun"])
        # We expect the importer to recognize that the file is already present 
        # in the destination tree, even if it has been sorted into a filter folder.
        self.assertEqual(report2['lights']['already_present'], 1)
        self.assertEqual(report2['lights']['copied'], 0)

    def test_duplicate_handling_different(self):
        # This requires a special setup where we manually put a file in destination 
        # with the same name but different content.
        light_dir = self.source_root / "Autorun" / "Light" / self.project_name
        light_dir.mkdir(parents=True)
        self.create_fits(light_dir / "img1.fit")
        
        # Create destination file with different data
        dest_light_dir = self.project_path / "lights"
        dest_light_dir.mkdir(parents=True)
        (dest_light_dir / "img1.fit").write_text("different content")
        
        importer = AstroImporter(self.project_name, self.source_root, self.projects_root, self.calib_root)
        report = importer.run_import(capture_types=["Autorun"])
        
        # The file might be sorted into a filter folder, so we check for any file starting with img1_
        found = False
        for f in dest_light_dir.rglob('img1_*.fit'):
            if f.name != "img1.fit":
                found = True
                break
        
        self.assertTrue(found, "Collision-safe renamed file was not found")
        self.assertEqual(report['lights']['collisions'], 1)

    def test_missing_project_fails(self):
        with self.assertRaises(FileNotFoundError):
            AstroImporter("NonExistent", self.source_root, self.projects_root, self.calib_root)

    def test_no_lights_fails(self):
        # Empty source
        self.source_root.mkdir(exist_ok=True)
        importer = AstroImporter(self.project_name, self.source_root, self.projects_root, self.calib_root)
        with self.assertRaises(RuntimeError) as cm:
            importer.run_import(capture_types=["Autorun"])
        self.assertIn("No matching light .fit files found", str(cm.exception))

    def test_path_traversal_source(self):
        # We need to mock the source_dir to be something that would allow traversal
        # but the current implementation uses resolve() and startswith().
        # Let's test a path that tries to go up.
        # Since the importer logic takes source_dir as root, traversal usually happens
        # when it joins with a project name that contains '..'.
        
        # But project_name is sanitized by the CLI. 
        # Let's test the AstroImporter with a "dirty" project name directly.
        with self.assertRaises(Exception):
            AstroImporter("../evil", self.source_root, self.projects_root, self.calib_root)

if __name__ == "__main__":
    unittest.main()
