"""
Environmental Data Retrieval Module

This module handles the download and extraction of environmental data (wind and AOT)
with robust error handling and fallback strategies.
"""

import os
import logging
from datetime import datetime, timedelta
from typing import Optional
import numpy as np
import requests
from requests.adapters import HTTPAdapter
from requests import Session
from netCDF4 import Dataset


class EnvironmentalDataRetriever:
    """Handles wind and AOT data retrieval with robust error handling."""
    
    def __init__(self):
        self.session = self._create_session()
        self.app_key = "67542f3132f114592c6655dcd7a69ae13b4d5898"  # NASA API key
    
    def _create_session(self) -> Session:
        """Create HTTP session with retry strategy."""
        session = Session()
        adapter = HTTPAdapter(max_retries=3)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session
    
    def get_wind_speed(self, date: datetime, latitude: float, longitude: float, 
                      output_dir: str) -> Optional[float]:
        """
        Retrieve wind speed with fallback to adjacent dates.
        
        Args:
            date: Target date for wind data
            latitude: Latitude coordinate
            longitude: Longitude coordinate
            output_dir: Directory to save downloaded files
            
        Returns:
            Wind speed in m/s or None if unavailable
        """
        logging.info(f"Retrieving wind data for {date.strftime('%Y-%m-%d')} at ({latitude:.3f}, {longitude:.3f})")
        
        for day_offset in [0, -1, 1, -2, 2]:
            try_date = date + timedelta(days=day_offset)
            wind_speed = self._download_and_extract_wind(try_date, latitude, longitude, output_dir)
            
            if wind_speed is not None:
                if day_offset != 0:
                    logging.info(f"Using wind data from {try_date.strftime('%Y-%m-%d')} "
                               f"(offset: {day_offset} days)")
                else:
                    logging.info(f"Using wind data from exact date: {try_date.strftime('%Y-%m-%d')}")
                return wind_speed
        
        logging.warning("No wind data available for date range")
        return None
    
    def _download_and_extract_wind(self, date: datetime, latitude: float, longitude: float, 
                                 output_dir: str) -> Optional[float]:
        """Download and extract wind data for specific date."""
        date_str = date.strftime("%Y%m%d")
        year_str = date.strftime("%Y")
        filename = f"NBSv02_wind_6hourly_{date_str}.nc"
        output_path = os.path.join(output_dir, filename)
        
        # Download if not exists
        if not os.path.exists(output_path):
            url = (f"https://coastwatch.noaa.gov/thredds/fileServer/"
                  f"uvcompNCEIBlendedGlobalSCISixHourWW00/{year_str}/{filename}")
            
            try:
                logging.debug(f"Downloading wind data from: {url}")
                response = self.session.get(url, timeout=60)
                response.raise_for_status()
                
                with open(output_path, "wb") as file:
                    file.write(response.content)
                
                logging.info(f"Downloaded wind data: {filename}")
            except Exception as e:
                logging.debug(f"Failed to download wind data for {date_str}: {e}")
                return None
        else:
            logging.debug(f"Wind data file already exists: {filename}")
        
        # Extract wind speed
        return self._extract_wind_from_file(output_path, latitude, longitude, date)
    
    def _extract_wind_from_file(self, filepath: str, latitude: float, longitude: float, 
                              target_datetime: datetime) -> Optional[float]:
        """Extract wind speed from NetCDF file."""
        try:
            with Dataset(filepath, 'r') as dataset:
                # Get coordinate indices
                lats = dataset.variables["lat"][:]
                lons = dataset.variables["lon"][:]
                lat_idx = np.argmin(np.abs(lats - latitude))
                lon_idx = np.argmin(np.abs(lons - longitude))
                
                # Get wind components
                u_wind = dataset.variables["u_wind"][:, 0, lat_idx, lon_idx]
                v_wind = dataset.variables["v_wind"][:, 0, lat_idx, lon_idx]
                time_var = dataset.variables["time"][:]
                
                # Convert times to datetime objects
                base_date = datetime(1970, 1, 1)
                time_offsets = time_var * 3600 + (1978 - 1970) * 365.25 * 24 * 3600
                times = [base_date + timedelta(seconds=float(offset)) for offset in time_offsets]
                
                # Find closest time
                target_datetime = target_datetime.replace(tzinfo=None)
                time_diffs = [abs((target_datetime - t).total_seconds()) for t in times]
                closest_idx = np.argmin(time_diffs)
                
                # Calculate wind speed
                u_val = float(u_wind[closest_idx])
                v_val = float(v_wind[closest_idx])
                
                if np.isnan(u_val) or np.isnan(v_val):
                    logging.warning(f"NaN values in wind data at ({latitude:.3f}, {longitude:.3f})")
                    return None
                
                wind_speed = np.sqrt(u_val**2 + v_val**2)
                logging.info(f"Extracted wind speed: {wind_speed:.2f} m/s")
                return float(wind_speed)
                
        except Exception as e:
            logging.error(f"Error extracting wind from {filepath}: {e}")
            return None
    
    def get_aot_value(self, date: datetime, latitude: float, longitude: float, 
                     output_dir: str) -> Optional[float]:
        """
        Retrieve AOT value with fallback strategies.
        
        Args:
            date: Target date for AOT data
            latitude: Latitude coordinate
            longitude: Longitude coordinate
            output_dir: Directory to save downloaded files
            
        Returns:
            AOT value or None if unavailable
        """
        logging.info(f"Retrieving AOT data for {date.strftime('%Y-%m-%d')} at ({latitude:.3f}, {longitude:.3f})")
        
        # Try different times on the same day first
        times_to_try = [0, 6, 12, 18]  # Hours
        for hour in times_to_try:
            try_datetime = date.replace(hour=hour, minute=0, second=0, microsecond=0)
            aot_value = self._download_and_extract_aot(try_datetime, latitude, longitude, output_dir)
            if aot_value is not None:
                return aot_value
        
        # Try adjacent days
        for day_offset in [-1, 1, -2, 2]:
            try_date = date + timedelta(days=day_offset)
            for hour in times_to_try:
                try_datetime = try_date.replace(hour=hour, minute=0, second=0, microsecond=0)
                aot_value = self._download_and_extract_aot(try_datetime, latitude, longitude, output_dir)
                if aot_value is not None:
                    logging.info(f"Using AOT data from {try_datetime.strftime('%Y-%m-%d %H:%M')} "
                               f"(offset: {day_offset} days)")
                    return aot_value
        
        logging.warning("No AOT data available for date range")
        return None
    
    def _download_and_extract_aot(self, datetime_obj: datetime, latitude: float, longitude: float, 
                                output_dir: str) -> Optional[float]:
        """Download and extract AOT data for specific datetime."""
        date_str = datetime_obj.strftime("%Y%m%dT%H%M%S")
        filename = f"GMAO_MERRA2.{date_str}.AER.nc"
        output_path = os.path.join(output_dir, filename)
        
        # Download if not exists
        if not os.path.exists(output_path):
            url = f"https://oceandata.sci.gsfc.nasa.gov/getfile/{filename}?appkey={self.app_key}"
            
            try:
                logging.debug(f"Downloading AOT data: {filename}")
                response = self.session.get(url, timeout=120)
                response.raise_for_status()
                
                with open(output_path, "wb") as file:
                    file.write(response.content)
                
                logging.info(f"Downloaded AOT data: {filename}")
            except Exception as e:
                logging.debug(f"Failed to download AOT data for {date_str}: {e}")
                return None
        else:
            logging.debug(f"AOT data file already exists: {filename}")
        
        # Extract AOT value
        return self._extract_aot_from_file(output_path, latitude, longitude)
    
    def _extract_aot_from_file(self, filepath: str, latitude: float, longitude: float) -> Optional[float]:
        """Extract AOT value from NetCDF file."""
        try:
            with Dataset(filepath, 'r') as nc:
                lons = nc.variables["lon"][:]
                lats = nc.variables["lat"][:]
                lon_idx = np.argmin(np.abs(lons - longitude))
                lat_idx = np.argmin(np.abs(lats - latitude))
                
                varname = "TOTEXTTAU"
                if varname not in nc.variables:
                    logging.error(f"Variable {varname} not found in AOT file")
                    return None
                
                if "time" in nc.variables[varname].dimensions:
                    aot_value = nc.variables[varname][0, lat_idx, lon_idx]
                else:
                    aot_value = nc.variables[varname][lat_idx, lon_idx]
                
                if np.isnan(aot_value) or np.ma.is_masked(aot_value):
                    logging.warning(f"NaN or masked AOT value at ({latitude:.3f}, {longitude:.3f})")
                    return None
                
                aot_value = float(aot_value)
                logging.info(f"Extracted AOT value: {aot_value:.4f}")
                return aot_value
                
        except Exception as e:
            logging.error(f"Error extracting AOT from {filepath}: {e}")
            return None
