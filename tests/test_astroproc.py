import unittest
import subprocess
import os
import shutil
from pathlib import Path
import tempfile

class TestAstroProc(unittest.TestCase):
    def setUp(self):
        # Create a temporary directory for testing projects
        self.test_dir = tempfile.mkdtemp()
        self.bin_path = "/home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/astroproc"
        
        # We need to override the projects_root in the script for testing.
        # Since the script currently has it hardcoded relative to __file__, 
        # I'll modify the script slightly to allow an environment variable for the projects root,
        # or I can just patch the script for tests. 
        # Actually, a better way is to modify the script to check for an environment variable.
        os.environ["ASTROPROC_PROJECTS_ROOT"] = self.test_dir

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def run_astroproc(self, args):
        return subprocess.run(
            [self.bin_path] + args,
            env=os.environ.copy(),
            capture_output=True,
            text=True
        )

    def test_create_basic_project(self):
        result = self.run_astroproc(["-np", "M16"])
        self.assertEqual(result.returncode, 0)
        self.assertTrue(Path(self.test_dir, "M16").is_dir())
        self.assertIn("Project created successfully", result.stdout)

    def test_create_project_with_spaces(self):
        result = self.run_astroproc(["-np", "M16 July 2026"])
        self.assertEqual(result.returncode, 0)
        self.assertTrue(Path(self.test_dir, "M16 July 2026").is_dir())

    def test_sanitization_invalid_chars(self):
        result = self.run_astroproc(["-np", "M16: July/2026"])
        self.assertEqual(result.returncode, 0)
        self.assertTrue(Path(self.test_dir, "M16_ July_2026").is_dir())
        self.assertIn("Project name: M16_ July_2026", result.stdout)

    def test_sanitization_edge_cases(self):
        # M16*Test? -> M16_Test_
        result = self.run_astroproc(["-np", "M16*Test?"])
        self.assertEqual(result.returncode, 0)
        self.assertTrue(Path(self.test_dir, "M16_Test_").is_dir())

    def test_missing_project_name(self):
        # Try running -np without a name
        result = self.run_astroproc(["-np"])
        # argparse will catch this and return 2 (or similar non-zero)
        self.assertNotEqual(result.returncode, 0)

    def test_empty_sanitized_name(self):
        # Name that becomes empty after sanitization (e.g. just invalid chars)
        # Wait, the current sanitize_name replaces with '_'. 
        # To get an empty name, it must be empty after strip().
        result = self.run_astroproc(["-np", "   "])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Error: Project name cannot be empty", result.stdout)

    def test_existing_project_fails(self):
        # Create project first
        self.run_astroproc(["-np", "M16"])
        # Try again
        result = self.run_astroproc(["-np", "M16"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Error: Project directory already exists", result.stdout)

    def test_path_traversal(self):
        result = self.run_astroproc(["-np", "../M16"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("prohibited path elements", result.stdout)

    def test_help_output(self):
        result = self.run_astroproc(["-h"])
        self.assertEqual(result.returncode, 0)
        self.assertIn("-np, --new-project NAME", result.stdout)

if __name__ == "__main__":
    unittest.main()
