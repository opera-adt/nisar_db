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

# Snow-analysis GeoJSON produced by scripts/snow-analysis/derive_blackout_windows.py
SNOW_GEOJSON ?= nisar-snow-analysis.geojson

# Previous release's consistent-GSLC JSON; when present, label-processing-mode
# reports which frames changed their winning (mode, coverage).
PREVIOUS_CONSISTENT ?=

# Define output filenames with dates
FRAME_TO_BOUND := opera-nisar-disp-frame-to-bounds-$(DATE).json
FRAME_GEOMETRIES := opera-nisar-disp-frame-geometries-simple-$(VERSION).geojson
BLACKOUT_FILE := opera-nisar-disp-blackout-dates-$(DATE).json
CONSISTENT_GSLC := opera-nisar-disp-consistent-gslc-$(DATE).json
CONSISTENT_NO_BLACKOUT := opera-nisar-disp-consistent-gslc-no-blackout.json
CONSISTENT_WITH_MODE := opera-nisar-disp-consistent-gslc-with-processing-mode-$(DATE).json
REFERENCE_DATES := opera-nisar-disp-reference-dates-$(DATE).json
# Side output of create-frame-to-bound: filtered NISAR frame polygons,
# consumed by create-consistent.
FRAMES_GPKG := opera-nisar-disp-frames.gpkg

# Main target
all: $(FRAME_TO_BOUND) $(CONSISTENT_GSLC) $(CONSISTENT_WITH_MODE) $(REFERENCE_DATES)
	@echo "================================================"
	@echo "Build complete for version $(VERSION)"
	@echo "================================================"

# Create Frame-to-Bound JSON (also writes $(FRAMES_GPKG) and the simplified GeoJSON)
$(FRAME_TO_BOUND): $(NISAR_GPKG)
	nisar-db create-frame-to-bound --nisar-gpkg $(NISAR_GPKG) --output $@ \
		--geojson $(FRAME_GEOMETRIES)

# The filtered frames GPKG and the GeoJSON are side effects of frame-to-bound
$(FRAMES_GPKG) $(FRAME_GEOMETRIES): $(FRAME_TO_BOUND)

# Turn the snow-analysis windows into per-frame blackout periods
$(BLACKOUT_FILE): $(SNOW_GEOJSON)
	nisar-db create-blackout-dates --input-file $(SNOW_GEOJSON) --output-file $@

# Create Consistent GSLC catalog (needs the catalog CSV and the frames GPKG).
# Built twice: the operational product filters out blacked-out acquisitions,
# the no-blackout variant exists to measure what that filtering costs.
$(CONSISTENT_GSLC): $(GSLC_CATALOG) $(FRAMES_GPKG) $(BLACKOUT_FILE)
	nisar-db create-consistent --catalog $(GSLC_CATALOG) --nisar-gpkg $(FRAMES_GPKG) \
		--output $(CONSISTENT_NO_BLACKOUT)
	nisar-db create-consistent --catalog $(GSLC_CATALOG) --nisar-gpkg $(FRAMES_GPKG) \
		--blackout-file $(BLACKOUT_FILE) --output $@

$(CONSISTENT_NO_BLACKOUT): $(CONSISTENT_GSLC)

# Label each sensing time historical / forward / no_run
$(CONSISTENT_WITH_MODE): $(CONSISTENT_GSLC)
	nisar-db label-processing-mode --consistent-json $(CONSISTENT_GSLC) --output $@ \
		$(if $(PREVIOUS_CONSISTENT),--previous-json $(PREVIOUS_CONSISTENT),)

# Reference (reset) dates, anchored on each frame's snow-free month
$(REFERENCE_DATES): $(CONSISTENT_GSLC) $(BLACKOUT_FILE)
	nisar-db create-reference-dates --consistent-json $(CONSISTENT_GSLC) \
		--blackout-file $(BLACKOUT_FILE) --output $@

# Create GSLC catalog from file list
gslc_catalog.csv: gslc_files.txt $(NISAR_GPKG)
	nisar-db create-catalog --input gslc_files.txt --output $@ --nisar-gpkg $(NISAR_GPKG) --na-only

