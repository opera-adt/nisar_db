"""MCP server: the API's catalog queries and jobs as tools for AI assistants.

The tools answer from the same frame store and run jobs through the same job
runner as the REST API, so an assistant sees what the viewer and the API see.
They are served two ways:

* ``nisar-db mcp``: over stdio, started by a local client (Claude Code,
  Claude Desktop) on your machine;
* ``/mcp`` on ``nisar-db serve``: streamable HTTP, for clients that connect by
  URL. A shared service asks for an API key there, and the job tools still
  need the key's ``jobs`` scope.
"""

from __future__ import annotations

import contextvars
import functools
import json
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Literal, TypeVar
from urllib.parse import urlencode

import requests
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.utilities.types import Image
from pydantic import ValidationError

from nisar_db.api import store
from nisar_db.api.browse import (
    LAYER_FOR_METRIC,
    browse_overlay,
    fetch_image,
    pick_qa_examples,
)
from nisar_db.api.jobs import KINDS, Jobs
from nisar_db.api.security import Caller
from nisar_db.api.settings import Settings
from nisar_db.api.viewer import VIEWER_PARAMS
from nisar_db.duplicates import duplicate_groups
from nisar_db.duplicates import summarize as summarize_duplicates
from nisar_db.events import find_volcanoes as find_volcano_records
from nisar_db.events import get_earthquake, search_earthquakes
from nisar_db.qa_drops import METRICS as QA_METRICS
from nisar_db.qa_drops import find_drops as find_qa_drops

INSTRUCTIONS = """\
NISAR frame database (OPERA nisar_db). Frames are NISAR track/frame cells; each
has GSLC acquisitions (granules) and GUNW interferograms (pairs), a consistent
mode used for OPERA DISP, seasonal blackout windows and rollout options.

Start with list_datasets ('published' is North America; rebuilt views such as
'globe-...' cover more). find_frames filters like the viewer's sidebar;
frame_granules lists one frame's granules or pairs; list_cycles gives cycle
date spans. Frame keys are a frame_idx ('8109') or track_frame ('34_19').
viewer_link returns a URL that opens the map in a given state, for the user to
click. Jobs run nisar-db commands (CMR search, consistent-GSLC, blackout or
reference dates, catalogs): list_job_kinds for parameters, start_job, then poll
job_status and read_job_output. Searches of the whole archive (max_results=0)
take minutes.

QA drops: frame_qa_drops flags a frame's GUNW pairs (or GSLC acquisitions)
whose QA metric (coherence 'cm', valid unwrapped 'v', components 'n',
ionosphere spread 'is', RFI 'rl', ...) falls far from the stack median, and the
acquisition dates behind them; scan_qa_drops does it over many frames and shows
dates flagged in several frames at once (wider events). list_qa_metrics lists
the metrics.

Duplicates: frame_duplicates lists granules delivered more than once (same date,
mode, coverage) with the reason and the one to keep; scan_duplicates finds the
frames that have them.

Events: find_earthquakes (USGS) and find_volcanoes (GVP) find events;
earthquake_frames gives the frames over an epicentre and their coseismic GUNW
pairs, volcano_frames the frames over a volcano and their pairs in a date
window; then browse_image / show_browse_image to look at a pair.

Frame health: frame_coverage / scan_coverage (missed cycles, stale frames),
frame_network / scan_network (GUNW network breaks), next_passes, and
frame_disp_readiness / scan_disp_readiness (DISP batches). frames_in_area takes
a box or polygon; event_qa and earthquake_compare compare QA before / across /
after an event; export_frames / export_entries give CSV, GeoJSON or KML.
DISP-NISAR assets: list_disp_assets, disp_consistent, disp_blackout_dates,
disp_reference_dates, build_disp_assets.
"""

#: Who is calling, set per request by the HTTP door; stdio is always local.
CALLER: contextvars.ContextVar[Caller | None] = contextvars.ContextVar(
    "nisar_db_mcp_caller", default=None
)


def _caller() -> Caller:
    """Return who is calling; no caller set means a local (stdio) client."""
    return CALLER.get() or Caller("local")


