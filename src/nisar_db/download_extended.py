"""
Extended download functionality for NASA EarthData granules.

This module provides additional download capabilities for NISAR data,
incorporating progress reporting and comprehensive error handling.
"""

import os
import sys
import requests
import logging
from pathlib import Path
from typing import List, Optional, Union

logger = logging.getLogger(__name__)

def download_earthdata_granule(
    granule_id: str,
    output_dir: Union[str, Path] = ".",
    skip_existing: bool = True,
    show_progress: bool = True,
    timeout: int = 60
) -> List[str]:
    """
    Download NASA EarthData granule using .netrc credentials with progress reporting.

    Parameters
    ----------
    granule_id : str
        The CMR concept ID of the granule to download.
    output_dir : str or Path
        Directory where downloaded files will be saved.
    skip_existing : bool
        If True, skip downloading files that already exist in output_dir.
    show_progress : bool
        If True, display download progress information.
    timeout : int
        Timeout in seconds for the HTTP requests.

    Returns
    -------
    list[str]
        List of paths to downloaded files.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Query CMR API for granule details
    logger.info(f"Fetching details for granule: {granule_id}")
    cmr_url = "https://cmr.earthdata.nasa.gov/search/granules.json"
    params = {"concept_id": granule_id, "page_size": 1}

    try:
        response = requests.get(cmr_url, params=params, timeout=timeout)
        response.raise_for_status()
        granule_data = response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Error fetching granule metadata: {e}")
        return []

    # Check if granule exists
    if not granule_data["feed"]["entry"]:
        logger.warning(f"No granule found for ID: {granule_id}")
        return []

    # Extract download URLs
    entry = granule_data["feed"]["entry"][0]
    title = entry.get("title", "Unknown")
    logger.info(f"Found granule: {title}")

    download_urls = [
        link["href"]
        for link in entry.get("links", [])
        if link.get("rel") == "http://esipfed.org/ns/fedsearch/1.1/data#"
    ]

    if not download_urls:
        logger.warning(f"No download URLs found for granule: {granule_id}")
        return []

    # Create session for authentication
    session = requests.Session()
    downloaded_files = []

    # Download each file
    for i, url in enumerate(download_urls, 1):
        filename = os.path.basename(url.split("?")[0])
        filepath = output_dir / filename

        if skip_existing and filepath.exists():
            logger.info(f"Skipping existing file: {filename}")
            downloaded_files.append(str(filepath))
            continue

        logger.info(f"Downloading [{i}/{len(download_urls)}]: {filename}")

        try:
            response = session.get(url, stream=True, timeout=timeout)
            response.raise_for_status()

            total_size = int(response.headers.get('content-length', 0))
            downloaded = 0

            with open(filepath, "wb") as f:
                for chunk in response.iter_content(chunk_size=1024*1024):  # 1MB chunks
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if show_progress and total_size:
                            percent = (downloaded / total_size) * 100
                            mb_downloaded = downloaded / (1024*1024)
                            mb_total = total_size / (1024*1024)

                            if sys.stdout.isatty():  # Only show progress bar in terminal
                                sys.stdout.write(f"\r  Progress: {percent:.1f}% ({mb_downloaded:.1f}/{mb_total:.1f} MB)")
                                sys.stdout.flush()

            if show_progress and sys.stdout.isatty():
                sys.stdout.write("\n")

            logger.info(f"Successfully downloaded: {filename} ({downloaded / (1024*1024):.1f} MB)")
            downloaded_files.append(str(filepath))

        except requests.exceptions.HTTPError as e:
            logger.error(f"HTTP error downloading {filename}: {e}")
            if e.response.status_code == 401:
                logger.error("Authentication failed. Check your .netrc file.")
            elif e.response.status_code == 403:
                logger.error("Access forbidden. You may not have permission to access this file.")
            elif e.response.status_code >= 500:
                logger.error("Server error. The server may be experiencing issues.")
        except requests.exceptions.ConnectionError:
            logger.error(f"Connection error downloading {filename}. Check your internet connection.")
        except requests.exceptions.Timeout:
            logger.error(f"Timeout downloading {filename}. The server took too long to respond.")
        except Exception as e:
            logger.error(f"Error downloading {filename}: {e}")

    if downloaded_files:
        logger.info(f"Downloaded {len(downloaded_files)} file(s) to {output_dir}")
    else:
        logger.warning("No files were downloaded")

    return downloaded_files


def download_from_url(
    url: str,
    output_dir: Union[str, Path] = ".",
    filename: Optional[str] = None,
    skip_existing: bool = True,
    show_progress: bool = True,
    timeout: int = 60
) -> Optional[str]:
    """
    Download a file directly from a URL using .netrc credentials with progress reporting.

    Parameters
    ----------
    url : str
        URL of the file to download.
    output_dir : str or Path
        Directory where downloaded file will be saved.
    filename : str, optional
        Name to save the file as. If None, uses the basename from the URL.
    skip_existing : bool
        If True, skip downloading file if it already exists in output_dir.
    show_progress : bool
        If True, display download progress information.
    timeout : int
        Timeout in seconds for the HTTP requests.

    Returns
    -------
    str or None
        Path to the downloaded file, or None if download failed.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Determine filename from URL if not provided
    if not filename:
        filename = os.path.basename(url.split("?")[0])

    filepath = output_dir / filename

    if skip_existing and filepath.exists():
        logger.info(f"Skipping existing file: {filename}")
        return str(filepath)

    logger.info(f"Downloading: {filename}")

    # Create session for authentication
    session = requests.Session()

    try:
        response = session.get(url, stream=True, timeout=timeout)
        response.raise_for_status()

        total_size = int(response.headers.get('content-length', 0))
        downloaded = 0

        with open(filepath, "wb") as f:
            for chunk in response.iter_content(chunk_size=1024*1024):  # 1MB chunks
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if show_progress and total_size:
                        percent = (downloaded / total_size) * 100
                        mb_downloaded = downloaded / (1024*1024)
                        mb_total = total_size / (1024*1024)

                        if sys.stdout.isatty():  # Only show progress bar in terminal
                            sys.stdout.write(f"\r  Progress: {percent:.1f}% ({mb_downloaded:.1f}/{mb_total:.1f} MB)")
                            sys.stdout.flush()

        if show_progress and sys.stdout.isatty():
            sys.stdout.write("\n")

        logger.info(f"Successfully downloaded: {filename} ({downloaded / (1024*1024):.1f} MB)")
        return str(filepath)

    except requests.exceptions.HTTPError as e:
        logger.error(f"HTTP error downloading {filename}: {e}")
        if e.response.status_code == 401:
            logger.error("Authentication failed. Check your .netrc file.")
        elif e.response.status_code == 403:
            logger.error("Access forbidden. You may not have permission to access this file.")
        elif e.response.status_code >= 500:
            logger.error("Server error. The server may be experiencing issues.")
    except requests.exceptions.ConnectionError:
        logger.error(f"Connection error downloading {filename}. Check your internet connection.")
    except requests.exceptions.Timeout:
        logger.error(f"Timeout downloading {filename}. The server took too long to respond.")
    except Exception as e:
        logger.error(f"Error downloading {filename}: {e}")

    return None


