from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
import pandas as pd
from .utils import parts_str, _DT_FMT, _DATE_FMT

@dataclass(frozen=True)
class GSLCFilename:
    mission: str
    instrument: str
    processing_type: str
    product: str
    cycle: str
    relative_orbit: str
    pass_direction: str
    track_frame: str
    mode: str
    polarization: str
    source: str
    start_datetime: datetime
    end_datetime: datetime
    crid: str
    orbits: str
    coverage: str
    location: str
    version: str
    path: str

    _NFIELDS_MIN: int = 13
    _NFIELDS_MAX: int = 18

    @classmethod
    def from_path(cls, path: str) -> "GSLCFilename":
        parts = Path(path).stem.split('_')
        n = len(parts)
        if not (cls._NFIELDS_MIN <= n <= cls._NFIELDS_MAX):
            raise ValueError(f"Expected {cls._NFIELDS_MIN}-{cls._NFIELDS_MAX} fields, got {n}: {Path(path).name}")
        return cls(
            mission=parts[0],
            instrument=parts[1],
            processing_type=parts[2],
            product=parts[3],
            cycle=parts[4],
            relative_orbit=parts[5],
            pass_direction=parts[6],
            track_frame=parts[7],
            mode=parts[8],
            polarization=parts[9],
            source=parts[10],
            start_datetime=datetime.strptime(parts[11], _DT_FMT),
            end_datetime=datetime.strptime(parts[12], _DT_FMT),
            crid=parts[13] if n > 13 else '',
            orbits=parts[14] if n > 14 else '',
            coverage=parts[15] if n > 15 else '',
            location=parts[16] if n > 16 else '',
            version=parts[17] if n > 17 else '',
            path=str(path)
        )

    @property
    def track(self) -> str:
        return self.relative_orbit

    @property
    def frame(self) -> str:
        return self.track_frame

    @property
    def date(self) -> str:
        return self.start_datetime.strftime(_DATE_FMT)

    @property
    def scene_id(self) -> str:
        return f"T{self.relative_orbit}_F{self.track_frame}_{self.pass_direction}"

    def to_dataframe(self) -> pd.DataFrame:
        d = asdict(self)
        d.pop('path')
        d['start_datetime'] = parts_str(self.start_datetime)
        d['end_datetime'] = parts_str(self.end_datetime)
        d['date'] = self.date
        d['scene_id'] = self.scene_id
        d['full_path'] = self.path
        return pd.DataFrame([d])


@dataclass(frozen=True)
class GUNWFilename:
    mission: str
    instrument: str
    processing_type: str
    product: str
    cycle1: str
    relative_orbit: str
    pass_direction: str
    track_frame: str
    cycle2: str
    mode: str
    polarization: str
    reference_start_datetime: datetime
    reference_end_datetime: datetime
    secondary_start_datetime: datetime
    secondary_end_datetime: datetime
    crid: str
    orbits: str
    coverage: str
    location: str
    version: str
    path: str

    _NFIELDS_MIN: int = 15
    _NFIELDS_MAX: int = 20

    @classmethod
    def from_path(cls, path: str) -> "GUNWFilename":
        parts = Path(path).stem.split('_')
        n = len(parts)
        if not (cls._NFIELDS_MIN <= n <= cls._NFIELDS_MAX):
            raise ValueError(f"Expected {cls._NFIELDS_MIN}-{cls._NFIELDS_MAX} fields, got {n}: {Path(path).name}")
        return cls(
            mission=parts[0],
            instrument=parts[1],
            processing_type=parts[2],
            product=parts[3],
            cycle1=parts[4],
            relative_orbit=parts[5],
            pass_direction=parts[6],
            track_frame=parts[7],
            cycle2=parts[8],
            mode=parts[9],
            polarization=parts[10],
            reference_start_datetime=datetime.strptime(parts[11], _DT_FMT),
            reference_end_datetime=datetime.strptime(parts[12], _DT_FMT),
            secondary_start_datetime=datetime.strptime(parts[13], _DT_FMT),
            secondary_end_datetime=datetime.strptime(parts[14], _DT_FMT),
            crid=parts[15] if n > 15 else '',
            orbits=parts[16] if n > 16 else '',
            coverage=parts[17] if n > 17 else '',
            location=parts[18] if n > 18 else '',
            version=parts[19] if n > 19 else '',
            path=str(path)
        )

    @property
    def track(self) -> str:
        return self.relative_orbit

    @property
    def frame(self) -> str:
        return self.track_frame

    @property
    def ref_date(self) -> str:
        return self.reference_start_datetime.strftime(_DATE_FMT)

    @property
    def sec_date(self) -> str:
        return self.secondary_start_datetime.strftime(_DATE_FMT)

    @property
    def date(self) -> str:
        return f"{self.ref_date}_{self.sec_date}"

    @property
    def scene_id(self) -> str:
        return f"T{self.relative_orbit}_F{self.track_frame}_{self.pass_direction}"

    def to_dataframe(self) -> pd.DataFrame:
        d = asdict(self)
        d.pop('path')
        for key in ('reference_start_datetime', 'reference_end_datetime',
                    'secondary_start_datetime', 'secondary_end_datetime'):
            d[key] = parts_str(getattr(self, key))
        d['ref_date'] = self.ref_date
        d['sec_date'] = self.sec_date
        d['scene_id'] = self.scene_id
        d['full_path'] = self.path
        return pd.DataFrame([d])