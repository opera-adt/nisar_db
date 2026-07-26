"""Convert and download :class:`NISARProduct` collections."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Union

import pandas as pd
from tqdm.auto import tqdm

from ..download import download_earthdata_granule
from .models import NISARProduct

__all__ = ["download_products", "products_to_dataframe"]

logger = logging.getLogger(__name__)


def download_products(
    products: List[NISARProduct],
    output_dir: Union[str, Path],
    skip_existing: bool = True,
    max_workers: int = 4,
) -> List[str]:
    """Download NISAR products.

    Parameters
    ----------
    products : List[NISARProduct]
        List of NISAR products to download.
    output_dir : Union[str, Path]
        Output directory.
    skip_existing : bool
        Skip files that already exist.
    max_workers : int
        Maximum number of concurrent downloads.

    Returns
    -------
    List[str]
        List of downloaded file paths.

    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    downloaded_files = []

    with tqdm(total=len(products), desc="Downloading") as pbar:
        # Use ThreadPoolExecutor for concurrent downloads
        if max_workers > 1 and len(products) > 1:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_product = {
                    executor.submit(
                        download_earthdata_granule,
                        product.granule_id,
                        output_dir=output_dir,
                        skip_existing=skip_existing,
                    ): product
                    for product in products
                }

                for future in tqdm(future_to_product, desc="Downloading", leave=False):
                    product = future_to_product[future]
                    try:
                        files = future.result()
                        downloaded_files.extend(files)
                    except Exception:
                        logger.exception(f"Error downloading {product.name}")
                    pbar.update(1)
        else:
            # Sequential download
            for product in products:
                try:
                    files = download_earthdata_granule(
                        product.granule_id,
                        output_dir=output_dir,
                        skip_existing=skip_existing,
                    )
                    downloaded_files.extend(files)
                except Exception:
                    logger.exception(f"Error downloading {product.name}")
                pbar.update(1)

    return downloaded_files


def products_to_dataframe(products: List[NISARProduct]) -> pd.DataFrame:
    """Convert a list of NISAR products to a pandas DataFrame.

    Parameters
    ----------
    products : List[NISARProduct]
        List of NISAR products.

    Returns
    -------
    pd.DataFrame
        DataFrame with product information.

    """
    data = []
    for product in products:
        data.append(
            {
                "granule_id": product.granule_id,
                "name": product.name,
                "product_type": product.product_type.value,
                "filename": product.filename,
                "url": product.url,
                "start_datetime": product.start_datetime,
                "end_datetime": product.end_datetime,
                "track": product.track,
                "frame": product.frame,
                "direction": product.direction,
                "cycle": product.cycle,
                "polarization": product.polarization,
                "crid": product.crid,
                "full_frame": product.full_frame,
                "joint_observation": product.joint_observation,
                "track_frame_id": product.track_frame_id,
                "date": product.date,
            }
        )

    return pd.DataFrame(data)
