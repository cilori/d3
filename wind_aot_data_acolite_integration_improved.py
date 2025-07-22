"""
This script integrates dynamic retrieval of wind and AOT (Aerosol Optical Thickness) data into the Acolite processing workflow.
The script performs the following tasks:

1. Extracts metadata (date, latitude, and longitude) from various satellite image metadata files (currently works for Planet, WorldView and Sentinel-2).
2. Downloads wind data from the NOAA CoastWatch server based on the extracted date, lat and lon.
3. Retrieves AOT data from the NASA Earthdata server using the extracted date, latitude, and longitude.
4. Runs the Acolite atmospheric correction using the dynamically retrieved wind and AOT data.
5. Provides a GUI for user input to customize Acolite processing parameters.
6. Stacks processed raster bands into a single file for easier analysis.
7. Append _Rrs to the stacked Rrs for downstream processing (mainly for IDA RTM)
8. Assign -9999 to nodata_value

Usage:
To run the script, use the following command:

    python wind_aot_data_acolite_integration.py --output <output_directory> --inputs <input_directories>

Arguments:
    --output: Specifies the output directory where processed files will be saved.
    --inputs: A list of input directories containing the satellite image metadata files to be processed. (Can use more than one input directory)

"""

import os
import re
import sys
import argparse
import logging
from datetime import datetime, timedelta
from pathlib import Path
import subprocess
from typing import Optional, Tuple, List, Dict, Any
import time
import numpy as np
import pandas as pd
import requests
from metpy.calc import wind_speed  # type: ignore
from metpy.units import units  # type: ignore
from netCDF4 import Dataset
from requests.adapters import HTTPAdapter
from requests import Session
from osgeo import gdal, osr
import xml.etree.ElementTree as ET
from urllib.parse import urlparse
import tempfile
import shutil
from concurrent.futures import ThreadPoolExecutor
from tqdm import tqdm
import multiprocessing
from dataclasses import dataclass
from contextlib import contextmanager

# Constants for AOT retrieval
DEFAULT_CHUNK_SIZE = 131072
BLOCKSIZE = 65536
APP_KEY = "67542f3132f114592c6655dcd7a69ae13b4d5898"

# Configure logging (fix duplicate logging configuration)
logging.basicConfig(
    level=logging.INFO, 
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('acolite_processing.log')
    ]
)
logger = logging.getLogger(__name__)

# Global session for AOT retrieval
obpgSession = None

# Resampling constants and functions
BAND_RESOLUTIONS = {
    "B01": 60, "B02": 10, "B03": 10, "B04": 10, "B05": 20, "B06": 20,
    "B07": 20, "B08": 10, "B8A": 20, "B09": 60, "B10": 60, "B11": 20, "B12": 20,
}

@dataclass
class ProcessingConfig:
    """Configuration for processing parameters"""
    acolite_script_path: str = "launch_acolite.py"
    max_workers: int = None
    default_wind_speed: float = 5.0
    default_aot: float = 0.1
    nodata_value: float = -9999
    temp_cleanup: bool = True

@contextmanager
def temp_file_manager(suffix: str = '.tmp', dir: Optional[str] = None):
    """Context manager for temporary files with automatic cleanup"""
    temp_file = tempfile.NamedTemporaryFile(suffix=suffix, dir=dir, delete=False)
    temp_path = Path(temp_file.name)
    temp_file.close()
    
    try:
        yield temp_path
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except Exception as e:
                logger.warning(f"Could not remove temporary file {temp_path}: {e}")

class ProcessingError(Exception):
    """Custom exception for processing errors"""
    pass

def validate_coordinates(latitude: float, longitude: float) -> bool:
    """Validate coordinate ranges"""
    return -90 <= latitude <= 90 and -180 <= longitude <= 180

def get_band_id(filename: str) -> str:
    """Extract band ID from filename"""
    for band in BAND_RESOLUTIONS:
        if f"_{band}" in filename.upper():
            return band
    return ""

def find_jp2_files(safe_dir: Path) -> List[Path]:
    """Find JP2 files in Sentinel-2 SAFE directory"""
    img_data_path = safe_dir / "GRANULE"
    granule_dirs = list(img_data_path.glob("*"))
    if not granule_dirs:
        raise FileNotFoundError(f"No granule directories found in {img_data_path}")
    
    img_data_dir = granule_dirs[0] / "IMG_DATA"
    jp2_files = sorted([f for f in img_data_dir.glob("*.jp2") if "TCI" not in f.name.upper()])
    
    if not jp2_files:
        raise FileNotFoundError(f"No JP2 files found in {img_data_dir}")
    
    return jp2_files

def get_10m_grid(jp2_files: List[Path]) -> Tuple[Any, str, int, int]:
    """Get reference grid from 10m bands"""
    for jp2 in jp2_files:
        band = get_band_id(jp2.name)
        if BAND_RESOLUTIONS.get(band) == 10:
            with gdal.Open(str(jp2)) as ds:
                if ds is None:
                    continue
                geotransform = ds.GetGeoTransform()
                projection = ds.GetProjection()
                rows = ds.RasterYSize
                cols = ds.RasterXSize
                return geotransform, projection, rows, cols
    
    raise RuntimeError("No 10m band found to define target grid.")