#: Frame fields an assistant rarely needs; dropped from list results to keep
#: answers small.
_LIST_DROP = (
    "gslc_modes",
    "gslc_pols",
    "gunw_modes",
    "gunw_pols",
    "blackout_ranges",
    "rollout_regions",
)


@dataclass
class McpContext:
    """What the tools work on: the API's store, job runner and settings."""

    settings: Settings
    frames: store.FrameStore
    jobs: Jobs
    #: Where viewer links point, e.g. ``http://127.0.0.1:8797``.
    viewer_url: str
    #: The viewer helper (QA images, grid corners); None outside a checkout.
    helper: Any = None


def _caller_may(scope: str) -> None:
    caller = _caller()
    if not caller.allows(scope):
        raise PermissionError(f"the key {caller.name!r} lacks the {scope!r} scope")


def _visible(owner: str) -> bool:
    caller = _caller()
    return caller.key is None or caller.allows("admin") or caller.name == owner


def _query(
    product: str,
    track: str | None,
    frame: str | None,
    direction: str | None,
    bbox: list[float] | None,
    cycle: str | None,
    start: str | None,
    end: str | None,
    modes: list[str] | None,
    pols: list[str] | None,
    crids: list[str] | None,
    has_data: bool | None,
    calval: bool | None,
    land: bool | None,
    rollout: list[str] | None,
    consistent_mode: str | None,
) -> store.FrameQuery:
    if bbox is not None and len(bbox) != 4:
        raise ValueError("bbox is [west, south, east, north]")
    for name, text in (("track", track), ("frame", frame), ("cycle", cycle)):
        try:
            store.int_set(text)
        except ValueError as exc:
            raise ValueError(f"{name}: use numbers, ranges (10-20) and commas") from exc
    return store.FrameQuery(
        product=product,
        track=track,
        frame=frame,
        direction=direction[0].upper() if direction else None,
        bbox=tuple(bbox) if bbox else None,  # type: ignore[arg-type]
        cycle=cycle,
        start=date.fromisoformat(start) if start else None,
        end=date.fromisoformat(end) if end else None,
        modes=tuple(modes or ()),
        pols=tuple(pols or ()),
        crids=tuple(crids or ()),
        has_data=has_data,
        calval=calval,
        land=land,
        rollout=tuple(rollout or ()),
        consistent_mode=consistent_mode,
    )


def _frame(ctx: McpContext, dataset: str, key: str) -> tuple[store.Dataset, dict]:
    ds = ctx.frames.get(dataset)
    f = ds.frame(key)
    if f is None:
        raise ValueError(f"no frame {key!r} in {dataset}")
    return ds, f


_F = TypeVar("_F", bound=Callable[..., Any])


