"""``nisar-db`` commands as background jobs.

Each job runs one ``nisar-db`` subcommand as its own process (the argparse
commands rewrite ``sys.argv``, and a CMR crawl or catalog build should not
share the server's memory) in its own folder under ``<cache>/jobs/<id>``,
where its outputs, its log and a ``job.json`` record stay. At most
``max_jobs`` run at once; the rest wait their turn.

Input files are paths on the server. A shared service accepts only paths
under its allowed roots (the checkout's ``catalog/``, the jobs folder, or
another job's outputs as ``job:<id>/<file>``), so a key cannot read arbitrary
files; local mode takes any path.
"""

from __future__ import annotations

import json
import secrets
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Job kinds: their parameters and the command line they become
# ---------------------------------------------------------------------------


class _Params(BaseModel):
    model_config = {"extra": "forbid"}


class SearchParams(_Params):
    """``nisar-db search``: CMR search, results written to ``results.csv``."""

    product_type: Literal["GSLC", "GUNW"] = "GSLC"
    bbox: tuple[float, float, float, float] | None = None
    track: int | None = None
    frame: int | None = None
    direction: Literal["A", "D"] | None = None
    cycle: int | None = None
    polarization: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    max_results: int = Field(1000, ge=0, description="0 = the whole matching archive")


class ConsistentParams(_Params):
    """``nisar-db create-consistent`` -> ``consistent.json``."""

    catalog: str = Field(..., description="GSLC catalog CSV (create-gslc-csv)")
    nisar_gpkg: str = Field(..., description="filtered NISAR frames GeoPackage")
    blackout_file: str | None = None
    keep_nonstandard_modes: bool = False


class BlackoutParams(_Params):
    """``nisar-db create-blackout-dates`` -> ``blackout-dates.json``."""

    source: Literal["manual", "snow", "monthly"] = "manual"
    input_file: str | None = None
    start_year: int | None = None
    end_year: int | None = None
    max_default_duration: int | None = None


class ReferenceParams(_Params):
    """``nisar-db create-reference-dates`` -> ``reference-dates.json``."""

    consistent_json: str
    blackout_file: str | None = None
    interval: float | None = None
    min_acquisitions: int | None = None


class FrameToBoundParams(_Params):
    """``nisar-db create-frame-to-bound`` -> ``frame_to_bound.json`` (+ GeoJSON)."""

    nisar_gpkg: str | None = Field(
        None, description="omit to download the TrackFrame GeoPackage"
    )
    geojson: bool = False
    simplify_tolerance: float = 0.1


class CatalogParams(_Params):
    """``nisar-db create-nisar-catalog`` -> DuckDB + JSON catalogs in the job folder."""

    product: Literal["gslc", "gunw", "all"] = "all"
    max_results: int = Field(0, ge=0, description="0 = the whole archive")


class ProcessingModeParams(_Params):
    """``nisar-db label-processing-mode`` -> ``labelled.json``."""

    consistent_json: str
    previous_json: str | None = None
    batch_size: int | None = None
    gap_threshold_years: float | None = None


class QueryParams(_Params):
    """``nisar-db query-catalog`` on an S3 catalog -> ``matches.csv``."""

    catalog: str
    product_type: Literal["GSLC", "GUNW"] | None = None
    track: int | None = None
    frame: int | None = None
    direction: Literal["A", "D"] | None = None
    cycle: int | None = None
    polarization: str | None = None
    mode: str | None = None
    crid: str | None = None
    crid_min: str | None = None


class DownloadParams(_Params):
    """``nisar-db download`` of CMR granules into the job folder (heavy)."""

    granule_ids: list[str] = Field(..., min_length=1, max_length=500)
    timeout: int = 60


class DispAssetsParams(_Params):
    """``nisar-db build-disp-assets``: the DISP-NISAR release assets (heavy)."""

    max_results: int = Field(0, ge=0, description="cap on CMR results; 0 = all")
    blackout_file: str | None = Field(
        None, description="blackout JSON (default: the repo's)"
    )
    snow_geojson: str | None = Field(
        None, description="derive the blackout dates from this instead"
    )
    previous_consistent: str | None = Field(
        None, description="previous consistent-GSLC JSON"
    )
    trackframe_gpkg: str | None = Field(
        None, description="reuse a TrackFrame GeoPackage"
    )


