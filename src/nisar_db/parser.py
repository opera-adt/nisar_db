import logging
import pandas as pd
from .filenames import GSLCFilename, GUNWFilename

logger = logging.getLogger(__name__)

def parse_gslc(file: str) -> pd.DataFrame | None:
    try:
        return GSLCFilename.from_path(file).to_dataframe()
    except Exception:
        return None

def parse_gunw(file: str) -> pd.DataFrame | None:
    try:
        return GUNWFilename.from_path(file).to_dataframe()
    except Exception:
        return None

def parse_s3_files_to_dataframe(s3_files: list, product_type="GUNW") -> pd.DataFrame:
    if not s3_files:
        logger.warning("No S3 files provided to parse")
        return pd.DataFrame()

    parse_func = parse_gunw if product_type == "GUNW" else parse_gslc
    parsed = []
    failed = 0
    for s3_path in s3_files:
        try:
            result = parse_func(s3_path.get_path())
            if result is not None:
                parsed.append(result)
            else:
                failed += 1
                logger.debug(f"Failed to parse: {s3_path}")
        except Exception as e:
            failed += 1
            logger.debug(f"Error parsing {s3_path}: {e}")

    if not parsed:
        return pd.DataFrame()

    df = pd.concat(parsed, ignore_index=True)
    logger.info(f"Parsed {len(df)} rows (failed: {failed})")
    return df