def resample_to_10m(jp2_path: Path, target_cols: int, target_rows: int, 
                   target_geotransform: Tuple, target_projection: str) -> None:
    """Resample JP2 file to 10m resolution"""
    with temp_file_manager(suffix='.tif', dir=jp2_path.parent) as temp_tif_path:
        with temp_file_manager(suffix='.jp2', dir=jp2_path.parent) as temp_jp2_path:
            
            # Open source dataset
            src_ds = gdal.Open(str(jp2_path))
            if src_ds is None:
                raise ProcessingError(f"Could not open {jp2_path}")
            
            try:
                # Create temporary GeoTIFF
                gtiff_driver = gdal.GetDriverByName("GTiff")
                temp_ds = gtiff_driver.Create(
                    str(temp_tif_path), target_cols, target_rows, 1, gdal.GDT_UInt16
                )
                temp_ds.SetGeoTransform(target_geotransform)
                temp_ds.SetProjection(target_projection)
                
                # Reproject
                gdal.ReprojectImage(
                    src_ds, temp_ds, src_ds.GetProjection(), 
                    target_projection, gdal.GRA_Bilinear
                )
                temp_ds.FlushCache()
                temp_ds = None
                src_ds = None
                
                # Convert back to JP2
                temp_tif_ds = gdal.Open(str(temp_tif_path))
                jp2_driver = get_jp2_driver()
                
                if not jp2_driver:
                    raise ProcessingError("No JP2 driver available")
                
                options = ["QUALITY=100"] if "OpenJPEG" in jp2_driver.GetDescription() else []
                jp2_ds = jp2_driver.CreateCopy(str(temp_jp2_path), temp_tif_ds, options=options)
                
                if jp2_ds is None:
                    raise ProcessingError(f"Failed to create JP2 file with {jp2_driver.GetDescription()}")
                
                jp2_ds.FlushCache()
                temp_tif_ds = None
                jp2_ds = None
                
                # Move result to original location
                shutil.move(str(temp_jp2_path), str(jp2_path))
                
            finally:
                if src_ds:
                    src_ds = None

def get_jp2_driver():
    """Get available JP2 driver"""
    for driver_name in ["JP2OpenJPEG", "JPEG2000", "JP2KAK"]:
        try:
            driver = gdal.GetDriverByName(driver_name)
            if driver:
                return driver
        except Exception:
            continue
    return None

def process_band(args: Tuple[Path, int, int, Tuple, str]) -> str:
    """Process a single band for resampling"""
    jp2_path, target_cols, target_rows, target_geotransform, target_projection = args
    
    band = get_band_id(jp2_path.name)
    if BAND_RESOLUTIONS.get(band) == 10:
        return f"Skipped (already 10m): {jp2_path.name}"
    elif BAND_RESOLUTIONS.get(band) in (20, 60):
        try:
            resample_to_10m(jp2_path, target_cols, target_rows, target_geotransform, target_projection)
            return f"Resampled: {jp2_path.name}"
        except Exception as e:
            return f"Error resampling {jp2_path.name}: {str(e)}"
    else:
        return f"Unknown band: {jp2_path.name}"

def resample_sentinel2_bands(safe_dir: str, config: ProcessingConfig) -> None:
    """Resample Sentinel-2 20m and 60m bands to 10m resolution"""
    logger.info(f"Resampling Sentinel-2 bands in {safe_dir}")
    
    safe_path = Path(safe_dir)
    jp2_files = find_jp2_files(safe_path)
    geotransform, projection, rows, cols = get_10m_grid(jp2_files)
    
    # Determine number of workers
    cpu_count = multiprocessing.cpu_count()
    max_workers = config.max_workers or max(1, int(cpu_count * 0.3))
    
    # Prepare arguments for parallel processing
    args_list = [
        (jp2_path, cols, rows, geotransform, projection)
        for jp2_path in jp2_files
    ]
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        results = list(tqdm(
            executor.map(process_band, args_list),
            total=len(args_list),
            desc="Resampling bands"
        ))
    
    for result in results:
        logger.info(result)
    
    logger.info("Sentinel-2 band resampling completed")

def calculate_utm_zone(longitude: float, latitude: float) -> Tuple[int, str]:
    """Calculate the appropriate UTM zone and EPSG code"""
    if not validate_coordinates(latitude, longitude):
        raise ValueError(f"Invalid coordinates: lat={latitude}, lon={longitude}")
    
    utm_zone = int((longitude + 180) / 6) + 1
    utm_zone = max(1, min(60, utm_zone))  # Clamp to valid range
    
    if latitude >= 0:
        epsg_code = f"326{utm_zone:02d}"
    else:
        epsg_code = f"327{utm_zone:02d}"
    
    return utm_zone, epsg_code