def download_s3_url(
    s3_url: str,
    output_dir: Union[str, Path] = ".",
    filename: Optional[str] = None,
    region: str = "us-west-2",
    profile: Optional[str] = None
) -> Optional[str]:
    """
    Download a file from an S3 URL using AWS CLI.

    Requires the AWS CLI to be installed and configured.

    Parameters
    ----------
    s3_url : str
        S3 URL of the file to download (s3://bucket/key).
    output_dir : str or Path
        Directory where downloaded file will be saved.
    filename : str, optional
        Name to save the file as. If None, uses the basename from the URL.
    region : str
        AWS region for the S3 bucket.
    profile : str, optional
        AWS profile to use for credentials.

    Returns
    -------
    str or None
        Path to the downloaded file, or None if download failed.
    """
    import subprocess

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Determine filename from URL if not provided
    if not filename:
        filename = os.path.basename(s3_url)

    filepath = output_dir / filename

    # Prepare AWS CLI command
    cmd = ["aws", "s3", "cp", s3_url, str(filepath), "--region", region]
    if profile:
        cmd.extend(["--profile", profile])

    logger.info(f"Downloading from S3: {s3_url} to {filepath}")

    try:
        # Run AWS CLI command
        process = subprocess.run(cmd, check=True, capture_output=True, text=True)
        logger.info(f"Successfully downloaded: {filename}")
        return str(filepath)
    except subprocess.CalledProcessError as e:
        logger.error(f"Error downloading from S3: {e}")
        logger.error(f"AWS CLI output: {e.stderr}")
    except Exception as e:
        logger.error(f"Error: {e}")

    return None