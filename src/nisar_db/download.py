import os
import requests
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

def download_earthdata_granule(granule_id: str, output_dir=".", skip_existing=True) -> list[str]:
    """Download NASA EarthData granule using .netrc credentials."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    cmr_url = "https://cmr.earthdata.nasa.gov/search/granules.json"
    params = {"concept_id": granule_id, "page_size": 1}
    response = requests.get(cmr_url, params=params, timeout=30)
    response.raise_for_status()
    granule_data = response.json()

    if not granule_data["feed"]["entry"]:
        logger.warning("No granule found")
        return []

    entry = granule_data["feed"]["entry"][0]
    download_urls = [
        link["href"]
        for link in entry.get("links", [])
        if link.get("rel") == "http://esipfed.org/ns/fedsearch/1.1/data#"
    ]
    if not download_urls:
        logger.warning("No download URLs found")
        return []

    session = requests.Session()
    downloaded_files = []

    for i, url in enumerate(download_urls, 1):
        filename = os.path.basename(url.split("?")[0])
        filepath = output_dir / filename
        if skip_existing and filepath.exists():
            downloaded_files.append(str(filepath))
            continue
        with session.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(filepath, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024*1024):
                    f.write(chunk)
        downloaded_files.append(str(filepath))

    logger.info(f"Downloaded {len(downloaded_files)} files")
    return downloaded_files