def _reported(fn: _F) -> _F:
    """Hand expected failures to the client as a ``ToolError`` with its reason.

    Anything else stays an unexpected error, which the SDK reports without
    details.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except KeyError as exc:
            raise ToolError(
                f"not found: {exc.args[0]!r}; see list_datasets / list_job_kinds"
            ) from exc
        except (ValueError, PermissionError) as exc:
            raise ToolError(str(exc)) from exc

    return wrapper  # type: ignore[return-value]


def build_mcp(ctx: McpContext) -> MCPServer:
    """Return the MCP server with every tool bound to ``ctx``."""
    server = MCPServer("nisar_db", instructions=INSTRUCTIONS, log_level="WARNING")

    def tool(fn: _F) -> _F:
        return server.tool()(_reported(fn))

    # -- catalog ---------------------------------------------------------------
    @tool
    def list_datasets() -> list[dict[str, Any]]:
        """List the datasets to query: 'published' (North America) and rebuilt views."""
        _caller_may("read")
        out = []
        for ds_id in ctx.frames.paths():
            try:
                out.append(ctx.frames.header(ds_id))
            except (OSError, ValueError) as exc:
                out.append({"id": ds_id, "error": str(exc)})
        return out

    @tool
    def find_frames(
        dataset: str = "published",
        product: Literal["gslc", "gunw"] = "gslc",
        track: str | None = None,
        frame: str | None = None,
        direction: Literal["A", "D"] | None = None,
        bbox: list[float] | None = None,
        cycle: str | None = None,
        start: str | None = None,
        end: str | None = None,
        modes: list[str] | None = None,
        pols: list[str] | None = None,
        crids: list[str] | None = None,
        has_data: bool | None = None,
        calval: bool | None = None,
        land: bool | None = None,
        rollout: list[str] | None = None,
        consistent_mode: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Find frames matching the viewer's sidebar filters.

        track / frame / cycle take '12', '10-20' or '12,34'; bbox is [west,
        south, east, north]; start / end are YYYY-MM-DD. Cycle, date, mode,
        polarization and CRID filters act on each frame's granules (GSLC) or
        pairs (GUNW, which match a cycle on either date); frames with none left
        drop out, and n_selected counts what remains. Returns the total and one
        page of frame summaries (limit at most 1000).
        """
        _caller_may("read")
        ds = ctx.frames.get(dataset)
        q = _query(
            product,
            track,
            frame,
            direction,
            bbox,
            cycle,
            start,
            end,
            modes,
            pols,
            crids,
            has_data,
            calval,
            land,
            rollout,
            consistent_mode,
        )
        sel = store.select(ds, q)
        page = sel[offset : offset + max(1, min(limit, 1000))]
        frames = [
            {k: v for k, v in store.frame_summary(f, q).items() if k not in _LIST_DROP}
            for f in page
        ]
        return {
            "dataset": dataset,
            "total": len(sel),
            "offset": offset,
            "frames": frames,
        }

    @tool
    def get_frame(key: str, dataset: str = "published") -> dict[str, Any]:
        """Return one frame's summary by frame_idx ('8109') or track_frame ('34_19')."""
        _caller_may("read")
        return store.frame_summary(_frame(ctx, dataset, key)[1])

    @tool
    def frame_granules(
        key: str,
        dataset: str = "published",
        product: Literal["gslc", "gunw"] = "gslc",
        cycle: str | None = None,
        start: str | None = None,
        end: str | None = None,
        modes: list[str] | None = None,
        pols: list[str] | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        """List a frame's GSLC granules, or its GUNW pairs with product='gunw'."""
        _caller_may("read")
        _, f = _frame(ctx, dataset, key)
        q = _query(
            product,
            None,
            None,
            None,
            None,
            cycle,
            start,
            end,
            modes,
            pols,
            None,
            None,
            None,
            None,
            None,
            None,
        )
        items = q.entries(f["properties"])
        return {
            "frame_idx": f["properties"]["frame_idx"],
            "product": product,
            "total": len(items),
            "items": items[: max(1, min(limit, 2000))],
        }

    @tool
    def frame_blackout(key: str, dataset: str = "published") -> dict[str, Any]:
        """Return a frame's blackout windows, monthly shares and reference dates."""
        _caller_may("read")
        return store.blackout(_frame(ctx, dataset, key)[1])

    @tool
    def list_cycles(
        dataset: str = "published", product: Literal["gslc", "gunw"] = "gslc"
    ) -> list[dict[str, Any]]:
        """List every cycle with its first and last date and how many frames hold it."""
        _caller_may("read")
        return store.cycles(ctx.frames.get(dataset), product)

    @tool
    def summarize(
        dataset: str = "published",
        product: Literal["gslc", "gunw"] = "gslc",
        track: str | None = None,
        direction: Literal["A", "D"] | None = None,
        bbox: list[float] | None = None,
        cycle: str | None = None,
        modes: list[str] | None = None,
    ) -> dict[str, Any]:
        """Summarise consistent modes (full / partial, frames per mode) and rollout."""
        _caller_may("read")
        ds = ctx.frames.get(dataset)
        sel = store.select(
            ds,
            _query(
                product,
                track,
                None,
                direction,
                bbox,
                cycle,
                None,
                None,
                modes,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
            ),
        )
        return {
            "consistent": store.consistent_summary(sel),
            "rollout": store.rollout_summary(ds, sel),
        }

    @tool
    def viewer_link(
        dataset: str = "published", params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Return a URL opening the viewer in a given state, for the user to click.

        params may hold: product, opera (0/1), track, frame, id, cycle, start,
        end, pass (all/asc/desc), modes, pols, color, basemap, sky
        (theme/white/black/space), theme, center ('lon,lat'), zoom, play
        (spin/cycles/time), spin, step_days, cumulative, fullscreen, gps, popup.
        """
        _caller_may("read")
        params = dict(params or {})
        unknown = sorted(set(params) - set(VIEWER_PARAMS))
        if unknown:
            raise ValueError(
                f"unknown viewer parameters {unknown}; known: {sorted(VIEWER_PARAMS)}"
            )
        if dataset != "published" and not store.VIEW_ID.match(dataset):
            raise ValueError(f"unknown dataset {dataset!r}")
        query = {
            k: int(v) if isinstance(v, bool) else v
            for k, v in params.items()
            if v is not None
        }
        path = "/" if dataset == "published" else f"/view/{dataset}"
        return {
            "url": (
                f"{ctx.viewer_url.rstrip('/')}{path}"
                + (f"?{urlencode(query)}" if query else "")
            )
        }

    # -- QA drops ----------------------------------------------------------------
    @tool
    def list_qa_metrics() -> list[dict[str, Any]]:
        """List the QA metrics, their products and which way is bad."""
        _caller_may("read")
        side = {-1: "low", 1: "high", 0: "either"}
        return [
            {
                "metric": m.key,
                "label": m.label,
                "products": list(m.products),
                "bad_when": side[m.bad],
            }
            for m in QA_METRICS.values()
        ]

    @tool
    def frame_qa_drops(
        key: str,
        metric: str = "cm",
        dataset: str = "published",
        product: Literal["gslc", "gunw"] = "gunw",
        threshold: float = 3.0,
        cycle: str | None = None,
        start: str | None = None,
        end: str | None = None,
        modes: list[str] | None = None,
        min_pairs: int = 2,
        min_share: float = 0.5,
    ) -> dict[str, Any]:
        """Flag a frame's pairs whose QA metric drops from the stack median.

        A pair (or GSLC acquisition) is flagged when its robust z-score on the
        metric's bad side reaches threshold; GUNW pairs are compared with pairs
        of the same temporal baseline. A date is flagged when at least
        min_pairs of its pairs, and min_share of them, are flagged. Returns the
        stack median, flagged entries (worst first) and flagged dates.
        """
        _caller_may("read")
        _, f = _frame(ctx, dataset, key)
        q = _query(
            product,
            None,
            None,
            None,
            None,
            cycle,
            start,
            end,
            modes,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        )
        report = find_qa_drops(
            q.entries(f["properties"]),
            metric,
            product=product,
            threshold=threshold,
            min_pairs=min_pairs,
            min_share=min_share,
            granules=f["properties"].get("granules"),
        )
        return {
            "id": f["properties"]["id"],
            "frame_idx": f["properties"]["frame_idx"],
            **report,
        }

    @tool
    def scan_qa_drops(
        metric: str = "cm",
        dataset: str = "published",
        product: Literal["gslc", "gunw"] = "gunw",
        threshold: float = 3.0,
        track: str | None = None,
        direction: Literal["A", "D"] | None = None,
        bbox: list[float] | None = None,
        cycle: str | None = None,
        start: str | None = None,
        end: str | None = None,
        modes: list[str] | None = None,
        limit: int = 30,
    ) -> dict[str, Any]:
        """Scan many frames for QA drops: worst frames, dates flagged in several.

        Uses the same test as frame_qa_drops on every frame the filters keep.
        A date flagged in many frames points to an event wider than one frame
        (ionosphere, processing, weather); one frame's dates to a local issue.
        """
        _caller_may("read")
        ds = ctx.frames.get(dataset)
        q = _query(
            product,
            track,
            None,
            direction,
            bbox,
            cycle,
            start,
            end,
            modes,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        )
        return store.qa_drops_scan(
            ds, q, metric, limit=max(1, min(limit, 200)), threshold=threshold
        )

    # -- browse images -----------------------------------------------------------
    def _image(gid: str, layer: str, max_side: int) -> tuple[Image, dict]:
        try:
            data, meta = fetch_image(
                gid, layer, helper=ctx.helper, max_side=max(128, min(max_side, 1600))
            )
        except requests.RequestException as exc:
            raise ValueError(
                f"could not fetch the {layer} image of {gid}: {type(exc).__name__}"
            ) from exc
        except (LookupError, OSError, PermissionError) as exc:
            raise ValueError(str(exc)) from exc
        return Image(data=data, format="jpeg"), meta

    @tool
    def browse_image(gid: str, layer: str = "browse", max_side: int = 768):
        """Return a granule's browse image (to look at), with what it shows.

        layer: browse (public GSLC backscatter / GUNW unwrapped phase), or for
        GUNW a QA-report layer: wrapped, coherence, coherence_wrapped, cc,
        unwrapped, rewrapped, iono, iono_unc (these need the local helper's
        Earthdata login).
        """
        _caller_may("read")
        img, meta = _image(gid, layer, max_side)
        return [json.dumps(meta), img]

    @tool
    def browse_on_map(
        gid: str, layer: str = "browse", dataset: str = "published"
    ) -> dict[str, Any]:
        """Put a granule's browse image on the map.

        Returns a viewer link that opens it there, plus the image URL and its
        corners for any other map (a MapLibre image source).
        """
        _caller_may("read")
        return browse_overlay(
            gid, layer, dataset=dataset, base=ctx.viewer_url, helper=ctx.helper
        )

    @tool
    def qa_drop_images(
        key: str,
        metric: str = "cm",
        dataset: str = "published",
        n: int = 2,
        layer: str | None = None,
        threshold: float = 3.0,
        max_side: int = 640,
    ):
        """Check a frame for QA drops and return images of the pairs behind them.

        The worst flagged pairs, and one typical pair for comparison.

        layer defaults to the QA layer that shows the metric (coherence for cm/ca,
        cc for v/l/n, iono for ionosphere) and falls back to the public browse.
        """
        _caller_may("read")
        _, f = _frame(ctx, dataset, key)
        props = f["properties"]
        entries = props.get("gunw_ifgs") or []
        report = find_qa_drops(
            entries,
            metric,
            product="gunw",
            threshold=threshold,
            granules=props.get("granules"),
        )
        picks = pick_qa_examples(
            report, entries, metric, n=max(0, min(n, 4)), granules=props.get("granules")
        )
        want = layer or LAYER_FOR_METRIC.get(metric, "browse")
        out: list[Any] = [
            json.dumps(
                {
                    "frame": props["id"],
                    "metric": metric,
                    "median": report.get("median"),
                    "n_flagged": len(report.get("flagged", [])),
                    "dates": [d["date"] for d in report.get("dates", [])],
                    "status": report.get("status"),
                }
            )
        ]
        for pick in picks:
            caption = {
                k: pick.get(k)
                for k in (
                    "role",
                    "ref",
                    "sec",
                    "date",
                    "value",
                    "median",
                    "score",
                    "gid",
                )
                if pick.get(k) is not None
            }
            try:
                img, meta = _image(pick["gid"], want, max_side)
            except ValueError as exc:
                if want == "browse":
                    raise
                img, meta = _image(pick["gid"], "browse", max_side)
                caption["note"] = f"{want} unavailable ({exc}); public browse instead"
            caption["layer"] = meta["layer"]
            out += [json.dumps(caption), img]
        return out

    # -- events: earthquakes and volcanoes --------------------------------------------
    @tool
    def find_earthquakes(
        bbox: list[float] | None = None,
        lon: float | None = None,
        lat: float | None = None,
        radius_km: float = 200,
        start: str | None = None,
        end: str | None = None,
        min_magnitude: float = 4.5,
        order: Literal["time", "magnitude"] = "time",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Search USGS earthquakes, newest or largest first.

        By box (west, south, east, north) or a circle around lon/lat, dates
        (YYYY-MM-DD) and magnitude.
        """
        _caller_may("read")
        center = (lon, lat) if lon is not None and lat is not None else None
        try:
            return search_earthquakes(
                bbox=tuple(bbox) if bbox else None,  # type: ignore[arg-type]
                center=center,
                radius_km=radius_km if center else None,
                start=start,
                end=end,
                min_magnitude=min_magnitude,
                limit=max(1, min(limit, 500)),
                order=order,
            )
        except requests.RequestException as exc:
            raise ValueError(f"USGS did not answer: {type(exc).__name__}") from exc

    @tool
    def earthquake_frames(
        event_id: str,
        dataset: str = "published",
        radius_km: float = 0,
        max_dt: int | None = None,
        n_pairs: int = 3,
        include_image: bool = False,
    ):
        """Find the frames over a USGS earthquake and their coseismic GUNW pairs.

        event_id comes from find_earthquakes (e.g. us6000u18k). Coseismic pairs
        have the reference before the event and the secondary after it,
        shortest baseline first, with browse and viewer links. include_image
        adds the best pair's browse image.
        """
        _caller_may("read")
        try:
            quake = get_earthquake(event_id)
        except requests.RequestException as exc:
            raise ValueError(f"USGS did not answer: {type(exc).__name__}") from exc
        report = store.earthquake_report(
            ctx.frames.get(dataset),
            quake,
            radius_km=radius_km,
            max_dt=max_dt,
            n_pairs=max(0, min(n_pairs, 20)),
            base=ctx.viewer_url.rstrip("/"),
        )
        if not include_image:
            return report
        best = next(
            (f["coseismic_pairs"][0] for f in report["frames"] if f["coseismic_pairs"]),
            None,
        )
        if best is None:
            return report
        img, _ = _image(best["gid"], "browse", 640)
        return [json.dumps(report), img]

    @tool
    def find_volcanoes(
        name: str | None = None,
        bbox: list[float] | None = None,
        country: str | None = None,
        erupted_since: int | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Search the Smithsonian GVP Holocene volcanoes.

        By name, box (west, south, east, north), country or last eruption year.
        """
        _caller_may("read")
        return find_volcano_records(
            ctx.frames.volcanoes(),
            name=name,
            bbox=tuple(bbox) if bbox else None,  # type: ignore[arg-type]
            country=country,
            erupted_since=erupted_since,
            limit=max(1, min(limit, 500)),
        )

    @tool
    def volcano_frames(
        vnum: int,
        dataset: str = "published",
        radius_km: float = 10,
        start: str | None = None,
        end: str | None = None,
        n_pairs: int = 5,
    ) -> dict[str, Any]:
        """Find the frames over a volcano and their GUNW pairs in a date window.

        vnum is the GVP number from find_volcanoes. Pairs come newest first,
        with browse and viewer links.
        """
        _caller_may("read")
        match = [v for v in ctx.frames.volcanoes() if v.get("vnum") == vnum]
        if not match:
            raise ValueError(f"no volcano {vnum}; use find_volcanoes")
        return store.volcano_report(
            ctx.frames.get(dataset),
            match[0],
            radius_km=radius_km,
            start=date.fromisoformat(start) if start else None,
            end=date.fromisoformat(end) if end else None,
            n_pairs=max(0, min(n_pairs, 20)),
            base=ctx.viewer_url.rstrip("/"),
        )

    # -- duplicates ----------------------------------------------------------------
    @tool
    def frame_duplicates(
        key: str,
        dataset: str = "published",
        product: Literal["gslc", "gunw"] = "gslc",
        cycle: str | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> dict[str, Any]:
        """List a frame's duplicate granules: one acquisition delivered more than once.

        GSLC granules repeat when they share date, mode and coverage; GUNW pairs
        when they share both dates, mode and coverage. Each group gives the
        reason (reprocessed = different CRIDs, split = one acquisition in
        pieces, repeat = delivered again) and the granule to keep.
        """
        _caller_may("read")
        _, f = _frame(ctx, dataset, key)
        q = _query(
            product,
            None,
            None,
            None,
            None,
            cycle,
            start,
            end,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        )
        groups = duplicate_groups(q.entries(f["properties"]), product)
        return {
            "id": f["properties"]["id"],
            "frame_idx": f["properties"]["frame_idx"],
            **summarize_duplicates(groups),
            "duplicates": groups,
        }

    @tool
    def scan_duplicates(
        dataset: str = "published",
        product: Literal["gslc", "gunw"] = "gslc",
        track: str | None = None,
        direction: Literal["A", "D"] | None = None,
        bbox: list[float] | None = None,
        cycle: str | None = None,
        start: str | None = None,
        end: str | None = None,
        limit: int = 30,
    ) -> dict[str, Any]:
        """Find frames with duplicate granules, with totals by reason, worst first."""
        _caller_may("read")
        ds = ctx.frames.get(dataset)
        q = _query(
            product,
            track,
            None,
            direction,
            bbox,
            cycle,
            start,
            end,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        )
        return store.duplicates_scan(ds, q, limit=max(1, min(limit, 200)))

    from nisar_db.api.mcp_more import register as register_more

    register_more(tool, ctx, _caller_may, _caller)

    # -- jobs --------------------------------------------------------------------
    @tool
    def list_job_kinds() -> list[dict[str, Any]]:
        """List the nisar-db commands that run as jobs, with their parameter schemas."""
        _caller_may("read")
        return [
            {
                "kind": k.name,
                "summary": k.summary,
                "enabled": k.name not in ctx.jobs.refuse,
                "params": k.params.model_json_schema(),
            }
            for k in KINDS.values()
        ]

    @tool
    def start_job(kind: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Start a nisar-db command (see list_job_kinds); poll job_status for its state.

        An input file may be another job's output, written 'job:<id>/<file>'.
        """
        _caller_may("jobs")
        if kind not in KINDS:
            raise ValueError(f"no job kind {kind!r}; known: {sorted(KINDS)}")
        try:
            job = ctx.jobs.submit(kind, params or {}, _caller().name)
        except ValidationError as exc:
            raise ValueError(str(exc)) from exc
        return job.public()

    @tool
    def job_status(job_id: str | None = None) -> dict[str, Any]:
        """Return a job's state, outputs and log tail; with no id, recent jobs."""
        _caller_may("jobs")
        if job_id is None:
            return {
                "jobs": [
                    {
                        k: j.public()[k]
                        for k in ("id", "kind", "state", "created", "outputs")
                    }
                    for j in ctx.jobs.listed()[:20]
                    if _visible(j.owner)
                ]
            }
        job = ctx.jobs.get(job_id)
        if job is None or not _visible(job.owner):
            raise ValueError(f"no job {job_id!r}")
        return {**job.public(), "log": ctx.jobs.log_tail(job_id, 20)}

    @tool
    def cancel_job(job_id: str) -> dict[str, Any]:
        """Cancel a queued or running job."""
        _caller_may("jobs")
        job = ctx.jobs.get(job_id)
        if job is None or not _visible(job.owner):
            raise ValueError(f"no job {job_id!r}")
        return ctx.jobs.cancel(job_id).public()

    @tool
    def read_job_output(
        job_id: str, name: str, max_chars: int = 20000
    ) -> dict[str, Any]:
        """Read a finished job's text output (CSV, JSON), up to max_chars characters."""
        _caller_may("jobs")
        job = ctx.jobs.get(job_id)
        if job is None or not _visible(job.owner):
            raise ValueError(f"no job {job_id!r}")
        try:
            path = ctx.jobs.output_path(job_id, name)
        except KeyError as exc:
            raise ValueError(
                f"job {job_id} has no output {name!r}; outputs: {job.outputs}"
            ) from exc
        if path.suffix not in (".csv", ".json", ".geojson", ".txt"):
            raise ValueError(
                f"{name} is not a text file; download it from the REST API instead"
            )
        text = path.read_text(errors="replace")
        limit = max(100, min(max_chars, 200000))
        return {
            "name": name,
            "size": len(text),
            "truncated": len(text) > limit,
            "text": text[:limit],
        }

    return server
