import unittest
import os
import shutil
from pathlib import Path
from datetime import datetime, date
from unittest.mock import patch
from src.astroproc.preparer import ProjectPreparer


class TestProjectPreparer(unittest.TestCase):
    def setUp(self):
        self.test_root = Path("/tmp/astroproc_test")
        self.projects_root = self.test_root / "Projects"
        self.cal_root = self.test_root / "calibration"
        
        # Setup basic structure
        (self.projects_root / "M16").mkdir(parents=True)
        (self.cal_root / "darks").mkdir(parents=True)
        (self.cal_root / "bias").mkdir(parents=True)

    def tearDown(self):
        if self.test_root.exists():
            shutil.rmtree(self.test_root)

    def create_fits_file(self, path, obs_date):
        # Create a dummy file that the fits_utils can read
        # Since we can't easily write real FITS without astropy in tests without overhead,
        # we will mock get_fits_header and get_observation_date if needed, 
        # but the actual code uses them. Let's mock the fits_utils.
        pass

    def test_basic_preparation(self):
        from unittest.mock import patch
        from datetime import date as d_date

        # Setup project
        proj_dir = self.projects_root / "M16"
        (proj_dir / "lights" / "Ha").mkdir(parents=True)
        (proj_dir / "flats" / "Ha").mkdir(parents=True)
        (proj_dir / "lights" / "Ha" / "light1.fit").touch()
        (proj_dir / "flats" / "Ha" / "flat1.fit").touch()

        # Setup calibration
        (self.cal_root / "darks" / "2026-07-01").mkdir(parents=True)
        (self.cal_root / "bias" / "2026-07-01").mkdir(parents=True)

        with patch('src.astroproc.preparer.get_fits_header'), \
             patch('src.astroproc.preparer.get_observation_date', return_value=d_date(2026, 7, 5)):
            
            preparer = ProjectPreparer("M16", self.projects_root, self.cal_root)
            results = preparer.prepare()
            
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]['filter'], 'Ha')
            
            # Check directory structure
            self.assertTrue((proj_dir / "source" / "lights" / "Ha").exists())
            self.assertTrue((proj_dir / "source" / "flats" / "Ha").exists())
            
            # Check symlinks
            proc_ha = proj_dir / "processing" / "Ha"
            self.assertTrue(proc_ha.joinpath("lights").is_symlink())
            self.assertTrue(proc_ha.joinpath("flats").is_symlink())
            self.assertTrue(proc_ha.joinpath("darks").is_symlink())
            self.assertTrue(proc_ha.joinpath("biases").is_symlink())

    def test_closest_date_selection(self):
        from datetime import date as d_date
        
        # Project lights observed on July 5
        proj_dir = self.projects_root / "M16"
        (proj_dir / "lights" / "Ha").mkdir(parents=True)
        (proj_dir / "flats" / "Ha").mkdir(parents=True)
        (proj_dir / "lights" / "Ha" / "light1.fit").touch()
        (proj_dir / "flats" / "Ha" / "flat1.fit").touch()

        # Calibration folders: July 1 and July 8 (diff 4 and 3)
        (self.cal_root / "darks" / "2026-07-01").mkdir(parents=True)
        (self.cal_root / "darks" / "2026-07-08").mkdir(parents=True)
        (self.cal_root / "bias" / "2026-07-01").mkdir(parents=True)
        (self.cal_root / "bias" / "2026-07-08").mkdir(parents=True)

        with patch('src.astroproc.preparer.get_fits_header'), \
             patch('src.astroproc.preparer.get_observation_date', return_value=d_date(2026, 7, 5)):
            
            preparer = ProjectPreparer("M16", self.projects_root, self.cal_root)
            results = preparer.prepare()
            
            self.assertEqual(results[0]['darks_date'], '2026-07-08')

    def test_equal_distance_dates(self):
        from datetime import date as d_date
        
        proj_dir = self.projects_root / "M16"
        (proj_dir / "lights" / "Ha").mkdir(parents=True)
        (proj_dir / "flats" / "Ha").mkdir(parents=True)
        (proj_dir / "lights" / "Ha" / "light1.fit").touch()
        (proj_dir / "flats" / "Ha" / "flat1.fit").touch()

        # Observed July 5. Folders July 4 and July 6 (diff 1 each)
        (self.cal_root / "darks" / "2026-07-04").mkdir(parents=True)
        (self.cal_root / "darks" / "2026-07-06").mkdir(parents=True)
        (self.cal_root / "bias" / "2026-07-04").mkdir(parents=True)
        (self.cal_root / "bias" / "2026-07-06").mkdir(parents=True)

        with patch('src.astroproc.preparer.get_fits_header'), \
             patch('src.astroproc.preparer.get_observation_date', return_value=d_date(2026, 7, 5)):
            
            preparer = ProjectPreparer("M16", self.projects_root, self.cal_root)
            results = preparer.prepare()
            
            # Should choose earlier date: 2026-07-04
            self.assertEqual(results[0]['darks_date'], '2026-07-04')

    def test_malformed_calibration_folders(self):
        from datetime import date as d_date
        
        proj_dir = self.projects_root / "M16"
        (proj_dir / "lights" / "Ha").mkdir(parents=True)
        (proj_dir / "flats" / "Ha").mkdir(parents=True)
        (proj_dir / "lights" / "Ha" / "light1.fit").touch()
        (proj_dir / "flats" / "Ha" / "flat1.fit").touch()

        # One valid, one malformed
        (self.cal_root / "darks" / "2026-07-01").mkdir(parents=True)
        (self.cal_root / "darks" / "not-a-date").mkdir(parents=True)
        (self.cal_root / "bias" / "2026-07-01").mkdir(parents=True)

        with patch('src.astroproc.preparer.get_fits_header'), \
             patch('src.astroproc.preparer.get_observation_date', return_value=d_date(2026, 7, 5)):
            
            preparer = ProjectPreparer("M16", self.projects_root, self.cal_root)
            results = preparer.prepare()
            self.assertEqual(results[0]['darks_date'], '2026-07-01')

    def test_missing_calibration_data(self):
        from datetime import date as d_date
        
        proj_dir = self.projects_root / "M16"
        (proj_dir / "lights" / "Ha").mkdir(parents=True)
        (proj_dir / "flats" / "Ha").mkdir(parents=True)
        (proj_dir / "lights" / "Ha" / "light1.fit").touch()
        (proj_dir / "flats" / "Ha" / "flat1.fit").touch()

        # No darks
        (self.cal_root / "bias" / "2026-07-01").mkdir(parents=True)

        with patch('src.astroproc.preparer.get_fits_header'), \
             patch('src.astroproc.preparer.get_observation_date', return_value=d_date(2026, 7, 5)):
            
            preparer = ProjectPreparer("M16", self.projects_root, self.cal_root)
            with self.assertRaises(RuntimeError) as cm:
                preparer.prepare()
            self.assertIn("No valid calibration darks found", str(cm.exception))

    def test_rerun_safety(self):
        from datetime import date as d_date
        
        proj_dir = self.projects_root / "M16"
        (proj_dir / "lights" / "Ha").mkdir(parents=True)
        (proj_dir / "flats" / "Ha").mkdir(parents=True)
        (proj_dir / "lights" / "Ha" / "light1.fit").touch()
        (proj_dir / "flats" / "Ha" / "flat1.fit").touch()

        (self.cal_root / "darks" / "2026-07-01").mkdir(parents=True)
        (self.cal_root / "bias" / "2026-07-01").mkdir(parents=True)

        with patch('src.astroproc.preparer.get_fits_header'), \
             patch('src.astroproc.preparer.get_observation_date', return_value=d_date(2026, 7, 5)):
            
            preparer = ProjectPreparer("M16", self.projects_root, self.cal_root)
            preparer.prepare()
            # Rerun should not fail
            preparer.prepare()
            self.assertTrue((proj_dir / "processing" / "Ha" / "lights").is_symlink())

if __name__ == '__main__':
    unittest.main()
