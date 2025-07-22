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
from subprocess import Popen, PIPE
from typing import Optional, Tuple
import time
import numpy as np
import pandas as pd
import requests
from metpy.calc import wind_speed  # type: ignore
from metpy.units import units  # type: ignore
from netCDF4 import Dataset
from requests.adapters import HTTPAdapter
from requests import Session
from osgeo import gdal
import xml.etree.ElementTree as ET
from urllib.parse import urlparse
import math
from osgeo import gdal, osr


from concurrent.futures import ThreadPoolExecutor
from tqdm import tqdm
import multiprocessing
import tempfile
import shutil

# Constants for AOT retrieval
DEFAULT_CHUNK_SIZE = 131072
BLOCKSIZE = 65536
APP_KEY = "67542f3132f114592c6655dcd7a69ae13b4d5898"

# Initialize logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Global session for AOT retrieval
obpgSession = None


logging.basicConfig(level=logging.DEBUG)


# Resampling constants and functions
BAND_RESOLUTIONS = {
    "B01": 60, "B02": 10, "B03": 10, "B04": 10, "B05": 20, "B06": 20,
    "B07": 20, "B08": 10, "B8A": 20, "B09": 60, "B10": 60, "B11": 20, "B12": 20,
}

def get_band_id(filename: str) -> str:
    for band in BAND_RESOLUTIONS:
        if f"_{band}" in filename.upper():
            return band
    return ""

def find_jp2_files(safe_dir: Path) -> list:
    img_data_path = safe_dir / "GRANULE"
    granule_dirs = list(img_data_path.glob("*"))
    if not granule_dirs:
        raise FileNotFoundError(f"No granule directories found in {img_data_path}")
    img_data_dir = granule_dirs[0] / "IMG_DATA"
    jp2_files = sorted(f for f in img_data_dir.glob("*.jp2") if "TCI" not in f.name.upper())
    if not jp2_files:
        raise FileNotFoundError(f"No JP2 files found in {img_data_dir}")
    return jp2_files

def get_10m_grid(jp2_files: list):
    for jp2 in jp2_files:
        band = get_band_id(jp2.name)
        if BAND_RESOLUTIONS.get(band) == 10:
            ds = gdal.Open(str(jp2))
            geotransform = ds.GetGeoTransform()
            projection = ds.GetProjection()
            rows = ds.RasterYSize
            cols = ds.RasterXSize
            ds = None
            return geotransform, projection, rows, cols
    raise RuntimeError("No 10m band found to define target grid.")

def resample_to_10m(jp2_path: Path, target_cols: int, target_rows: int, target_geotransform, target_projection):
    with tempfile.NamedTemporaryFile(suffix='.tif', dir=jp2_path.parent, delete=False) as tmp_tif:
        temp_tif_path = Path(tmp_tif.name)
    with tempfile.NamedTemporaryFile(suffix='.jp2', dir=jp2_path.parent, delete=False) as tmp_jp2:
        temp_jp2_path = Path(tmp_jp2.name)
    
    try:
        src_ds = gdal.Open(str(jp2_path))
        gtiff_driver = gdal.GetDriverByName("GTiff")
        temp_ds = gtiff_driver.Create(str(temp_tif_path), target_cols, target_rows, 1, gdal.GDT_UInt16)
        temp_ds.SetGeoTransform(target_geotransform)
        temp_ds.SetProjection(target_projection)
        
        gdal.ReprojectImage(src_ds, temp_ds, src_ds.GetProjection(), target_projection, gdal.GRA_Bilinear)
        temp_ds.FlushCache()
        temp_ds = None
        src_ds = None
        
        temp_tif_ds = gdal.Open(str(temp_tif_path))
        jp2_driver = None
        for driver_name in ["JP2OpenJPEG", "JPEG2000", "JP2KAK"]:
            try:
                jp2_driver = gdal.GetDriverByName(driver_name)
                if jp2_driver:
                    break
            except:
                continue
        
        if not jp2_driver:
            raise RuntimeError("No JP2 driver available")
        
        jp2_ds = jp2_driver.CreateCopy(
            str(temp_jp2_path), temp_tif_ds,
            options=["QUALITY=100"] if "OpenJPEG" in jp2_driver.GetDescription() else []
        )
        
        if jp2_ds is None:
            raise RuntimeError(f"Failed to create JP2 file with {jp2_driver.GetDescription()}")
        
        jp2_ds.FlushCache()
        temp_tif_ds = None
        jp2_ds = None
        shutil.move(str(temp_jp2_path), str(jp2_path))
        
    except Exception as e:
        raise e
    finally:
        for temp_path in [temp_tif_path, temp_jp2_path]:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except:
                    pass

def process_band(jp2_path: Path, target_cols: int, target_rows: int, target_geotransform, target_projection):
    band = get_band_id(jp2_path.name)
    if BAND_RESOLUTIONS.get(band) == 10:
        return f"Skipped (already 10m): {jp2_path.name}"
    elif BAND_RESOLUTIONS.get(band) in (20, 60):
        resample_to_10m(jp2_path, target_cols, target_rows, target_geotransform, target_projection)
        return f"Resampled: {jp2_path.name}"
    else:
        return f"Unknown band: {jp2_path.name}"

def resample_sentinel2_bands(safe_dir: str):
    """Resample Sentinel-2 20m and 60m bands to 10m resolution"""
    logging.info(f"Resampling Sentinel-2 bands in {safe_dir}")
    
    safe_path = Path(safe_dir)
    jp2_files = find_jp2_files(safe_path)
    geotransform, projection, rows, cols = get_10m_grid(jp2_files)
    
    cpu_count = multiprocessing.cpu_count()
    max_workers = max(1, int(cpu_count * 0.3))
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(process_band, jp2_path, cols, rows, geotransform, projection)
            for jp2_path in jp2_files
        ]
        
        for f in tqdm(futures, desc="Resampling bands"):
            result = f.result()
            logging.info(result)
    
    logging.info("Sentinel-2 band resampling completed")


