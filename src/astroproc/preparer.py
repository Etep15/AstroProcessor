import os
import shutil
from pathlib import Path
from datetime import datetime
from .fits_utils import get_fits_header, get_observation_date

class ProjectPreparer:
    def __init__(self, project_name, projects_root, calibration_root):
        self.project_name = project_name
        self.projects_root = Path(projects_root)
        self.calibration_root = Path(calibration_root)
        self.project_path = self.projects_root / project_name
        
        if not self.project_path.exists() or not self.project_path.is_dir():
            raise FileNotFoundError(f"Project directory not found: {self.project_path}")

    def _get_closest_calibration_folder(self, cal_type, observation_date):
        """
        Finds the closest dated calibration folder in calibration/<cal_type>/YYYY-MM-DD/.
        cal_type: 'darks' or 'bias'
        """
        return self._find_closest_date(self.calibration_root / cal_type, observation_date)

    def _find_closest_date(self, cal_base, observation_date):
        best_folder = None
        best_date = None
        min_diff = None

        for folder in cal_base.iterdir():
            if not folder.is_dir():
                continue
            try:
                folder_date = datetime.strptime(folder.name, '%Y-%m-%d').date()
            except ValueError:
                continue

            diff = abs((observation_date - folder_date).days)
            if min_diff is None or diff < min_diff:
                min_diff = diff
                best_folder = folder
                best_date = folder_date
            elif diff == min_diff:
                if folder_date < best_date:
                    best_folder = folder
                    best_date = folder_date
        
        return best_folder

    def prepare(self):
        # 1. Copy lights and flats to source/
        source_dir = self.project_path / "source"
        source_lights = source_dir / "lights"
        source_flats = source_dir / "flats"

        # Existing directories to copy from
        orig_lights = self.project_path / "lights"
        orig_flats = self.project_path / "flats"

        if not orig_lights.exists():
            raise RuntimeError(f"Missing lights directory in project: {orig_lights}")
        if not orig_flats.exists():
            raise RuntimeError(f"Missing flats directory in project: {orig_flats}")

        # Copying logic: preserve original, make safe to rerun.
        # We'll copy the directories if they don't exist, or update them.
        # To keep it simple and follow "do not overwrite real files", 
        # we'll use a helper that copies if missing or different.
        self._ensure_dir_copy(orig_lights, source_lights)
        self._ensure_dir_copy(orig_flats, source_flats)

        # 2. Detect filters from lights
        filters = [f.name for f in orig_lights.iterdir() if f.is_dir()]
        if not filters:
            raise RuntimeError("No filters detected in lights directory.")

        processing_dir = self.project_path / "processing"
        processing_dir.mkdir(exist_ok=True)

        results = []

        for filt in filters:
            filt_path = orig_lights / filt
            
            # 3. Read DATE-OBS from first valid .fit light
            light_files = sorted(filt_path.glob("*.fit"))
            if not light_files:
                raise RuntimeError(f"No .fit files found for filter {filt}")
            
            obs_date = None
            for lf in light_files:
                header = get_fits_header(lf)
                obs_date = get_observation_date(header)
                if obs_date:
                    break
            
            if not obs_date:
                raise RuntimeError(f"Could not read DATE-OBS for filter {filt}")

            # 4 & 5. Find closest calibration and create symlinks
            darks_folder = self._get_closest_calibration_folder('darks', obs_date)
            biases_folder = self._get_closest_calibration_folder('bias', obs_date)

            if not darks_folder:
                raise RuntimeError(f"No valid calibration darks found for filter {filt} (Obs Date: {obs_date})")
            if not biases_folder:
                raise RuntimeError(f"No valid calibration biases found for filter {filt} (Obs Date: {obs_date})")

            # Create filter processing directory
            filt_proc_dir = processing_dir / filt
            filt_proc_dir.mkdir(exist_ok=True)

            # Define symlinks
            links = {
                "lights": source_lights / filt,
                "flats": source_flats / filt,
                "darks": darks_folder,
                "biases": biases_folder
            }

            for link_name, target in links.items():
                link_path = filt_proc_dir / link_name
                self._safe_symlink(link_path, target)

            results.append({
                "filter": filt,
                "obs_date": obs_date,
                "darks_date": darks_folder.name,
                "biases_date": biases_folder.name
            })

        return results

    def _ensure_dir_copy(self, src, dst):
        """Copies src directory to dst. If dst exists, ensures it is updated."""
        if dst.exists():
            # Check if it's a directory. If not, it's a collision.
            if not dst.is_dir():
                raise RuntimeError(f"Collision: {dst} exists and is not a directory.")
            # We will just ensure the contents are there.
            # For the sake of "safe to rerun" and "do not overwrite real files",
            # and given the requirement "Copy the project's existing lights and flats",
            # we'll just sync them.
            for item in src.iterdir():
                s_item = item
                d_item = dst / item.name
                if s_item.is_dir():
                    self._ensure_dir_copy(s_item, d_item)
                else:
                    if not d_item.exists():
                        shutil.copy2(s_item, d_item)
        else:
            shutil.copytree(src, dst)

    def _safe_symlink(self, link_path, target):
        """Creates a symlink. Replaces if it's a symlink to wrong target."""
        if link_path.exists() or link_path.is_symlink():
            if link_path.is_symlink():
                current_target = os.readlink(link_path)
                # We compare absolute paths to be sure
                if os.path.abspath(current_target) != os.path.abspath(target):
                    link_path.unlink()
                    link_path.symlink_to(target)
            elif link_path.is_file() or link_path.is_dir():
                # Requirement: "Do not overwrite real files or directories."
                raise RuntimeError(f"Cannot create symlink {link_path}: a real file or directory already exists.")
        else:
            link_path.symlink_to(target)
