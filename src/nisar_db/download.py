"""Download NISAR granules and files from NASA EarthData, HTTPS, or S3.

Provides granule-level (CMR concept id), direct-URL, and ``s3://`` downloads
with optional streamed progress reporting. Authentication for HTTPS transfers
uses the caller's ``.netrc`` credentials via :mod:`requests`.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

_CMR_GRANULES_URL = "https://cmr.earthdata.nasa.gov/search/granules.json"
_DATA_REL = "http://esipfed.org/ns/fedsearch/1.1/data#"
_CHUNK_SIZE = 1024 * 1024  # 1 MB
_MB = 1024 * 1024


def _filename_from_url(url: str) -> str:
    """Return the file name of ``url``, ignoring any query string."""
    return Path(url.split("?", maxsplit=1)[0]).name


def _stream_to_file(
    response: requests.Response,
    filepath: Path,
    show_progress: bool,
) -> float:
    """Stream ``response`` body to ``filepath`` and return the size in MB.

    Parameters
    ----------
    response : requests.Response
        A streamed (``stream=True``) response whose status has been validated.
    filepath : Path
        Destination file.
    show_progress : bool
        If True and stdout is a TTY, render a single-line progress bar.

    """
    total_size = int(response.headers.get("content-length", 0))
    downloaded = 0
    to_tty = show_progress and sys.stdout.isatty()

    with filepath.open("wb") as f:
        for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
            if not chunk:
                continue
            f.write(chunk)
            downloaded += len(chunk)
            if to_tty and total_size:
                percent = downloaded / total_size * 100
                sys.stdout.write(
                    f"\r  Progress: {percent:.1f}% "
                    f"({downloaded / _MB:.1f}/{total_size / _MB:.1f} MB)"
                )
                sys.stdout.flush()

    if to_tty:
        sys.stdout.write("\n")
    return downloaded / _MB


def _log_http_error(error: requests.exceptions.HTTPError, filename: str) -> None:
    """Log an HTTP error with a hint tailored to the status code."""
    logger.exception("HTTP error downloading %s", filename)
    status = error.response.status_code
    if status == 401:
        logger.error("Authentication failed. Check your .netrc file.")
    elif status == 403:
        logger.error("Access forbidden. You may lack permission for this file.")
    elif status >= 500:
        logger.error("Server error. The server may be experiencing issues.")


def download_earthdata_granule(
    granule_id: str,
    output_dir: str | Path = ".",
    skip_existing: bool = True,
    show_progress: bool = True,
    timeout: int = 60,
) -> list[str]:
    """Download a NASA EarthData granule using ``.netrc`` credentials.

    Parameters
    ----------
    granule_id : str
        The CMR concept id of the granule to download.
    output_dir : str or Path
        Directory where downloaded files will be saved.
    skip_existing : bool
        If True, skip downloading files that already exist in ``output_dir``.
    show_progress : bool
        If True, display download progress information.
    timeout : int
        Timeout in seconds for the HTTP requests.

    Returns
    -------
    list[str]
        Paths to downloaded files.

    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"Fetching details for granule: {granule_id}")
    params: dict[str, str | int] = {"concept_id": granule_id, "page_size": 1}
    try:
        response = requests.get(_CMR_GRANULES_URL, params=params, timeout=timeout)
        response.raise_for_status()
        granule_data = response.json()
    except requests.exceptions.RequestException:
        logger.exception("Error fetching granule metadata")
        return []

    if not granule_data["feed"]["entry"]:
        logger.warning(f"No granule found for ID: {granule_id}")
        return []

    entry = granule_data["feed"]["entry"][0]
    logger.info(f"Found granule: {entry.get('title', 'Unknown')}")

    download_urls = [
        link["href"] for link in entry.get("links", []) if link.get("rel") == _DATA_REL
    ]
    if not download_urls:
        logger.warning(f"No download URLs found for granule: {granule_id}")
        return []

    session = requests.Session()
    downloaded_files: list[str] = []
    for i, url in enumerate(download_urls, 1):
        filename = _filename_from_url(url)
        filepath = output_dir / filename

        if skip_existing and filepath.exists():
            logger.info(f"Skipping existing file: {filename}")
            downloaded_files.append(str(filepath))
            continue

        logger.info(f"Downloading [{i}/{len(download_urls)}]: {filename}")
        try:
            response = session.get(url, stream=True, timeout=timeout)
            response.raise_for_status()
            size_mb = _stream_to_file(response, filepath, show_progress)
        except requests.exceptions.HTTPError as e:
            _log_http_error(e, filename)
            continue
        except requests.exceptions.ConnectionError:
            logger.exception(f"Connection error downloading {filename}")
            continue
        except requests.exceptions.Timeout:
            logger.exception(f"Timeout downloading {filename}")
            continue

        logger.info(f"Successfully downloaded: {filename} ({size_mb:.1f} MB)")
        downloaded_files.append(str(filepath))

    if downloaded_files:
        logger.info(f"Downloaded {len(downloaded_files)} file(s) to {output_dir}")
    else:
        logger.warning("No files were downloaded")

    return downloaded_files


