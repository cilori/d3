#!/usr/bin/env python3
"""
Acolite Processing with Dynamic Wind and AOT Data Integration

This script integrates dynamic retrieval of wind and AOT (Aerosol Optical Thickness) 
data into the Acolite processing workflow.

Features:
- Extracts metadata from satellite images (Planet, WorldView, Sentinel-2)
- Downloads wind data from NOAA CoastWatch server
- Retrieves AOT data from NASA Earthdata server
- Runs Acolite atmospheric correction with dynamic parameters
- Provides GUI for user input customization
- Stacks processed raster bands into single files
- Assigns proper EPSG codes and handles projections

Usage:
    python wind_aot_data_acolite_integration.py --output <output_dir> 
           --inputs <input_dirs> --settings <settings_file>

Arguments:
    --output: Output directory for processed files
    --inputs: Input directories containing satellite image metadata files
    --settings: Path to settings file with processing parameters
"""

import argparse
import logging
import math
import multiprocessing
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union
from urllib.parse import urlparse

import numpy as np
import pandas as pd
import requests
from metpy.calc import wind_speed
from metpy.units import units
from netCDF4 import Dataset
from osgeo import gdal, osr
from requests import Session
from requests.adapters import HTTPAdapter
from tqdm import tqdm

# Constants
DEFAULT_CHUNK_SIZE = 131072
BLOCKSIZE = 65536
APP_KEY = "67542f3132f114592c6655dcd7a69ae13b4d5898"

# Band resolution mapping for Sentinel-2
BAND_RESOLUTIONS = {
    "B01": 60, "B02": 10, "B03": 10, "B04": 10, "B05": 20, "B06": 20,
    "B07": 20, "B08": 10, "B8A": 20, "B09": 60, "B10": 60, "B11": 20, "B12": 20,
}

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('acolite_processing.log')
    ]
)
logger = logging.getLogger(__name__)

# Global session for AOT retrieval
obpg_session: Optional[Session] = None


class ProcessingError(Exception):
    """Custom exception for processing errors."""
    pass


class MetadataExtractor:
    """Handle metadata extraction from different satellite image formats."""

    @staticmethod
    def find_metadata_file(input_dir: Union[str, Path], extension: str) -> Optional[str]:
        """Find a specific metadata file in the input directory."""
        input_path = Path(input_dir)
        for file_path in input_path.rglob(f"*{extension}"):
            return str(file_path)
        return None

    @staticmethod
    def parse_imd_file(imd_file: Union[str, Path]) -> Dict[str, Union[float, str]]:
        """Parse .IMD metadata file and extract metadata."""
        metadata = {}
        with open(imd_file, "r", encoding='utf-8') as file:
            for line in file:
                line = line.strip()
                if "ULLon" in line:
                    metadata["ULLon"] = float(line.split("=")[1].strip().strip(";"))
                elif "ULLat" in line:
                    metadata["ULLat"] = float(line.split("=")[1].strip().strip(";"))
                elif "firstLineTime" in line:
                    metadata["firstLineTime"] = line.split("=")[1].strip().strip(";")
        return metadata

    @staticmethod
    def extract_date_lat_lon_from_imd(imd_file: Union[str, Path]) -> Tuple[datetime, float, float]:
        """Extract date and center coordinates from WorldView IMD file."""
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
            raise ProcessingError("No band coordinates found in IMD file")
            
        first_band = list(band_coordinates.keys())[0]
        required_coords = ['ULLat', 'ULLon', 'URLat', 'URLon', 'LLLat', 'LLLon', 'LRLat', 'LRLon']
        
        if not all(coord in band_coordinates[first_band] for coord in required_coords):
            raise ProcessingError("Missing coordinate information in IMD file")
            
        coords = band_coordinates[first_band]
        
        # Calculate center coordinates
        ul_lat, ul_lon = float(coords["ULLat"]), float(coords["ULLon"])
        ur_lat, ur_lon = float(coords["URLat"]), float(coords["URLon"])
        ll_lat, ll_lon = float(coords["LLLat"]), float(coords["LLLon"])
        lr_lat, lr_lon = float(coords["LRLat"]), float(coords["LRLon"])
        
        center_lat = (ul_lat + ur_lat + ll_lat + lr_lat) / 4
        center_lon = (ul_lon + ur_lon + ll_lon + lr_lon) / 4
        
        logger.info(f"WorldView center coordinates: ({center_lat:.6f}, {center_lon:.6f})")
        
        # Extract date
        date_str = metadata.get("earliestAcqTime", "").split('T')[0]
        if not date_str:
            raise ProcessingError("No acquisition time found in IMD file")
            
        date = datetime.strptime(date_str, "%Y-%m-%d")
        
        return date, center_lat, center_lon

    @staticmethod
    def parse_xml_file_for_date_lat_lon(xml_file: Union[str, Path]) -> Tuple[datetime, float, float]:
        """Parse XML metadata file and extract date, latitude, and longitude."""
        date, latitude, longitude = None, None, None
        
        with open(xml_file, "r", encoding='utf-8') as file:
            content = file.read()
            
            # Extract acquisition date
            acq_match = re.search(r"<ps:acquisitionDateTime>(.*?)</ps:acquisitionDateTime>", content)
            if acq_match:
                date_str = acq_match.group(1)
                date = datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S%z")
                
            # Extract position
            pos_match = re.search(r"<gml:pos>(.*?)</gml:pos>", content)
            if pos_match:
                pos_text = pos_match.group(1)
                longitude, latitude = map(float, pos_text.split())
                
        if date is None or latitude is None or longitude is None:
            raise ProcessingError(f"Could not extract metadata from {xml_file}")
            
        return date, latitude, longitude

    @staticmethod
    def parse_sentinel2_metadata(xml_file: Union[str, Path]) -> Tuple[datetime, float, float]:
        """Parse Sentinel-2 XML metadata file and extract date, latitude, and longitude."""
        try:
            tree = ET.parse(xml_file)
            root = tree.getroot()
            
            generation_time_elem = root.find(".//GENERATION_TIME")
            pos_list_elem = root.find(".//EXT_POS_LIST")
            
            if generation_time_elem is None or pos_list_elem is None:
                raise ProcessingError("Required elements not found in Sentinel-2 metadata")
                
            generation_time = generation_time_elem.text
            pos_list = pos_list_elem.text.strip().split()
            
            latitude = float(pos_list[0])
            longitude = float(pos_list[1])
            date = datetime.strptime(generation_time, "%Y-%m-%dT%H:%M:%S.%fZ")
            
            return date, latitude, longitude
            
        except ET.ParseError as e:
            raise ProcessingError(f"Error parsing Sentinel-2 XML: {e}")