# Clean up intermediate files
clean:
	rm -f $(FRAME_TO_BOUND) $(CONSISTENT_GSLC) $(CONSISTENT_NO_BLACKOUT) \
		$(CONSISTENT_WITH_MODE) $(REFERENCE_DATES) $(BLACKOUT_FILE) \
		$(FRAME_GEOMETRIES) *.json.zip *.geojson.zip

# Clean all generated files
cleanall: clean
	rm -f gslc_catalog*.csv *_frame_summary.csv $(FRAMES_GPKG)

# --- Packaging -------------------------------------------------------------
# Anaconda.org channel/user the conda package is uploaded to
CONDA_CHANNEL ?= opera-adt
CONDA_LABEL ?= main

# Build the sdist + wheel into dist/
dist:
	python -m pip install --upgrade build twine
	rm -rf dist
	python -m build
	twine check dist/*

# Upload to TestPyPI first; needs ~/.pypirc or TWINE_USERNAME/TWINE_PASSWORD
publish-testpypi: dist
	twine upload --repository testpypi dist/*

publish-pypi: dist
	twine upload dist/*

# Build the noarch conda package into build-conda/
conda-build:
	NISAR_DB_VERSION=$(VERSION) conda build conda \
		--no-anaconda-upload --output-folder build-conda -c conda-forge

# Upload to anaconda.org; needs ANACONDA_API_TOKEN or `anaconda login`
publish-conda: conda-build
	anaconda upload --user $(CONDA_CHANNEL) --label $(CONDA_LABEL) --skip-existing \
		$$(find build-conda -name '*.conda' -o -name '*.tar.bz2')

# Show current version
show-version:
	@echo "Current version: $(VERSION)"

# Show help
help:
	@echo "NISAR_DB Makefile"
	@echo ""
	@echo "Usage:"
	@echo "  make all                   Build all release assets"
	@echo "  make gslc_catalog.csv      Create GSLC catalog from file list"
	@echo "  make clean                 Remove output files"
	@echo "  make cleanall              Remove all generated files"
	@echo "  make show-version          Show current version"
	@echo ""
	@echo "Release assets built by 'make all':"
	@echo "  opera-nisar-disp-frame-to-bounds-{date}.json[.zip]"
	@echo "  opera-nisar-disp-frame-geometries-simple-{version}.geojson.zip"
	@echo "  opera-nisar-disp-blackout-dates-{date}.json[.zip]"
	@echo "  opera-nisar-disp-consistent-gslc-{date}.json[.zip]"
	@echo "  opera-nisar-disp-consistent-gslc-no-blackout.json[.zip]"
	@echo "  opera-nisar-disp-consistent-gslc-with-processing-mode-{date}.json[.zip]"
	@echo "  opera-nisar-disp-reference-dates-{date}.json[.zip]"
	@echo ""
	@echo "Packaging:"
	@echo "  make dist                  Build sdist + wheel into dist/"
	@echo "  make publish-testpypi      Upload dist/ to TestPyPI"
	@echo "  make publish-pypi          Upload dist/ to PyPI"
	@echo "  make conda-build           Build the noarch conda package"
	@echo "  make publish-conda         Upload the conda package to anaconda.org"
	@echo ""
	@echo "Parameters:"
	@echo "  VERSION=x.y.z              Override version number"
	@echo "  NISAR_GPKG=file.gpkg       Specify NISAR TrackFrame GeoPackage"
	@echo "  GSLC_CATALOG=file.csv      Specify GSLC catalog file"
	@echo "  SNOW_GEOJSON=file.geojson  Snow-analysis windows for the blackout dates"
	@echo "  PREVIOUS_CONSISTENT=f.json Previous release, to diff changed frames"
	@echo "  CONDA_CHANNEL=name         anaconda.org user/channel (default: opera-adt)"
	@echo "  CONDA_LABEL=name           anaconda.org label (default: main)"

.PHONY: all clean cleanall show-version help \
	dist publish-testpypi publish-pypi conda-build publish-conda
