"""
Enhanced Metadata Extraction Module

This module provides robust metadata extraction capabilities for multiple satellite formats
including WorldView, Planet, and Sentinel-2 data.
"""

import os
import logging
from datetime import datetime
from typing import Optional, Tuple
import xml.etree.ElementTree as ET


class MetadataExtractor:
    """Enhanced metadata extraction with robust error handling."""
    
    @staticmethod
    def extract_metadata(input_dir: str) -> Optional[Tuple[datetime, float, float]]:
        """
        Extract metadata from directory using multiple extraction strategies.
        
        Args:
            input_dir: Path to input directory containing satellite data
            
        Returns:
            Tuple of (date, latitude, longitude) or None if extraction fails
        """
        extractors = [
            MetadataExtractor._extract_worldview_metadata,
            MetadataExtractor._extract_planet_metadata,
            MetadataExtractor._extract_sentinel2_metadata
        ]
        
        logging.info(f"Extracting metadata from: {input_dir}")
        
        for extractor in extractors:
            try:
                result = extractor(input_dir)
                if result:
                    date, lat, lon = result
                    logging.info(f"Successfully extracted metadata: {date.strftime('%Y-%m-%d')}, "
                               f"Lat: {lat:.6f}, Lon: {lon:.6f}")
                    return result
            except Exception as e:
                logging.debug(f"Extractor {extractor.__name__} failed: {e}")
                continue
        
        logging.error(f"No compatible metadata found in {input_dir}")
        return None
    
    @staticmethod
    def _find_file_with_extension(directory: str, extension: str) -> Optional[str]:
        """Find file with specific extension in directory tree."""
        for root, dirs, files in os.walk(directory):
            for file in files:
                if file.endswith(extension):
                    return os.path.join(root, file)
        return None
    
    @staticmethod
    def _extract_worldview_metadata(input_dir: str) -> Optional[Tuple[datetime, float, float]]:
        """Extract metadata from WorldView IMD file."""
        imd_file = MetadataExtractor._find_file_with_extension(input_dir, ".IMD")
        if not imd_file:
            return None
        
        logging.debug(f"Processing WorldView IMD file: {imd_file}")
        
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
            raise ValueError("No band coordinate information found in IMD file")
        
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
            raise ValueError("No acquisition time found in IMD file")
        
        date = datetime.strptime(date_str, "%Y-%m-%d")
        return date, center_lat, center_lon
    
    @staticmethod
    def _extract_planet_metadata(input_dir: str) -> Optional[Tuple[datetime, float, float]]:
        """Extract metadata from Planet XML file."""
        xml_files = [
            MetadataExtractor._find_file_with_extension(input_dir, "metadata_clip.xml"),
            MetadataExtractor._find_file_with_extension(input_dir, "metadata.xml")
        ]
        
        xml_file = next((f for f in xml_files if f), None)
        if not xml_file:
            return None
        
        logging.debug(f"Processing Planet XML file: {xml_file}")
        
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
    
    @staticmethod
    def _extract_sentinel2_metadata(input_dir: str) -> Optional[Tuple[datetime, float, float]]:
        """Extract metadata from Sentinel-2 XML file."""
        xml_file = MetadataExtractor._find_file_with_extension(input_dir, "MTD_MSIL1C.xml")
        if not xml_file:
            return None
        
        logging.debug(f"Processing Sentinel-2 XML file: {xml_file}")
        
        tree = ET.parse(xml_file)
        root = tree.getroot()
        
        # Extract generation time
        generation_time_elem = root.find(".//GENERATION_TIME")
        if generation_time_elem is None:
            raise ValueError("No generation time found in Sentinel-2 metadata")
        
        date = datetime.strptime(generation_time_elem.text, "%Y-%m-%dT%H:%M:%S.%fZ")
        
        # Extract coordinates
        pos_list_elem = root.find(".//EXT_POS_LIST")
        if pos_list_elem is None:
            raise ValueError("No position list found in Sentinel-2 metadata")
        
        pos_list = pos_list_elem.text.strip().split()
        latitude = float(pos_list[0])
        longitude = float(pos_list[1])
        
        return date, latitude, longitude
