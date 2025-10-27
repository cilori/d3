"""
Sentinel-2 Processing Module

Handles Sentinel-2 specific processing tasks like band resampling.
"""

import logging
import multiprocessing
import tempfile
import shutil
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from tqdm import tqdm
from osgeo import gdal


class SentinelProcessor:
    """Handles Sentinel-2 specific processing tasks."""
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.band_resolutions = {
            "B01": 60, "B02": 10, "B03": 10, "B04": 10, "B05": 20, "B06": 20,
            "B07": 20, "B08": 10, "B8A": 20, "B09": 60, "B10": 60, "B11": 20, "B12": 20,
        }
    
    def resample_bands(self, safe_dir: str) -> bool:
        """Resample Sentinel-2 20m and 60m bands to 10m resolution."""
        self.logger.info(f"Resampling Sentinel-2 bands in {safe_dir}")
        
        try:
            safe_path = Path(safe_dir)
            jp2_files = self._find_jp2_files(safe_path)
            geotransform, projection, rows, cols = self._get_10m_grid(jp2_files)
            
            cpu_count = multiprocessing.cpu_count()
            max_workers = max(1, int(cpu_count * 0.3))
            
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [
                    executor.submit(self._process_band, jp2_path, cols, rows, geotransform, projection)
                    for jp2_path in jp2_files
                ]
                
                for f in tqdm(futures, desc="Resampling bands"):
                    result = f.result()
                    self.logger.info(result)
            
            self.logger.info("Sentinel-2 band resampling completed")
            return True
            
        except Exception as e:
            self.logger.error(f"Sentinel-2 band resampling failed: {e}")
            return False
    
    def _find_jp2_files(self, safe_path: Path) -> list:
        """Find JP2 files in SAFE directory."""
        img_data_path = safe_path / "GRANULE"
        granule_dirs = list(img_data_path.glob("*"))
        if not granule_dirs:
            raise FileNotFoundError(f"No granule directories found in {img_data_path}")
        
        img_data_dir = granule_dirs[0] / "IMG_DATA"
        jp2_files = sorted(f for f in img_data_dir.glob("*.jp2") if "TCI" not in f.name.upper())
        if not jp2_files:
            raise FileNotFoundError(f"No JP2 files found in {img_data_dir}")
        
        return jp2_files
    
    def _get_band_id(self, filename: str) -> str:
        """Extract band ID from filename."""
        for band in self.band_resolutions:
            if f"_{band}" in filename.upper():
                return band
        return ""
    
    def _get_10m_grid(self, jp2_files: list):
        """Get 10m reference grid from available bands."""
        for jp2 in jp2_files:
            band = self._get_band_id(jp2.name)
            if self.band_resolutions.get(band) == 10:
                ds = gdal.Open(str(jp2))
                geotransform = ds.GetGeoTransform()
                projection = ds.GetProjection()
                rows = ds.RasterYSize
                cols = ds.RasterXSize
                ds = None
                return geotransform, projection, rows, cols
        
        raise RuntimeError("No 10m band found to define target grid.")
    
    def _process_band(self, jp2_path: Path, target_cols: int, target_rows: int, 
                     target_geotransform, target_projection) -> str:
        """Process a single band for resampling."""
        band = self._get_band_id(jp2_path.name)
        resolution = self.band_resolutions.get(band)
        
        if resolution == 10:
            return f"Skipped (already 10m): {jp2_path.name}"
        elif resolution in (20, 60):
            try:
                self._resample_to_10m(jp2_path, target_cols, target_rows, target_geotransform, target_projection)
                return f"Resampled: {jp2_path.name}"
            except Exception as e:
                return f"Failed to resample {jp2_path.name}: {e}"
        else:
            return f"Unknown band: {jp2_path.name}"
    
    def _resample_to_10m(self, jp2_path: Path, target_cols: int, target_rows: int, 
                        target_geotransform, target_projection):
        """Resample a single JP2 file to 10m resolution."""
        with tempfile.NamedTemporaryFile(suffix='.tif', dir=jp2_path.parent, delete=False) as tmp_tif:
            temp_tif_path = Path(tmp_tif.name)
        with tempfile.NamedTemporaryFile(suffix='.jp2', dir=jp2_path.parent, delete=False) as tmp_jp2:
            temp_jp2_path = Path(tmp_jp2.name)
        
        try:
            # Open source dataset
            src_ds = gdal.Open(str(jp2_path))
            if not src_ds:
                raise RuntimeError(f"Cannot open source file: {jp2_path}")
            
            # Create temporary TIFF
            gtiff_driver = gdal.GetDriverByName("GTiff")
            temp_ds = gtiff_driver.Create(str(temp_tif_path), target_cols, target_rows, 1, gdal.GDT_UInt16)
            temp_ds.SetGeoTransform(target_geotransform)
            temp_ds.SetProjection(target_projection)
            
            # Resample
            gdal.ReprojectImage(src_ds, temp_ds, src_ds.GetProjection(), target_projection, gdal.GRA_Bilinear)
            temp_ds.FlushCache()
            temp_ds = None
            src_ds = None
            
            # Convert TIFF to JP2
            temp_tif_ds = gdal.Open(str(temp_tif_path))
            jp2_driver = self._get_jp2_driver()
            
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
            
            # Replace original file
            shutil.move(str(temp_jp2_path), str(jp2_path))
            
        except Exception as e:
            raise e
        finally:
            # Cleanup temporary files
            for temp_path in [temp_tif_path, temp_jp2_path]:
                if temp_path.exists():
                    try:
                        temp_path.unlink()
                    except:
                        pass
    
    def _get_jp2_driver(self):
        """Get available JP2 driver."""
        for driver_name in ["JP2OpenJPEG", "JPEG2000", "JP2KAK"]:
            try:
                driver = gdal.GetDriverByName(driver_name)
                if driver:
                    return driver
            except:
                continue
        return None