class WindDataDownloader:
    """Handle wind data download and processing."""

    @staticmethod
    def download_wind_data(date: datetime, output_dir: Union[str, Path], 
                          output_filename: str) -> Optional[str]:
        """Download wind data for a specific date."""
        date_str = date.strftime("%Y%m%d")
        year_str = date.strftime("%Y")
        base_url = (f"https://coastwatch.noaa.gov/thredds/fileServer/"
                   f"uvcompNCEIBlendedGlobalSCISixHourWW00/{year_str}/")
        filename = f"NBSv02_wind_6hourly_{date_str}.nc"
        url = f"{base_url}{filename}"

        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            
            output_path = Path(output_dir) / f"{output_filename}.nc"
            with open(output_path, "wb") as file:
                file.write(response.content)
                
            logger.info(f"Downloaded wind data: {output_path}")
            return str(output_path)
            
        except requests.RequestException as e:
            logger.error(f"Error downloading wind data: {e}")
            return None

    @staticmethod
    def convert_datetime(hours_since_19780101_array: np.ndarray) -> List[datetime]:
        """Convert hours since 19780101 to datetime objects."""
        base_date = datetime(1970, 1, 1)
        seconds_since_1970 = (hours_since_19780101_array * 3600 + 
                             (1978 - 1970) * 365.25 * 24 * 3600)
        return [base_date + timedelta(seconds=float(sec)) for sec in seconds_since_1970]

    @staticmethod
    def extract_wind_speed(filename: Union[str, Path], latitude: float, longitude: float,
                          target_datetime: datetime, 
                          output_filepath: Optional[Union[str, Path]] = None) -> Tuple[float, pd.DataFrame]:
        """Extract wind speed from NetCDF file."""
        try:
            with Dataset(filename) as dataset:
                latitudes = dataset.variables["lat"][:]
                longitudes = dataset.variables["lon"][:]
                lat_index = int(np.abs(latitudes - latitude).argmin())
                lon_index = int(np.abs(longitudes - longitude).argmin())

                u_wind = dataset.variables["u_wind"]
                v_wind = dataset.variables["v_wind"]
                time_variable = dataset.variables["time"]

                u_wind_values = u_wind[:, 0, lat_index, lon_index].data
                v_wind_values = v_wind[:, 0, lat_index, lon_index].data
                time_values = time_variable[:]

                date_times = WindDataDownloader.convert_datetime(time_values.data)

                # Make target_datetime timezone-naive
                target_datetime = target_datetime.replace(tzinfo=None)

                # Find closest time
                time_deltas = [abs((target_datetime - dt).total_seconds()) for dt in date_times]
                closest_time_index = min(range(len(time_deltas)), key=lambda i: time_deltas[i])
                closest_time_str = date_times[closest_time_index].strftime("%Y-%m-%d %H:%M:%S")

                # Calculate wind speed
                wind_speed_values = wind_speed(
                    u=u_wind_values * units(u_wind.units), 
                    v=v_wind_values * units(v_wind.units)
                )
                closest_wind_speed = float(wind_speed_values[closest_time_index].to_tuple()[0])

                wind_data = {
                    "windspeed": [closest_wind_speed],
                    "wind_speed_date_time": [closest_time_str],
                    "input_date_time": [target_datetime.strftime("%Y-%m-%d %H:%M:%S")],
                    "wind_speed_height": [v_wind.height],
                    "filename": [str(filename)],
                    "longitude": [longitude],
                    "latitude": [latitude],
                }

                wind_speed_df = pd.DataFrame(wind_data)

                if output_filepath:
                    wind_speed_df.to_csv(output_filepath, index=False)

                return closest_wind_speed, wind_speed_df
                
        except Exception as e:
            raise ProcessingError(f"Error extracting wind speed: {e}")


