"""NISAR Frame database generation for OPERA DISP-NISAR."""

try:
    from ._version import __version__
except ImportError:
    __version__ = "0.1.0.dev0"

from .s3_catalog import (
    build_s3_catalog,
    catalog_to_gdf,
    products_to_catalog_df,
    query_catalog,
)
from .search_nisar import (
    NISARProduct,
    ProductType,
    UrlType,
    download_products,
    parse_s3_uri,
    products_to_dataframe,
    search_nisar_products,
    search_s3_products,
)
