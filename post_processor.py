"""
Post-Processing Module

Handles post-processing tasks like EPSG assignment and file cleanup.
"""

import os
import logging
import re
from typing import Dict, Tuple
from osgeo import gdal, osr


class PostProcessor:
    """Handles post-processing tasks."""
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
    
    def process_outputs(self, output_dir: str, coordinates_dict: Dict[str, Tuple[float, float]], 
                       sensor_type: str) -> None:
        """Run all post-processing steps."""
        try:
            self.logger.info("Starting post-processing...")
            
            # Assign projections
            self._assign_projections(output_dir, coordinates_dict, sensor_type)
            
            # Cleanup files
            self._cleanup_files(output_dir)
            
            self.logger.info("Post-processing completed")
            
        except Exception as e:
            self.logger.error(f"Post-processing failed: {e}")
    
    def _assign_projections(self, output_dir: str, coordinates_dict: Dict[str, Tuple[float, float]], 
                           sensor_type: str) -> None:
        """Assign proper projection information to all rasters."""
        self.logger.info("Assigning projections to output rasters...")
        
        for root, dirs, files in os.walk(output_dir):
            tif_files = [f for f in files if f.endswith('.tif')]
            
            if tif_files:
                for tif_file in tif_files:
                    # Try to get EPSG from Sentinel-2 tile ID first
                    utm_epsg = self._get_utm_epsg_from_sentinel_tile(tif_file)
                    
                    if utm_epsg:
                        self.logger.info(f"Using tile-based EPSG {utm_epsg} for {tif_file}")
                    else:
                        # Fallback to coordinate-based calculation
                        input_dir = self._find_input_dir_for_output(root, coordinates_dict)
                        if input_dir and input_dir in coordinates_dict:
                            longitude, latitude = coordinates_dict[input_dir]
                            utm_epsg = self._get_utm_epsg(longitude, latitude)
                            self.logger.info(f"Using coordinate-based EPSG {utm_epsg} for {tif_file}")
                    
                    if utm_epsg:
                        tif_path = os.path.join(root, tif_file)
                        self._assign_epsg_to_raster(tif_path, utm_epsg)
    
    def _get_utm_epsg_from_sentinel_tile(self, filename: str) -> str:
        """Extract EPSG from Sentinel-2 tile ID in filename."""
        # Look for pattern like T18RTP in filename
        match = re.search(r'T(\d{1,2})[A-Z]{3}', filename)
        
        if match:
            zone_number = int(match.group(1))
            epsg = 32600 + zone_number
            return str(epsg)
        
        return None
    
    def _find_input_dir_for_output(self, output_path: str, coordinates_dict: Dict[str, Tuple[float, float]]) -> str:
        """Find the input directory that corresponds to this output path."""
        output_basename = os.path.basename(output_path)
        
        for input_dir in coordinates_dict.keys():
            input_basename = os.path.basename(input_dir)
            if input_basename in output_basename:
                return input_dir
        return None
    
    def _get_utm_epsg(self, longitude: float, latitude: float) -> str:
        """Get EPSG code for UTM zone."""
        zone_number = int((longitude + 180) / 6) + 1
        zone_letter = "N" if latitude >= 0 else "S"
        
        # EPSG codes for UTM zones
        if zone_letter == "N":
            epsg = 32600 + zone_number
        else:
            epsg = 32700 + zone_number
        
        return str(epsg)
    
    def _assign_epsg_to_raster(self, raster_path: str, epsg_code: str) -> bool:
        """Assign EPSG code to a single raster without reprojecting."""
        try:
            # Open raster in update mode
            dataset = gdal.Open(raster_path, gdal.GA_Update)
            if dataset is None:
                self.logger.error(f"Could not open {raster_path}")
                return False
            
            # Create spatial reference
            srs = osr.SpatialReference()
            srs.ImportFromEPSG(int(epsg_code))
            
            dataset.SetProjection(srs.ExportToWkt())
            dataset.FlushCache()
            dataset = None
            
            self.logger.info(f"Successfully assigned EPSG:{epsg_code} to {os.path.basename(raster_path)}")
            return True
            
        except Exception as e:
            self.logger.error(f"Error assigning EPSG to {raster_path}: {e}")
            return False
    
    def _cleanup_files(self, output_dir: str) -> None:
        """Clean up unwanted files from output directory."""
        self.logger.info("Cleaning up output files...")
        
        # Extensions to remove
        extensions_to_remove = ['.png', '.csv']
        
        # Individual Rrs files pattern
        rrs_pattern = re.compile(r"Rrs_\d{3,4}\.tif$")
        
        removed_count = 0
        
        for root, dirs, files in os.walk(output_dir):
            for file in files:
                file_path = os.path.join(root, file)
                should_remove = False
                
                # Check for unwanted extensions
                if any(file.endswith(ext) for ext in extensions_to_remove):
                    should_remove = True
                    reason = f"unwanted extension"
                
                # Check for individual Rrs files (keep stacked versions)
                elif rrs_pattern.search(file) and not self._is_stacked_file(file):
                    should_remove = True
                    reason = "individual Rrs file"
                
                if should_remove:
                    try:
                        os.remove(file_path)
                        removed_count += 1
                        self.logger.debug(f"Removed {file} ({reason})")
                    except Exception as e:
                        self.logger.warning(f"Could not remove {file}: {e}")
        
        self.logger.info(f"Cleanup completed: removed {removed_count} files")
    
    def _is_stacked_file(self, filename: str) -> bool:
        """Check if file is a stacked Rrs file (should be kept)."""
        # Stacked files typically have names like "scene_name_Rrs.tif"
        return filename.endswith("_Rrs.tif") and not re.search(r"Rrs_\d{3,4}\.tif$", filename)