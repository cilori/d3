"""
Logging Setup Module

Provides enhanced logging configuration and utilities.
"""

import logging
import os
import sys
import time
from datetime import datetime
from typing import Optional


def setup_logging(level: int = logging.INFO, output_dir: Optional[str] = None) -> None:
    """Setup comprehensive logging configuration."""
    # Create formatters
    console_format = '%(asctime)s - %(levelname)s - %(message)s'
    file_format = '%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s'
    
    # Setup root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    
    # Clear existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
    
    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(logging.Formatter(
        console_format, datefmt='%Y-%m-%d %H:%M:%S'
    ))
    root_logger.addHandler(console_handler)
    
    # File handler
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
        log_file = os.path.join(output_dir, "acolite_processing.log")
        file_handler = logging.FileHandler(log_file, mode='a')
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(
            file_format, datefmt='%Y-%m-%d %H:%M:%S'
        ))
        root_logger.addHandler(file_handler)


def log_processing_time(output_dir: str, filename: str, start_time: float, 
                       success: bool = True) -> float:
    """Log processing time for individual files."""
    end_time = time.time()
    duration = end_time - start_time
    
    # Format duration
    hours, remainder = divmod(duration, 3600)
    minutes, seconds = divmod(remainder, 60)
    formatted_duration = f"{int(hours):02d}:{int(minutes):02d}:{seconds:.2f}"
    
    # Create or append to log file
    log_file = os.path.join(output_dir, "processing_times.txt")
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    status = "SUCCESS" if success else "FAILED"
    
    # Create header if file doesn't exist
    if not os.path.exists(log_file):
        with open(log_file, "w") as f:
            f.write(f"Processing Time Log - Created {timestamp}\n")
            f.write("=" * 100 + "\n")
            f.write(f"{'Filename':<50} {'Duration':<15} {'Status':<10} {'Timestamp':<20}\n")
            f.write("-" * 100 + "\n")
    
    # Append entry
    with open(log_file, "a") as f:
        f.write(f"{filename:<50} {formatted_duration:<15} {status:<10} {timestamp:<20}\n")
    
    return duration