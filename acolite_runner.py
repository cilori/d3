"""
Acolite Runner Module

Handles Acolite execution and post-processing tasks.
"""

import os
import logging
import subprocess
import re
from typing import Dict, List, Tuple
import numpy as np
from osgeo import gdal

from config_manager import ConfigManager


class AcoliteRunner:
    """Handles Acolite execution and related processing tasks."""
    
    def __init__(self, config: ConfigManager):
        self.config = config
        self.logger = logging.getLogger(__name__)
    
    def run_processing(self, params: Dict[str, str], input_directories: List[str], 
                      coordinates_dict: Dict[str, Tuple[float, float]], 
                      sensor_type: str) -> bool:
        """Run complete Acolite processing pipeline."""
        try:
            # Configure settings files
            settings_files = self._configure_acolite_settings(input_directories, params)
            
            # Run Acolite for each settings file
            success = True
            for settings_file in settings_files:
                if not self._run_acolite_direct(settings_file):
                    success = False
            
            if success:
                # Post-processing
                self._stack_bands()
            
            return success
            
        except Exception as e:
            self.logger.error(f"Acolite processing failed: {e}")
            return False
    
    def _configure_acolite_settings(self, input_directories: List[str], params: Dict[str, str]) -> List[str]:
        """Configure Acolite settings files."""
        settings_files = []
        
        for input_dir in input_directories:
            try:
                # Get base name for main folder
                base_name = self._get_base_folder_name(input_dir)
                
                # Create main folder
                main_folder = os.path.join(self.config.output_dir, base_name)
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
                
                with open(settings_file_path, "w") as file:
                    file.write(f"inputfile={input_dir}\n")
                    file.write(f"output={output_folder_path}\n")
                    for key, value in params.items():
                        file.write(f"{key}={value}\n")
                
                self.logger.debug(f"Created settings file: {settings_file_path}")
                
            except Exception as e:
                self.logger.error(f"Failed to create settings for {input_dir}: {e}")
                continue
        
        return settings_files
    
    def _get_base_folder_name(self, input_folder: str) -> str:
        """Extract base folder name without version suffix."""
        base_name = re.sub(r"\.SAFE_v\d+$", "", os.path.basename(input_folder))
        return base_name
    
    def _get_next_version(self, base_name: str, main_folder: str) -> str:
        """Get next version number for output folder inside main folder."""
        version = 0
        while os.path.exists(os.path.join(main_folder, f"{base_name}_v{version}")):
            version += 1
        return f"{base_name}_v{version}"
    
    def _run_acolite_direct(self, settings_file: str) -> bool:
        """Run Acolite with specific settings file."""
        try:
            command = [
                "python", self.config.acolite_script_path,
                "--cli", f"--settings={settings_file}"
            ]
            
            self.logger.info(f"Running Acolite: {' '.join(command)}")
            
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=3600  # 1 hour timeout
            )
            
            if result.returncode == 0:
                self.logger.info(f"Acolite completed successfully for {settings_file}")
                return True
            else:
                self.logger.error(f"Acolite failed for {settings_file}")
                self.logger.error(f"stdout: {result.stdout}")
                self.logger.error(f"stderr: {result.stderr}")
                return False
                
        except subprocess.TimeoutExpired:
            self.logger.error(f"Acolite timeout for {settings_file}")
            return False
        except Exception as e:
            self.logger.error(f"Error running Acolite for {settings_file}: {e}")
            return False
    
    def _stack_bands(self):
        """Stack individual Rrs bands into multi-band rasters."""
        self.logger.info("Stacking Rrs bands...")
        
        for root, _, files in os.walk(self.config.output_dir):
            rrs_files = [
                os.path.join(root, file)
                for file in files
                if file.endswith(".tif") and re.search(r"Rrs_\d{3,4}\.tif$", file)
            ]
            if rrs_files:
                self._stack_rasters(rrs_files, root)
    
    def _stack_rasters(self, input_files: List[str], output_folder: str):
        """Stack individual Rrs files into a single multi-band raster with LZW compression."""
        if not input_files:
            self.logger.warning("No input files to stack.")
            return
        
        # Sort files by wavelength number for consistent band order
        input_files.sort(key=lambda x: self._extract_number_from_filename(os.path.basename(x)))
        
        # Extract base name from the first file
        first_file = os.path.basename(input_files[0])
        match = re.search(r'(.*?)_L2W_Rrs_\d{3,4}\.tif', first_file)
        
        if match:
            base_name = match.group(1) + "_Rrs"
        else:
            base_name = "Rrs_stack"
        
        output_path = os.path.join(output_folder, f"{base_name}.tif")
        nodata_value = -9999
        
        try:
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
            
            self.logger.info(f"Stacked raster saved to {output_path}")
            
        except Exception as e:
            self.logger.error(f"Error stacking rasters: {e}")
    
    def _extract_number_from_filename(self, filename: str) -> int:
        """Extract the wavelength number from Rrs filename."""
        match = re.search(r'Rrs_(\d{3,4})\.tif', filename)
        if match:
            return int(match.group(1))
        return -1  # Default value if no number found