# Configuration
# VERSION can be passed in: make VERSION=0.2.0
# Or defaults to reading from git tag
VERSION ?= $(shell git describe --tags --abbrev=0 | sed 's/^v//')
ifeq ($(VERSION),)
    $(error No VERSION specified and no git tags found. Use: make VERSION=0.2.0)
endif

DATE := $(shell date +%Y-%m-%d)
# Verbosely echo commands
SHELL = sh -xv

# Find the latest NISAR TrackFrame GeoPackage
NISAR_GPKG ?= $(shell ls -t NISAR_TrackFrame_L_*.gpkg | head -n1)
ifeq ($(NISAR_GPKG),)
    $(error No NISAR_TrackFrame_L_*.gpkg file found. Please provide one: make NISAR_GPKG=path/to/NISAR_TrackFrame_L_YYYYMMDD.gpkg)
endif

# Find the latest GSLC catalog file
GSLC_CATALOG ?= $(shell ls -t gslc_catalog*.csv | head -n1)
ifeq ($(GSLC_CATALOG),)
    $(warning No gslc_catalog*.csv file found. Some targets may fail.)
endif

# Define output filenames with dates
FRAME_TO_BOUND := opera-nisar-disp-frame-to-bounds-$(DATE).json
CONSISTENT_GSLC := opera-nisar-disp-consistent-gslc-$(DATE).json

# Main target
all: $(FRAME_TO_BOUND) $(CONSISTENT_GSLC)
	@echo "================================================"
	@echo "Build complete for version $(VERSION)"
	@echo "================================================"

# Create Frame-to-Bound JSON
$(FRAME_TO_BOUND): $(NISAR_GPKG)
	python create_frame_to_bound.py --nisar-gpkg $(NISAR_GPKG) --output $@

# Create Consistent GSLC catalog
$(CONSISTENT_GSLC): $(GSLC_CATALOG)
	python create_consistent_gslc_catalog.py --input $(GSLC_CATALOG) --output $@

# Create GSLC catalog from file list
gslc_catalog.csv: gslc_files.txt $(NISAR_GPKG)
	python create_gslc_catalog.py --input gslc_files.txt --output $@ --nisar-gpkg $(NISAR_GPKG) --na-only

# Clean up intermediate files
clean:
	rm -f $(FRAME_TO_BOUND) $(CONSISTENT_GSLC)

# Clean all generated files
cleanall: clean
	rm -f gslc_catalog*.csv *_frame_summary.csv

# Show current version
show-version:
	@echo "Current version: $(VERSION)"

# Show help
help:
	@echo "NISAR_DB Makefile"
	@echo ""
	@echo "Usage:"
	@echo "  make all                   Build all outputs"
	@echo "  make FRAME_TO_BOUND        Build frame-to-bound JSON"
	@echo "  make CONSISTENT_GSLC       Build consistent GSLC catalog"
	@echo "  make gslc_catalog.csv      Create GSLC catalog from file list"
	@echo "  make clean                 Remove output files"
	@echo "  make cleanall              Remove all generated files"
	@echo "  make show-version          Show current version"
	@echo ""
	@echo "Parameters:"
	@echo "  VERSION=x.y.z              Override version number"
	@echo "  NISAR_GPKG=file.gpkg       Specify NISAR TrackFrame GeoPackage"
	@echo "  GSLC_CATALOG=file.csv      Specify GSLC catalog file"

.PHONY: all clean cleanall show-version help