"""
NISAR catalog module for generating and maintaining a database of NISAR products.

This module provides tools to:
1. Search for NISAR products in CMR
2. Extract metadata from NISAR products
3. Store metadata in a DuckDB database
4. Generate JSON catalog files for use by applications
"""

from pathlib import Path

# Define catalog directory
CATALOG_DIR = Path(__file__).parent / "catalog"
CATALOG_DIR.mkdir(exist_ok=True)

# Define database paths
GSLC_DB_PATH = Path(__file__).parent / "gslc_catalog.duckdb"
GUNW_DB_PATH = Path(__file__).parent / "gunw_catalog.duckdb"