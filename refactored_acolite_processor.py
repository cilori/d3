#!/usr/bin/env python3
"""
Refactored Acolite Processing Pipeline

This refactored version provides:
- Clean separation of concerns
- Better error handling
- Improved maintainability
- Enhanced logging
- Configuration management
- Type hints and documentation

Usage:
    python refactored_acolite_processor.py --output <output_dir> --inputs <input_dirs> --settings <settings_file>
"""

import argparse
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from config_manager import ConfigManager
from logger_setup import setup_logging, log_processing_time
from metadata_extractor import MetadataExtractorFactory
from environmental_data import EnvironmentalDataManager
from sentinel_processor import SentinelProcessor
from acolite_runner import AcoliteRunner
from post_processor import PostProcessor


class AcoliteProcessingPipeline:
    """Main orchestrator for the Acolite processing pipeline."""
    
    def __init__(self, config: ConfigManager):
        self.config = config
        self.logger = logging.getLogger(__name__)
        self.env_data_manager = EnvironmentalDataManager()
        self.sentinel_processor = SentinelProcessor()
        self.acolite_runner = AcoliteRunner(config)
        self.post_processor = PostProcessor()
        self.coordinates_dict = {}
    
    def process_directories(self, input_directories: List[str]) -> Dict[str, bool]:
        """Process all input directories and return success status."""
        results = {}
        total_start_time = time.time()
        
        self.logger.info(f"Starting processing of {len(input_directories)} directories")
        
        for input_dir in input_directories:
            filename = os.path.basename(os.path.normpath(input_dir))
            self.logger.info(f"Processing: {filename}")
            
            start_time = time.time()
            success = self._process_single_directory(input_dir)
            
            # Log processing time
            duration = log_processing_time(
                self.config.output_dir, filename, start_time, success
            )
            
            results[input_dir] = success
            status = "✓ SUCCESS" if success else "✗ FAILED"
            self.logger.info(f"{status} - {filename} completed in {duration:.2f}s")
        
        # Log summary
        self._log_summary(total_start_time, results)
        return results
    
    def _process_single_directory(self, input_dir: str) -> bool:
        """Process a single input directory."""
        try:
            # Step 1: Extract metadata
            metadata = self._extract_metadata(input_dir)
            if not metadata:
                return False
            
            date, latitude, longitude = metadata
            self.coordinates_dict[input_dir] = (longitude, latitude)
            
            # Step 2: Preprocess if needed (Sentinel-2 resampling)
            if not self._preprocess_if_needed(input_dir):
                self.logger.warning(f"Preprocessing failed for {input_dir}")
                return False
            
            # Step 3: Get environmental data
            wind_speed, aot_value = self._get_environmental_data(date, latitude, longitude)
            
            # Step 4: Prepare parameters
            params = self._prepare_parameters(wind_speed, aot_value)
            
            # Step 5: Detect sensor type
            sensor_type = self._detect_sensor_type(input_dir)
            
            # Step 6: Run Acolite processing
            success = self.acolite_runner.run_processing(
                params, [input_dir], {input_dir: (longitude, latitude)}, sensor_type
            )
            
            if success:
                # Step 7: Post-processing
                self.post_processor.process_outputs(
                    self.config.output_dir, self.coordinates_dict, sensor_type
                )
            
            return success
            
        except Exception as e:
            self.logger.error(f"Error processing {input_dir}: {e}", exc_info=True)
            return False
    
    def _extract_metadata(self, input_dir: str) -> Optional[Tuple[datetime, float, float]]:
        """Extract metadata from input directory."""
        try:
            extractor = MetadataExtractorFactory.create_extractor(input_dir)
            if not extractor:
                self.logger.error(f"No metadata extractor found for {input_dir}")
                return None
            
            metadata = extractor.extract_metadata()
            if metadata:
                date, latitude, longitude = metadata
                self.logger.info(f"Extracted metadata: Date={date}, Lat={latitude:.6f}, Lon={longitude:.6f}")
                return metadata
            
            return None
            
        except Exception as e:
            self.logger.error(f"Metadata extraction failed for {input_dir}: {e}")
            return None
    
    def _preprocess_if_needed(self, input_dir: str) -> bool:
        """Preprocess data if needed (e.g., Sentinel-2 resampling)."""
        if self._is_sentinel2_data(input_dir):
            self.logger.info("Preprocessing Sentinel-2 data...")
            return self.sentinel_processor.resample_bands(input_dir)
        return True
    
    def _get_environmental_data(self, date: datetime, latitude: float, longitude: float) -> Tuple[float, float]:
        """Get wind speed and AOT data with fallbacks."""
        wind_speed = self.env_data_manager.get_wind_speed(
            date, latitude, longitude, self.config.output_dir
        ) or self.config.default_wind_speed
        
        aot_value = self.env_data_manager.get_aot_value(
            date, latitude, longitude, self.config.output_dir
        ) or self.config.default_aot_value
        
        self.logger.info(f"Environmental data: Wind={wind_speed:.2f} m/s, AOT={aot_value:.4f}")
        return wind_speed, aot_value
    
    def _prepare_parameters(self, wind_speed: float, aot_value: float) -> Dict[str, str]:
        """Prepare processing parameters."""
        params = self.config.base_params.copy()
        params.update({
            "wind": str(wind_speed),
            "aot": str(aot_value)
        })
        return params
    
    def _detect_sensor_type(self, input_dir: str) -> str:
        """Detect sensor type from input directory."""
        if self._is_sentinel2_data(input_dir):
            return 'sentinel2'
        elif any(Path(input_dir).rglob("*.IMD")):
            return 'worldview'
        elif any(Path(input_dir).rglob("metadata*.xml")):
            return 'planet'
        else:
            return 'unknown'
    
    def _is_sentinel2_data(self, input_dir: str) -> bool:
        """Check if input directory contains Sentinel-2 data."""
        return input_dir.endswith('.SAFE') or '.SAFE' in input_dir
    
    def _log_summary(self, start_time: float, results: Dict[str, bool]) -> None:
        """Log processing summary."""
        total_duration = time.time() - start_time
        successful = sum(results.values())
        total = len(results)
        
        hours, remainder = divmod(total_duration, 3600)
        minutes, seconds = divmod(remainder, 60)
        formatted_duration = f"{int(hours):02d}:{int(minutes):02d}:{seconds:.2f}"
        
        # Write summary file
        summary_file = os.path.join(self.config.output_dir, "processing_summary.txt")
        with open(summary_file, "w") as f:
            f.write(f"Processing Summary - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("=" * 80 + "\n")
            f.write(f"Total directories: {total}\n")
            f.write(f"Successful: {successful}\n")
            f.write(f"Failed: {total - successful}\n")
            f.write(f"Success rate: {(successful/total)*100:.1f}%\n")
            f.write(f"Total time: {formatted_duration}\n")
            f.write("=" * 80 + "\n")
        
        self.logger.info(f"Processing completed: {successful}/{total} successful in {formatted_duration}")


def parse_arguments() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Refactored Acolite Processing Pipeline with wind and AOT data integration",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Process specific directories
  python refactored_acolite_processor.py -o ./output -i dir1 dir2 -s settings.txt
  
  # Process all subdirectories
  python refactored_acolite_processor.py -o ./output -d ./data -s settings.txt
  
  # Enable verbose logging
  python refactored_acolite_processor.py -o ./output -i dir1 -s settings.txt -v
        """
    )
    
    parser.add_argument(
        "--output", "-o",
        type=str, required=True,
        help="Output directory for processed files"
    )
    
    parser.add_argument(
        "--inputs", "-i",
        type=str, nargs="+",
        help="Input directories to process"
    )
    
    parser.add_argument(
        "--input_dir", "-d",
        type=str,
        help="Parent directory containing input subdirectories"
    )
    
    parser.add_argument(
        "--settings", "-s",
        type=str, required=True,
        help="Settings file path"
    )
    
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose logging"
    )
    
    return parser.parse_args()


def validate_arguments(args: argparse.Namespace) -> None:
    """Validate command line arguments."""
    # Check settings file
    if not os.path.exists(args.settings):
        raise FileNotFoundError(f"Settings file not found: {args.settings}")
    
    # Check input specification
    if not args.inputs and not args.input_dir:
        raise ValueError("Must specify either --inputs or --input_dir")
    
    # Check input_dir if specified
    if args.input_dir and not os.path.exists(args.input_dir):
        raise FileNotFoundError(f"Input directory not found: {args.input_dir}")


def gather_input_directories(args: argparse.Namespace) -> List[str]:
    """Gather and validate input directories."""
    input_dirs = []
    
    # Add explicit inputs
    if args.inputs:
        input_dirs.extend(args.inputs)
    
    # Add subdirectories from input_dir
    if args.input_dir:
        subdirs = [
            os.path.join(args.input_dir, d)
            for d in os.listdir(args.input_dir)
            if os.path.isdir(os.path.join(args.input_dir, d))
        ]
        input_dirs.extend(subdirs)
    
    # Validate directories exist
    missing = [d for d in input_dirs if not os.path.exists(d)]
    if missing:
        raise FileNotFoundError(f"Input directories not found: {missing}")
    
    return input_dirs


def main():
    """Main entry point."""
    try:
        # Parse and validate arguments
        args = parse_arguments()
        validate_arguments(args)
        
        # Create output directory
        os.makedirs(args.output, exist_ok=True)
        
        # Setup logging
        log_level = logging.DEBUG if args.verbose else logging.INFO
        setup_logging(log_level, args.output)
        logger = logging.getLogger(__name__)
        
        logger.info("=" * 80)
        logger.info("Refactored Acolite Processing Pipeline Starting")
        logger.info("=" * 80)
        
        # Gather input directories
        input_directories = gather_input_directories(args)
        logger.info(f"Found {len(input_directories)} input directories")
        
        # Load configuration
        config = ConfigManager(args.settings, args.output)
        logger.info("Configuration loaded successfully")
        
        # Create and run pipeline
        pipeline = AcoliteProcessingPipeline(config)
        results = pipeline.process_directories(input_directories)
        
        # Final results
        successful = sum(results.values())
        total = len(results)
        
        logger.info("=" * 80)
        logger.info(f"FINAL RESULTS: {successful}/{total} directories processed successfully")
        logger.info("=" * 80)
        
        # Exit with appropriate code
        sys.exit(0 if successful == total else 1)
        
    except KeyboardInterrupt:
        logging.error("Processing interrupted by user")
        sys.exit(130)
    except Exception as e:
        logging.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()