def get_utm_epsg(longitude: float, latitude: float) -> str:
    """Get EPSG code for UTM zone"""
    _, epsg_code = calculate_utm_zone(longitude, latitude)
    return epsg_code

class MetadataExtractor:
    """Class to handle metadata extraction from different satellite image formats"""

    @staticmethod
    def find_metadata_file(input_dir: str, extension: str) -> Optional[str]:
        """Find a specific metadata file in the input directory"""
        for root, _, files in os.walk(input_dir):
            for file in files:
                if file.endswith(extension):
                    return os.path.join(root, file)
        return None

    @staticmethod
    def extract_date_lat_lon_from_imd(imd_file: str) -> Tuple[datetime, float, float]:
        """Extract date and center coordinates from WorldView IMD file"""
        metadata = {}
        current_group = None
        band_coordinates = {}
        
        with open(imd_file, 'r', encoding='utf-8') as file:
            for line in file:
                line = line.strip()
                
                if line.startswith('BEGIN_GROUP = BAND_'):
                    current_group = line.split('=')[1].strip()
                    band_coordinates[current_group] = {}
                elif line.startswith('END_GROUP'):
                    current_group = None
                elif '=' in line and current_group:
                    key, value = line.split('=', 1)
                    key = key.strip()
                    value = value.strip().strip(';').strip('"')
                    band_coordinates[current_group][key] = value
                elif '=' in line and not current_group:
                    key, value = line.split('=', 1)
                    key = key.strip()
                    value = value.strip().strip(';').strip('"')
                    metadata[key] = value
        
        # Use coordinates from first band
        if not band_coordinates:
            raise ValueError("No band coordinate information found in IMD file")
        
        first_band = list(band_coordinates.keys())[0]
        required_coords = ['ULLat', 'ULLon', 'URLat', 'URLon', 'LLLat', 'LLLon', 'LRLat', 'LRLon']
        
        if not all(coord in band_coordinates[first_band] for coord in required_coords):
            raise ValueError("Missing coordinate information in IMD file")
        
        coords = band_coordinates[first_band]
        
        # Calculate center coordinates
        try:
            ul_lat = float(coords["ULLat"])
            ul_lon = float(coords["ULLon"])
            ur_lat = float(coords["URLat"])
            ur_lon = float(coords["URLon"])
            ll_lat = float(coords["LLLat"])
            ll_lon = float(coords["LLLon"])
            lr_lat = float(coords["LRLat"])
            lr_lon = float(coords["LRLon"])
            
            center_lat = (ul_lat + ur_lat + ll_lat + lr_lat) / 4
            center_lon = (ul_lon + ur_lon + ll_lon + lr_lon) / 4
            
            if not validate_coordinates(center_lat, center_lon):
                raise ValueError(f"Invalid calculated coordinates: lat={center_lat}, lon={center_lon}")
            
        except (ValueError, KeyError) as e:
            raise ValueError(f"Error parsing coordinates from IMD file: {e}")
        
        # Extract date
        date_str = metadata.get("earliestAcqTime", "").split('T')[0]
        if not date_str:
            raise ValueError("No acquisition time found in IMD file")
        
        try:
            date = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError as e:
            raise ValueError(f"Error parsing date from IMD file: {e}")
        
        logger.info(f"WorldView center coordinates: ({center_lat:.6f}, {center_lon:.6f})")
        return date, center_lat, center_lon

    @staticmethod
    def parse_xml_file_for_date_lat_lon(xml_file: str) -> Tuple[datetime, float, float]:
        """Parse XML metadata file and extract date, latitude, and longitude"""
        date, latitude, longitude = None, None, None
        
        try:
            with open(xml_file, "r", encoding='utf-8') as file:
                for line in file:
                    if "<ps:acquisitionDateTime>" in line:
                        date_str = (
                            line.strip()
                            .replace("<ps:acquisitionDateTime>", "")
                            .replace("</ps:acquisitionDateTime>", "")
                        )
                        date = datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S%z")
                    if "<gml:pos>" in line:
                        pos_text = line.strip().replace("<gml:pos>", "").replace("</gml:pos>", "")
                        longitude, latitude = map(float, pos_text.split())
                        
                        if not validate_coordinates(latitude, longitude):
                            raise ValueError(f"Invalid coordinates in XML: lat={latitude}, lon={longitude}")
        
        except Exception as e:
            raise ValueError(f"Error parsing XML file {xml_file}: {e}")
        
        if date is None or latitude is None or longitude is None:
            raise ValueError(f"Incomplete metadata extracted from {xml_file}")
        
        return date, latitude, longitude

    @staticmethod
    def parse_sentinel2_metadata(xml_file: str) -> Tuple[datetime, float, float]:
        """Parse Sentinel-2 XML metadata file and extract date, latitude, and longitude"""
        try:
            tree = ET.parse(xml_file)
            root = tree.getroot()
            
            generation_time_elem = root.find(".//GENERATION_TIME")
            if generation_time_elem is None:
                raise ValueError("GENERATION_TIME not found in Sentinel-2 metadata")
            
            pos_list_elem = root.find(".//EXT_POS_LIST")
            if pos_list_elem is None:
                raise ValueError("EXT_POS_LIST not found in Sentinel-2 metadata")
            
            generation_time = generation_time_elem.text
            pos_list = pos_list_elem.text.strip().split()
            
            latitude = float(pos_list[0])
            longitude = float(pos_list[1])
            
            if not validate_coordinates(latitude, longitude):
                raise ValueError(f"Invalid coordinates in Sentinel-2 metadata: lat={latitude}, lon={longitude}")
            
            date = datetime.strptime(generation_time, "%Y-%m-%dT%H:%M:%S.%fZ")
            
        except Exception as e:
            raise ValueError(f"Error parsing Sentinel-2 metadata {xml_file}: {e}")
        
        return date, latitude, longitude