def get_raster_epsg(raster_path):
    """Get EPSG code from raster"""
    try:
        raster_in = gdal.Open(raster_path)
        if raster_in is None:
            return None
        projection = osr.SpatialReference(wkt=raster_in.GetProjection())
        projection_auth = projection.GetAttrValue('AUTHORITY', 1)
        raster_in = None
        return projection_auth
    except:
        return None

def get_raster_center_coords(raster_path):
    """Get the center coordinates of a raster in WGS84"""
    try:
        raster = gdal.Open(raster_path)
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
            except:
                pass
        
        raster = None
        return center_x, center_y
    except:
        return None, None

def calculate_utm_zone(longitude, latitude):
    """Calculate the appropriate UTM zone and EPSG code"""
    utm_zone = int((longitude + 180) / 6) + 1
    if utm_zone < 1:
        utm_zone = 1
    elif utm_zone > 60:
        utm_zone = 60
    
    if latitude >= 0:
        epsg_code = f"326{utm_zone:02d}"
    else:
        epsg_code = f"327{utm_zone:02d}"
    
    return utm_zone, epsg_code

def get_correct_epsg_for_raster(raster_path):
    """Determine the correct EPSG code for a raster based on its location"""
    lon, lat = get_raster_center_coords(raster_path)
    if lon is None or lat is None:
        return None
    utm_zone, epsg_code = calculate_utm_zone(lon, lat)
    return epsg_code

def update_epsg(raster_path, target_epsg):
    """Update EPSG code for a raster file"""
    try:
        raster = gdal.Open(raster_path, gdal.GA_Update)
        if raster is None:
            return False
        
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(int(target_epsg))
        raster.SetProjection(srs.ExportToWkt())
        raster = None
        return True
    except Exception as e:
        print(f"Error updating EPSG for {raster_path}: {str(e)}")
        return False

def assign_epsg_to_acolite_output(output_dir):
    """Assign correct EPSG codes to Acolite output files that have no projection"""
    print(f":: Assigning EPSG codes to Acolite output in {output_dir}")
    
    for root, dirs, files in os.walk(output_dir):
        tif_files = [os.path.join(root, f) for f in files if f.endswith('.tif')]
        
        if tif_files:
            print(f"   Processing directory: {os.path.basename(root)}")
            for tif_file in tif_files:
                current_epsg = get_raster_epsg(tif_file)
                
                if current_epsg is None:  # No EPSG assigned (this is the Acolite issue)
                    correct_epsg = get_correct_epsg_for_raster(tif_file)
                    
                    if correct_epsg is not None:
                        if update_epsg(tif_file, correct_epsg):
                            print(f"      ✓ {os.path.basename(tif_file)}: Assigned EPSG:{correct_epsg}")
                        else:
                            print(f"      ✗ Failed to assign EPSG to {os.path.basename(tif_file)}")
                    else:
                        print(f"      ? Could not determine EPSG for {os.path.basename(tif_file)}")
                else:
                    print(f"      ✓ {os.path.basename(tif_file)}: Already has EPSG:{current_epsg}")


class MetadataExtractor:
    """Class to handle metadata extraction from different satellite image formats."""

    @staticmethod
    def find_metadata_file(input_dir: str, extension: str) -> Optional[str]:
        for root, _, files in os.walk(input_dir):
            for file in files:
                if file.endswith(extension):
                    return os.path.join(root, file)
        return None

    @staticmethod
    def parse_imd_file(imd_file: str) -> dict:
        metadata = {}
        with open(imd_file, "r") as file:
            for line in file:
                if "ULLon" in line:
                    metadata["ULLon"] = float(line.split("=")[1].strip().strip(";"))
                elif "ULLat" in line:
                    metadata["ULLat"] = float(line.split("=")[1].strip().strip(";"))
                elif "firstLineTime" in line:
                    metadata["firstLineTime"] = line.split("=")[1].strip().strip(";")
        return metadata

    @staticmethod
    def extract_date_lat_lon_from_imd(imd_file: str):
        """Extract date and center coordinates from WorldView IMD file"""
        metadata = {}
        current_group = None
        band_coordinates = {}
        
        with open(imd_file, 'r') as file:
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
        first_band = list(band_coordinates.keys())[0] if band_coordinates else None
        
        if first_band and all(coord in band_coordinates[first_band] for coord in ['ULLat', 'ULLon', 'URLat', 'URLon', 'LLLat', 'LLLon', 'LRLat', 'LRLon']):
            coords = band_coordinates[first_band]
            
            ul_lat = float(coords["ULLat"])
            ul_lon = float(coords["ULLon"])
            ur_lat = float(coords["URLat"])
            ur_lon = float(coords["URLon"])
            ll_lat = float(coords["LLLat"])
            ll_lon = float(coords["LLLon"])
            lr_lat = float(coords["LRLat"])
            lr_lon = float(coords["LRLon"])
            
            # Calculate center coordinates
            center_lat = (ul_lat + ur_lat + ll_lat + lr_lat) / 4
            center_lon = (ul_lon + ur_lon + ll_lon + lr_lon) / 4
            
            logging.info(f"WorldView center coordinates: ({center_lat:.6f}, {center_lon:.6f})")
            
        else:
            logging.error("Could not extract coordinates from WorldView IMD file")
            raise ValueError("Missing coordinate information in IMD file")
        
        # Extract date
        date_str = metadata.get("earliestAcqTime", "").split('T')[0]
        date = datetime.strptime(date_str, "%Y-%m-%d").date() if date_str else None
        
        return date, center_lat, center_lon


    @staticmethod
    def parse_xml_file_for_date_lat_lon(
        xml_file: str,
    ) -> Tuple[Optional[datetime], Optional[float], Optional[float]]:
        date, latitude, longitude = None, None, None
        with open(xml_file, "r") as file:
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
        return date, latitude, longitude

    @staticmethod
    def parse_sentinel2_metadata(xml_file: str) -> Tuple[datetime, float, float]:
        tree = ET.parse(xml_file)
        root = tree.getroot()
        generation_time = root.find(".//GENERATION_TIME").text
        pos_list = root.find(".//EXT_POS_LIST").text.strip().split()
        latitude = float(pos_list[0])
        longitude = float(pos_list[1])
        date = datetime.strptime(generation_time, "%Y-%m-%dT%H:%M:%S.%fZ")
        return date, latitude, longitude


