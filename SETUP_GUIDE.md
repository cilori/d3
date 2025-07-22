# Quick Setup Guide for Enhanced Acolite Processor

## 🚀 Quick Start

### 1. Setup Your Configuration File
Copy and modify the example settings:
```bash
cp example_settings.txt my_config.txt
```

Edit `my_config.txt` with your specific paths:
```ini
# Essential settings - UPDATE THESE
acolite_path=/your/path/to/acolite/launch_acolite.py
default_wind_speed=5.0
default_aot_value=0.1

# Acolite parameters
l2w_parameters=Rrs_*
atmospheric_correction=acolite
keep_individual_rrs=false
```

### 2. Install Dependencies
```bash
pip install numpy pandas requests netCDF4 gdal metpy
```

### 3. Test with Dry Run
```bash
python improved_acolite_processor.py \
    --output ./test_output \
    --inputs ./your_satellite_data \
    --settings my_config.txt \
    --dry-run
```

### 4. Run Actual Processing
```bash
python improved_acolite_processor.py \
    --output ./results \
    --inputs ./your_satellite_data \
    --settings my_config.txt \
    --verbose
```

## �� File Structure After Setup
```
your_project/
├── improved_acolite_processor.py    # Main script
├── metadata_extractor.py           # Metadata module
├── environmental_data.py           # Data retrieval module
├── my_config.txt                   # Your configuration
├── results/                        # Output directory
│   ├── acolite_processing.log      # Processing log
│   ├── processing_times.txt        # Timing info
│   └── scene_name_v0/              # Processed data
└── satellite_data/                 # Your input data
```

## 🔧 Key Improvements Over Original

### Better Error Handling
- ✅ Continues processing if one scene fails
- ✅ Falls back to default values for missing data
- ✅ Provides clear error messages

### Enhanced Monitoring
- ✅ Real-time progress updates
- ✅ Detailed processing logs
- ✅ Success/failure statistics

### Flexible Configuration
- ✅ External configuration file
- ✅ Easy parameter adjustment
- ✅ Default values for all settings

### Professional Quality
- ✅ Modular, maintainable code
- ✅ Comprehensive documentation
- ✅ Production-ready structure

## 🆘 Troubleshooting

### Common Issues

**1. "acolite_path not found"**
```bash
# Solution: Update path in config file
acolite_path=/correct/path/to/acolite/launch_acolite.py
```

**2. "No metadata found"**
- Check if input directory contains supported metadata files
- Supported formats: .IMD (WorldView), metadata.xml (Planet), MTD_MSIL1C.xml (Sentinel-2)

**3. "Network timeout"**
```bash
# Solution: Check internet connection or use offline mode
default_wind_speed=5.0  # Will use this if download fails
default_aot_value=0.1   # Will use this if download fails
```

### Debug Mode
Enable verbose logging for troubleshooting:
```bash
python improved_acolite_processor.py --verbose [other options]
```

## 📊 Monitoring Your Processing

### Check Progress
- Watch console output for real-time updates
- Monitor `acolite_processing.log` for detailed info
- Check `processing_times.txt` for timing statistics

### Success Indicators
- ✅ "SUCCESS" status in processing times log
- ✅ Output files created in results directory
- ✅ Final summary shows all scenes processed

## 🎯 Migration from Original Script

### What Changed
1. **Command Line**: New argument format (see examples above)
2. **Configuration**: External settings file required
3. **Output**: Better organized with versioned directories
4. **Logging**: Enhanced with timestamps and levels

### What Stayed the Same
- ✅ All core Acolite functionality preserved
- ✅ Same input data formats supported
- ✅ Same output data formats produced
- ✅ All environmental data sources maintained

## 📞 Support

If you encounter issues:
1. Check the detailed logs in `acolite_processing.log`
2. Run with `--verbose` flag for more information
3. Use `--dry-run` to test configuration without processing
4. Review this guide and the main documentation

The enhanced script provides the same functionality as your original with significant improvements in reliability, maintainability, and user experience!