def download_wind_data(date: datetime, output_dir: str, output_filename: str) -> Optional[str]:
    """Download wind data for a specific date"""
    date_str = date.strftime("%Y%m%d")
    year_str = date.strftime("%Y")
    base_url = f"https://coastwatch.noaa.gov/thredds/fileServer/uvcompNCEIBlendedGlobalSCISixHourWW00/{year_str}/"
    filename = f"NBSv02_wind_6hourly_{date_str}.nc"
    url = f"{base_url}{filename}"

    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        
        output_path = os.path.join(output_dir, f"{output_filename}.nc")
        with open(output_path, "wb") as file:
            file.write(response.content)
        
        logger.info(f"Downloaded wind data: {output_path}")
        return output_path
    
    except requests.RequestException as e:
        logger.error(f"Error downloading wind data: {e}")
        return None

def extract_wind_speed(filename: str, latitude: float, longitude: float, 
                      target_datetime: datetime, output_filepath: Optional[str] = None) -> Tuple[float, pd.DataFrame]:
    """Extract wind speed from NetCDF file"""
    
    if not validate_coordinates(latitude, longitude):
        raise ValueError(f"Invalid coordinates: lat={latitude}, lon={longitude}")
    
    try:
        with Dataset(filename, 'r') as dataset:
            latitudes = dataset.variables["lat"][:]
            longitudes = dataset.variables["lon"][:]
            
            # Find nearest grid point
            lat_index = (np.abs(latitudes - latitude)).argmin()
            lon_index = (np.abs(longitudes - longitude)).argmin()

            u_wind = dataset.variables["u_wind"]
            v_wind = dataset.variables["v_wind"]
            time_variable = dataset.variables["time"]

            u_wind_values = u_wind[:, 0, lat_index, lon_index].data
            v_wind_values = v_wind[:, 0, lat_index, lon_index].data
            time_values = time_variable[:]

            # Convert times
            base_date = datetime(1970, 1, 1)
            seconds_since_1970 = time_values.data * 3600 + (1978 - 1970) * 365.25 * 24 * 3600
            date_times = [base_date + timedelta(seconds=sec) for sec in seconds_since_1970]

            # Make target_datetime timezone-naive for comparison
            target_datetime = target_datetime.replace(tzinfo=None)

            # Find closest time
            time_deltas = [abs((target_datetime - dt).total_seconds()) for dt in date_times]
            closest_time_index = np.argmin(time_deltas)
            closest_time_str = date_times[closest_time_index].strftime("%Y-%m-%d %H:%M:%S")

            # Calculate wind speed
            wind_speed_values = wind_speed(
                u=u_wind_values * units(u_wind.units), 
                v=v_wind_values * units(v_wind.units)
            )
            closest_wind_speed = float(wind_speed_values[closest_time_index].magnitude)

            wind_data = {
                "windspeed": [closest_wind_speed],
                "wind_speed_date_time": [closest_time_str],
                "input_date_time": [target_datetime.strftime("%Y-%m-%d %H:%M:%S")],
                "wind_speed_height": [v_wind.height if hasattr(v_wind, 'height') else 10],
                "filename": [filename],
                "longitude": [longitude],
                "latitude": [latitude],
            }

            wind_speed_df = pd.DataFrame(wind_data)

            if output_filepath:
                wind_speed_df.to_csv(output_filepath, index=False)

            return closest_wind_speed, wind_speed_df
    
    except Exception as e:
        logger.error(f"Error extracting wind speed from {filename}: {e}")
        raise ProcessingError(f"Failed to extract wind speed: {e}")

def get_session(verbose: int = 0, ntries: int = 5) -> Session:
    """Get or create a session for AOT retrieval"""
    global obpgSession
    if not obpgSession:
        obpgSession = Session()
        obpgSession.mount("https://", HTTPAdapter(max_retries=ntries))
        if verbose > 1:
            logger.debug("Created new session for AOT retrieval")
    return obpgSession