def find_metadata_file(input_dir: str, extension: str) -> Optional[str]:
    """Find a specific metadata file in the input directory."""
    for root, _, files in os.walk(input_dir):
        for file in files:
            if file.endswith(extension):
                return os.path.join(root, file)
    return None


def parse_imd_file(imd_file: str) -> dict:
    """Parse .IMD metadata file and extract metadata."""
    metadata = {}
    with open(imd_file, "r") as file:
        for line in file:
            if "ULLon" in line:
                metadata["ULLon"] = float(line.split("=")[1].strip().strip(";"))
            elif "ULLat" in line:
                metadata["ULLat"] = float(line.split("=")[1].strip().strip(";"))
            elif "firstLineTime" in line:
                metadata["firstLineTime"] = line.split("=")[1].strip().strip(";")
    return metadata


def extract_date_lat_lon_from_imd(imd_file: str) -> Tuple[datetime, float, float]:
    """Extract date, latitude, and longitude from .IMD file."""
    metadata = parse_imd_file(imd_file)
    date = datetime.strptime(metadata["firstLineTime"], "%Y-%m-%dT%H:%M:%S.%fZ")
    latitude = metadata["ULLat"]
    longitude = metadata["ULLon"]
    return date, latitude, longitude


def parse_xml_file_for_date_lat_lon(
    xml_file: str,
) -> Tuple[Optional[datetime], Optional[float], Optional[float]]:
    """Parse XML metadata file and extract date, latitude, and longitude."""
    date, latitude, longitude = None, None, None
    with open(xml_file, "r") as file:
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
    return date, latitude, longitude


def parse_sentinel2_metadata(xml_file: str) -> Tuple[datetime, float, float]:
    """Parse Sentinel-2 XML metadata file and extract date, latitude, and longitude."""
    tree = ET.parse(xml_file)
    root = tree.getroot()
    generation_time = root.find(".//GENERATION_TIME").text
    pos_list = root.find(".//EXT_POS_LIST").text.strip().split()
    latitude = float(pos_list[0])
    longitude = float(pos_list[1])
    date = datetime.strptime(generation_time, "%Y-%m-%dT%H:%M:%S.%fZ")
    return date, latitude, longitude


def download_wind_data(date: datetime, output_dir: str, output_filename: str) -> Optional[str]:
    """Download wind data for a specific date."""
    date_str = date.strftime("%Y%m%d")
    year_str = date.strftime("%Y")
    base_url = f"https://coastwatch.noaa.gov/thredds/fileServer/uvcompNCEIBlendedGlobalSCISixHourWW00/{year_str}/"
    filename = f"NBSv02_wind_6hourly_{date_str}.nc"
    url = f"{base_url}{filename}"

    response = requests.get(url)
    if response.status_code == 200:
        output_path = os.path.join(output_dir, f"{output_filename}.nc")
        with open(output_path, "wb") as file:
            file.write(response.content)
        logging.info(f"Downloaded {output_path}")
        return output_path
    else:
        logging.error(f"Error downloading file: {response.status_code}")
        logging.error(response.text)
        return None


def convert_datetime(hours_since_19780101_array: np.ndarray) -> list:
    """Convert hours since 19780101 to datetime objects."""
    base_date = datetime(1970, 1, 1)
    seconds_since_1970 = hours_since_19780101_array * 3600 + (1978 - 1970) * 365.25 * 24 * 3600
    return [base_date + timedelta(seconds=sec) for sec in seconds_since_1970]


def extract_wind_speed(
    filename: str,
    latitude: float,
    longitude: float,
    target_datetime: datetime,
    output_filepath: Optional[str] = None,
) -> Tuple[float, pd.DataFrame]:
    """Extract wind speed from NetCDF file."""
    dataset = Dataset(filename)
    latitudes = dataset.variables["lat"][:]
    longitudes = dataset.variables["lon"][:]
    lat_index = (np.abs(latitudes - latitude)).argmin()
    lon_index = (np.abs(longitudes - longitude)).argmin()

    u_wind = dataset.variables["u_wind"]
    v_wind = dataset.variables["v_wind"]
    time_variable = dataset.variables["time"]

    u_wind_values = u_wind[:, 0, lat_index, lon_index].data
    v_wind_values = v_wind[:, 0, lat_index, lon_index].data
    time_values = time_variable[:]

    date_times = convert_datetime(time_values.data)

    # Make target_datetime timezone-naive
    target_datetime = target_datetime.replace(tzinfo=None)

    # Calculate time deltas between target_datetime and date_times
    time_deltas = [target_datetime - dt for dt in date_times]
    hours_difference = [np.abs(delta) for delta in time_deltas]

    # Find the index of the closest datetime
    closest_time_index = min(
        range(len(hours_difference)), key=lambda i: hours_difference[i].total_seconds()
    )
    closest_time_str = date_times[closest_time_index].strftime("%Y-%m-%d %H:%M:%S")

    # Calculate wind speed
    wind_speed_values = wind_speed(
        u=u_wind_values * units(u_wind.units), v=v_wind_values * units(v_wind.units)
    )
    closest_wind_speed = wind_speed_values[closest_time_index].to_tuple()[0]

    wind_data = {
        "windspeed": [closest_wind_speed],
        "wind_speed_date_time": [closest_time_str],
        "input_date_time": [target_datetime.strftime("%Y-%m-%d %H:%M:%S")],
        "wind_speed_height": [v_wind.height],
        "filename": [filename],
        "longitude": [longitude],
        "latitude": [latitude],
    }

    wind_speed_df = pd.DataFrame(wind_data)

    if output_filepath:
        wind_speed_df.to_csv(output_filepath, index=False)

    dataset.close()
    return closest_wind_speed, wind_speed_df

def get_session(verbose: int = 0, ntries: int = 5) -> Session:
    """Get or create a session for AOT retrieval."""
    global obpgSession
    if not obpgSession:
        if verbose > 1:
            logging.basicConfig(level=logging.DEBUG)
        obpgSession = Session()
        obpgSession.mount("https://", HTTPAdapter(max_retries=ntries))
    else:
        if verbose > 1:
            print("Reusing existing session")
    return obpgSession