class AOTRetriever:
    """Handle AOT (Aerosol Optical Thickness) data retrieval."""

    def __init__(self):
        self.session = self._get_session()

    def _get_session(self, verbose: int = 0, ntries: int = 5) -> Session:
        """Get or create a session for AOT retrieval."""
        global obpg_session
        if not obpg_session:
            if verbose > 1:
                logging.basicConfig(level=logging.DEBUG)
            obpg_session = Session()
            obpg_session.mount("https://", HTTPAdapter(max_retries=ntries))
        return obpg_session

    @staticmethod
    def _is_request_auth_failure(req: requests.Response) -> bool:
        """Check if a request failed due to authentication."""
        ctype = req.headers.get("Content-Type")
        if ctype and ctype.startswith("text/html"):
            return "<title>Earthdata Login</title>" in req.text
        return False

    def _http_download(self, server: str, request: str, localpath: Union[str, Path] = ".",
                      outputfilename: Optional[str] = None, ntries: int = 5,
                      uncompress: bool = False, timeout: float = 30.0,
                      verbose: int = 0, force_download: bool = False,
                      chunk_size: int = DEFAULT_CHUNK_SIZE) -> int:
        """Download a file via HTTP."""
        status = 0
        url_str = f"https://{server}{request}"
        localpath = Path(localpath)
        
        headers = {}
        if not force_download and outputfilename:
            ofile = localpath / outputfilename
            modified_since = self._get_file_time(ofile)
            if modified_since:
                headers = {"If-Modified-Since": modified_since.strftime("%a, %d %b %Y %H:%M:%S GMT")}

        try:
            with self.session.get(url_str, stream=True, timeout=timeout, headers=headers) as req:
                if req.status_code != 200:
                    status = req.status_code
                elif self._is_request_auth_failure(req):
                    status = 401
                else:
                    localpath.mkdir(mode=0o2775, parents=True, exist_ok=True)

                    if not outputfilename:
                        cd = req.headers.get("Content-Disposition")
                        if cd:
                            outputfilename = re.findall("filename=(.+)", cd)[0]
                        else:
                            outputfilename = url_str.split("/")[-1]

                    ofile = localpath / outputfilename
                    download = True
                    
                    if "last-modified" in req.headers and not force_download:
                        remote_lmt = req.headers["last-modified"]
                        remote_ftime = datetime.strptime(
                            remote_lmt, "%a, %d %b %Y %H:%M:%S GMT"
                        ).replace(tzinfo=None)
                        modified_since = self._get_file_time(ofile)
                        if modified_since and (remote_ftime - modified_since).total_seconds() < 0:
                            download = False
                            if verbose > 0:
                                logger.info(f"Skipping download of {outputfilename}")

                    if download:
                        total_length = int(req.headers.get("content-length", 0))
                        if verbose > 0:
                            logger.info(f"Downloading {outputfilename} "
                                      f"({total_length / 1024 / 1024:.2f} MB)")

                        with open(ofile, "wb") as fd:
                            for chunk in req.iter_content(chunk_size=chunk_size):
                                if chunk:
                                    fd.write(chunk)

                        status = 0
                        if verbose:
                            logger.info("Download completed")
                            
        except requests.RequestException as e:
            logger.error(f"Error downloading {url_str}: {e}")
            status = 1
            
        return status

    @staticmethod
    def _get_file_time(local_file: Union[str, Path]) -> Optional[datetime]:
        """Get the modification time of a local file."""
        local_file = Path(local_file)
        if not local_file.is_file():
            # Check for compressed versions
            for suffix in [".Z", ".gz", ".bz2"]:
                if local_file.with_suffix(suffix).is_file():
                    local_file = local_file.with_suffix(suffix)
                    break
                    
        if local_file.is_file():
            return datetime.fromtimestamp(local_file.stat().st_mtime)
        return None

    def retrieve_url(self, request: str, localpath: Union[str, Path] = ".",
                    uncompress: bool = False, verbose: int = 0,
                    force_download: bool = False, appkey: str = APP_KEY) -> int:
        """Retrieve a file from a URL."""
        if verbose:
            logger.info(f"Retrieving {request.rstrip()}")

        server = "oceandata.sci.gsfc.nasa.gov"
        parsed_request = urlparse(request)
        netpath = parsed_request.path

        if parsed_request.netloc:
            server = parsed_request.netloc
        else:
            netpath = f"/ob/getfile/{netpath}"

        if re.match(r".*getfile", netpath) and appkey:
            netpath = f"{netpath}?appkey={appkey}"

        if parsed_request.query:
            netpath = f"{netpath}&{parsed_request.query}"

        return self._http_download(
            server, netpath, localpath=localpath, uncompress=uncompress,
            verbose=verbose, force_download=force_download
        )

    @staticmethod
    def extract_vars_from_netcdf(lon: float, lat: float, ncdir: Union[str, Path], 
                                varname: str) -> float:
        """Extract variables from NetCDF file."""
        try:
            with Dataset(ncdir, "r") as nc:
                lons = nc.variables["lon"][:]
                lats = nc.variables["lat"][:]
                lon_idx = int(np.abs(lons - lon).argmin())
                lat_idx = int(np.abs(lats - lat).argmin())
                
                var = nc.variables[varname]
                if "time" in var.dimensions:
                    return float(var[0, lat_idx, lon_idx])
                else:
                    return float(var[lat_idx, lon_idx])
                    
        except Exception as e:
            raise ProcessingError(f"Error extracting variables from NetCDF: {e}")

    @staticmethod
    def find_closest_times_on_same_day(date: datetime) -> List[datetime]:
        """Find the closest times on the same day."""
        base_times = [0, 6, 12, 18]
        times = [date.replace(hour=bt, minute=0, second=0, microsecond=0) 
                for bt in base_times]
        times.sort(key=lambda x: abs((x - date).total_seconds()))
        return times

    def retrieve_aot_data(self, date: datetime, lat: float, lon: float, 
                         output_dir: Union[str, Path]) -> Optional[float]:
        """Retrieve AOT data for a specific date."""
        output_dir = Path(output_dir)
        closest_times = self.find_closest_times_on_same_day(date)
        nc_filename = None
        status = 1

        # Try same day first
        for closest_time in closest_times:
            date_str = closest_time.strftime("%Y%m%dT%H%M%S")
            nc_filename = f"GMAO_MERRA2.{date_str}.AER.nc"
            output_file = output_dir / nc_filename

            if output_file.exists():
                logger.info(f"File {nc_filename} already exists, skipping download.")
                status = 0
                break
            else:
                url = f"https://oceandata.sci.gsfc.nasa.gov/getfile/{nc_filename}?appkey={APP_KEY}"
                status = self.retrieve_url(url, localpath=output_dir, verbose=0)
                if status == 0:
                    logger.info(f"Downloaded {nc_filename}")
                    break

        # Try adjacent days if same day fails
        if status != 0:
            logger.warning("No data available for the exact time on the same day, "
                          "checking previous and next days...")
            for day_offset in [-1, 1]:
                target_day = date + timedelta(days=day_offset)
                closest_times = self.find_closest_times_on_same_day(target_day)
                
                for closest_time in closest_times:
                    date_str = closest_time.strftime("%Y%m%dT%H%M%S")
                    nc_filename = f"GMAO_MERRA2.{date_str}.AER.nc"
                    output_file = output_dir / nc_filename

                    if output_file.exists():
                        status = 0
                        break
                    else:
                        url = (f"https://oceandata.sci.gsfc.nasa.gov/getfile/"
                              f"{nc_filename}?appkey={APP_KEY}")
                        status = self.retrieve_url(url, localpath=output_dir, verbose=0)
                        if status == 0:
                            logger.info(f"Downloaded {nc_filename}")
                            break
                            
                if status == 0:
                    break

        if status != 0:
            logger.error("No AOT data available for the target date or adjacent days.")
            return None

        try:
            varname = "TOTEXTTAU"
            output_file = output_dir / nc_filename
            totexttau = self.extract_vars_from_netcdf(lon, lat, output_file, varname)
            logger.info(f"TOTEXTTAU at ({lat}, {lon}) for {date}: {totexttau}")
            return totexttau
            
        except Exception as e:
            logger.error(f"Error extracting AOT data: {e}")
            return None


