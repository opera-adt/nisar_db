"""NISAR product search: CMR and S3 backends, product model, and helpers."""

from __future__ import annotations

from .cmr import fetch_cmr_pages, search_nisar_granules, search_nisar_products
from .frames import download_products, products_to_dataframe
from .models import NISARProduct, ProductType, UrlType
from .s3 import parse_s3_uri, search_s3_products

__all__ = [
    "NISARProduct",
    "ProductType",
    "UrlType",
    "download_products",
    "fetch_cmr_pages",
    "parse_s3_uri",
    "products_to_dataframe",
    "search_nisar_granules",
    "search_nisar_products",
    "search_s3_products",
]