def retrieve_aot_data(date: datetime, lat: float, lon: float, output_dir: str) -> Optional[float]:
    """Retrieve AOT data for a specific date"""
    
    if not validate_coordinates(lat, lon):
        raise ValueError(f"Invalid coordinates: lat={lat}, lon={lon}")
    
    # Find closest available times
    base_times = [0, 6, 12, 18]
    times = [date.replace(hour=bt, minute=0, second=0, microsecond=0) for bt in base_times]
    times.sort(key=lambda x: abs((x - date).total_seconds()))
    
    # Try current day first, then adjacent days
    days_to_try = [date, date - timedelta(days=1), date + timedelta(days=1)]
    
    for day in days_to_try:
        for time_candidate in times:
            time_candidate = time_candidate.replace(year=day.year, month=day.month, day=day.day)
            date_str = time_candidate.strftime("%Y%m%dT%H%M%S")
            nc_filename = f"GMAO_MERRA2.{date_str}.AER.nc"
            output_file = os.path.join(output_dir, nc_filename)

            if os.path.exists(output_file):
                logger.info(f"Using existing AOT file: {nc_filename}")
                break
            
            # Try to download
            url = f"https://oceandata.sci.gsfc.nasa.gov/getfile/{nc_filename}?appkey={APP_KEY}"
            try:
                response = requests.get(url, timeout=30)
                if response.status_code == 200:
                    with open(output_file, 'wb') as f:
                        f.write(response.content)
                    logger.info(f"Downloaded AOT data: {nc_filename}")
                    break
            except requests.RequestException as e:
                logger.debug(f"Failed to download {nc_filename}: {e}")
                continue
        else:
            continue  # Continue to next day if no files found
        break  # Break out of day loop if file was found/downloaded
    else:
        logger.warning("No AOT data available for the specified date range")
        return None

    # Extract AOT value
    try:
        with Dataset(output_file, "r") as nc:
            lons = nc.variables["lon"][:]
            lats = nc.variables["lat"][:]
            
            lon_idx = (np.abs(lons - lon)).argmin()
            lat_idx = (np.abs(lats - lat)).argmin()
            
            varname = "TOTEXTTAU"
            if varname not in nc.variables:
                logger.error(f"Variable {varname} not found in AOT file")
                return None
            
            var = nc.variables[varname]
            if "time" in var.dimensions:
                totexttau = float(var[0, lat_idx, lon_idx])
            else:
                totexttau = float(var[lat_idx, lon_idx])
            
            logger.info(f"TOTEXTTAU at ({lat}, {lon}) for {date}: {totexttau}")
            return totexttau
    
    except Exception as e:
        logger.error(f"Error extracting AOT from {output_file}: {e}")
        return None