class S3CatalogParams(_Params):
    """``nisar-db build-s3-catalog`` -> ``catalog.parquet`` (heavy)."""

    bucket: str
    prefix: str = ""
    product_type: Literal["GSLC", "GUNW"] = "GSLC"
    profile: str | None = None
    region: str = "us-west-2"


def _opt(args: list[str], flag: str, value: object) -> None:
    if value is None or value is False:
        return
    if value is True:
        args.append(flag)
    elif isinstance(value, (list, tuple)):
        args += [flag, ",".join(str(v) for v in value)]
    else:
        args += [flag, str(value)]


@dataclass(frozen=True)
class JobKind:
    """A job kind: its parameter model and how it becomes a command line."""

    name: str
    params: type[_Params]
    #: Field names holding input file paths, resolved against the allowed roots.
    inputs: tuple[str, ...]
    argv: Callable[[_Params, Callable[[str], str]], list[str]]
    #: Files (globs) in the job folder to list as outputs.
    outputs: tuple[str, ...]
    summary: str


def _search(p: SearchParams, _r: Callable[[str], str]) -> list[str]:
    a = ["search", "--output-csv", "results.csv", "--product-type", p.product_type]
    _opt(a, "--bbox", p.bbox)
    for flag, v in (
        ("--track", p.track),
        ("--frame", p.frame),
        ("--direction", p.direction),
        ("--cycle", p.cycle),
        ("--polarization", p.polarization),
        ("--start-date", p.start_date),
        ("--end-date", p.end_date),
    ):
        _opt(a, flag, v)
    return [*a, "--max-results", str(p.max_results)]


def _consistent(p: ConsistentParams, r: Callable[[str], str]) -> list[str]:
    a = [
        "create-consistent",
        "--catalog",
        r(p.catalog),
        "--nisar-gpkg",
        r(p.nisar_gpkg),
        "--output",
        "consistent.json",
    ]
    if p.blackout_file:
        a += ["--blackout-file", r(p.blackout_file)]
    _opt(a, "--keep-nonstandard-modes", p.keep_nonstandard_modes)
    return a


def _blackout(p: BlackoutParams, r: Callable[[str], str]) -> list[str]:
    a = ["create-blackout-dates", "--output-file", "blackout-dates.json"]
    if p.source == "manual":
        a.append("--manual")
    else:
        if not p.input_file:
            raise ValueError(f"source {p.source!r} needs input_file")
        a += ["--input-file", r(p.input_file)]
        if p.source == "monthly":
            a.append("--monthly")
    for flag, v in (
        ("--start-year", p.start_year),
        ("--end-year", p.end_year),
        ("--max-default-duration", p.max_default_duration),
    ):
        _opt(a, flag, v)
    return a


def _reference(p: ReferenceParams, r: Callable[[str], str]) -> list[str]:
    a = [
        "create-reference-dates",
        "--consistent-json",
        r(p.consistent_json),
        "--output",
        "reference-dates.json",
    ]
    if p.blackout_file:
        a += ["--blackout-file", r(p.blackout_file)]
    _opt(a, "--interval", p.interval)
    _opt(a, "--min-acquisitions", p.min_acquisitions)
    return a


def _frame_to_bound(p: FrameToBoundParams, r: Callable[[str], str]) -> list[str]:
    a = [
        "create-frame-to-bound",
        "--output",
        "frame_to_bound.json",
        "--simplify-tolerance",
        str(p.simplify_tolerance),
    ]
    if p.nisar_gpkg:
        a += ["--nisar-gpkg", r(p.nisar_gpkg)]
    else:
        a += ["--gpkg-dir", "."]
    if p.geojson:
        a += ["--geojson", "frames.geojson"]
    return a


def _catalog(p: CatalogParams, _r: Callable[[str], str]) -> list[str]:
    a = [
        "create-nisar-catalog",
        "--output-dir",
        "catalog",
        "--gslc-db",
        "gslc.duckdb",
        "--gunw-db",
        "gunw.duckdb",
        f"--{p.product}",
    ]
    return [*a, "--max-results", str(p.max_results)]