class BandResampler:
    """Handle Sentinel-2 band resampling operations."""

    @staticmethod
    def get_band_id(filename: str) -> str:
        """Extract band ID from filename."""
        filename_upper = filename.upper()
        for band in BAND_RESOLUTIONS:
            if f"_{band}" in filename_upper:
                return band
        return ""

    @staticmethod
    def find_jp2_files(safe_dir: Path) -> List[Path]:
        """Find all JP2 files in Sentinel-2 SAFE directory."""
        img_data_path = safe_dir / "GRANULE"
        granule_dirs = list(img_data_path.glob("*"))
        
        if not granule_dirs:
            raise ProcessingError(f"No granule directories found in {img_data_path}")
            
        img_data_dir = granule_dirs[0] / "IMG_DATA"
        jp2_files = sorted([f for f in img_data_dir.glob("*.jp2") 
                           if "TCI" not in f.name.upper()])
        
        if not jp2_files:
            raise ProcessingError(f"No JP2 files found in {img_data_dir}")
            
        return jp2_files

    @staticmethod
    def get_10m_grid(jp2_files: List[Path]) -> Tuple[tuple, str, int, int]:
        """Get reference grid from 10m band."""
        for jp2 in jp2_files:
            band = BandResampler.get_band_id(jp2.name)
            if BAND_RESOLUTIONS.get(band) == 10:
                ds = gdal.Open(str(jp2))
                if ds is None:
                    continue
                    
                geotransform = ds.GetGeoTransform()
                projection = ds.GetProjection()
                rows = ds.RasterYSize
                cols = ds.RasterXSize
                ds = None
                return geotransform, projection, rows, cols
                
        raise ProcessingError("No 10m band found to define target grid.")

    @staticmethod
    def resample_to_10m(jp2_path: Path, target_cols: int, target_rows: int,
                       target_geotransform: tuple, target_projection: str) -> None:
        """Resample a band to 10m resolution."""
        temp_dir = jp2_path.parent
        
        with tempfile.NamedTemporaryFile(suffix='.tif', dir=temp_dir, delete=False) as tmp_tif:
            temp_tif_path = Path(tmp_tif.name)
        with tempfile.NamedTemporaryFile(suffix='.jp2', dir=temp_dir, delete=False) as tmp_jp2:
            temp_jp2_path = Path(tmp_jp2.name)
        
        try:
            # Open source dataset
            src_ds = gdal.Open(str(jp2_path))
            if src_ds is None:
                raise ProcessingError(f"Could not open {jp2_path}")
            
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
            
            # Convert to JP2
            temp_tif_ds = gdal.Open(str(temp_tif_path))
            jp2_driver = None
            
            for driver_name in ["JP2OpenJPEG", "JPEG2000", "JP2KAK"]:
                try:
                    jp2_driver = gdal.GetDriverByName(driver_name)
                    if jp2_driver:
                        break
                except Exception:
                    continue
            
            if not jp2_driver:
                raise ProcessingError("No JP2 driver available")
            
            options = ["QUALITY=100"] if "OpenJPEG" in jp2_driver.GetDescription() else []
            jp2_ds = jp2_driver.CreateCopy(str(temp_jp2_path), temp_tif_ds, options=options)
            
            if jp2_ds is None:
                raise ProcessingError(f"Failed to create JP2 file with {jp2_driver.GetDescription()}")
            
            jp2_ds.FlushCache()
            temp_tif_ds = None
            jp2_ds = None
            
            # Replace original file
            shutil.move(str(temp_jp2_path), str(jp2_path))
            
        except Exception as e:
            raise ProcessingError(f"Error resampling {jp2_path}: {e}")
        finally:
            # Cleanup temporary files
            for temp_path in [temp_tif_path, temp_jp2_path]:
                if temp_path.exists():
                    try:
                        temp_path.unlink()
                    except Exception:
                        pass

    @staticmethod
    def process_band(jp2_path: Path, target_cols: int, target_rows: int,
                    target_geotransform: tuple, target_projection: str) -> str:
        """Process a single band for resampling."""
        band = BandResampler.get_band_id(jp2_path.name)
        resolution = BAND_RESOLUTIONS.get(band)
        
        if resolution == 10:
            return f"Skipped (already 10m): {jp2_path.name}"
        elif resolution in (20, 60):
            BandResampler.resample_to_10m(
                jp2_path, target_cols, target_rows, target_geotransform, target_projection
            )
            return f"Resampled: {jp2_path.name}"
        else:
            return f"Unknown band: {jp2_path.name}"

    @staticmethod
    def resample_sentinel2_bands(safe_dir: Union[str, Path]) -> None:
        """Resample Sentinel-2 20m and 60m bands to 10m resolution."""
        logger.info(f"Resampling Sentinel-2 bands in {safe_dir}")
        
        safe_path = Path(safe_dir)
        jp2_files = BandResampler.find_jp2_files(safe_path)
        geotransform, projection, rows, cols = BandResampler.get_10m_grid(jp2_files)
        
        cpu_count = multiprocessing.cpu_count()
        max_workers = max(1, int(cpu_count * 0.3))
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(
                    BandResampler.process_band, jp2_path, cols, rows, geotransform, projection
                )
                for jp2_path in jp2_files
            ]
            
            for future in tqdm(futures, desc="Resampling bands"):
                try:
                    result = future.result()
                    logger.info(result)
                except Exception as e:
                    logger.error(f"Error in band processing: {e}")
        
        logger.info("Sentinel-2 band resampling completed")


