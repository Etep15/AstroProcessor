import os
import shutil
import hashlib
from pathlib import Path
from datetime import timedelta
from .fits_utils import get_fits_header, get_observation_date, get_filter_name

def calculate_hash(path, chunk_size=65536):
    """Calculate SHA256 hash of a file to verify identity."""
    sha256 = hashlib.sha256()
    with open(path, 'rb') as f:
        while chunk:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            sha256.update(chunk)
    return sha256.hexdigest()

def calculate_hash_fixed(path, chunk_size=65536):
    """Corrected SHA256 hash calculation."""
    sha256 = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            sha256.update(chunk)
    return sha256.hexdigest()

def safe_copy(src_path, dest_dir, report):
    """
    Copies a file to the destination directory with collision handling.
    Returns: 'copied', 'skipped' (identical), or 'renamed'.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    
    # Check if the file already exists anywhere in the destination project's subdirs
    # for lights and flats, because it might have been sorted into a filter folder.
    # We look for any file with the same name in the project path.
    # This is a bit expensive but necessary for the "safe to run more than once" requirement.
    
    # In the context of this function, dest_dir is likely Projects/<project>/lights or /flats.
    # We check if any file with the same name exists in that root.
    
    # To keep it efficient, we can just check the current dest_dir and any subdirs.
    # For calibration, it's simpler.
    
    found_identical = False
    for existing in dest_dir.rglob(src_path.name):
        if calculate_hash_fixed(src_path) == calculate_hash_fixed(existing):
            found_identical = True
            break
            
    if found_identical:
        report['already_present'] += 1
        return 'skipped'

    dest_path = dest_dir / src_path.name
    
    # If a file exists with the same name but is DIFFERENT, we rename the new one.
    if dest_path.exists():
        # We already checked for identicals in the whole tree, 
        # so if it exists here, it's different.
        stem = dest_path.stem
        suffix = dest_path.suffix
        counter = 1
        while dest_path.exists():
            dest_path = dest_dir / f"{stem}_{counter}{suffix}"
            counter += 1
        shutil.copy2(src_path, dest_path)
        report['copied'] += 1
        report['collisions'] += 1
        return 'renamed'
    
    shutil.copy2(src_path, dest_path)
    report['copied'] += 1
    return 'copied'

def get_all_fit_files(root_path):
    """Recursively finds all .fit files (case-insensitive)."""
    fits_files = []
    for path in root_path.rglob('*'):
        if path.suffix.lower() == '.fit':
            fits_files.append(path)
    return fits_files

class AstroImporter:
    def __init__(self, project_name, source_dir, projects_root, calibration_root):
        self.project_name = project_name
        self.source_dir = Path(source_dir).resolve()
        self.projects_root = Path(projects_root).resolve()
        self.calibration_root = Path(calibration_root).resolve()
        
        # Validate source_dir
        if not self.source_dir.exists() or not self.source_dir.is_dir():
            raise FileNotFoundError(f"Source directory {source_dir} does not exist or is not a directory.")
        
        # Resolve project path (using sanitization from CLI)
        # We assume project_name passed here is already sanitized.
        self.project_path = self.projects_root / project_name
        if not self.project_path.exists():
            raise FileNotFoundError(f"Project {project_name} does not exist at {self.project_path}. Please create it first with -np.")

    def validate_path(self, path):
        """Prevent path traversal."""
        resolved = Path(path).resolve()
        if not str(resolved).startswith(str(self.source_dir)):
            raise PermissionError(f"Path {path} escapes the source directory root.")
        return resolved

    def run_import(self, capture_types=None):
        """
        Main import logic.
        capture_types: List of strings (e.g. ['Autorun', 'Live']). If None, search all.
        """
        supported_types = ['Autorun', 'Live', 'Plan', 'Preview', 'Stacked', 'Video']
        if capture_types is None:
            types_to_search = supported_types
        else:
            # Case-insensitive match for requested types
            types_to_search = [t for t in supported_types if t.lower() == capture_types[0].lower()]
            if not types_to_search:
                raise ValueError(f"Unsupported capture type. Allowed: {', '.join(supported_types)}")

        report = {
            'lights': {'discovered': 0, 'copied': 0, 'already_present': 0, 'collisions': 0, 'filters': {}},
            'flats': {'discovered': 0, 'copied': 0, 'already_present': 0, 'collisions': 0, 'matched_by_date': 0, 'filters': {}},
            'darks': {'copied': 0, 'already_present': 0, 'collisions': 0},
            'bias': {'copied': 0, 'already_present': 0, 'collisions': 0},
            'collisions': 0,
            'warnings': []
        }

        # 1. Discover and Copy Lights
        light_observation_dates = set()
        light_files_copied = []

        for ct in types_to_search:
            ct_dir = self.source_dir / ct
            if not ct_dir.exists():
                continue
            
            # <source-directory>/<CaptureType>/Light/<project-name>
            light_src_dir = ct_dir / 'Light' / self.project_name
            if light_src_dir.exists():
                files = get_all_fit_files(light_src_dir)
                report['lights']['discovered'] += len(files)
                
                for f in files:
                    dest_dir = self.project_path / 'lights'
                    safe_copy(f, dest_dir, report['lights'])
                    
                    # Analysis for dates/filters
                    header = get_fits_header(f)
                    date = get_observation_date(header)
                    if date:
                        light_observation_dates.add(date)
                    else:
                        report['warnings'].append(f"Light frame {f.name} missing observation date.")
                    
                    light_files_copied.append((f, header))

        if not light_files_copied:
            raise RuntimeError("No matching light .fit files found in any selected capture types.")

        # 2. Discover and Copy Flats
        for ct in types_to_search:
            ct_dir = self.source_dir / ct
            if not ct_dir.exists():
                continue
            
            # Preferred: <source-directory>/<CaptureType>/Flat/<project-name>
            flat_src_dir_named = ct_dir / 'Flat' / self.project_name
            flat_src_dir_shared = ct_dir / 'Flat'
            
            if flat_src_dir_named.exists():
                files = get_all_fit_files(flat_src_dir_named)
                report['flats']['discovered'] += len(files)
                for f in files:
                    dest_dir = self.project_path / 'flats'
                    safe_copy(f, dest_dir, report['flats'])
            elif flat_src_dir_shared.exists():
                files = get_all_fit_files(flat_src_dir_shared)
                # Fallback: Date matching
                for f in files:
                    report['flats']['discovered'] += 1
                    header = get_fits_header(f)
                    flat_date = get_observation_date(header)
                    
                    if not flat_date:
                        report['warnings'].append(f"Shared flat {f.name} skipped: missing observation date.")
                        continue
                    
                    # Match within +/- 1 day of any light
                    match = False
                    for l_date in light_observation_dates:
                        if abs((flat_date - l_date).days) <= 1:
                            match = True
                            break
                    
                    if match:
                        report['flats']['matched_by_date'] += 1
                        dest_dir = self.project_path / 'flats'
                        safe_copy(f, dest_dir, report['flats'])

        # 3. Darks and Bias
        for ct in types_to_search:
            ct_dir = self.source_dir / ct
            if not ct_dir.exists():
                continue
            
            dark_dir = ct_dir / 'Dark'
            if dark_dir.exists():
                files = get_all_fit_files(dark_dir)
                dest_dir = self.calibration_root / 'darks'
                for f in files:
                    safe_copy(f, dest_dir, report['darks'])
            
            bias_dir = ct_dir / 'Bias'
            if bias_dir.exists():
                files = get_all_fit_files(bias_dir)
                dest_dir = self.calibration_root / 'bias'
                for f in files:
                    safe_copy(f, dest_dir, report['bias'])

        # 4. Sort Lights and Flats by Filter
        # Sort Lights
        lights_root = self.project_path / 'lights'
        for f in lights_root.glob('*.fit'):
            header = get_fits_header(f)
            filter_name = get_filter_name(header) or 'Unknown'
            filter_dir = lights_root / filter_name
            filter_dir.mkdir(exist_ok=True)
            
            # Use a temporary file to avoid issues with moving to the same dir
            target = filter_dir / f.name
            if target.exists() and calculate_hash_fixed(f) == calculate_hash_fixed(target):
                # This shouldn't happen if sorted correctly, but for safety
                f.unlink()
            else:
                # Collision check for sorting (should already be handled by safe_copy, 
                # but here we are moving within the project tree)
                if target.exists() and calculate_hash_fixed(f) != calculate_hash_fixed(target):
                    stem = target.stem
                    suffix = target.suffix
                    c = 1
                    while (filter_dir / f"{stem}_{c}{suffix}").exists():
                        c += 1
                    target = filter_dir / f"{stem}_{c}{suffix}"
                
                f.rename(target)
                report['lights']['filters'][filter_name] = report['lights']['filters'].get(filter_name, 0) + 1

        # Sort Flats
        flats_root = self.project_path / 'flats'
        for f in flats_root.glob('*.fit'):
            header = get_fits_header(f)
            filter_name = get_filter_name(header) or 'Unknown'
            filter_dir = flats_root / filter_name
            filter_dir.mkdir(exist_ok=True)
            
            target = filter_dir / f.name
            if target.exists() and calculate_hash_fixed(f) == calculate_hash_fixed(target):
                f.unlink()
            else:
                if target.exists() and calculate_hash_fixed(f) != calculate_hash_fixed(target):
                    stem = target.stem
                    suffix = target.suffix
                    c = 1
                    while (filter_dir / f"{stem}_{c}{suffix}").exists():
                        c += 1
                    target = filter_dir / f"{stem}_{c}{suffix}"
                
                f.rename(target)
                report['flats']['filters'][filter_name] = report['flats']['filters'].get(filter_name, 0) + 1

        return report
