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
        # 1. Move lights and flats to source/
        source_dir = self.project_path / "source"
        source_dir.mkdir(exist_ok=True)

        # Existing directories to move
        orig_lights = self.project_path / "lights"
        orig_flats = self.project_path / "flats"

        # Check if they already moved (safe to rerun)
        if not orig_lights.exists() and not (source_dir / "lights").exists():
            raise RuntimeError(f"Missing lights directory in project: {orig_lights}")
        if not orig_flats.exists() and not (source_dir / "flats").exists():
            raise RuntimeError(f"Missing flats directory in project: {orig_flats}")

        # Perform the move
        if orig_lights.exists():
            self._safe_move(orig_lights, source_dir / "lights")
        if orig_flats.exists():
            self._safe_move(orig_flats, source_dir / "flats")

        # 2. Detect filters from source/lights
        source_lights = source_dir / "lights"
        source_flats = source_dir / "flats"
        
        filters = [f.name for f in source_lights.iterdir() if f.is_dir()]
        if not filters:
            raise RuntimeError("No filters detected in source/lights directory.")

        processing_dir = self.project_path / "processing"
        processing_dir.mkdir(exist_ok=True)

        results = []

        for filt in filters:
            filt_path = source_lights / filt
            
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

    def _safe_move(self, src, dst):
        """Moves src directory to dst. If dst exists, merges contents."""
        if dst.exists():
            if not dst.is_dir():
                raise RuntimeError(f"Collision: {dst} exists and is not a directory.")
            
            # Merge src into dst
            for item in src.iterdir():
                s_item = item
                d_item = dst / item.name
                if s_item.is_dir():
                    self._safe_move(s_item, d_item)
                else:
                    # If file exists, we only overwrite if it's different or just let it be.
                    # The requirement is "do not overwrite real files". 
                    # In a move/merge context, we'll only move if it doesn't exist.
                    if not d_item.exists():
                        shutil.move(str(s_item), str(d_item))
            
            # Remove the now empty (or partially empty) src
            if not any(src.iterdir()):
                src.rmdir()
        else:
            shutil.move(str(src), str(dst))


    def _safe_symlink(self, link_path, target):
        """Creates a relative symlink. Replaces if it's a symlink to wrong target."""
        # Calculate relative path from link's parent to target
        # target and link_path are Path objects
        link_parent = link_path.parent.resolve()
        target_abs = Path(target).resolve()
        
        try:
            relative_target = os.path.relpath(target_abs, link_parent)
        except ValueError:
            # Fallback to absolute if relpath fails (e.g. different drives on Windows)
            relative_target = str(target_abs)

        if link_path.exists() or link_path.is_symlink():
            if link_path.is_symlink():
                # Use realpath to resolve existing link and compare with target_abs
                current_resolved = Path(os.readlink(link_path))
                # If the existing link is relative, resolve it relative to the link's parent
                if not current_resolved.is_absolute():
                    current_resolved = (link_parent / current_resolved).resolve()
                else:
                    current_resolved = current_resolved.resolve()
                
                if current_resolved != target_abs:
                    link_path.unlink()
                    link_path.symlink_to(relative_target)
            elif link_path.is_file() or link_path.is_dir():
                # Requirement: "Do not overwrite real files or directories."
                raise RuntimeError(f"Cannot create symlink {link_path}: a real file or directory already exists.")
        else:
            link_path.symlink_to(relative_target)