def is_request_auth_failure(req: requests.Response) -> bool:
    """Check if a request failed due to authentication."""
    ctype = req.headers.get("Content-Type")
    if ctype and ctype.startswith("text/html"):
        return "<title>Earthdata Login</title>" in req.text
    return False

def http_download(
    server: str,
    request: str,
    localpath: str = ".",
    outputfilename: Optional[str] = None,
    ntries: int = 5,
    uncompress: bool = False,
    timeout: float = 30.0,
    verbose: int = 0,
    force_download: bool = False,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> int:
    """Download a file via HTTP."""
    status = 0
    url_str = f"https://{server}{request}"
    localpath = Path(localpath)
    get_session(verbose=verbose, ntries=ntries)
    modified_since = None
    headers = {}

    if not force_download:
        if outputfilename:
            ofile = localpath / outputfilename
            modified_since = get_file_time(ofile)
        else:
            rpath = Path(request.rstrip().split("?")[0])
            ofile = localpath / rpath.name
            modified_since = get_file_time(ofile)

        if modified_since:
            headers = {"If-Modified-Since": modified_since.strftime("%a, %d %b %Y %H:%M:%S GMT")}

    with obpgSession.get(url_str, stream=True, timeout=timeout, headers=headers) as req:
        if req.status_code != 200:
            status = req.status_code
        elif is_request_auth_failure(req):
            status = 401
        else:
            if not Path.exists(localpath):
                os.umask(0o02)
                Path.mkdir(localpath, mode=0o2775, parents=True)

            if not outputfilename:
                cd = req.headers.get("Content-Disposition")
                if cd:
                    outputfilename = re.findall("filename=(.+)", cd)[0]
                else:
                    outputfilename = url_str.split("/")[-1]

            ofile = localpath / outputfilename
            download = True
            if "last-modified" in req.headers:
                remote_lmt = req.headers["last-modified"]
                remote_ftime = datetime.strptime(remote_lmt, "%a, %d %b %Y %H:%M:%S GMT").replace(
                    tzinfo=None
                )
                if modified_since and not force_download:
                    if (remote_ftime - modified_since).total_seconds() < 0:
                        download = False
                        if verbose > 0:
                            print(f"Skipping download of {outputfilename}")

            if download:
                total_length = int(req.headers.get("content-length", 0))
                length_downloaded = 0
                if verbose > 0:
                    print(f"Downloading {outputfilename} ({total_length / 1024 / 1024:.2f} MB)")

                with open(ofile, "wb") as fd:
                    for chunk in req.iter_content(chunk_size=chunk_size):
                        if chunk:
                            length_downloaded += len(chunk)
                            fd.write(chunk)
                            if verbose > 0:
                                percent_done = int(50 * length_downloaded / total_length)
                                sys.stdout.write(
                                    f"\r[{'=' * percent_done}{' ' * (50 - percent_done)}]"
                                )
                                sys.stdout.flush()

                if uncompress and ofile.suffix in {".Z", ".gz", ".bz2"}:
                    if verbose:
                        print(f"\nUncompressing {ofile}")
                    compress_status = uncompress_file(ofile)  # type: ignore # noqa: F821
                    if compress_status:
                        status = compress_status
                else:
                    status = 0

                if verbose:
                    print("\n...Done")
    return status

def get_file_time(local_file: str) -> Optional[datetime]:
    """Get the modification time of a local file."""
    local_file = Path(local_file)
    if not Path.is_file(local_file):
        while local_file.suffix in {".Z", ".gz", ".bz2"}:
            local_file = local_file.with_suffix("")
    if Path.is_file(local_file):
        return datetime.fromtimestamp(local_file.stat().st_mtime)
    return None

def retrieve_url(
    request: str,
    localpath: str = ".",
    uncompress: bool = False,
    verbose: int = 0,
    force_download: bool = False,
    appkey: str = APP_KEY,
) -> int:
    """Retrieve a file from a URL."""
    if verbose:
        print(f"Retrieving {request.rstrip()}")

    server = "oceandata.sci.gsfc.nasa.gov"
    parsed_request = urlparse(request)
    netpath = parsed_request.path

    if parsed_request.netloc:
        server = parsed_request.netloc
    else:
        netpath = f"/ob/getfile/{netpath}"

    if re.match(".*getfile", netpath) and appkey:
        netpath = f"{netpath}?appkey={appkey}"

    if parsed_request.query:
        netpath = f"{netpath}&{parsed_request.query}"

    return http_download(
        server,
        netpath,
        localpath=localpath,
        uncompress=uncompress,
        verbose=verbose,
        force_download=force_download,
    )

def extract_vars_from_netcdf(lon: float, lat: float, ncdir: str, varname: str) -> float:
    """Extract variables from NetCDF file."""
    with Dataset(ncdir, "r") as nc:
        lons = nc.variables["lon"][:]
        lats = nc.variables["lat"][:]
        lon_idx = (np.abs(lons - lon)).argmin()
        lat_idx = (np.abs(lats - lat)).argmin()
        if "time" in nc.variables[varname].dimensions:
            return nc.variables[varname][
                0, lat_idx, lon_idx
            ]  
        else:
            return nc.variables[varname][lat_idx, lon_idx]

def find_closest_times_on_same_day(date: datetime) -> list:
    """Find the closest times on the same day."""
    base_times = [0, 6, 12, 18]
    times = [date.replace(hour=bt, minute=0, second=0, microsecond=0) for bt in base_times]
    times.sort(key=lambda x: abs(x - date))
    return times

def retrieve_aot_data(date: datetime, lat: float, lon: float, output_dir: str) -> Optional[float]:
    """Retrieve AOT data for a specific date."""
    closest_times = find_closest_times_on_same_day(date)
    nc_filename = None

    for closest_time in closest_times:
        date_str = closest_time.strftime("%Y%m%dT%H%M%S")
        nc_filename = f"GMAO_MERRA2.{date_str}.AER.nc"
        output_file = os.path.join(output_dir, nc_filename)

        if not os.path.exists(output_file):
            url = f"https://oceandata.sci.gsfc.nasa.gov/getfile/{nc_filename}?appkey={APP_KEY}"
            status = retrieve_url(url, localpath=output_dir, verbose=0)
            if status == 0:
                logging.info(f"Downloaded {nc_filename}")
                break
        else:
            logging.info(f"File {nc_filename} already exists, skipping download.")
            break
    else:
        logging.warning(
            "No data available for the exact time on the same day, checking previous and next days..."
        )
        previous_day = date - timedelta(days=1)
        next_day = date + timedelta(days=1)
        for day in [previous_day, next_day]:
            closest_times = find_closest_times_on_same_day(day)
            for closest_time in closest_times:
                date_str = closest_time.strftime("%Y%m%dT%H%M%S")
                nc_filename = f"GMAO_MERRA2.{date_str}.AER.nc"
                output_file = os.path.join(output_dir, nc_filename)

                if not os.path.exists(output_file):
                    url = f"https://oceandata.sci.gsfc.nasa.gov/getfile/{nc_filename}?appkey={APP_KEY}"
                    status = retrieve_url(url, localpath=output_dir, verbose=0)
                    if status == 0:
                        logging.info(f"Downloaded {nc_filename}")
                        break
            if status == 0:
                break
        else:
            logging.error("No data available for the day before or after the target date.")
            return None

    varname = "TOTEXTTAU"
    totexttau = extract_vars_from_netcdf(lon, lat, output_file, varname)
    logging.info(f"TOTEXTTAU at ({lat}, {lon}) for {date}: {totexttau}")
    return totexttau

class AcoliteRunner:
    """Class to run Acolite processing."""

    def __init__(self, output_directory: str, acolite_script_path: str):
        self.output_directory = output_directory
        self.acolite_script_path = acolite_script_path

    def get_next_version(self, base_folder: str) -> str:
        version = 0
        while os.path.exists(f"{base_folder}_v{version}"):
            version += 1
        return f"{base_folder}_v{version}"

    def configure_acolite_settings(self, input_directories: list, params: dict) -> list:
        settings_files = []
        for input_dir in input_directories:
            unique_name = os.path.basename(os.path.normpath(input_dir))
            base_output_folder = os.path.join(self.output_directory, unique_name)
            output_folder = self.get_next_version(base_output_folder)
            os.makedirs(output_folder, exist_ok=True)

            settings_file_name = f"{unique_name}_settings.txt"
            settings_file_path = os.path.join(output_folder, settings_file_name)
            settings_files.append(settings_file_path)

            with open(settings_file_path, "w") as file:
                file.write(f"inputfile={input_dir}\n")
                file.write(f"output={output_folder}\n")
                for key, value in params.items():
                    file.write(f"{key}={value}\n")

    def get_base_folder_name(self, input_folder: str) -> str:
        """Extract base folder name without version suffix"""
        # Remove .SAFE_v* suffix
        base_name = re.sub(r"\.SAFE_v\d+$", "", os.path.basename(input_folder))
        return base_name

    def get_next_version(self, base_folder: str, main_folder: str) -> str:  # noqa: F811
        """Get next version number for output folder inside main folder"""
        version = 0
        while os.path.exists(
            os.path.join(main_folder, f"{os.path.basename(base_folder)}_v{version}")
        ):
            version += 1
        return f"{os.path.basename(base_folder)}_v{version}"

    def configure_acolite_settings(self, input_directories: list, params: dict) -> list:  # noqa: F811
        settings_files = []
        for input_dir in input_directories:
            # Get base name for main folder
            base_name = self.get_base_folder_name(input_dir)

            # Create main folder
            main_folder = os.path.join(self.output_directory, base_name)
            os.makedirs(main_folder, exist_ok=True)

            # Get unique name and next version
            unique_name = os.path.basename(os.path.normpath(input_dir))
            base_output_folder = os.path.join(main_folder, unique_name)
            output_folder = self.get_next_version(base_output_folder, main_folder)

            # Create versioned output folder inside main folder
            output_folder_path = os.path.join(main_folder, output_folder)
            os.makedirs(output_folder_path, exist_ok=True)

            # Create settings file
            settings_file_name = f"{unique_name}_settings.txt"
            settings_file_path = os.path.join(output_folder_path, settings_file_name)
            settings_files.append(settings_file_path)

            with open(settings_file_path, "w") as file:
                file.write(f"inputfile={input_dir}\n")
                file.write(f"output={output_folder_path}\n")
                for key, value in params.items():
                    file.write(f"{key}={value}\n")

        return settings_files
    
    
    def run_acolite_direct(self, settings_files: list):
        for settings_file in settings_files:
            # Create a command to run Acolite
            command_string = f"python {self.acolite_script_path} --cli --settings={settings_file}"
            logging.info(f"Running Acolite with settings: {settings_file}")
            
            # Run the process directly with output going to console
            subprocess.run(command_string, shell=True, check=True)

    def stack_bands(self):
        for root, _, files in os.walk(self.output_directory):
            tif_files = [
                os.path.join(root, file)
                for file in files
                if file.endswith(".tif") and re.search(r"Rrs_\d{3}\.tif$", file)
            ]
            if tif_files:
                self.stack_rasters(tif_files, root)
    
    def stack_rasters(self, input_files: list, output_folder: str):
        """Stack individual Rrs files into a single multi-band raster with LZW compression"""
        if not input_files:
            print("No input files to stack.")
            return
        
        # Sort files by wavelength number for consistent band order
        input_files.sort(key=lambda x: self.extract_number_from_filename(os.path.basename(x)))
        
        # Extract base name from the first file
        first_file = os.path.basename(input_files[0])
        # Parse pattern like S2A_MSI_2022_08_22_17_12_03_T20XNK_L2W_Rrs_492.tif
        match = re.search(r'(.*?)_L2W_Rrs_\d{3}\.tif', first_file)
        
        if match:
            base_name = match.group(1) + "_Rrs"
        else:
            # Fallback if pattern doesn't match
            base_name = "Rrs"
        
        # Output directly to the folder (no subfolder)
        output_path = os.path.join(output_folder, f"{base_name}.tif")
        
        nodata_value = -9999
        
        # Open the first raster to get metadata
        ref_ds = gdal.Open(input_files[0], gdal.GA_ReadOnly)
        x_size, y_size = ref_ds.RasterXSize, ref_ds.RasterYSize
        geotransform, projection = ref_ds.GetGeoTransform(), ref_ds.GetProjection()
        
        # Create output raster with LZW compression
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
        out_ds.SetGeoTransform(geotransform)
        out_ds.SetProjection(projection)
        
        # Stack the bands
        for i, raster_path in enumerate(input_files, start=1):
            src_ds = gdal.Open(raster_path, gdal.GA_ReadOnly)
            band_data = src_ds.GetRasterBand(1).ReadAsArray()
            band_data = np.nan_to_num(band_data, nan=nodata_value)
            
            out_band = out_ds.GetRasterBand(i)
            out_band.WriteArray(band_data)
            out_band.SetNoDataValue(nodata_value)
            out_band.SetDescription(os.path.splitext(os.path.basename(raster_path))[0])
            
            src_ds = None
        
        out_ds = None
        ref_ds = None
        
        logging.info(f"Stacked raster with LZW compression saved to {output_path}")


    def extract_number_from_filename(self, filename):
        """Extract the numerical part from the filename."""
        match = re.search(r'Rrs_(\d{3})\.tif', filename)
        if match:
            return int(match.group(1))
        return -1  # Default value if no 3-digit number found


    def get_utm_zone(longitude: float, latitude: float) -> str:
        """Calculate UTM zone from longitude and latitude"""
        print(f"DEBUG: get_utm_zone called with lon={longitude}, lat={latitude}")
        
        zone_number = int((longitude + 180) / 6) + 1
        zone_letter = "N" if latitude >= 0 else "S"
        result = f"{zone_number}{zone_letter}"
        
        print(f"DEBUG: Calculated zone = {result}")
        return result

    def get_utm_epsg(longitude: float, latitude: float) -> str:
        """Get EPSG code for UTM zone"""
        
        logging.debug(f"UTM calculation - Input longitude: {longitude}, latitude: {latitude}")
        
        zone_number = int((longitude + 180) / 6) + 1
        zone_letter = "N" if latitude >= 0 else "S"
        
        if zone_letter == "N":
            epsg = 32600 + zone_number
        else:
            epsg = 32700 + zone_number
        
        logging.debug(f"Calculated UTM zone: {zone_number}{zone_letter}, Final EPSG: {epsg}")
        return str(epsg)


    def post_process_acolite_outputs(self, longitude: float, latitude: float):
        """Apply both UTM and Geographic coordinate systems dynamically"""
        current_dir = os.path.dirname(os.path.abspath(__file__))
        fix_epsg_path = os.path.join(current_dir, "fix_epsg.py")

        # Dynamically determine UTM zone EPSG
        utm_epsg = self.get_utm_epsg(longitude, latitude)

        # Apply UTM projection
        cmd_utm = f"python {fix_epsg_path} -p{self.output_directory} -e{utm_epsg} -f"
        subprocess.run(cmd_utm, shell=True)

        # Apply WGS84
        cmd_wgs = f"python {fix_epsg_path} -p{self.output_directory} -e4326"
        subprocess.run(cmd_wgs, shell=True)

def run_acolite_pipeline(params, output_dir, input_directories, coordinates_dict, sensor_type=None):

    acolite_runner = AcoliteRunner(
        output_directory=output_dir,
        #acolite_script_path="D:\\15_Gitlab\\acolite\\launch_acolite.py",
        acolite_script_path="W:\\03_RnD\\02_RTM\\CI_SPACE\\Softwares\\acolite-20250402.0\\launch_acolite.py",
    )
    
    settings_files = acolite_runner.configure_acolite_settings(input_directories, params)
    acolite_runner.run_acolite_direct(settings_files)
    acolite_runner.stack_bands()
    
    # Assign EPSG codes to Acolite output that has no projection
    print("\n:: Post-processing: Assigning EPSG codes to Acolite output...")
    #assign_epsg_to_acolite_output(output_dir)
    
    # ONLY assign CRS info without reprojecting
    if coordinates_dict:
        assign_projection_to_rasters(output_dir, coordinates_dict)
        
    cleanup_individual_rrs_files(
        output_dir, keep_individual=params.get("keep_individual_rrs", True)
    )
    delete_files_by_extension(output_dir, [".png", ".nc", ".csv"])


def find_scene_output_directory(output_directory: str, input_dir: str) -> str:
    """Find the output directory corresponding to an input directory"""
    input_basename = os.path.basename(os.path.normpath(input_dir))
    
    # Look for directories that match the input basename pattern
    for root, dirs, files in os.walk(output_directory):
        if input_basename in os.path.basename(root):
            return root
    return None

def apply_utm_projection_to_scene(scene_dir: str, longitude: float, latitude: float):
    """Apply UTM projection to a specific scene directory"""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    fix_epsg_path = os.path.join(current_dir, "fix_epsg.py")
    
    # Calculate UTM zone for this specific scene
    utm_epsg = get_utm_epsg(longitude, latitude)
    
    if os.path.exists(fix_epsg_path):
        cmd = f"python {fix_epsg_path} -p{scene_dir} -e{utm_epsg} -f"
        try:
            subprocess.run(cmd, shell=True, check=True)
            logging.info(f"Applied EPSG:{utm_epsg} to scene: {os.path.basename(scene_dir)}")
        except subprocess.CalledProcessError as e:
            logging.error(f"Failed to apply EPSG:{utm_epsg} to {scene_dir}: {e}")
    else:
        logging.warning(f"fix_epsg.py not found at {fix_epsg_path}")

def get_utm_epsg(longitude: float, latitude: float) -> str:
    """Get EPSG code for UTM zone"""
    zone_number = int((longitude + 180) / 6) + 1
    zone_letter = "N" if latitude >= 0 else "S"
    
    # EPSG codes for UTM zones
    if zone_letter == "N":
        epsg = 32600 + zone_number
    else:
        epsg = 32700 + zone_number
    
    return str(epsg)

def cleanup_individual_rrs_files(output_directory: str, keep_individual: bool = False):
    if keep_individual:
        logging.info("Keeping individual Rrs files as requested")
        return

    deleted_count = 0
    for root, _, files in os.walk(output_directory):
        for file in files:
            # Match individual Rrs files with both 3 and 4 digits (e.g., Rrs_443.tif
            # or Rrs_1373.tif)
            if file.endswith(".tif") and re.search(r"Rrs_\d{3,4}\.tif$", file):
                file_path = os.path.join(root, file)
                try:
                    os.remove(file_path)
                    deleted_count += 1
                    logging.info(f"Deleted individual Rrs file: {file}")
                except Exception as e:
                    logging.error(f"Error deleting {file}: {e}")

def delete_files_by_extension(output_directory, extensions_to_delete):
    """Delete files with specified extensions in the output directory and subdirectories."""
    deleted_files = []
    for root, _, files in os.walk(output_directory):
        for file in files:
            if any(file.endswith(ext) for ext in extensions_to_delete):
                file_path = os.path.join(root, file)
                try:
                    os.remove(file_path)
                    deleted_files.append(file_path)
                    logging.info(f"Deleted: {file_path}")
                except Exception as e:
                    logging.error(f"Error deleting {file_path}: {e}")
    logging.info(f"Total files deleted: {len(deleted_files)}")

def read_settings_file(settings_file_path):
    """Read settings from a file and return them as a dictionary."""
    settings = {}
    with open(settings_file_path, "r") as file:
        for line in file:
            if line.strip() and not line.startswith("#"):  # Ignore empty lines and comments
                key, value = line.strip().split("=", 1)
                settings[key.strip()] = value.strip()
    return settings

def log_processing_time(output_dir, filename, start_time, success=True):
    """Log the processing time for a file to processing_time.txt"""
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
    log_file = os.path.join(output_dir, "processing_time.txt")
    if not os.path.exists(log_file):
        with open(log_file, "w") as f:
            f.write(f"Processing Time Log - Created on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("=" * 80 + "\n")
            f.write("Filename | Start Time | End Time | Duration (seconds) | Duration (formatted) | Status\n")
            f.write("-" * 80 + "\n")
    
    # Log the processing time
    status = "SUCCESS" if success else "FAILED"
    with open(log_file, "a") as f:
        f.write(f"{filename} | {start_timestamp} | {end_timestamp} | {duration:.2f} | {formatted_duration} | {status}\n")
    
    return duration

def get_utm_epsg_from_sentinel_tile(filename: str) -> str:
    """Extract EPSG from Sentinel-2 tile ID in filename"""
    import re
    
    # Look for pattern like T18RTP in filename
    match = re.search(r'T(\d{1,2})[A-Z]{3}', filename)
    
    if match:
        zone_number = int(match.group(1))
        epsg = 32600 + zone_number
        print(f"DEBUG: Extracted Zone {zone_number} from {filename} -> EPSG {epsg}")
        return str(epsg)
    
    return None

def assign_epsg_to_raster(raster_path: str, epsg_code: str):
    """Assign EPSG code to a single raster without reprojecting"""
    try:
        from osgeo import gdal, osr
        
        # Open raster in update mode
        dataset = gdal.Open(raster_path, gdal.GA_Update)
        if dataset is None:
            logging.error(f"Could not open {raster_path}")
            return False
        
        # Create spatial reference
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(int(epsg_code))
        
        dataset.SetProjection(srs.ExportToWkt())
        
        dataset.FlushCache()
        dataset = None
        
        logging.info(f"Successfully assigned EPSG:{epsg_code} to {os.path.basename(raster_path)}")
        return True
        
    except Exception as e:
        logging.error(f"Error assigning EPSG to {raster_path}: {e}")
        return False

def find_input_dir_for_output(output_path: str, coordinates_dict: dict) -> str:
    """Find the input directory that corresponds to this output path"""
    output_basename = os.path.basename(output_path)
    
    for input_dir in coordinates_dict.keys():
        input_basename = os.path.basename(input_dir)
        if input_basename in output_basename:
            return input_dir
    return None

def assign_projection_to_rasters(output_dir: str, coordinates_dict: dict, sensor_type: str = None):  # Changed parameter name
    """Assign proper projection information to all rasters in output directory"""
    from osgeo import gdal, osr
    
    # New approach for WV 
    if sensor_type and sensor_type.lower() == 'worldview':
        assign_worldview_projection_preserve_georef(output_dir, coordinates_dict)  # Changed variable name
        return
    
    # Sentinel-2 and Planet (unchanged)
    for root, dirs, files in os.walk(output_dir):  # Changed from output_directory to output_dir
        tif_files = [f for f in files if f.endswith('.tif')]
        
        if tif_files:
            for tif_file in tif_files:
                # Try to get EPSG from Sentinel-2 tile ID first
                utm_epsg = get_utm_epsg_from_sentinel_tile(tif_file)
                
                if utm_epsg:
                    logging.info(f"Using tile-based EPSG {utm_epsg} for {tif_file}")
                else:
                    # Fallback to coordinate-based calculation
                    input_dir = find_input_dir_for_output(root, coordinates_dict)
                    if input_dir and input_dir in coordinates_dict:
                        longitude, latitude = coordinates_dict[input_dir]
                        utm_epsg = get_utm_epsg(longitude, latitude)
                        logging.info(f"Using coordinate-based EPSG {utm_epsg} for {tif_file}")
                
                if utm_epsg:
                    tif_path = os.path.join(root, tif_file)
                    assign_epsg_to_raster(tif_path, utm_epsg)
                    logging.info(f"Assigned EPSG:{utm_epsg} to {tif_file}")

def assign_projection_to_rasters(output_dir: str, coordinates_dict: dict, sensor_type: str = None):
    """Assign proper projection information to all rasters in output directory"""
    from osgeo import gdal, osr
    
    # New approach for WV 
    if sensor_type and sensor_type.lower() == 'worldview':
        assign_worldview_projection_preserve_georef(output_dir, coordinates_dict)  # Changed from output_directory
        return
    
    # Sentinel-2 and Planet (unchanged)
    for root, dirs, files in os.walk(output_dir):  # This was already correct
        tif_files = [f for f in files if f.endswith('.tif')]
        
        if tif_files:
            for tif_file in tif_files:
                # Try to get EPSG from Sentinel-2 tile ID first
                utm_epsg = get_utm_epsg_from_sentinel_tile(tif_file)
                
                if utm_epsg:
                    logging.info(f"Using tile-based EPSG {utm_epsg} for {tif_file}")
                else:
                    # Fallback to coordinate-based calculation
                    input_dir = find_input_dir_for_output(root, coordinates_dict)
                    if input_dir and input_dir in coordinates_dict:
                        longitude, latitude = coordinates_dict[input_dir]
                        utm_epsg = get_utm_epsg(longitude, latitude)
                        logging.info(f"Using coordinate-based EPSG {utm_epsg} for {tif_file}")
                
                if utm_epsg:
                    tif_path = os.path.join(root, tif_file)
                    assign_epsg_to_raster(tif_path, utm_epsg)
                    logging.info(f"Assigned EPSG:{utm_epsg} to {tif_file}")
def detect_sensor_type(input_dir: str) -> str:
    """Detect sensor type from input directory"""
    if input_dir.endswith('.SAFE') or '.SAFE' in input_dir:
        return 'sentinel2'
    elif find_metadata_file(input_dir, ".IMD"):
        return 'worldview'
    elif find_metadata_file(input_dir, "metadata_clip.xml") or find_metadata_file(input_dir, "metadata.xml"):
        return 'planet'
    else:
        return 'unknown'


def main():
    """Main function to parse command-line arguments and start processing."""
    logging.getLogger().setLevel(logging.DEBUG)
    
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

    args = parser.parse_args()
    output_dir = args.output
    
    if not os.path.exists(output_dir):
        os.makedirs(output_dir, exist_ok=True)

    # Gather input directories
    input_directories = args.inputs if args.inputs else []
    if args.input_dir:
        input_directories.extend(
            [
                os.path.join(args.input_dir, d)
                for d in os.listdir(args.input_dir)
                if os.path.isdir(os.path.join(args.input_dir, d))
            ]
        )

    # Ensure there are input directories to process
    if not input_directories:
        raise ValueError("No input directories specified. Use --inputs or --input_dir.")

    # Read settings from the specified file
    params = read_settings_file(args.settings)
    
    # Track total processing time
    total_start_time = time.time()
    
    # Dictionary to store coordinates for each input directory
    coordinates_dict = {}
    
    # Process each input directory to extract metadata and prepare parameters
    for input_dir in input_directories:
        filename = os.path.basename(os.path.normpath(input_dir))
        start_time = time.time()
        success = True
        
        try:
            #for resampling
            if input_dir.endswith('.SAFE') or '.SAFE' in input_dir:
                logging.info(f"Detected Sentinel-2 data: {filename}")
                resample_sentinel2_bands(input_dir)
            
            # Extract metadata
            imd_file = find_metadata_file(input_dir, ".IMD")
            xml_file = find_metadata_file(input_dir, "metadata_clip.xml") or find_metadata_file(input_dir, "metadata.xml")
            sentinel2_xml_file = find_metadata_file(input_dir, "MTD_MSIL1C.xml")
            
            if imd_file:
                date, latitude, longitude = extract_date_lat_lon_from_imd(imd_file)
            elif xml_file:
                date, latitude, longitude = parse_xml_file_for_date_lat_lon(xml_file)
                if date is None or latitude is None or longitude is None:
                    logging.warning(f"No valid metadata found in {xml_file}")
                    success = False
                    continue
            elif sentinel2_xml_file:
                date, latitude, longitude = parse_sentinel2_metadata(sentinel2_xml_file)
                if date is None or latitude is None or longitude is None:
                    logging.warning(f"No valid metadata found in {sentinel2_xml_file}")
                    success = False
                    continue
            else:
                logging.warning(f"No metadata file found in {input_dir}")
                success = False
                continue

            logging.info(f"Extracted: Date: {date}, Latitude: {latitude}, Longitude: {longitude}")
            
            # Store coordinates for this input directory
            coordinates_dict[input_dir] = (longitude, latitude)
            
            # Download wind data
            filename_wind = download_wind_data(date, output_dir, "wind")
            if filename_wind is None or not os.path.exists(filename_wind):
                logging.warning(f"Wind data not available for {date}. Using default wind speed.")
                closest_wind_speed = 5  # Default wind speed
            else:
                closest_wind_speed, wind_speed_df = extract_wind_speed(
                    filename_wind,
                    latitude,
                    longitude,
                    date,
                    os.path.join(output_dir, "wind_speed.csv"),
                )
                logging.info(wind_speed_df)

            # Retrieve AOT data
            closest_aot = retrieve_aot_data(date, latitude, longitude, output_dir)
            if closest_aot is None:
                logging.warning(f"AOT data not available for {date}. Using default AOT value.")

            # Update parameters for this specific image
            current_params = params.copy()
            current_params["wind"] = str(closest_wind_speed)
            current_params["aot"] = str(closest_aot) if closest_aot is not None else "0.1"
            
            # Run Acolite processing for this single directory
            # Detect sensor type
            sensor_type = detect_sensor_type(input_dir)
            logging.info(f"Detected sensor type: {sensor_type}")

            # Run Acolite processing for this single directory
            run_acolite_pipeline(current_params, output_dir, [input_dir], {input_dir: (longitude, latitude)}, sensor_type)

        except Exception as e:
            logging.error(f"Error processing {filename}: {str(e)}")
            success = False

        # Log the processing time
        duration = log_processing_time(output_dir, filename, start_time, success)
        logging.info(f"Processed {filename} in {duration:.2f} seconds")

    # Log the total processing time
    total_duration = time.time() - total_start_time
    hours, remainder = divmod(total_duration, 3600)
    minutes, seconds = divmod(remainder, 60)
    formatted_total = f"{int(hours):02d}:{int(minutes):02d}:{seconds:.2f}"
    
    with open(os.path.join(output_dir, "processing_time.txt"), "a") as f:
        f.write("=" * 80 + "\n")
        f.write(f"Summary - Completed on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Total processing time: {total_duration:.2f} seconds ({formatted_total})\n")
        f.write("=" * 80 + "\n\n")


if __name__ == "__main__":
    main()