class CoordinateUtils:
    """Utility functions for coordinate transformations and EPSG handling."""

    @staticmethod
    def get_raster_epsg(raster_path: Union[str, Path]) -> Optional[str]:
        """Get EPSG code from raster."""
        try:
            raster_in = gdal.Open(str(raster_path))
            if raster_in is None:
                return None
                
            projection = osr.SpatialReference(wkt=raster_in.GetProjection())
            projection_auth = projection.GetAttrValue('AUTHORITY', 1)
            raster_in = None
            return projection_auth
            
        except Exception:
            return None

    @staticmethod
    def get_raster_center_coords(raster_path: Union[str, Path]) -> Tuple[Optional[float], Optional[float]]:
        """Get the center coordinates of a raster in WGS84."""
        try:
            raster = gdal.Open(str(raster_path))
            if raster is None:
                return None, None
            
            gt = raster.GetGeoTransform()
            cols = raster.RasterXSize
            rows = raster.RasterYSize
            
            center_x = gt[0] + (cols / 2) * gt[1]
            center_y = gt[3] + (rows / 2) * gt[5]
            
            proj = osr.SpatialReference(wkt=raster.GetProjection())
            wgs84 = osr.SpatialReference()
            wgs84.ImportFromEPSG(4326)
            
            if not proj.IsSame(wgs84):
                try:
                    transform = osr.CoordinateTransformation(proj, wgs84)
                    center_x, center_y, _ = transform.TransformPoint(center_x, center_y)
                except Exception:
                    pass
            
            raster = None
            return center_x, center_y
            
        except Exception:
            return None, None

    @staticmethod
    def calculate_utm_zone(longitude: float, latitude: float) -> Tuple[int, str]:
        """Calculate the appropriate UTM zone and EPSG code."""
        utm_zone = int((longitude + 180) / 6) + 1
        utm_zone = max(1, min(60, utm_zone))  # Clamp between 1 and 60
        
        epsg_code = f"326{utm_zone:02d}" if latitude >= 0 else f"327{utm_zone:02d}"
        return utm_zone, epsg_code

    @staticmethod
    def get_utm_epsg(longitude: float, latitude: float) -> str:
        """Get EPSG code for UTM zone."""
        zone_number = int((longitude + 180) / 6) + 1
        zone_number = max(1, min(60, zone_number))
        
        epsg = 32600 + zone_number if latitude >= 0 else 32700 + zone_number
        return str(epsg)

    @staticmethod
    def get_utm_epsg_from_sentinel_tile(filename: str) -> Optional[str]:
        """Extract EPSG from Sentinel-2 tile ID in filename."""
        match = re.search(r'T(\d{1,2})[A-Z]{3}', filename)
        if match:
            zone_number = int(match.group(1))
            epsg = 32600 + zone_number
            logger.debug(f"Extracted Zone {zone_number} from {filename} -> EPSG {epsg}")
            return str(epsg)
        return None

    @staticmethod
    def update_epsg(raster_path: Union[str, Path], target_epsg: str) -> bool:
        """Update EPSG code for a raster file."""
        try:
            raster = gdal.Open(str(raster_path), gdal.GA_Update)
            if raster is None:
                return False
            
            srs = osr.SpatialReference()
            srs.ImportFromEPSG(int(target_epsg))
            raster.SetProjection(srs.ExportToWkt())
            raster = None
            return True
            
        except Exception as e:
            logger.error(f"Error updating EPSG for {raster_path}: {e}")
            return False

    @staticmethod
    def assign_epsg_to_raster(raster_path: Union[str, Path], epsg_code: str) -> bool:
        """Assign EPSG code to a single raster without reprojecting."""
        try:
            dataset = gdal.Open(str(raster_path), gdal.GA_Update)
            if dataset is None:
                logger.error(f"Could not open {raster_path}")
                return False
            
            srs = osr.SpatialReference()
            srs.ImportFromEPSG(int(epsg_code))
            dataset.SetProjection(srs.ExportToWkt())
            dataset.FlushCache()
            dataset = None
            
            logger.info(f"Successfully assigned EPSG:{epsg_code} to {Path(raster_path).name}")
            return True
            
        except Exception as e:
            logger.error(f"Error assigning EPSG to {raster_path}: {e}")
            return False


