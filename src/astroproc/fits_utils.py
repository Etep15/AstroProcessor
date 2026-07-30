import os
from pathlib import Path
from astropy.io import fits
from datetime import datetime, timedelta

def get_fits_header(path):
    """Reads the FITS header and returns a dictionary of keys."""
    try:
        with fits.open(path, mode='readonly') as hdul:
            return hdul[0].header
    except Exception:
        return None

def get_observation_date(header):
    """Extracts the observation date from the FITS header."""
    if not header:
        return None
    
    # Prefer DATE-OBS
    date_val = header.get('DATE-OBS')
    if not date_val:
        return None
    
    try:
        # FITS DATE-OBS is usually 'YYYY-MM-DD' or 'YYYY-MM-DDThh:mm:ss'
        if 'T' in date_val:
            return datetime.strptime(date_val.split('T')[0], '%Y-%m-%d').date()
        return datetime.strptime(date_val, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None

def get_filter_name(header):
    """Extracts and sanitizes the filter name from the FITS header."""
    if not header:
        return None
    
    filter_val = header.get('FILTER')
    if not filter_val:
        return None
    
    # Trim whitespace
    filter_val = str(filter_val).strip()
    if not filter_val:
        return None
    
    # Canonical mappings
    canonical = {
        'ha': 'Ha',
        'oiii': 'OIII',
        'sii': 'SII',
        'r': 'R',
        'g': 'G',
        'b': 'B',
        'l': 'L'
    }
    
    low_val = filter_val.lower()
    if low_val in canonical:
        return canonical[low_val]
    
    return filter_val
