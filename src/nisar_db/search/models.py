"""Product types and the :class:`NISARProduct` metadata model.

``NISARProduct`` normalises a granule from either CMR UMM-G search results
(:meth:`NISARProduct.from_cmr_item`) or an S3 object key
(:meth:`NISARProduct.from_s3_key`) into a single dataclass, with static helpers
that pull structured fields out of the CMR ``umm`` metadata dict.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, ClassVar, Dict, Optional, Tuple

from ..filenames import GSLCFilename, GUNWFilename

__all__ = ["NISARProduct", "ProductType", "UrlType"]


class ProductType(str, Enum):
    """NISAR product types."""

    GSLC = "GSLC"
    GUNW = "GUNW"


class UrlType(str, Enum):
    """URL types for NISAR products."""

    HTTPS = "https"
    S3 = "s3"


@dataclass
class NISARProduct:
    """NISAR product metadata from CMR search results."""

    granule_id: str
    name: str
    product_type: ProductType
    filename: str
    url: str
    url_type: UrlType = UrlType.HTTPS
    start_datetime: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    end_datetime: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    bbox: Optional[Tuple[float, float, float, float]] = None
    track: Optional[int] = None
    frame: Optional[int] = None
    direction: Optional[str] = None
    cycle: Optional[int] = None
    polarization: Optional[str] = None
    crid: Optional[str] = None
    full_frame: Optional[bool] = None
    joint_observation: Optional[bool] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Metadata field extractors
    #
    # CMR UMM-G granule metadata uses PascalCase keys (``RelatedUrls``,
    # ``TemporalExtent``, ``SpatialExtent``, ``AdditionalAttributes``). These
    # helpers pull structured fields out of that ``umm`` dict; they are also
    # exposed as instance methods (see ``extract_*``) so callers can re-parse
    # ``self.metadata`` after construction.
    # ------------------------------------------------------------------

    # CMR RelatedUrls "Type" -> (destination key, required URL scheme). The
    # first matching link per key wins.
    _URL_RULES: ClassVar[Dict[str, Tuple[str, str]]] = {
        "GET DATA": ("https", "http"),
        "GET DATA VIA DIRECT ACCESS": ("s3", "s3://"),
        "GET RELATED VISUALIZATION": ("browse", "http"),
        "EXTENDED METADATA": ("metadata", "http"),
    }

    @staticmethod
    def extract_urls_from_metadata(umm: Dict[str, Any]) -> Dict[str, str]:
        """Return {'https', 's3', 'browse', 'metadata'} data URLs from a umm dict."""
        urls = {"https": "", "s3": "", "browse": "", "metadata": ""}
        for link in umm.get("RelatedUrls", []) or []:
            rule = NISARProduct._URL_RULES.get(link.get("Type", ""))
            href = link.get("URL", "")
            if not rule or not href:
                continue
            key, scheme = rule
            if href.startswith(scheme) and not urls[key]:
                urls[key] = href
        return urls

    @staticmethod
    def extract_temporal_from_metadata(
        umm: Dict[str, Any],
    ) -> Tuple[datetime, datetime]:
        """Return (start, end) datetimes from a umm dict (defaults to now if absent)."""
        rng = umm.get("TemporalExtent", {}).get("RangeDateTime", {})
        now = datetime.now(timezone.utc)

        def _parse(key: str) -> datetime:
            value = rng.get(key)
            return (
                datetime.fromisoformat(value.replace("Z", "+00:00")) if value else now
            )

        return _parse("BeginningDateTime"), _parse("EndingDateTime")

    @staticmethod
    def extract_bbox_from_metadata(
        umm: Dict[str, Any],
    ) -> Optional[Tuple[float, float, float, float]]:
        """Return (west, south, east, north) from a umm dict, or None if absent."""
        geom = (
            umm.get("SpatialExtent", {})
            .get("HorizontalSpatialDomain", {})
            .get("Geometry", {})
        )
        rects = geom.get("BoundingRectangles")
        if rects:
            r = rects[0]
            return (
                r["WestBoundingCoordinate"],
                r["SouthBoundingCoordinate"],
                r["EastBoundingCoordinate"],
                r["NorthBoundingCoordinate"],
            )
        polygons = geom.get("GPolygons")
        if polygons:
            points = polygons[0].get("Boundary", {}).get("Points", [])
            if points:
                lons = [p["Longitude"] for p in points]
                lats = [p["Latitude"] for p in points]
                return (min(lons), min(lats), max(lons), max(lats))
        return None

    @staticmethod
    def extract_attributes_from_metadata(umm: Dict[str, Any]) -> Dict[str, Any]:
        """Return track/frame/direction/cycle/pol/full_frame/joint_observation/crid.

        Most fields come from ``AdditionalAttributes``; ``crid`` lives in
        ``DataGranule.Identifiers`` (IdentifierType == "CRID").
        """
        fields: Dict[str, Any] = {
            "track": None,
            "frame": None,
            "direction": None,
            "cycle": None,
            "polarization": None,
            "full_frame": None,
            "joint_observation": None,
            "crid": None,
        }
        for attr in umm.get("AdditionalAttributes", []) or []:
            name = attr.get("Name")
            values = attr.get("Values") or []
            if not values:
                continue
            if name == "TRACK_NUMBER":
                fields["track"] = int(values[0])
            elif name == "FRAME_NUMBER":
                fields["frame"] = int(values[0])
            elif name == "ASCENDING_DESCENDING":
                fields["direction"] = "A" if values[0] == "ASCENDING" else "D"
            elif name == "CYCLE_NUMBER":
                fields["cycle"] = int(values[0])
            elif name == "POLARIZATION":
                fields["polarization"] = values[0]
            elif name == "FULL_FRAME":
                fields["full_frame"] = values[0].upper() == "TRUE"
            elif name == "JOINT_OBSERVATION":
                fields["joint_observation"] = values[0].upper() == "TRUE"

        # CRID is a granule Identifier, not an AdditionalAttribute.
        for ident in umm.get("DataGranule", {}).get("Identifiers", []) or []:
            if ident.get("IdentifierType") == "CRID":
                fields["crid"] = ident.get("Identifier")
                break
        return fields

    # Instance-level convenience wrappers over the stored ``metadata`` (umm) dict.
    def extract_urls(self) -> Dict[str, str]:
        """Extract all data/browse/metadata URLs from this product's metadata."""
        return self.extract_urls_from_metadata(self.metadata)

    def extract_fields(self) -> Dict[str, Any]:
        """Extract every structured field (urls, bbox, times, attrs) from metadata."""
        start_dt, end_dt = self.extract_temporal_from_metadata(self.metadata)
        return {
            "granule_id": self.granule_id,
            "name": self.name,
            "product_type": self.product_type.value,
            **self.extract_attributes_from_metadata(self.metadata),
            "bbox": self.extract_bbox_from_metadata(self.metadata),
            "start_datetime": start_dt,
            "end_datetime": end_dt,
            "urls": self.extract_urls(),
        }

    @classmethod
    def from_cmr_item(
        cls, item: Dict[str, Any], url_type: UrlType = UrlType.HTTPS
    ) -> "NISARProduct":
        """Create a NISARProduct from a CMR item."""
        umm = item.get("umm", {})
        granule_id = item.get("meta", {}).get("concept-id") or item.get("id", "")
        name = umm.get("GranuleUR", "")

        # Determine product type from name
        product_type = ProductType.GSLC if "GSLC" in name else ProductType.GUNW

        bbox = cls.extract_bbox_from_metadata(umm)
        start_dt, end_dt = cls.extract_temporal_from_metadata(umm)
        attrs = cls.extract_attributes_from_metadata(umm)

        # Pick the requested URL flavour, falling back to whichever exists.
        urls = cls.extract_urls_from_metadata(umm)
        url = urls["s3"] if url_type == UrlType.S3 else urls["https"]
        if not url:
            url = urls["https"] or urls["s3"]

        # Extract filename from URL
        filename = url.split("/")[-1] if url else name

        return cls(
            granule_id=granule_id,
            name=name,
            product_type=product_type,
            filename=filename,
            url=url,
            url_type=url_type,
            start_datetime=start_dt,
            end_datetime=end_dt,
            bbox=bbox,
            metadata=umm,
            **attrs,
        )

    @classmethod
    def from_s3_key(
        cls,
        bucket: str,
        key: str,
        size: Optional[int] = None,
        url_type: UrlType = UrlType.S3,
        last_modified: Optional[datetime] = None,
    ) -> "NISARProduct":
        """Create a NISARProduct from an S3 object key by parsing its granule name.

        Unlike :meth:`from_cmr_item`, all fields come from the filename
        (via ``GSLCFilename``/``GUNWFilename``); no geometry/bbox is available.
        The parsed filename fields are stored in ``metadata`` (with the s3 key
        and object size), so ``products_to_dataframe`` and downstream code can
        reach ``mode``, ``crid``, ``version``, etc.

        Parameters
        ----------
        bucket : str
            The S3 bucket name; used to build the ``s3://`` URL.
        key : str
            The S3 object key. Its final path component is the granule name
            that gets parsed for all product fields.
        size : Optional[int]
            The S3 object size in bytes, stored as ``metadata["s3_size_bytes"]``.
        url_type : UrlType
            The URL type to record on the product (defaults to ``UrlType.S3``).
        last_modified : Optional[datetime]
            The S3 object's ``LastModified``, stored as
            ``metadata["s3_last_modified"]``. The granule name carries no
            production time, so for a forward-processing bucket this delivery
            time is the available proxy for when the product was produced.

        """
        name = key.rsplit("/", 1)[-1]
        stem = name[:-3] if name.endswith(".h5") else name

        parsed: GSLCFilename | GUNWFilename
        if "GUNW" in stem:
            gunw = GUNWFilename.from_path(stem)
            parsed = gunw
            product_type = ProductType.GUNW
            start_dt = gunw.reference_start_datetime
            end_dt = gunw.secondary_end_datetime
            cycle_raw = gunw.cycle1
        else:
            gslc = GSLCFilename.from_path(stem)
            parsed = gslc
            product_type = ProductType.GSLC
            start_dt, end_dt = gslc.start_datetime, gslc.end_datetime
            cycle_raw = gslc.cycle

        try:
            cycle = int(cycle_raw)
        except (TypeError, ValueError):
            cycle = None

        metadata = parsed.to_dataframe().iloc[0].to_dict()
        metadata["s3_key"] = key
        if size is not None:
            metadata["s3_size_bytes"] = size
        if last_modified is not None:
            metadata["s3_last_modified"] = last_modified

        # crid and full-frame are encoded in the filename; joint_observation is
        # not (it only exists in CMR metadata), so leave it None.
        full_frame = parsed.coverage == "F" if parsed.coverage else None

        return cls(
            granule_id=stem,
            name=name,
            product_type=product_type,
            filename=name,
            url=f"s3://{bucket}/{key}",
            url_type=url_type,
            start_datetime=start_dt,
            end_datetime=end_dt,
            bbox=None,
            track=int(parsed.relative_orbit),
            frame=int(parsed.track_frame),
            direction=parsed.pass_direction,
            cycle=cycle,
            polarization=parsed.polarization,
            crid=parsed.crid or None,
            full_frame=full_frame,
            metadata=metadata,
        )

    @property
    def track_frame_id(self) -> str:
        """Get track frame ID in format 'XXX_D_YYY'."""
        if self.track is None or self.frame is None or self.direction is None:
            return ""
        return f"{self.track:03d}_{self.direction}_{self.frame:03d}"

    @property
    def date(self) -> str:
        """Get date in YYYY-MM-DD format."""
        return self.start_datetime.strftime("%Y-%m-%d")
