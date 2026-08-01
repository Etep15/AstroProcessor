import os
import shutil
import hashlib
from pathlib import Path
from datetime import timedelta
from .fits_utils import get_fits_header, get_observation_date, get_filter_name
from .progress import ProgressReporter

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
    
    # Check if the file already exists in the destination directory.
    # For calibration files, we only care if it exists in its specific calibration dir.
    # For lights/flats, we may have grouped by filter, but safe_copy is called with
    # the filter-specific dest_dir in recent versions, or the general one.
    
    found_identical = False
    # We search for the filename in the immediate dest_dir and subdirectories.
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
    def __init__(self, project_name, source_dir, projects_root, calibration_root, source_project_name=None):
        self.project_name = project_name
        self.source_dir = Path(source_dir).resolve()
        self.projects_root = Path(projects_root).resolve()
        self.calibration_root = Path(calibration_root).resolve()
        self.source_project_name = source_project_name or project_name
        
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

        # 1. Lights
        light_observation_dates = set()
        light_files_copied = []
        all_light_files = []
        for ct in types_to_search:
            ct_dir = self.source_dir / ct
            if not ct_dir.exists():
                continue
            light_src_dir = ct_dir / 'Light' / self.source_project_name
            if light_src_dir.exists():
                all_light_files.extend(get_all_fit_files(light_src_dir))

        if not all_light_files:
            print("No Light files found.", flush=True)
            # Existing behavior is to raise RuntimeError if no lights are found
            raise RuntimeError("No matching light .fit files found in any selected capture types.")

        # Group by filter
        light_groups = {}
        for f in all_light_files:
            header = get_fits_header(f)
            filter_name = get_filter_name(header) or 'Unknown'
            light_groups.setdefault(filter_name, []).append((f, header))

        for filter_name, files in light_groups.items():
            reporter = ProgressReporter(f"Copying {len(files)} Lights - {filter_name}", len(files))
            group_stats = {'copied': 0, 'already_present': 0, 'collisions': 0}
            try:
                for f, header in files:
                    dest_dir = self.project_path / 'lights'
                    safe_copy(f, dest_dir, group_stats)
                    reporter.increment()
                    
                    date = get_observation_date(header)
                    if date:
                        light_observation_dates.add(date)
                    else:
                        report['warnings'].append(f"Light frame {f.name} missing observation date.")
                    light_files_copied.append((f, header))
                
                stats_text = f"{group_stats['copied']} copied, {group_stats['already_present']} already present"
                reporter.finish(stats_text)
                
                # Update main report
                report['lights']['copied'] += group_stats['copied']
                report['lights']['already_present'] += group_stats['already_present']
                report['lights']['collisions'] += group_stats['collisions']
                report['lights']['filters'][filter_name] = len(files)
            except Exception as e:
                reporter.fail(str(e))
                raise

        report['lights']['discovered'] = len(all_light_files)

        # 2. Flats
        all_flat_candidates = []
        for ct in types_to_search:
            ct_dir = self.source_dir / ct
            if not ct_dir.exists():
                continue
            flat_src_dir_named = ct_dir / 'Flat' / self.project_name
            if flat_src_dir_named.exists():
                all_flat_candidates.extend([(f, 'named') for f in get_all_fit_files(flat_src_dir_named)])
            flat_src_dir_shared = ct_dir / 'Flat'
            if flat_src_dir_shared.exists():
                all_flat_candidates.extend([(f, 'shared') for f in get_all_fit_files(flat_src_dir_shared)])

        # Filter shared and group by filter
        final_flats = []
        for f, source_type in all_flat_candidates:
            if source_type == 'named':
                final_flats.append(f)
            else:
                header = get_fits_header(f)
                flat_date = get_observation_date(header)
                if not flat_date:
                    report['warnings'].append(f"Shared flat {f.name} skipped: missing observation date.")
                    continue
                if any(abs((flat_date - l_date).days) <= 1 for l_date in light_observation_dates):
                    final_flats.append(f)
                    report['flats']['matched_by_date'] += 1

        if not final_flats:
            print("No matching Flat files found.", flush=True)
        else:
            flat_groups = {}
            for f in final_flats:
                header = get_fits_header(f)
                filter_name = get_filter_name(header) or 'Unknown'
                flat_groups.setdefault(filter_name, []).append(f)
            
            for filter_name, files in flat_groups.items():
                reporter = ProgressReporter(f"Copying {len(files)} Flats - {filter_name}", len(files))
                group_stats = {'copied': 0, 'already_present': 0, 'collisions': 0}
                try:
                    for f in files:
                        dest_dir = self.project_path / 'flats'
                        safe_copy(f, dest_dir, group_stats)
                        reporter.increment()
                    
                    stats_text = f"{group_stats['copied']} copied, {group_stats['already_present']} already present"
                    reporter.finish(stats_text)
                    
                    report['flats']['copied'] += group_stats['copied']
                    report['flats']['already_present'] += group_stats['already_present']
                    report['flats']['collisions'] += group_stats['collisions']
                    report['flats']['filters'][filter_name] = len(files)
                except Exception as e:
                    reporter.fail(str(e))
                    raise

        report['flats']['discovered'] = len(final_flats)

        # 3. Darks and Bias
        for frame_type in ['Dark', 'Bias']:
            frame_key = 'darks' if frame_type == 'Dark' else 'bias'
            for ct in types_to_search:
                ct_dir = self.source_dir / ct
                if not ct_dir.exists():
                    continue
                f_dir = ct_dir / frame_type
                if not f_dir.exists():
                    continue
                
                files = get_all_fit_files(f_dir)
                if not files:
                    continue
                
                # Determine date from first eligible image in this source folder
                obs_date = None
                for f in files:
                    header = get_fits_header(f)
                    obs_date = get_observation_date(header)
                    if obs_date:
                        break
                
                if obs_date:
                    date_str = obs_date.strftime('%Y-%m-%d')
                    dest_dir = self.calibration_root / frame_key / date_str
                    print(f"Sorting {frame_type}s into {dest_dir}...", flush=True)
                else:
                    # Fallback to root calibration folder if no date found
                    dest_dir = self.calibration_root / frame_key
                    print(f"No date found for {frame_type}s in {f_dir}, using root {dest_dir}", flush=True)

                reporter = ProgressReporter(f"Copying {len(files)} {frame_type}s", len(files))
                group_stats = {'copied': 0, 'already_present': 0, 'collisions': 0}
                try:
                    for f in files:
                        safe_copy(f, dest_dir, group_stats)
                        reporter.increment()
                    stats_text = f"{group_stats['copied']} copied, {group_stats['already_present']} already present"
                    reporter.finish(stats_text)
                    
                    report[frame_key]['copied'] += group_stats['copied']
                    report[frame_key]['already_present'] += group_stats['already_present']
                    report[frame_key]['collisions'] += group_stats['collisions']
                except Exception as e:
                    reporter.fail(str(e))
                    raise

        # 4. Sorting
        for frame_type in ['lights', 'flats']:
            root_dir = self.project_path / frame_type
            files_to_sort = list(root_dir.glob('*.fit'))
            if not files_to_sort:
                continue
                
            reporter = ProgressReporter(f"Sorting {len(files_to_sort)} {frame_type.capitalize()} by filter", len(files_to_sort))
            try:
                for f in files_to_sort:
                    header = get_fits_header(f)
                    filter_name = get_filter_name(header) or 'Unknown'
                    filter_dir = root_dir / filter_name
                    filter_dir.mkdir(exist_ok=True)
                    
                    target = filter_dir / f.name
                    if target.exists() and calculate_hash_fixed(f) == calculate_hash_fixed(target):
                        f.unlink()
                    else:
                        if target.exists() and calculate_hash_fixed(f) != calculate_hash_fixed(target):
                            stem, suffix = target.stem, target.suffix
                            c = 1
                            while (filter_dir / f"{stem}_{c}{suffix}").exists():
                                c += 1
                            target = filter_dir / f"{stem}_{c}{suffix}"
                        f.rename(target)
                    reporter.increment()
                
                dist = report[frame_type]['filters']
                dist_text = ", ".join([f"{k}: {v}" for k, v in dist.items()])
                reporter.finish(dist_text if dist_text else "")
            except Exception as e:
                reporter.fail(str(e))
                raise

        # 5. Calibration Sorting (Loose files)
        for frame_type in ['darks', 'bias']:
            calib_dir = self.calibration_root / frame_type
            if not calib_dir.exists():
                continue
                
            loose_files = [f for f in calib_dir.glob('*.fit')]
            if not loose_files:
                continue
            
            # Determine date from first eligible loose image
            obs_date = None
            for f in loose_files:
                header = get_fits_header(f)
                obs_date = get_observation_date(header)
                if obs_date:
                    break
            
            if obs_date:
                date_str = obs_date.strftime('%Y-%m-%d')
                target_dir = calib_dir / date_str
                print(f"Sorting existing loose {frame_type} into {target_dir}...", flush=True)
                target_dir.mkdir(exist_ok=True)
                
                for f in loose_files:
                    target_path = target_dir / f.name
                    if target_path.exists():
                        if calculate_hash_fixed(f) == calculate_hash_fixed(target_path):
                            f.unlink()
                        else:
                            stem, suffix = target_path.stem, target_path.suffix
                            c = 1
                            while (target_dir / f"{stem}_{c}{suffix}").exists():
                                c += 1
                            f.rename(target_dir / f"{stem}_{c}{suffix}")
                    else:
                        f.rename(target_path)
            else:
                print(f"No date found in loose {frame_type} files; skipping sort.", flush=True)

        return report
