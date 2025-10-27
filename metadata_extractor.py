"""
Metadata Extraction Module

Handles metadata extraction from different satellite image formats with a factory pattern.
"""

import os
import logging
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple
import xml.etree.ElementTree as ET


class MetadataExtractor(ABC):
    """Abstract base class for metadata extractors."""
    
    def __init__(self, input_dir: str):
        self.input_dir = input_dir
        self.logger = logging.getLogger(self.__class__.__name__)
    
    @abstractmethod
    def extract_metadata(self) -> Optional[Tuple[datetime, float, float]]:
        """Extract metadata and return (date, latitude, longitude)."""
        pass
    
    def _find_file_with_extension(self, extension: str) -> Optional[str]:
        """Find file with specific extension in directory tree."""
        for root, dirs, files in os.walk(self.input_dir):
            for file in files:
                if file.endswith(extension):
                    return os.path.join(root, file)
        return None


class WorldViewExtractor(MetadataExtractor):
    """Metadata extractor for WorldView IMD files."""
    
    def extract_metadata(self) -> Optional[Tuple[datetime, float, float]]:
        """Extract metadata from WorldView IMD file."""
        imd_file = self._find_file_with_extension(".IMD")
        if not imd_file:
            return None
        
        self.logger.debug(f"Processing WorldView IMD file: {imd_file}")
        
        try:
            metadata = {}
            band_coordinates = {}
            current_group = None
            
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
                        band_coordinates[current_group][key.strip()] = value.strip().strip(';').strip('"')
                    elif '=' in line and not current_group:
                        key, value = line.split('=', 1)
                        metadata[key.strip()] = value.strip().strip(';').strip('"')
            
            # Extract coordinates from first band
            if not band_coordinates:
                raise ValueError("No band coordinate information found")
            
            first_band = list(band_coordinates.keys())[0]
            coords = band_coordinates[first_band]
            
            required_coords = ['ULLat', 'ULLon', 'URLat', 'URLon', 'LLLat', 'LLLon', 'LRLat', 'LRLon']
            if not all(coord in coords for coord in required_coords):
                raise ValueError("Missing coordinate information in IMD file")
            
            # Calculate center coordinates
            lats = [float(coords[f"{corner}Lat"]) for corner in ['UL', 'UR', 'LL', 'LR']]
            lons = [float(coords[f"{corner}Lon"]) for corner in ['UL', 'UR', 'LL', 'LR']]
            center_lat = sum(lats) / 4
            center_lon = sum(lons) / 4
            
            # Extract date
            date_str = metadata.get("earliestAcqTime", "").split('T')[0]
            if not date_str:
                raise ValueError("No acquisition time found")
            
            date = datetime.strptime(date_str, "%Y-%m-%d")
            return date, center_lat, center_lon
            
        except Exception as e:
            self.logger.error(f"Error extracting WorldView metadata: {e}")
            return None


class PlanetExtractor(MetadataExtractor):
    """Metadata extractor for Planet XML files."""
    
    def extract_metadata(self) -> Optional[Tuple[datetime, float, float]]:
        """Extract metadata from Planet XML file."""
        xml_files = [
            self._find_file_with_extension("metadata_clip.xml"),
            self._find_file_with_extension("metadata.xml")
        ]
        
        xml_file = next((f for f in xml_files if f), None)
        if not xml_file:
            return None
        
        self.logger.debug(f"Processing Planet XML file: {xml_file}")
        
        try:
            with open(xml_file, "r") as file:
                content = file.read()
            
            # Extract acquisition date
            date = None
            if "<ps:acquisitionDateTime>" in content:
                start = content.find("<ps:acquisitionDateTime>") + len("<ps:acquisitionDateTime>")
                end = content.find("</ps:acquisitionDateTime>")
                date_str = content[start:end]
                date = datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S%z").replace(tzinfo=None)
            
            # Extract coordinates
            latitude, longitude = None, None
            if "<gml:pos>" in content:
                start = content.find("<gml:pos>") + len("<gml:pos>")
                end = content.find("</gml:pos>")
                pos_text = content[start:end]
                longitude, latitude = map(float, pos_text.split())
            
            if all(v is not None for v in [date, latitude, longitude]):
                return date, latitude, longitude
            
            return None
            
        except Exception as e:
            self.logger.error(f"Error extracting Planet metadata: {e}")
            return None


class Sentinel2Extractor(MetadataExtractor):
    """Metadata extractor for Sentinel-2 XML files."""
    
    def extract_metadata(self) -> Optional[Tuple[datetime, float, float]]:
        """Extract metadata from Sentinel-2 XML file."""
        xml_file = self._find_file_with_extension("MTD_MSIL1C.xml")
        if not xml_file:
            return None
        
        self.logger.debug(f"Processing Sentinel-2 XML file: {xml_file}")
        
        try:
            tree = ET.parse(xml_file)
            root = tree.getroot()
            
            # Extract generation time
            generation_time_elem = root.find(".//GENERATION_TIME")
            if generation_time_elem is None:
                raise ValueError("No generation time found")
            
            date = datetime.strptime(generation_time_elem.text, "%Y-%m-%dT%H:%M:%S.%fZ")
            
            # Extract coordinates
            pos_list_elem = root.find(".//EXT_POS_LIST")
            if pos_list_elem is None:
                raise ValueError("No position list found")
            
            pos_list = pos_list_elem.text.strip().split()
            latitude = float(pos_list[0])
            longitude = float(pos_list[1])
            
            return date, latitude, longitude
            
        except Exception as e:
            self.logger.error(f"Error extracting Sentinel-2 metadata: {e}")
            return None


class MetadataExtractorFactory:
    """Factory class for creating appropriate metadata extractors."""
    
    @staticmethod
    def create_extractor(input_dir: str) -> Optional[MetadataExtractor]:
        """Create appropriate metadata extractor based on input directory."""
        # Check for WorldView IMD files
        if any(Path(input_dir).rglob("*.IMD")):
            return WorldViewExtractor(input_dir)
        
        # Check for Sentinel-2 SAFE format
        if input_dir.endswith('.SAFE') or '.SAFE' in input_dir:
            return Sentinel2Extractor(input_dir)
        
        # Check for Planet XML files
        if (any(Path(input_dir).rglob("metadata_clip.xml")) or 
            any(Path(input_dir).rglob("metadata.xml"))):
            return PlanetExtractor(input_dir)
        
        # No compatible format found
        return None