def _processing_mode(p: ProcessingModeParams, r: Callable[[str], str]) -> list[str]:
    a = [
        "label-processing-mode",
        "--consistent-json",
        r(p.consistent_json),
        "--output",
        "labelled.json",
    ]
    if p.previous_json:
        a += ["--previous-json", r(p.previous_json)]
    _opt(a, "--batch-size", p.batch_size)
    _opt(a, "--gap-threshold-years", p.gap_threshold_years)
    return a


def _query(p: QueryParams, r: Callable[[str], str]) -> list[str]:
    a = ["query-catalog", r(p.catalog), "--output-csv", "matches.csv"]
    for flag, v in (
        ("--product-type", p.product_type),
        ("--track", p.track),
        ("--frame", p.frame),
        ("--direction", p.direction),
        ("--cycle", p.cycle),
        ("--polarization", p.polarization),
        ("--mode", p.mode),
        ("--crid", p.crid),
        ("--crid-min", p.crid_min),
    ):
        _opt(a, flag, v)
    return a


def _download(p: DownloadParams, _r: Callable[[str], str]) -> list[str]:
    # The id list goes to a file the job folder keeps (written by ``Jobs.submit``).
    return [
        "download",
        "--granule-list",
        "granules.txt",
        "--output-dir",
        ".",
        "--no-progress",
        "--timeout",
        str(p.timeout),
    ]


def _disp_assets(p: DispAssetsParams, r: Callable[[str], str]) -> list[str]:
    a = ["build-disp-assets", "--out-dir", ".", "--max-results", str(p.max_results)]
    for flag, v in (
        ("--blackout-file", p.blackout_file),
        ("--snow-geojson", p.snow_geojson),
        ("--previous-consistent", p.previous_consistent),
        ("--trackframe-gpkg", p.trackframe_gpkg),
    ):
        if v:
            a += [flag, r(v)]
    return a


def _s3_catalog(p: S3CatalogParams, _r: Callable[[str], str]) -> list[str]:
    a = [
        "build-s3-catalog",
        "--bucket",
        p.bucket,
        "--output",
        "catalog.parquet",
        "--product-type",
        p.product_type,
        "--region",
        p.region,
    ]
    if p.prefix:
        a += ["--prefix", p.prefix]
    _opt(a, "--profile", p.profile)
    return a


KINDS: dict[str, JobKind] = {
    k.name: k
    for k in (
        JobKind(
            "search",
            SearchParams,
            (),
            _search,
            ("results.csv",),
            "Search CMR for GSLC / GUNW products",
        ),
        JobKind(
            "create-consistent",
            ConsistentParams,
            ("catalog", "nisar_gpkg", "blackout_file"),
            _consistent,
            ("consistent.json*",),
            "Consistent-GSLC JSON for DISP",
        ),
        JobKind(
            "create-blackout-dates",
            BlackoutParams,
            ("input_file",),
            _blackout,
            ("blackout-dates.json*",),
            "Blackout-dates JSON",
        ),
        JobKind(
            "create-reference-dates",
            ReferenceParams,
            ("consistent_json", "blackout_file"),
            _reference,
            ("reference-dates.json*",),
            "Reference-dates JSON",
        ),
        JobKind(
            "create-frame-to-bound",
            FrameToBoundParams,
            ("nisar_gpkg",),
            _frame_to_bound,
            ("frame_to_bound.json*", "frames.geojson", "*.gpkg"),
            "Frame-to-bounds JSON",
        ),
        JobKind(
            "create-nisar-catalog",
            CatalogParams,
            (),
            _catalog,
            ("catalog/*", "*.duckdb"),
            "GSLC / GUNW catalogs from CMR",
        ),
        JobKind(
            "label-processing-mode",
            ProcessingModeParams,
            ("consistent_json", "previous_json"),
            _processing_mode,
            ("labelled.json*",),
            "Historical / forward processing-mode labels",
        ),
        JobKind(
            "query-catalog",
            QueryParams,
            ("catalog",),
            _query,
            ("matches.csv",),
            "Filter an S3 catalog",
        ),
        JobKind(
            "download",
            DownloadParams,
            (),
            _download,
            ("*.h5",),
            "Download granules (heavy)",
        ),
        JobKind(
            "build-disp-assets",
            DispAssetsParams,
            ("blackout_file", "snow_geojson", "previous_consistent", "trackframe_gpkg"),
            _disp_assets,
            ("opera-nisar-disp-*", "gslc_catalog.csv"),
            "DISP-NISAR release assets from CMR (heavy)",
        ),
        JobKind(
            "build-s3-catalog",
            S3CatalogParams,
            (),
            _s3_catalog,
            ("catalog.parquet",),
            "Catalog an S3 bucket (heavy, needs AWS credentials)",
        ),
    )
}