class AcoliteRunner:
    """Handle Acolite processing operations."""

    def __init__(self, output_directory: Union[str, Path], acolite_script_path: Union[str, Path]):
        self.output_directory = Path(output_directory)
        self.acolite_script_path = Path(acolite_script_path)

    def get_base_folder_name(self, input_folder: Union[str, Path]) -> str:
        """Extract base folder name without version suffix."""
        base_name = re.sub(r"\.SAFE_v\d+$", "", Path(input_folder).name)
        return base_name

    def get_next_version(self, base_folder: Union[str, Path], main_folder: Union[str, Path]) -> str:
        """Get next version number for output folder inside main folder."""
        version = 0
        main_folder = Path(main_folder)
        base_name = Path(base_folder).name
        
        while (main_folder / f"{base_name}_v{version}").exists():
            version += 1
        return f"{base_name}_v{version}"

    def configure_acolite_settings(self, input_directories: List[Union[str, Path]], 
                                 params: Dict[str, str]) -> List[str]:
        """Configure Acolite settings files for processing."""
        settings_files = []
        
        for input_dir in input_directories:
            input_path = Path(input_dir)
            base_name = self.get_base_folder_name(input_path)
            
            # Create main folder
            main_folder = self.output_directory / base_name
            main_folder.mkdir(parents=True, exist_ok=True)
            
            # Get unique name and next version
            unique_name = input_path.name
            base_output_folder = main_folder / unique_name
            output_folder = self.get_next_version(base_output_folder, main_folder)
            
            # Create versioned output folder inside main folder
            output_folder_path = main_folder / output_folder
            output_folder_path.mkdir(parents=True, exist_ok=True)
            
            # Create settings file
            settings_file_name = f"{unique_name}_settings.txt"
            settings_file_path = output_folder_path / settings_file_name
            settings_files.append(str(settings_file_path))
            
            with open(settings_file_path, "w", encoding='utf-8') as file:
                file.write(f"inputfile={input_dir}\n")
                file.write(f"output={output_folder_path}\n")
                for key, value in params.items():
                    file.write(f"{key}={value}\n")
        
        return settings_files

    def run_acolite_direct(self, settings_files: List[str]) -> None:
        """Run Acolite processing directly."""
        for settings_file in settings_files:
            command_string = f"python {self.acolite_script_path} --cli --settings={settings_file}"
            logger.info(f"Running Acolite with settings: {settings_file}")
            
            try:
                subprocess.run(command_string, shell=True, check=True)
            except subprocess.CalledProcessError as e:
                raise ProcessingError(f"Acolite processing failed: {e}")

    def extract_number_from_filename(self, filename: str) -> int:
        """Extract the numerical part from the filename."""
        match = re.search(r'Rrs_(\d{3,4})\.tif', filename)
        return int(match.group(1)) if match else -1

    def stack_rasters(self, input_files: List[str], output_folder: Union[str, Path]) -> None:
        """Stack individual Rrs files into a single multi-band raster with LZW compression."""
        if not input_files:
            logger.info("No input files to stack.")
            return
        
        output_folder = Path(output_folder)
        
        # Sort files by wavelength number for consistent band order
        input_files.sort(key=lambda x: self.extract_number_from_filename(Path(x).name))
        
        # Extract base name from the first file
        first_file = Path(input_files[0]).name
        match = re.search(r'(.*?)_L2W_Rrs_\d{3,4}\.tif', first_file)
        base_name = match.group(1) + "_Rrs" if match else "Rrs"
        
        output_path = output_folder / f"{base_name}.tif"
        nodata_value = -9999
        
        try:
            # Open the first raster to get metadata
            ref_ds = gdal.Open(input_files[0], gdal.GA_ReadOnly)
            if ref_ds is None:
                raise ProcessingError(f"Could not open reference raster: {input_files[0]}")
                
            x_size, y_size = ref_ds.RasterXSize, ref_ds.RasterYSize
            geotransform, projection = ref_ds.GetGeoTransform(), ref_ds.GetProjection()
            
            # Create output raster with LZW compression
            driver = gdal.GetDriverByName('GTiff')
            creation_options = ['COMPRESS=LZW', 'TILED=YES', 'BIGTIFF=IF_SAFER']
            
            out_ds = driver.Create(
                str(output_path), x_size, y_size, len(input_files), 
                gdal.GDT_Float32, options=creation_options
            )
            out_ds.SetGeoTransform(geotransform)
            out_ds.SetProjection(projection)
            
            # Stack the bands
            for i, raster_path in enumerate(input_files, start=1):
                src_ds = gdal.Open(raster_path, gdal.GA_ReadOnly)
                if src_ds is None:
                    logger.warning(f"Could not open {raster_path}, skipping...")
                    continue
                    
                band_data = src_ds.GetRasterBand(1).ReadAsArray()
                band_data = np.nan_to_num(band_data, nan=nodata_value)
                
                out_band = out_ds.GetRasterBand(i)
                out_band.WriteArray(band_data)
                out_band.SetNoDataValue(nodata_value)
                out_band.SetDescription(Path(raster_path).stem)
                
                src_ds = None
            
            out_ds = None
            ref_ds = None
            
            logger.info(f"Stacked raster with LZW compression saved to {output_path}")
            
        except Exception as e:
            raise ProcessingError(f"Error stacking rasters: {e}")

    def stack_bands(self) -> None:
        """Stack all Rrs bands in the output directory."""
        for root, _, files in os.walk(self.output_directory):
            tif_files = [
                os.path.join(root, file)
                for file in files
                if file.endswith(".tif") and re.search(r"Rrs_\d{3,4}\.tif$", file)
            ]
            if tif_files:
                self.stack_rasters(tif_files, root)


