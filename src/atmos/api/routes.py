"""Public read-only endpoints.

Every list endpoint is bounded. Nothing here can be asked for the whole archive
in one request, because bulk access is a download, not an API call.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from atmos.api import db
from atmos.api.models import (
    ConsensusSet,
    Divergence,
    Meta,
    Observation,
    Parameter,
    Source,
    Station,
    StationHealth,
)

router = APIRouter()

MAX_ROWS = 5000
DEFAULT_ROWS = 500

# Consensus scans a window rather than a point, so it is capped harder than a
# plain reading listing.
MAX_CONSENSUS_DAYS = 31


def _window(
    start: datetime | None, end: datetime | None, max_days: int
) -> tuple[datetime, datetime]:
    if end is None:
        end = datetime.now().astimezone()
    if start is None:
        start = end - timedelta(days=1)
    if start >= end:
        raise HTTPException(400, "start must be before end")
    if end - start > timedelta(days=max_days):
        raise HTTPException(400, f"window longer than {max_days} days")
    return start, end


@router.get("/meta", response_model=Meta, summary="What this is and how to credit it")
async def meta() -> Meta:
    rows = await db.fetch_all(
        """
        select s.slug, s.name, s.operator, s.tier, s.base_url, s.attribution,
               s.license as licence, s.license_url as licence_url, s.timezone,
               s.is_primary,
               (select count(*) from stations st where st.source_id = s.id) as stations
          from sources s
         order by s.slug
        """
    )
    return Meta(sources=[Source(**r) for r in rows])


@router.get("/sources", response_model=list[Source], summary="Who publishes what")
async def sources() -> list[Source]:
    rows = await db.fetch_all(
        """
        select s.slug, s.name, s.operator, s.tier, s.base_url, s.attribution,
               s.license as licence, s.license_url as licence_url, s.timezone,
               s.is_primary,
               (select count(*) from stations st where st.source_id = s.id) as stations
          from sources s
         order by s.slug
        """
    )
    return [Source(**r) for r in rows]


@router.get("/parameters", response_model=list[Parameter], summary="What is measured")
async def parameters() -> list[Parameter]:
    rows = await db.fetch_all(
        """
        select p.code, p.canonical_unit,
               coalesce(t.allows_negative, false) as allows_negative
          from parameters p
          left join quality_thresholds t on t.parameter_id = p.id
         order by p.code
        """
    )
    return [Parameter(**r) for r in rows]


@router.get("/stations", response_model=list[Station], summary="Where measurements come from")
async def stations(
    source: Annotated[str | None, Query(description="Source slug")] = None,
    parameter: Annotated[str | None, Query(description="Only stations publishing this")] = None,
    near_lat: Annotated[float | None, Query(ge=-90, le=90)] = None,
    near_lon: Annotated[float | None, Query(ge=-180, le=180)] = None,
    radius_m: Annotated[int, Query(ge=1, le=200_000)] = 10_000,
    limit: Annotated[int, Query(ge=1, le=MAX_ROWS)] = DEFAULT_ROWS,
) -> list[Station]:
    if (near_lat is None) != (near_lon is None):
        raise HTTPException(400, "near_lat and near_lon must be given together")

    rows = await db.fetch_all(
        """
        select st.id, so.slug as source, st.source_station_id, st.name,
               st.operator,
               st_y(st.geom::geometry) as latitude,
               st_x(st.geom::geometry) as longitude,
               st.elevation_m, st.station_type, st.area_type,
               st.is_indoor, st.is_mobile, st.location_precise,
               nullif(canonical_station(st.id, now()), st.id) as canonical_station_id,
               st.first_seen_at, st.last_seen_at
          from stations st
          join sources so on so.id = st.source_id
         where (%s::text is null or so.slug = %s::text)
           and (%s::text is null or exists (
                 select 1 from station_parameters sp
                   join parameters p on p.id = sp.parameter_id
                  where sp.station_id = st.id and p.code = %s::text))
           and (%s::float8 is null or (
                 st.geom is not null
                 and st_dwithin(st.geom,
                                st_setsrid(st_makepoint(%s, %s), 4326)::geography,
                                %s)))
         order by st.name
         limit %s
        """,
        (source, source, parameter, parameter,
         near_lat, near_lon, near_lat, radius_m, limit),
    )
    return [Station(**r) for r in rows]


@router.get("/stations/{station_id}", response_model=Station, summary="One station")
async def station(station_id: int) -> Station:
    row = await db.fetch_one(
        """
        select st.id, so.slug as source, st.source_station_id, st.name,
               st.operator,
               st_y(st.geom::geometry) as latitude,
               st_x(st.geom::geometry) as longitude,
               st.elevation_m, st.station_type, st.area_type,
               st.is_indoor, st.is_mobile, st.location_precise,
               nullif(canonical_station(st.id, now()), st.id) as canonical_station_id,
               st.first_seen_at, st.last_seen_at
          from stations st
          join sources so on so.id = st.source_id
         where st.id = %s
        """,
        (station_id,),
    )
    if row is None:
        raise HTTPException(404, "no such station")
    return Station(**row)


@router.get(
    "/observations",
    response_model=list[Observation],
    summary="Readings as published, including every revision",
)
async def observations(
    station_id: Annotated[int, Query(description="Station to read")],
    parameter: Annotated[str, Query(description="Parameter code, such as pm10")],
    start: datetime | None = None,
    end: datetime | None = None,
    revisions: Annotated[
        bool, Query(description="Include later revisions, not only the first value")
    ] = False,
    limit: Annotated[int, Query(ge=1, le=MAX_ROWS)] = DEFAULT_ROWS,
) -> list[Observation]:
    start, end = _window(start, end, max_days=366)
    rows = await db.fetch_all(
        """
        select o.station_id, p.code as parameter, o.phenomenon_start,
               o.phenomenon_end, o.value, o.unit, o.raw_value, o.raw_unit,
               o.revision, o.revision_kind, o.previous_value, o.confirmations,
               coalesce(o.quality_flags, '{}')
                 || coalesce(array(select f.flag from observation_flags f
                       where f.station_id = o.station_id
                         and f.parameter_id = o.parameter_id
                         and f.phenomenon_start = o.phenomenon_start
                         and f.phenomenon_end = o.phenomenon_end
                         and f.revision = o.revision), '{}') as quality_flags
          from observations o
          join parameters p on p.id = o.parameter_id
         where o.station_id = %s
           and p.code = %s
           and o.phenomenon_start >= %s
           and o.phenomenon_start <  %s
           and (%s::bool or o.revision = 1)
         order by o.phenomenon_start desc, o.revision
         limit %s
        """,
        (station_id, parameter, start, end, revisions, limit),
    )
    return [Observation(**r) for r in rows]


@router.get(
    "/consensus",
    response_model=list[ConsensusSet],
    summary="What nearby instruments agreed on, and how firmly",
)
async def consensus(
    parameter: str,
    start: datetime | None = None,
    end: datetime | None = None,
    radius_m: Annotated[int, Query(ge=100, le=50_000)] = 5000,
    sources: Annotated[
        list[str] | None,
        Query(description="Limit to these sources. Every figure recomputes against it"),
    ] = None,
    bucket_minutes: Annotated[int, Query(ge=1, le=1440)] = 60,
    station_id: Annotated[int | None, Query(description="Only this station's set")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_ROWS)] = DEFAULT_ROWS,
) -> list[ConsensusSet]:
    start, end = _window(start, end, MAX_CONSENSUS_DAYS)
    rows = await db.fetch_all(
        """
        select * from consensus(%s, %s, %s, %s, %s, make_interval(mins => %s))
         where (%s::bigint is null or anchor_station_id = %s::bigint)
         order by bucket_start desc, anchor_station_id
         limit %s
        """,
        (parameter, start, end, radius_m, sources, bucket_minutes,
         station_id, station_id, limit),
    )
    return [ConsensusSet(**r) for r in rows]


@router.get(
    "/divergence",
    response_model=list[Divergence],
    summary="How far a station sits from the instruments around it",
)
async def divergence(
    parameter: str,
    start: datetime | None = None,
    end: datetime | None = None,
    radius_m: Annotated[int, Query(ge=100, le=50_000)] = 5000,
    sources: list[str] | None = None,
    bucket_minutes: Annotated[int, Query(ge=1, le=1440)] = 60,
    station_id: int | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_ROWS)] = DEFAULT_ROWS,
) -> list[Divergence]:
    start, end = _window(start, end, MAX_CONSENSUS_DAYS)
    rows = await db.fetch_all(
        """
        select * from divergence(%s, %s, %s, %s, %s, make_interval(mins => %s))
         where (%s::bigint is null or station_id = %s::bigint)
         order by bucket_start desc, station_id
         limit %s
        """,
        (parameter, start, end, radius_m, sources, bucket_minutes,
         station_id, station_id, limit),
    )
    return [Divergence(**r) for r in rows]


@router.get(
    "/station-health",
    response_model=list[StationHealth],
    summary="How much of what a station publishes looks questionable",
)
async def station_health(
    days: Annotated[int, Query(ge=1, le=3650)] = 7,
    source: str | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_ROWS)] = DEFAULT_ROWS,
) -> list[StationHealth]:
    rows = await db.fetch_all(
        """
        with r as (
          select o.station_id, o.parameter_id, o.phenomenon_start,
                 o.phenomenon_end, o.revision, o.value
            from observations o
           where o.revision = 1
             and o.phenomenon_start >= now() - make_interval(days => %s)
        )
        select r.station_id, so.slug as source, st.name as station,
               p.code as parameter,
               count(*) as readings,
               count(*) filter (where r.value = 0) as zeros,
               count(f.flag) as flagged,
               max(r.phenomenon_start) as last_reading
          from r
          join stations st on st.id = r.station_id
          join sources so on so.id = st.source_id
          join parameters p on p.id = r.parameter_id
          left join observation_flags f
            on f.station_id = r.station_id
           and f.parameter_id = r.parameter_id
           and f.phenomenon_start = r.phenomenon_start
           and f.phenomenon_end = r.phenomenon_end
           and f.revision = r.revision
         where (%s::text is null or so.slug = %s::text)
         group by r.station_id, so.slug, st.name, p.code
         order by (count(*) filter (where r.value = 0))::numeric / count(*) desc
         limit %s
        """,
        (days, source, source, limit),
    )
    return [StationHealth(**r) for r in rows]
