import unittest
import shutil
from pathlib import Path
import tempfile
import numpy as np
from astropy.io import fits

# Add src to path
import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from src.astroproc.importer import AstroImporter

class TestCalibrationBug(unittest.TestCase):
    def setUp(self):
        self.test_root = Path(tempfile.mkdtemp())
        self.source_root = self.test_root / "source"
        self.projects_root = self.test_root / "Projects"
        self.calib_root = self.test_root / "calibration"
        
        self.source_root.mkdir()
        self.projects_root.mkdir()
        self.calib_root.mkdir()
        
        self.project_name = "M16"
        self.project_path = self.projects_root / self.project_name
        self.project_path.mkdir()

    def tearDown(self):
        shutil.rmtree(self.test_root)

    def create_fits(self, path, filter_val=None, date_val=None):
        data = np.zeros((10, 10), dtype=np.float32)
        hdu = fits.PrimaryHDU(data)
        header = hdu.header
        if filter_val:
            header['FILTER'] = filter_val
        if date_val:
            header['DATE-OBS'] = date_val
        hdu.writeto(path, overwrite=True)

    def test_calibration_persistence_bug(self):
        # 1. Setup Source Files
        light_dir = self.source_root / "Autorun" / "Light" / self.project_name
        light_dir.mkdir(parents=True)
        self.create_fits(light_dir / "L1.fit")

        dark_dir = self.source_root / "Autorun" / "Dark"
        dark_dir.mkdir(parents=True)
        self.create_fits(dark_dir / "D1.fit")

        bias_dir = self.source_root / "Autorun" / "Bias"
        bias_dir.mkdir(parents=True)
        self.create_fits(bias_dir / "B1.fit")

        # 2. Initial Import
        importer = AstroImporter(self.project_name, self.source_root, self.projects_root, self.calib_root)
        report1 = importer.run_import(capture_types=["Autorun"])
        
        self.assertEqual(report1['darks']['copied'], 1)
        self.assertEqual(report1['bias']['copied'], 1)
        self.assertTrue((self.calib_root / "darks" / "D1.fit").exists())
        self.assertTrue((self.calib_root / "bias" / "B1.fit").exists())

        # 3. Delete entire calibration directory
        shutil.rmtree(self.calib_root)
        self.assertFalse(self.calib_root.exists())

        # 4. Rerun Import
        report2 = importer.run_import(capture_types=["Autorun"])

        # Lights should be skipped (already present in Projects/M16/lights/...)
        self.assertEqual(report2['lights']['already_present'], 1)
        self.assertEqual(report2['lights']['copied'], 0)

        # Calibration should be recreated and files copied
        self.assertTrue(self.calib_root.exists(), "Calibration directory was not recreated")
        self.assertTrue((self.calib_root / "darks").exists())
        self.assertTrue((self.calib_root / "bias").exists())
        
        self.assertTrue((self.calib_root / "darks" / "D1.fit").exists(), "Dark file was not recopying")
        self.assertTrue((self.calib_root / "bias" / "B1.fit").exists(), "Bias file was not recopying")
        
        # counts must be exactly 0 for already_present if we deleted the folder
        self.assertEqual(report2['darks']['already_present'], 0, "Darks reported as already present unexpectedly")
        self.assertEqual(report2['bias']['already_present'], 0, "Bias reported as already present unexpectedly")
        self.assertEqual(report2['darks']['copied'], 1)
        self.assertEqual(report2['bias']['copied'], 1)

if __name__ == "__main__":
    unittest.main()