class AcoliteRunner:
    """Class to run Acolite processing"""

    def __init__(self, output_directory: str, config: ProcessingConfig):
        self.output_directory = output_directory
        self.config = config

    def configure_acolite_settings(self, input_directories: List[str], params: Dict[str, Any]) -> List[str]:
        """Configure Acolite settings files"""
        settings_files = []
        
        for input_dir in input_directories:
            # Get base name for main folder
            base_name = self._get_base_folder_name(input_dir)

            # Create main folder
            main_folder = os.path.join(self.output_directory, base_name)
            os.makedirs(main_folder, exist_ok=True)

            # Get unique name and next version
            unique_name = os.path.basename(os.path.normpath(input_dir))
            output_folder = self._get_next_version(unique_name, main_folder)

            # Create versioned output folder inside main folder
            output_folder_path = os.path.join(main_folder, output_folder)
            os.makedirs(output_folder_path, exist_ok=True)

            # Create settings file
            settings_file_name = f"{unique_name}_settings.txt"
            settings_file_path = os.path.join(output_folder_path, settings_file_name)
            settings_files.append(settings_file_path)

            with open(settings_file_path, "w", encoding='utf-8') as file:
                file.write(f"inputfile={input_dir}\n")
                file.write(f"output={output_folder_path}\n")
                for key, value in params.items():
                    file.write(f"{key}={value}\n")

        return settings_files

    def _get_base_folder_name(self, input_folder: str) -> str:
        """Extract base folder name without version suffix"""
        base_name = re.sub(r"\.SAFE_v\d+$", "", os.path.basename(input_folder))
        return base_name

    def _get_next_version(self, base_name: str, main_folder: str) -> str:
        """Get next version number for output folder inside main folder"""
        version = 0
        while os.path.exists(os.path.join(main_folder, f"{base_name}_v{version}")):
            version += 1
        return f"{base_name}_v{version}"

    def run_acolite_direct(self, settings_files: List[str]) -> None:
        """Run Acolite processing directly"""
        for settings_file in settings_files:
            command_string = f"python {self.config.acolite_script_path} --cli --settings={settings_file}"
            logger.info(f"Running Acolite with settings: {settings_file}")
            
            try:
                result = subprocess.run(
                    command_string, 
                    shell=True, 
                    check=True,
                    capture_output=True,
                    text=True
                )
                logger.info(f"Acolite processing completed successfully for {settings_file}")
                
            except subprocess.CalledProcessError as e:
                logger.error(f"Acolite processing failed for {settings_file}: {e}")
                if e.stdout:
                    logger.error(f"Stdout: {e.stdout}")
                if e.stderr:
                    logger.error(f"Stderr: {e.stderr}")
                raise ProcessingError(f"Acolite processing failed: {e}")

    def stack_bands(self) -> None:
        """Stack Rrs bands into multi-band files"""
        for root, _, files in os.walk(self.output_directory):
            tif_files = [
                os.path.join(root, file)
                for file in files
                if file.endswith(".tif") and re.search(r"Rrs_\d{3}\.tif$", file)
            ]
            if tif_files:
                self._stack_rasters(tif_files, root)

    def _stack_rasters(self, input_files: List[str], output_folder: str) -> None:
        """Stack individual Rrs files into a single multi-band raster"""
        if not input_files:
            logger.warning("No input files to stack")
            return
        
        # Sort files by wavelength for consistent band order
        input_files.sort(key=lambda x: self._extract_number_from_filename(os.path.basename(x)))
        
        # Extract base name from the first file
        first_file = os.path.basename(input_files[0])
        match = re.search(r'(.*?)_L2W_Rrs_\d{3}\.tif', first_file)
        
        base_name = match.group(1) + "_Rrs" if match else "Rrs"
        output_path = os.path.join(output_folder, f"{base_name}.tif")
        
        try:
            # Open the first raster to get metadata
            with gdal.Open(input_files[0], gdal.GA_ReadOnly) as ref_ds:
                if ref_ds is None:
                    raise ProcessingError(f"Could not open reference file: {input_files[0]}")
                
                x_size, y_size = ref_ds.RasterXSize, ref_ds.RasterYSize
                geotransform, projection = ref_ds.GetGeoTransform(), ref_ds.GetProjection()

            # Create output raster with compression
            driver = gdal.GetDriverByName('GTiff')
            creation_options = [
                'COMPRESS=LZW',
                'TILED=YES',
                'BIGTIFF=IF_SAFER'
            ]
            
            out_ds = driver.Create(
                output_path, 
                x_size, 
                y_size, 
                len(input_files), 
                gdal.GDT_Float32,
                options=creation_options
            )
            
            if out_ds is None:
                raise ProcessingError(f"Could not create output file: {output_path}")
            
            out_ds.SetGeoTransform(geotransform)
            out_ds.SetProjection(projection)
            
            # Stack the bands
            for i, raster_path in enumerate(input_files, start=1):
                with gdal.Open(raster_path, gdal.GA_ReadOnly) as src_ds:
                    if src_ds is None:
                        logger.warning(f"Could not open {raster_path}, skipping")
                        continue
                    
                    band_data = src_ds.GetRasterBand(1).ReadAsArray()
                    band_data = np.nan_to_num(band_data, nan=self.config.nodata_value)
                    
                    out_band = out_ds.GetRasterBand(i)
                    out_band.WriteArray(band_data)
                    out_band.SetNoDataValue(self.config.nodata_value)
                    out_band.SetDescription(os.path.splitext(os.path.basename(raster_path))[0])
            
            out_ds.FlushCache()
            out_ds = None
            
            logger.info(f"Stacked raster saved to {output_path}")
            
        except Exception as e:
            logger.error(f"Error stacking rasters: {e}")
            raise ProcessingError(f"Failed to stack rasters: {e}")

    def _extract_number_from_filename(self, filename: str) -> int:
        """Extract wavelength number from filename"""
        match = re.search(r'Rrs_(\d{3})\.tif', filename)
        return int(match.group(1)) if match else -1

def detect_sensor_type(input_dir: str) -> str:
    """Detect sensor type from input directory"""
    if input_dir.endswith('.SAFE') or '.SAFE' in input_dir:
        return 'sentinel2'
    elif MetadataExtractor.find_metadata_file(input_dir, ".IMD"):
        return 'worldview'
    elif (MetadataExtractor.find_metadata_file(input_dir, "metadata_clip.xml") or 
          MetadataExtractor.find_metadata_file(input_dir, "metadata.xml")):
        return 'planet'
    else:
        return 'unknown'

def cleanup_individual_rrs_files(output_directory: str, keep_individual: bool = False) -> None:
    """Clean up individual Rrs files if not needed"""
    if keep_individual:
        logger.info("Keeping individual Rrs files as requested")
        return

    deleted_count = 0
    for root, _, files in os.walk(output_directory):
        for file in files:
            if file.endswith(".tif") and re.search(r"Rrs_\d{3,4}\.tif$", file):
                file_path = os.path.join(root, file)
                try:
                    os.remove(file_path)
                    deleted_count += 1
                    logger.debug(f"Deleted individual Rrs file: {file}")
                except Exception as e:
                    logger.error(f"Error deleting {file}: {e}")
    
    logger.info(f"Cleaned up {deleted_count} individual Rrs files")