def download_from_url(
    url: str,
    output_dir: str | Path = ".",
    filename: str | None = None,
    skip_existing: bool = True,
    show_progress: bool = True,
    timeout: int = 60,
) -> str | None:
    """Download a file directly from a URL using ``.netrc`` credentials.

    Parameters
    ----------
    url : str
        URL of the file to download.
    output_dir : str or Path
        Directory where the downloaded file will be saved.
    filename : str, optional
        Name to save the file as. If None, uses the basename from the URL.
    skip_existing : bool
        If True, skip downloading the file if it already exists.
    show_progress : bool
        If True, display download progress information.
    timeout : int
        Timeout in seconds for the HTTP requests.

    Returns
    -------
    str or None
        Path to the downloaded file, or None if the download failed.

    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    filename = filename or _filename_from_url(url)
    filepath = output_dir / filename

    if skip_existing and filepath.exists():
        logger.info(f"Skipping existing file: {filename}")
        return str(filepath)

    logger.info(f"Downloading: {filename}")
    session = requests.Session()
    try:
        response = session.get(url, stream=True, timeout=timeout)
        response.raise_for_status()
        size_mb = _stream_to_file(response, filepath, show_progress)
    except requests.exceptions.HTTPError as e:
        _log_http_error(e, filename)
        return None
    except requests.exceptions.ConnectionError:
        logger.exception(f"Connection error downloading {filename}")
        return None
    except requests.exceptions.Timeout:
        logger.exception(f"Timeout downloading {filename}")
        return None

    logger.info(f"Successfully downloaded: {filename} ({size_mb:.1f} MB)")
    return str(filepath)


def download_s3_url(
    s3_url: str,
    output_dir: str | Path = ".",
    filename: str | None = None,
    region: str = "us-west-2",
    profile: str | None = None,
) -> str | None:
    """Download a file from an ``s3://`` URL using the AWS CLI.

    Requires the AWS CLI to be installed and configured.

    Parameters
    ----------
    s3_url : str
        S3 URL of the file to download (``s3://bucket/key``).
    output_dir : str or Path
        Directory where the downloaded file will be saved.
    filename : str, optional
        Name to save the file as. If None, uses the basename from the URL.
    region : str
        AWS region for the S3 bucket.
    profile : str, optional
        AWS profile to use for credentials.

    Returns
    -------
    str or None
        Path to the downloaded file, or None if the download failed.

    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    filename = filename or Path(s3_url).name
    filepath = output_dir / filename

    cmd = ["aws", "s3", "cp", s3_url, str(filepath), "--region", region]
    if profile:
        cmd.extend(["--profile", profile])

    logger.info(f"Downloading from S3: {s3_url} to {filepath}")
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        logger.exception(f"Error downloading from S3; AWS CLI output: {e.stderr}")
        return None

    logger.info(f"Successfully downloaded: {filename}")
    return str(filepath)