# ---------------------------------------------------------------------------
# Running them
# ---------------------------------------------------------------------------

JobState = Literal["queued", "running", "done", "failed", "cancelled"]


@dataclass
class Job:
    """One job's record, mirrored to ``job.json`` in its folder."""

    id: str
    kind: str
    params: dict
    owner: str
    argv: list[str]
    state: JobState = "queued"
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    returncode: int | None = None
    error: str | None = None
    outputs: list[str] = field(default_factory=list)

    def public(self) -> dict:
        """Return the record as the API shows it."""
        return asdict(self)


#: ``launcher(argv, cwd, log) -> Popen-like``: the seam tests replace.
Launcher = Callable[[list[str], Path, Path], "subprocess.Popen"]


def default_launcher(argv: list[str], cwd: Path, log: Path) -> subprocess.Popen:
    """Start ``python -m nisar_db.cli <argv>`` in ``cwd``, logging to ``log``."""
    out = log.open("w")
    return subprocess.Popen(
        [sys.executable, "-u", "-m", "nisar_db.cli", *argv],
        cwd=cwd,
        stdout=out,
        stderr=subprocess.STDOUT,
        text=True,
    )


class Jobs:
    """Submit, run (``max_jobs`` at a time), list and cancel jobs."""

    def __init__(
        self,
        root: Path,
        *,
        max_jobs: int = 2,
        allowed_roots: tuple[Path, ...] | None = None,
        refuse: frozenset[str] = frozenset(),
        launcher: Launcher = default_launcher,
    ) -> None:
        """Keep jobs under ``root``, reloading the records already there."""
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_jobs = max(1, max_jobs)
        self.allowed_roots = allowed_roots
        self.refuse = refuse
        self.launcher = launcher
        self._jobs: dict[str, Job] = {}
        self._procs: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()
        self._load()

    # -- records ----------------------------------------------------------
    def _load(self) -> None:
        for rec in sorted(self.root.glob("*/job.json")):
            try:
                job = Job(**json.loads(rec.read_text()))
            except (ValueError, TypeError):
                continue
            # A job the last server left running did not finish.
            if job.state in ("queued", "running"):
                job.state, job.error = "failed", "the server stopped while it ran"
            self._jobs[job.id] = job

    def _save(self, job: Job) -> None:
        # Written aside and swapped in, so a reader never sees half a record.
        path = self.root / job.id / "job.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(job.public(), indent=1))
        tmp.replace(path)

    def folder(self, job_id: str) -> Path:
        """Return a job's folder."""
        return self.root / job_id

    def get(self, job_id: str) -> Job | None:
        """Return a job by id."""
        return self._jobs.get(job_id)

    def listed(self, owner: str | None = None) -> list[Job]:
        """Return jobs, newest first, optionally only ``owner``'s."""
        jobs = sorted(self._jobs.values(), key=lambda j: j.created, reverse=True)
        return [j for j in jobs if owner is None or j.owner == owner]

    # -- inputs -------------------------------------------------------------
    def resolve(self, value: str) -> str:
        """Return an input path as the job will read it.

        ``job:<id>/<file>`` names another job's output. Shared services accept
        only paths inside their allowed roots.

        Raises
        ------
        ValueError
            For a missing file, or one outside the allowed roots.

        """
        if value.startswith("job:"):
            job_id, _, name = value[4:].partition("/")
            path = (self.folder(job_id) / name).resolve()
        else:
            path = Path(value).expanduser().resolve()
        if self.allowed_roots is not None and not any(
            path.is_relative_to(Path(r).resolve())
            for r in (*self.allowed_roots, self.root)
        ):
            raise ValueError(
                f"{value!r} is outside the folders this service reads from"
            )
        if not path.exists():
            raise ValueError(f"no such file: {value!r}")
        return str(path)

    # -- lifecycle ------------------------------------------------------------
    def submit(self, kind: str, params: dict, owner: str) -> Job:
        """Validate ``params`` for ``kind`` and queue the job.

        Raises
        ------
        KeyError
            For an unknown kind.
        PermissionError
            For a kind this service refuses.
        ValueError
            For bad parameters or input files (pydantic's error is a
            ``ValueError``).

        """
        spec = KINDS[kind]
        if kind in self.refuse:
            raise PermissionError(f"this service does not run {kind!r} jobs")
        p = spec.params(**params)
        argv = spec.argv(p, self.resolve)
        job_id = f"{kind}-{time.strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(3)}"
        job = Job(
            id=job_id,
            kind=kind,
            params=p.model_dump(mode="json"),
            owner=owner,
            argv=argv,
        )
        self.folder(job_id).mkdir(parents=True)
        if isinstance(p, DownloadParams):
            (self.folder(job_id) / "granules.txt").write_text(
                "\n".join(p.granule_ids) + "\n"
            )
        with self._lock:
            self._jobs[job_id] = job
            self._save(job)
        self._pump()
        return job

    def _pump(self) -> None:
        """Start queued jobs while fewer than ``max_jobs`` run."""
        with self._lock:
            running = sum(j.state == "running" for j in self._jobs.values())
            queued = sorted(
                (j for j in self._jobs.values() if j.state == "queued"),
                key=lambda j: j.created,
            )
            to_start = queued[: max(0, self.max_jobs - running)]
            for job in to_start:
                job.state, job.started = "running", time.time()
                self._save(job)
        for job in to_start:
            threading.Thread(target=self._run, args=(job,), daemon=True).start()

    def _run(self, job: Job) -> None:
        folder = self.folder(job.id)
        try:
            proc = self.launcher(job.argv, folder, folder / "log.txt")
            with self._lock:
                self._procs[job.id] = proc
                # A cancel that came while the process started found nothing
                # to stop yet.
                cancelled = job.state == "cancelled"
            if cancelled:
                proc.terminate()
            rc = proc.wait()
        except OSError as exc:
            rc, job.error = -1, f"could not start: {exc}"
        # Everything a finished job reports is in place before its state says
        # so: readers poll ``get`` without the lock.
        outputs = self._outputs(job)
        error = job.error
        if rc != 0 and not error:
            tail = self.log_tail(job.id, 1)
            error = tail[0] if tail else f"exit code {rc}"
        with self._lock:
            self._procs.pop(job.id, None)
            job.returncode, job.outputs, job.finished = rc, outputs, time.time()
            if job.state != "cancelled":
                job.error = error
                job.state = "done" if rc == 0 else "failed"
            self._save(job)
        self._pump()

    def _outputs(self, job: Job) -> list[str]:
        folder = self.folder(job.id)
        found: set[str] = set()
        for pattern in KINDS[job.kind].outputs:
            found.update(
                str(p.relative_to(folder)) for p in folder.glob(pattern) if p.is_file()
            )
        return sorted(found)

    def cancel(self, job_id: str) -> Job:
        """Stop a queued or running job.

        Raises
        ------
        KeyError
            For an unknown job.

        """
        job = self._jobs[job_id]
        with self._lock:
            if job.state in ("queued", "running"):
                job.state, job.finished = "cancelled", time.time()
                proc = self._procs.get(job_id)
                if proc is not None:
                    proc.terminate()
                self._save(job)
        return job

    def log_tail(self, job_id: str, lines: int = 50) -> list[str]:
        """Return the last ``lines`` lines of a job's log."""
        log = self.folder(job_id) / "log.txt"
        if not log.exists():
            return []
        return [
            ln for ln in log.read_text(errors="replace").splitlines() if ln.strip()
        ][-lines:]

    def output_path(self, job_id: str, name: str) -> Path:
        """Return one of a job's output files.

        Raises
        ------
        KeyError
            For an unknown job or a file it did not produce.

        """
        job = self._jobs[job_id]
        if name not in job.outputs:
            raise KeyError(name)
        return self.folder(job_id) / name