def delete_files_by_extension(output_directory: str, extensions_to_delete: List[str]) -> None:
    """Delete files with specified extensions"""
    deleted_files = []
    for root, _, files in os.walk(output_directory):
        for file in files:
            if any(file.endswith(ext) for ext in extensions_to_delete):
                file_path = os.path.join(root, file)
                try:
                    os.remove(file_path)
                    deleted_files.append(file_path)
                    logger.debug(f"Deleted: {file_path}")
                except Exception as e:
                    logger.error(f"Error deleting {file_path}: {e}")
    
    logger.info(f"Deleted {len(deleted_files)} files with extensions {extensions_to_delete}")

def read_settings_file(settings_file_path: str) -> Dict[str, str]:
    """Read settings from a file and return them as a dictionary"""
    settings = {}
    try:
        with open(settings_file_path, "r", encoding='utf-8') as file:
            for line_num, line in enumerate(file, 1):
                line = line.strip()
                if line and not line.startswith("#"):
                    if "=" not in line:
                        logger.warning(f"Invalid line {line_num} in settings file: {line}")
                        continue
                    key, value = line.split("=", 1)
                    settings[key.strip()] = value.strip()
    except Exception as e:
        raise ProcessingError(f"Error reading settings file {settings_file_path}: {e}")
    
    return settings