class ProcessingUtils:
    """Utility functions for file processing and cleanup."""

    @staticmethod
    def detect_sensor_type(input_dir: Union[str, Path]) -> str:
        """Detect sensor type from input directory."""
        input_path = Path(input_dir)
        
        if '.SAFE' in str(input_path):
            return 'sentinel2'
        elif MetadataExtractor.find_metadata_file(input_path, ".IMD"):
            return 'worldview'
        elif (MetadataExtractor.find_metadata_file(input_path, "metadata_clip.xml") or 
              MetadataExtractor.find_metadata_file(input_path, "metadata.xml")):
            return 'planet'
        else:
            return 'unknown'

    @staticmethod
    def cleanup_individual_rrs_files(output_directory: Union[str, Path], 
                                   keep_individual: bool = False) -> None:
        """Remove individual Rrs files after stacking."""
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
        
        logger.info(f"Deleted {deleted_count} individual Rrs files")

    @staticmethod
    def delete_files_by_extension(output_directory: Union[str, Path], 
                                extensions_to_delete: List[str]) -> None:
        """Delete files with specified extensions in the output directory."""
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
        
        logger.info(f"Total files deleted: {len(deleted_files)}")

    @staticmethod
    def read_settings_file(settings_file_path: Union[str, Path]) -> Dict[str, str]:
        """Read settings from a file and return them as a dictionary."""
        settings = {}
        with open(settings_file_path, "r", encoding='utf-8') as file:
            for line in file:
                line = line.strip()
                if line and not line.startswith("#"):
                    if "=" in line:
                        key, value = line.split("=", 1)
                        settings[key.strip()] = value.strip()
        return settings

    @staticmethod
    def log_processing_time(output_dir: Union[str, Path], filename: str, 
                          start_time: float, success: bool = True) -> float:
        """Log the processing time for a file."""
        end_time = time.time()
        duration = end_time - start_time
        
        # Format the duration as HH:MM:SS
        hours, remainder = divmod(duration, 3600)
        minutes, seconds = divmod(remainder, 60)
        formatted_duration = f"{int(hours):02d}:{int(minutes):02d}:{seconds:.2f}"
        
        # Format the timestamps
        start_timestamp = datetime.fromtimestamp(start_time).strftime('%Y-%m-%d %H:%M:%S')
        end_timestamp = datetime.fromtimestamp(end_time).strftime('%Y-%m-%d %H:%M:%S')
        
        # Create log file
        log_file = Path(output_dir) / "processing_time.txt"
        if not log_file.exists():
            with open(log_file, "w", encoding='utf-8') as f:
                f.write(f"Processing Time Log - Created on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write("=" * 80 + "\n")
                f.write("Filename | Start Time | End Time | Duration (seconds) | Duration (formatted) | Status\n")
                f.write("-" * 80 + "\n")
        
        # Log the processing time
        status = "SUCCESS" if success else "FAILED"
        with open(log_file, "a", encoding='utf-8') as f:
            f.write(f"{filename} | {start_timestamp} | {end_timestamp} | "
                   f"{duration:.2f} | {formatted_duration} | {status}\n")
        
        return duration


def find_input_dir_for_output(output_path: str, coordinates_dict: Dict[str, Tuple[float, float]]) -> Optional[str]:
    """Find the input directory that corresponds to this output path."""
    output_basename = os.path.basename(output_path)
    
    for input_dir in coordinates_dict.keys():
        input_basename = os.path.basename(input_dir)
        if input_basename in output_basename:
            return input_dir
    return None


def assign_projection_to_rasters(output_dir: Union[str, Path], 
                                coordinates_dict: Dict[str, Tuple[float, float]], 
                                sensor_type: Optional[str] = None) -> None:
    """Assign proper projection information to all rasters in output directory."""
    output_dir = Path(output_dir)
    
    for root, dirs, files in os.walk(output_dir):
        tif_files = [f for f in files if f.endswith('.tif')]
        
        if tif_files:
            for tif_file in tif_files:
                # Try to get EPSG from Sentinel-2 tile ID first
                utm_epsg = CoordinateUtils.get_utm_epsg_from_sentinel_tile(tif_file)
                
                if utm_epsg:
                    logger.info(f"Using tile-based EPSG {utm_epsg} for {tif_file}")
                else:
                    # Fallback to coordinate-based calculation
                    input_dir = find_input_dir_for_output(root, coordinates_dict)
                    if input_dir and input_dir in coordinates_dict:
                        longitude, latitude = coordinates_dict[input_dir]
                        utm_epsg = CoordinateUtils.get_utm_epsg(longitude, latitude)
                        logger.info(f"Using coordinate-based EPSG {utm_epsg} for {tif_file}")
                
                if utm_epsg:
                    tif_path = os.path.join(root, tif_file)
                    CoordinateUtils.assign_epsg_to_raster(tif_path, utm_epsg)


def run_acolite_pipeline(params: Dict[str, str], output_dir: Union[str, Path], 
                        input_directories: List[Union[str, Path]], 
                        coordinates_dict: Dict[str, Tuple[float, float]], 
                        sensor_type: Optional[str] = None) -> None:
    """Run the complete Acolite processing pipeline."""
    # Update acolite script path as needed
    acolite_script_path = "W:\\03_RnD\\02_RTM\\CI_SPACE\\Softwares\\acolite-20250402.0\\launch_acolite.py"
    
    acolite_runner = AcoliteRunner(
        output_directory=output_dir,
        acolite_script_path=acolite_script_path,
    )
    
    try:
        settings_files = acolite_runner.configure_acolite_settings(input_directories, params)
        acolite_runner.run_acolite_direct(settings_files)
        acolite_runner.stack_bands()
        
        logger.info("Post-processing: Assigning EPSG codes to Acolite output...")
        
        if coordinates_dict:
            assign_projection_to_rasters(output_dir, coordinates_dict, sensor_type)
            
        ProcessingUtils.cleanup_individual_rrs_files(
            output_dir, keep_individual=params.get("keep_individual_rrs", "true").lower() == "true"
        )
        ProcessingUtils.delete_files_by_extension(output_dir, [".png", ".nc", ".csv"])
        
    except Exception as e:
        raise ProcessingError(f"Acolite pipeline failed: {e}")


def main() -> None:
    """Main function to parse command-line arguments and start processing."""
    parser = argparse.ArgumentParser(
        description="Run Acolite batch processing with wind and AOT data integration.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument(
        "--output", type=str, required=True,
        help="Output directory for processed files."
    )
    parser.add_argument(
        "--inputs", type=str, nargs="+",
        help="List of input directories to process."
    )
    parser.add_argument(
        "--input_dir", type=str,
        help="Directory containing all input subdirectories."
    )
    parser.add_argument(
        "--settings", type=str, required=True,
        help="Path to the settings file containing processing parameters."
    )

    args = parser.parse_args()
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Gather input directories
    input_directories = []
    if args.inputs:
        input_directories.extend(args.inputs)
    if args.input_dir:
        input_dir_path = Path(args.input_dir)
        input_directories.extend([
            str(d) for d in input_dir_path.iterdir() 
            if d.is_dir()
        ])

    if not input_directories:
        raise ValueError("No input directories specified. Use --inputs or --input_dir.")

    # Read settings from the specified file
    try:
        params = ProcessingUtils.read_settings_file(args.settings)
    except Exception as e:
        raise ProcessingError(f"Error reading settings file: {e}")
    
    # Track total processing time
    total_start_time = time.time()
    coordinates_dict: Dict[str, Tuple[float, float]] = {}
    
    # Initialize data retrievers
    wind_downloader = WindDataDownloader()
    aot_retriever = AOTRetriever()
    
    # Process each input directory
    for input_dir in input_directories:
        input_path = Path(input_dir)
        filename = input_path.name
        start_time = time.time()
        success = True
        
        try:
            logger.info(f"Processing {filename}...")
            
            # Resample Sentinel-2 bands if needed
            if '.SAFE' in str(input_path):
                logger.info(f"Detected Sentinel-2 data: {filename}")
                BandResampler.resample_sentinel2_bands(input_path)
            
            # Extract metadata
            metadata_extractor = MetadataExtractor()
            
            imd_file = metadata_extractor.find_metadata_file(input_path, ".IMD")
            xml_file = (metadata_extractor.find_metadata_file(input_path, "metadata_clip.xml") or 
                       metadata_extractor.find_metadata_file(input_path, "metadata.xml"))
            sentinel2_xml_file = metadata_extractor.find_metadata_file(input_path, "MTD_MSIL1C.xml")
            
            if imd_file:
                date, latitude, longitude = metadata_extractor.extract_date_lat_lon_from_imd(imd_file)
            elif xml_file:
                date, latitude, longitude = metadata_extractor.parse_xml_file_for_date_lat_lon(xml_file)
            elif sentinel2_xml_file:
                date, latitude, longitude = metadata_extractor.parse_sentinel2_metadata(sentinel2_xml_file)
            else:
                raise ProcessingError(f"No metadata file found in {input_dir}")

            logger.info(f"Extracted metadata - Date: {date}, Latitude: {latitude}, Longitude: {longitude}")
            
            # Store coordinates for this input directory
            coordinates_dict[str(input_path)] = (longitude, latitude)
            
            # Download wind data
            wind_filename = wind_downloader.download_wind_data(date, output_dir, "wind")
            if wind_filename and Path(wind_filename).exists():
                closest_wind_speed, wind_speed_df = wind_downloader.extract_wind_speed(
                    wind_filename, latitude, longitude, date,
                    output_dir / "wind_speed.csv"
                )
                logger.info(f"Wind speed: {closest_wind_speed}")
            else:
                logger.warning(f"Wind data not available for {date}. Using default wind speed.")
                closest_wind_speed = 5.0  # Default wind speed

            # Retrieve AOT data
            closest_aot = aot_retriever.retrieve_aot_data(date, latitude, longitude, output_dir)
            if closest_aot is None:
                logger.warning(f"AOT data not available for {date}. Using default AOT value.")
                closest_aot = 0.1

            # Update parameters for this specific image
            current_params = params.copy()
            current_params["wind"] = str(closest_wind_speed)
            current_params["aot"] = str(closest_aot)
            
            # Detect sensor type and run Acolite processing
            sensor_type = ProcessingUtils.detect_sensor_type(input_path)
            logger.info(f"Detected sensor type: {sensor_type}")

            run_acolite_pipeline(
                current_params, output_dir, [str(input_path)], 
                {str(input_path): (longitude, latitude)}, sensor_type
            )

        except Exception as e:
            logger.error(f"Error processing {filename}: {e}")
            success = False

        # Log the processing time
        duration = ProcessingUtils.log_processing_time(output_dir, filename, start_time, success)
        logger.info(f"Processed {filename} in {duration:.2f} seconds")

    # Log the total processing time
    total_duration = time.time() - total_start_time
    hours, remainder = divmod(total_duration, 3600)
    minutes, seconds = divmod(remainder, 60)
    formatted_total = f"{int(hours):02d}:{int(minutes):02d}:{seconds:.2f}"
    
    log_file = output_dir / "processing_time.txt"
    with open(log_file, "a", encoding='utf-8') as f:
        f.write("=" * 80 + "\n")
        f.write(f"Summary - Completed on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Total processing time: {total_duration:.2f} seconds ({formatted_total})\n")
        f.write("=" * 80 + "\n\n")

    logger.info(f"Processing completed in {formatted_total}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)