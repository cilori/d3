"""
Configuration Management Module

Handles loading and validation of configuration settings from file.
"""

import os
import logging
from typing import Dict, Any


class ConfigManager:
    """Manages configuration loading and validation."""
    
    def __init__(self, settings_file: str, output_dir: str):
        self.settings_file = settings_file
        self.output_dir = output_dir
        self.base_params: Dict[str, str] = {}
        self.acolite_script_path = ""
        self.default_wind_speed = 5.0
        self.default_aot_value = 0.1
        self.keep_individual_rrs = True
        self._load_settings()
    
    def _load_settings(self) -> None:
        """Load and validate settings from file."""
        if not os.path.exists(self.settings_file):
            raise FileNotFoundError(f"Settings file not found: {self.settings_file}")
        
        try:
            with open(self.settings_file, "r") as file:
                for line_num, line in enumerate(file, 1):
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        try:
                            key, value = line.split("=", 1)
                            self.base_params[key.strip()] = value.strip()
                        except ValueError:
                            logging.warning(f"Invalid line {line_num} in settings: {line}")
            
            # Extract special configuration values
            self.acolite_script_path = self.base_params.pop("acolite_script_path", "")
            self.default_wind_speed = float(self.base_params.pop("default_wind_speed", "5.0"))
            self.default_aot_value = float(self.base_params.pop("default_aot_value", "0.1"))
            self.keep_individual_rrs = self.base_params.pop("keep_individual_rrs", "True").lower() == "true"
            
            if not self.acolite_script_path:
                raise ValueError("acolite_script_path must be specified in settings file")
                
        except Exception as e:
            raise RuntimeError(f"Failed to load settings from {self.settings_file}: {e}")
    
    def get_parameter(self, key: str, default: Any = None) -> Any:
        """Get a configuration parameter with optional default."""
        return self.base_params.get(key, default)
    
    def validate_acolite_path(self) -> bool:
        """Validate that the Acolite script path exists and is executable."""
        if not os.path.exists(self.acolite_script_path):
            logging.error(f"Acolite script not found: {self.acolite_script_path}")
            return False
        
        if not os.access(self.acolite_script_path, os.R_OK):
            logging.error(f"Acolite script not readable: {self.acolite_script_path}")
            return False
        
        return True