def log_processing_time(output_dir: str, filename: str, start_time: float, success: bool = True) -> float:
    """Log the processing time for a file"""
    end_time = time.time()
    duration = end_time - start_time
    
    # Format the duration
    hours, remainder = divmod(duration, 3600)
    minutes, seconds = divmod(remainder, 60)
    formatted_duration = f"{int(hours):02d}:{int(minutes):02d}:{seconds:.2f}"
    
    # Format timestamps
    start_timestamp = datetime.fromtimestamp(start_time).strftime('%Y-%m-%d %H:%M:%S')
    end_timestamp = datetime.fromtimestamp(end_time).strftime('%Y-%m-%d %H:%M:%S')
    
    # Create/update log file
    log_file = os.path.join(output_dir, "processing_time.txt")
    
    if not os.path.exists(log_file):
        with open(log_file, "w", encoding='utf-8') as f:
            f.write(f"Processing Time Log - Created on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("=" * 80 + "\n")
            f.write("Filename | Start Time | End Time | Duration (seconds) | Duration (formatted) | Status\n")
            f.write("-" * 80 + "\n")
    
    status = "SUCCESS" if success else "FAILED"
    with open(log_file, "a", encoding='utf-8') as f:
        f.write(f"{filename} | {start_timestamp} | {end_timestamp} | {duration:.2f} | {formatted_duration} | {status}\n")
    
    return duration

def run_acolite_pipeline(params: Dict[str, Any], output_dir: str, input_directories: List[str], 
                        coordinates_dict: Dict[str, Tuple[float, float]], config: ProcessingConfig) -> None:
    """Run the complete Acolite processing pipeline"""
    
    acolite_runner = AcoliteRunner(output_directory=output_dir, config=config)
    
    try:
        settings_files = acolite_runner.configure_acolite_settings(input_directories, params)
        acolite_runner.run_acolite_direct(settings_files)
        acolite_runner.stack_bands()
        
        cleanup_individual_rrs_files(
            output_dir, 
            keep_individual=params.get("keep_individual_rrs", True)
        )
        delete_files_by_extension(output_dir, [".png", ".nc", ".csv"])
        
    except Exception as e:
        logger.error(f"Error in Acolite pipeline: {e}")
        raise ProcessingError(f"Acolite pipeline failed: {e}")

def main() -> None:
    """Main function to parse command-line arguments and start processing"""
    parser = argparse.ArgumentParser(
        description="Run Acolite batch processing with wind and AOT data integration."
    )
    parser.add_argument(
        "--output",
        type=str,
        required=True,
        help="Output directory for processed files.",
    )
    parser.add_argument(
        "--inputs",
        type=str,
        nargs="+",
        help="List of input directories to process.",
    )
    parser.add_argument(
        "--input_dir",
        type=str,
        help="Directory containing all input subdirectories.",
    )
    parser.add_argument(
        "--settings",
        type=str,
        required=True,
        help="Path to the settings file containing processing parameters.",
    )
    parser.add_argument(
        "--acolite_path",
        type=str,
        default="launch_acolite.py",
        help="Path to Acolite launch script.",
    )
    parser.add_argument(
        "--max_workers",
        type=int,
        help="Maximum number of worker threads for parallel processing.",
    )

    args = parser.parse_args()
    
    # Create processing configuration
    config = ProcessingConfig(
        acolite_script_path=args.acolite_path,
        max_workers=args.max_workers
    )
    
    # Setup output directory
    output_dir = args.output
    os.makedirs(output_dir, exist_ok=True)

    # Gather input directories
    input_directories = []
    if args.inputs:
        input_directories.extend(args.inputs)
    
    if args.input_dir:
        if not os.path.exists(args.input_dir):
            raise ValueError(f"Input directory does not exist: {args.input_dir}")
        
        input_directories.extend([
            os.path.join(args.input_dir, d)
            for d in os.listdir(args.input_dir)
            if os.path.isdir(os.path.join(args.input_dir, d))
        ])

    if not input_directories:
        raise ValueError("No input directories specified. Use --inputs or --input_dir.")

    # Read settings
    try:
        params = read_settings_file(args.settings)
    except Exception as e:
        logger.error(f"Failed to read settings file: {e}")
        sys.exit(1)
    
    # Track total processing time
    total_start_time = time.time()
    coordinates_dict = {}
    successful_processed = 0
    failed_processed = 0
    
    # Process each input directory
    for input_dir in input_directories:
        filename = os.path.basename(os.path.normpath(input_dir))
        start_time = time.time()
        success = True
        
        try:
            logger.info(f"Starting processing of {filename}")
            
            # Resample Sentinel-2 bands if needed
            if input_dir.endswith('.SAFE') or '.SAFE' in input_dir:
                logger.info(f"Detected Sentinel-2 data: {filename}")
                resample_sentinel2_bands(input_dir, config)
            
            # Extract metadata
            metadata_extractor = MetadataExtractor()
            
            imd_file = metadata_extractor.find_metadata_file(input_dir, ".IMD")
            xml_file = (metadata_extractor.find_metadata_file(input_dir, "metadata_clip.xml") or 
                       metadata_extractor.find_metadata_file(input_dir, "metadata.xml"))
            sentinel2_xml_file = metadata_extractor.find_metadata_file(input_dir, "MTD_MSIL1C.xml")
            
            if imd_file:
                date, latitude, longitude = metadata_extractor.extract_date_lat_lon_from_imd(imd_file)
            elif xml_file:
                date, latitude, longitude = metadata_extractor.parse_xml_file_for_date_lat_lon(xml_file)
            elif sentinel2_xml_file:
                date, latitude, longitude = metadata_extractor.parse_sentinel2_metadata(sentinel2_xml_file)
            else:
                raise ProcessingError(f"No metadata file found in {input_dir}")

            logger.info(f"Extracted metadata - Date: {date}, Latitude: {latitude}, Longitude: {longitude}")
            coordinates_dict[input_dir] = (longitude, latitude)
            
            # Download wind data
            wind_file = download_wind_data(date, output_dir, "wind")
            if wind_file and os.path.exists(wind_file):
                try:
                    closest_wind_speed, wind_speed_df = extract_wind_speed(
                        wind_file, latitude, longitude, date,
                        os.path.join(output_dir, "wind_speed.csv")
                    )
                    logger.info(f"Extracted wind speed: {closest_wind_speed} m/s")
                except Exception as e:
                    logger.warning(f"Failed to extract wind speed: {e}. Using default.")
                    closest_wind_speed = config.default_wind_speed
            else:
                logger.warning(f"Wind data not available for {date}. Using default wind speed.")
                closest_wind_speed = config.default_wind_speed

            # Retrieve AOT data
            try:
                closest_aot = retrieve_aot_data(date, latitude, longitude, output_dir)
                if closest_aot is None:
                    closest_aot = config.default_aot
                    logger.warning(f"AOT data not available for {date}. Using default AOT value: {closest_aot}")
            except Exception as e:
                logger.warning(f"Failed to retrieve AOT data: {e}. Using default.")
                closest_aot = config.default_aot

            # Update parameters
            current_params = params.copy()
            current_params["wind"] = str(closest_wind_speed)
            current_params["aot"] = str(closest_aot)
            
            # Detect sensor type and run processing
            sensor_type = detect_sensor_type(input_dir)
            logger.info(f"Detected sensor type: {sensor_type}")

            run_acolite_pipeline(
                current_params, output_dir, [input_dir], 
                {input_dir: (longitude, latitude)}, config
            )
            
            successful_processed += 1
            logger.info(f"Successfully processed {filename}")

        except Exception as e:
            logger.error(f"Error processing {filename}: {str(e)}")
            success = False
            failed_processed += 1

        # Log processing time
        duration = log_processing_time(output_dir, filename, start_time, success)
        logger.info(f"Processed {filename} in {duration:.2f} seconds")

    # Log final summary
    total_duration = time.time() - total_start_time
    hours, remainder = divmod(total_duration, 3600)
    minutes, seconds = divmod(remainder, 60)
    formatted_total = f"{int(hours):02d}:{int(minutes):02d}:{seconds:.2f}"
    
    summary_msg = (f"Processing completed. Successfully processed: {successful_processed}, "
                  f"Failed: {failed_processed}, Total time: {formatted_total}")
    logger.info(summary_msg)
    
    # Update log file with summary
    with open(os.path.join(output_dir, "processing_time.txt"), "a", encoding='utf-8') as f:
        f.write("=" * 80 + "\n")
        f.write(f"Summary - Completed on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Total files processed: {successful_processed + failed_processed}\n")
        f.write(f"Successful: {successful_processed}, Failed: {failed_processed}\n")
        f.write(f"Total processing time: {total_duration:.2f} seconds ({formatted_total})\n")
        f.write("=" * 80 + "\n\n")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Processing interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)