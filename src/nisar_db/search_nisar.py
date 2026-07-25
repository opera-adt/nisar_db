"""Back-compat facade for the :mod:`nisar_db.search` subpackage.

Search for NISAR GSLC and GUNW products from CMR (or an S3 bucket). The
implementation now lives in :mod:`nisar_db.search`; this module re-exports the
public API so existing ``from nisar_db.search_nisar import ...`` imports keep
working.

Examples
--------
Command line:
$ nisar-db search --track 76 --frame 22 --direction A --product-type GSLC

Python:
>>> from nisar_db.search_nisar import search_nisar_products
>>> products = search_nisar_products(
...     bbox=(40.62, 13.56, 40.72, 13.64),
...     product_type="GSLC"
... )

"""

from __future__ import annotations

from .search.cli import main
from .search.cmr import (
    fetch_cmr_pages,
    search_nisar_granules,
    search_nisar_products,
)
from .search.frames import download_products, products_to_dataframe
from .search.models import NISARProduct, ProductType, UrlType
from .search.s3 import parse_s3_uri, search_s3_products

__all__ = [
    "NISARProduct",
    "ProductType",
    "UrlType",
    "download_products",
    "fetch_cmr_pages",
    "main",
    "parse_s3_uri",
    "products_to_dataframe",
    "search_nisar_granules",
    "search_nisar_products",
    "search_s3_products",
]


if __name__ == "__main__":
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )
    main()
