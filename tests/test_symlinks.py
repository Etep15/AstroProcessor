import unittest
import os
import shutil
from pathlib import Path
from datetime import datetime, date
from unittest.mock import patch
from src.astroproc.preparer import ProjectPreparer

class TestProjectPreparerSymlinks(unittest.TestCase):
    def setUp(self):
        self.test_root = Path("/tmp/astroproc_symlink_test")
        self.projects_root = self.test_root / "Projects"
        self.cal_root = self.test_root / "calibration"
        
        # Setup basic structure
        (self.projects_root).mkdir(parents=True, exist_ok=True)
        (self.cal_root / "darks").mkdir(parents=True, exist_ok=True)
        (self.cal_root / "bias").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        if self.test_root.exists():
            shutil.rmtree(self.test_root)

    def setup_project(self, project_name):
        proj_dir = self.projects_root / project_name
        proj_dir.mkdir(parents=True, exist_ok=True)
        (proj_dir / "lights" / "Ha").mkdir(parents=True, exist_ok=True)
        (proj_dir / "flats" / "Ha").mkdir(parents=True, exist_ok=True)
        (proj_dir / "lights" / "Ha" / "light1.fit").touch()
        (proj_dir / "flats" / "Ha" / "flat1.fit").touch()
        return proj_dir

    def test_relative_symlinks_resolution(self):
        from datetime import date as d_date
        
        project_name = "M16 July 2026" # Test space in name
        proj_dir = self.setup_project(project_name)
        
        # Calibration
        cal_date = "2026-07-01"
        (self.cal_root / "darks" / cal_date).mkdir(parents=True, exist_ok=True)
        (self.cal_root / "bias" / cal_date).mkdir(parents=True, exist_ok=True)

        with patch('src.astroproc.preparer.get_fits_header'), \
             patch('src.astroproc.preparer.get_observation_date', return_value=d_date(2026, 7, 5)):
            
            preparer = ProjectPreparer(project_name, self.projects_root, self.cal_root)
            preparer.prepare()
            
            proc_ha = proj_dir / "processing" / "Ha"
            
            links = {
                "lights": proj_dir / "source" / "lights" / "Ha",
                "flats": proj_dir / "source" / "flats" / "Ha",
                "darks": self.cal_root / "darks" / cal_date,
                "biases": self.cal_root / "bias" / cal_date,
            }
            
            for link_name, expected_abs in links.items():
                link_path = proc_ha / link_name
                
                # 1. Must be a symlink
                self.assertTrue(link_path.is_symlink(), f"{link_name} should be a symlink")
                
                # 2. Target must be relative
                target = os.readlink(link_path)
                self.assertFalse(os.path.isabs(target), f"{link_name} target should be relative, got {target}")
                
                # 3. Must resolve to the intended absolute path
                # Use resolve() to find where it actually points
                self.assertEqual(link_path.resolve(), expected_abs.resolve(), f"{link_name} resolves to wrong path")

    def test_replace_incorrect_symlinks(self):
        from datetime import date as d_date
        
        project_name = "M16"
        proj_dir = self.setup_project(project_name)
        
        # Initial state: incorrect symlinks
        (proj_dir / "processing" / "Ha").mkdir(parents=True, exist_ok=True)
        wrong_link = proj_dir / "processing" / "Ha" / "lights"
        wrong_link.symlink_to("/tmp/wrong_place")
        
        (self.cal_root / "darks" / "2026-07-01").mkdir(parents=True, exist_ok=True)
        (self.cal_root / "bias" / "2026-07-01").mkdir(parents=True, exist_ok=True)

        with patch('src.astroproc.preparer.get_fits_header'), \
             patch('src.astroproc.preparer.get_observation_date', return_value=d_date(2026, 7, 5)):
            
            preparer = ProjectPreparer(project_name, self.projects_root, self.cal_root)
            preparer.prepare()
            
            # Should have been replaced
            self.assertEqual(wrong_link.resolve(), (proj_dir / "source" / "lights" / "Ha").resolve())

    def test_do_not_replace_real_files(self):
        from datetime import date as d_date
        
        project_name = "M16"
        proj_dir = self.setup_project(project_name)
        
        # Create a real directory where a symlink should go
        real_dir = proj_dir / "processing" / "Ha" / "lights"
        real_dir.mkdir(parents=True, exist_ok=True)
        
        (self.cal_root / "darks" / "2026-07-01").mkdir(parents=True, exist_ok=True)
        (self.cal_root / "bias" / "2026-07-01").mkdir(parents=True, exist_ok=True)

        with patch('src.astroproc.preparer.get_fits_header'), \
             patch('src.astroproc.preparer.get_observation_date', return_value=d_date(2026, 7, 5)):
            
            preparer = ProjectPreparer(project_name, self.projects_root, self.cal_root)
            with self.assertRaises(RuntimeError) as cm:
                preparer.prepare()
            self.assertIn("a real file or directory already exists", str(cm.exception))

if __name__ == '__main__':
    unittest.main()
