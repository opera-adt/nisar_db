"""Offer MCP tools for frame health, areas, events, exports and DISP assets.

The REST API offers the same in :mod:`nisar_db.api.routes_more`.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Callable, Literal

from nisar_db.api import analysis, assets, store

#: Exports longer than this return a download link instead of the text.
EXPORT_CHARS = 60000


def register(
    tool: Callable, ctx: Any, may: Callable[[str], None], caller: Callable[[], Any]
) -> None:
    """Add the tools to an MCP server.

    ``tool`` is its decorator, ``may`` checks a scope.
    """

    def ds(name: str) -> store.Dataset:
        return ctx.frames.get(name)

    def day(text: str | None) -> date | None:
        return date.fromisoformat(text) if text else None

    def query(
        product: str = "gslc",
        track: str | None = None,
        direction: str | None = None,
        bbox: list[float] | None = None,
        cycle: str | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> store.FrameQuery:
        return store.FrameQuery(
            product=product,
            track=track,
            direction=direction,
            bbox=tuple(bbox) if bbox else None,
            cycle=cycle,
            start=day(start),
            end=day(end),
        )

    def repo() -> Path | None:
        rd = ctx.settings.repo_dir
        return Path(rd) if rd else None

    # -- frame health ------------------------------------------------------------------
    @tool
    def frame_coverage(
        key: str, dataset: str = "published", today: str | None = None
    ) -> dict[str, Any]:
        """Return a frame's GSLC record.

        Missed cycles (with their expected dates), gaps and days since the last
        acquisition.
        """
        may("read")
        return analysis.frame_coverage(ds(dataset), key, today=day(today))

    @tool
    def scan_coverage(
        dataset: str = "published",
        track: str | None = None,
        direction: str | None = None,
        bbox: list[float] | None = None,
        stale_days: int = 30,
        today: str | None = None,
        limit: int = 30,
    ) -> dict[str, Any]:
        """List frames with missed cycles or stale for stale_days, worst first."""
        may("read")
        return analysis.coverage_scan(
            ds(dataset),
            query("gslc", track, direction, bbox),
            today=day(today),
            stale_days=stale_days,
            limit=max(1, min(limit, 200)),
        )

    @tool
    def frame_network(key: str, dataset: str = "published") -> dict[str, Any]:
        """Return a frame's GUNW network health.

        Pieces, breaks no pair bridges, and acquisitions in no pair.
        """
        may("read")
        return analysis.frame_network(ds(dataset), key)

    @tool
    def scan_network(
        dataset: str = "published",
        track: str | None = None,
        direction: str | None = None,
        bbox: list[float] | None = None,
        limit: int = 30,
    ) -> dict[str, Any]:
        """Count connected / disconnected GUNW networks; list the disconnected."""
        may("read")
        return analysis.network_scan(
            ds(dataset),
            query("gunw", track, direction, bbox),
            limit=max(1, min(limit, 200)),
        )

    @tool
    def next_passes(
        key: str | None = None,
        lon: float | None = None,
        lat: float | None = None,
        dataset: str = "published",
        n: int = 3,
        today: str | None = None,
    ) -> dict[str, Any]:
        """Return the next expected NISAR passes for a frame or over a point.

        Give a frame ``key``, or ``lon`` and ``lat``; passes are projected from the
        12-day repeat.
        """
        may("read")
        if key:
            return analysis.frame_next_passes(ds(dataset), key, n=n, today=day(today))
        if lon is None or lat is None:
            raise ValueError("give a frame key, or lon and lat")
        return analysis.point_next_passes(ds(dataset), lon, lat, n=n, today=day(today))

    @tool
    def frame_disp_readiness(
        key: str,
        dataset: str = "published",
        batch_size: int = 15,
        today: str | None = None,
    ) -> dict[str, Any]:
        """Return a frame's progress towards its DISP-NISAR batches.

        Usable consistent-mode acquisitions outside blackouts, batches done, and
        when the next batch completes.
        """
        may("read")
        return analysis.frame_readiness(
            ds(dataset), key, batch_size=batch_size, today=day(today)
        )

    @tool
    def scan_disp_readiness(
        dataset: str = "published",
        track: str | None = None,
        direction: str | None = None,
        bbox: list[float] | None = None,
        batch_size: int = 15,
        today: str | None = None,
        limit: int = 30,
    ) -> dict[str, Any]:
        """Count frames by DISP status and list those closest to their next batch."""
        may("read")
        return analysis.readiness_scan(
            ds(dataset),
            query("gslc", track, direction, bbox),
            batch_size=batch_size,
            today=day(today),
            limit=max(1, min(limit, 200)),
        )

    # -- areas -------------------------------------------------------------------------
    @tool
    def frames_in_area(
        bbox: list[float] | None = None,
        geometry: dict[str, Any] | None = None,
        dataset: str = "published",
        min_share: float = 0.0,
    ) -> dict[str, Any]:
        """List the frames over a bbox (west, south, east, north) or GeoJSON polygon.

        Each with its share of the area, latest pair, next pass and DISP status.
        """
        may("read")
        return analysis.aoi(
            ds(dataset),
            geometry=geometry,
            box=tuple(bbox) if bbox else None,
            min_share=min_share,
        )

    # -- event comparisons -------------------------------------------------------------
    @tool
    def event_qa(
        key: str,
        when: str,
        metric: str = "cm",
        dataset: str = "published",
        window_days: int = 120,
    ) -> dict[str, Any]:
        """Compare a QA metric before, across and after an event date on one frame.

        The metric is e.g. coherence 'cm'; returns medians, change from before,
        and the pairs in time order.
        """
        may("read")
        return analysis.frame_event_qa(
            ds(dataset), key, when, metric=metric, window_days=window_days
        )

    @tool
    def earthquake_compare(
        event_id: str,
        metric: str = "cm",
        dataset: str = "published",
        window_days: int = 120,
        radius_km: float = 0,
    ) -> dict[str, Any]:
        """Compare a QA metric around a USGS earthquake on every frame over it.

        Before, across (coseismic) and after the event.
        """
        import requests

        from nisar_db.events import get_earthquake

        may("read")
        try:
            quake = get_earthquake(event_id)
        except requests.RequestException as exc:
            raise ValueError(f"USGS did not answer: {type(exc).__name__}") from exc
        return analysis.earthquake_compare(
            ds(dataset),
            quake,
            metric=metric,
            window_days=window_days,
            radius_km=radius_km,
        )

    # -- exports -----------------------------------------------------------------------
    @tool
    def export_frames(
        format: Literal["csv", "geojson", "kml"] = "csv",  # noqa: A002
        dataset: str = "published",
        product: Literal["gslc", "gunw"] = "gslc",
        track: str | None = None,
        direction: str | None = None,
        bbox: list[float] | None = None,
        cycle: str | None = None,
    ) -> dict[str, Any]:
        """Export the filtered frames as CSV, GeoJSON or KML.

        Returns the text, or a download link when it is long.
        """
        may("read")
        q = query(product, track, direction, bbox, cycle)
        return _text_or_link(
            analysis.export_frames(ds(dataset), q, format),
            "/api/v1/export/frames",
            {
                "dataset": dataset,
                "format": format,
                "product": product,
                "track": track,
                "direction": direction,
                "cycle": cycle,
                "bbox": ",".join(map(str, bbox)) if bbox else None,
            },
        )

    @tool
    def export_entries(
        dataset: str = "published",
        product: Literal["gslc", "gunw"] = "gunw",
        track: str | None = None,
        direction: str | None = None,
        bbox: list[float] | None = None,
        cycle: str | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> dict[str, Any]:
        """Export the filtered frames' GUNW pairs (or GSLC granules) as CSV."""
        may("read")
        q = query(product, track, direction, bbox, cycle, start, end)
        return _text_or_link(
            analysis.export_entries(ds(dataset), q, "csv"),
            "/api/v1/export/entries",
            {
                "dataset": dataset,
                "product": product,
                "track": track,
                "direction": direction,
                "cycle": cycle,
                "start": start,
                "end": end,
                "bbox": ",".join(map(str, bbox)) if bbox else None,
            },
        )

    def _text_or_link(text: str, path: str, params: dict) -> dict[str, Any]:
        from urllib.parse import urlencode

        url = f"{ctx.viewer_url.rstrip('/')}{path}?" + urlencode(
            {k: v for k, v in params.items() if v is not None}
        )
        if len(text) > EXPORT_CHARS:
            return {
                "size": len(text),
                "truncated": True,
                "download_url": url,
                "head": text[:2000],
            }
        return {
            "size": len(text),
            "truncated": False,
            "download_url": url,
            "text": text,
        }

    # -- DISP-NISAR assets -------------------------------------------------------------
    @tool
    def list_disp_assets() -> dict[str, Any]:
        """List the DISP-NISAR assets.

        The consistent-GSLC DB, blackout and reference dates and frame bounds, from
        the published release, the latest build on this server, and the repo.
        """
        may("read")
        return assets.overview(repo_dir=repo(), jobs=ctx.jobs)

    def frame_asset(kind: str, key: str, dataset: str) -> dict[str, Any]:
        path = assets.local_file(kind, repo_dir=repo(), jobs=ctx.jobs)
        if path is None:
            raise ValueError(
                f"no local {kind} asset; run build_disp_assets, "
                "or use list_disp_assets for the published release"
            )
        f = ctx.frames.get(dataset).frame(key)
        idx = f["properties"]["frame_idx"] if f is not None else key
        entry = assets.frame_entry(assets.load_json(path), idx)
        if entry is None:
            raise ValueError(f"frame {key} is not in {path.name}")
        return {"frame_idx": idx, "asset": path.name, kind: entry}

    @tool
    def disp_consistent(key: str, dataset: str = "published") -> dict[str, Any]:
        """Return a frame's consistent-GSLC entry from the DISP-NISAR asset.

        Mode, coverage and sensing times.
        """
        may("read")
        return frame_asset("consistent_gslc", key, dataset)

    @tool
    def disp_blackout_dates(key: str, dataset: str = "published") -> dict[str, Any]:
        """Return a frame's blackout windows from the DISP-NISAR asset."""
        may("read")
        return frame_asset("blackout_dates", key, dataset)

    @tool
    def disp_reference_dates(key: str, dataset: str = "published") -> dict[str, Any]:
        """Return a frame's reference (reset) dates from the DISP-NISAR asset."""
        may("read")
        return frame_asset("reference_dates", key, dataset)

    @tool
    def build_disp_assets(max_results: int = 0) -> dict[str, Any]:
        """Start a fresh DISP-NISAR asset build from CMR, as the release does.

        A job that takes minutes; poll job_status, outputs are downloadable when
        done.
        """
        may("jobs")
        params: dict = {"max_results": max_results}
        rb = assets.repo_blackout(repo())
        if rb is not None:
            params["blackout_file"] = str(rb)
        return ctx.jobs.submit("build-disp-assets", params, caller().name).public()
