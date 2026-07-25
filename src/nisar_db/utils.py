from datetime import datetime

_DT_FMT = "%Y%m%dT%H%M%S"
_DATE_FMT = "%Y%m%d"


def parts_str(dt: datetime) -> str:
    """Format datetime as NISAR timestamp string."""
    return dt.strftime(_DT_FMT)
