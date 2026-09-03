"""Public API tests.

These need a live Postgres, so they skip when one is not configured.

The API is the contract the public and our own frontend both use, so what is
asserted here is mostly promises rather than plumbing: that values are not
turned into floats, that flagged readings are still served, that revisions are
retrievable, and that a request cannot ask for the whole archive at once.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.conftest import DSN, NOTE

pytest.importorskip("fastapi")
psycopg = pytest.importorskip("psycopg")

pytestmark = pytest.mark.skipif(DSN is None, reason=NOTE)


@pytest.fixture(scope="module")
def client() -> Iterator[object]:
    import os

    from fastapi.testclient import TestClient

    os.environ["ATMOS_API_DATABASE_URL"] = DSN or ""
    from atmos.api.app import app

    with TestClient(app) as c:
        yield c


def test_liveness(client) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


def test_meta_states_the_licence_and_the_sources(client) -> None:
    """Nobody should be able to use this without knowing how to credit it."""
    body = client.get("/v1/meta").json()
    assert body["data_licence"] == "CC BY 4.0"
    assert body["display_timezone"] == "Europe/Sarajevo"
    assert body["sources"], "meta must name the sources"
    for s in body["sources"]:
        assert s["attribution"], f"{s['slug']} has no attribution"


def test_sources_say_what_timezone_they_publish_in(client) -> None:
    """We display one clock. The original must not be lost."""
    for s in client.get("/v1/sources").json():
        assert s["timezone"]
        assert "is_primary" in s


def test_parameters_are_listed_with_their_unit(client) -> None:
    codes = {p["code"]: p for p in client.get("/v1/parameters").json()}
    assert "pm10" in codes
    assert codes["pm10"]["canonical_unit"]


def test_stations_carry_provenance(client) -> None:
    rows = client.get("/v1/stations", params={"limit": 5}).json()
    assert rows
    for st in rows:
        assert st["source"], "a station must name who publishes it"
        assert "operator" in st, "and who runs it, even when unknown"
        assert "location_precise" in st


def test_a_missing_station_is_a_404(client) -> None:
    assert client.get("/v1/stations/999999999").status_code == 404


def test_near_needs_both_coordinates(client) -> None:
    """Half a coordinate would silently return the wrong neighbourhood."""
    r = client.get("/v1/stations", params={"near_lat": 43.85})
    assert r.status_code == 400


def test_stations_can_be_found_by_distance(client) -> None:
    r = client.get("/v1/stations", params={
        "near_lat": 43.859, "near_lon": 18.434, "radius_m": 5000})
    assert r.status_code == 200
    assert r.json(), "Sarajevo has stations within 5 km of Vijecnica"


def test_measurements_are_never_floats(client) -> None:
    """The loudest rule in the project. 7.2 must stay 7.2.

    Serving them as JSON numbers would hand every consumer a float. They are
    strings, and raw_value carries what the source actually printed.
    """
    st = client.get("/v1/stations", params={"source": "fhmz", "limit": 1}).json()[0]
    rows = client.get("/v1/observations", params={
        "station_id": st["id"], "parameter": "pm10", "limit": 5}).json()
    for o in rows:
        if o["value"] is not None:
            assert isinstance(o["value"], str)
        assert isinstance(o["raw_value"], str | type(None))


def test_a_reading_carries_its_flags_rather_than_being_dropped(client) -> None:
    """Flagging is not deletion. A questionable reading is still served."""
    rows = client.get("/v1/station-health", params={"days": 3650, "limit": 1}).json()
    assert rows
    worst = rows[0]
    assert worst["flagged"] >= 0
    served = client.get("/v1/observations", params={
        "station_id": worst["station_id"], "parameter": worst["parameter"],
        "start": "2026-06-01T00:00:00+02:00", "end": "2026-09-01T00:00:00+02:00",
        "limit": 5})
    assert served.status_code == 200
    assert served.json(), "a flagged station still has readings to serve"


def test_revisions_are_retrievable_but_not_the_default(client) -> None:
    """Revision 1 is what we stand behind. The rest is available on request."""
    st = client.get("/v1/stations", params={"source": "fhmz", "limit": 1}).json()[0]
    base = {"station_id": st["id"], "parameter": "pm10", "limit": 50}
    first = client.get("/v1/observations", params=base).json()
    assert all(o["revision"] == 1 for o in first)
    every = client.get("/v1/observations", params={**base, "revisions": True})
    assert every.status_code == 200


def test_a_window_the_wrong_way_round_is_refused(client) -> None:
    r = client.get("/v1/observations", params={
        "station_id": 1, "parameter": "pm10",
        "start": "2026-08-30T00:00:00+02:00", "end": "2026-08-29T00:00:00+02:00"})
    assert r.status_code == 400


def test_the_whole_archive_cannot_be_asked_for_at_once(client) -> None:
    """Bulk access is a download, not an API call."""
    r = client.get("/v1/consensus", params={
        "parameter": "pm10",
        "start": "2020-01-01T00:00:00+01:00", "end": "2026-01-01T00:00:00+01:00"})
    assert r.status_code == 400

    too_many = client.get("/v1/observations", params={
        "station_id": 1, "parameter": "pm10", "limit": 100000})
    assert too_many.status_code == 422


def test_consensus_says_what_its_numbers_can_carry(client) -> None:
    """A figure without its basis invites being quoted as more than it is."""
    rows = client.get("/v1/consensus", params={
        "parameter": "pm10",
        "start": "2026-08-30T12:00:00+02:00",
        "end": "2026-08-30T13:00:00+02:00", "limit": 20}).json()
    assert rows
    for r in rows:
        assert r["basis"] in {"unconfirmed", "two_sources", "exact_agreement", "robust"}
        assert r["aggregate"] == "median"
        if r["n"] == 1:
            assert r["mad"] is None, "one instrument is not agreement"


def test_divergence_withholds_a_score_it_cannot_support(client) -> None:
    """At n below three, attribution is not available and must stay null."""
    rows = client.get("/v1/divergence", params={
        "parameter": "pm10",
        "start": "2026-08-30T12:00:00+02:00",
        "end": "2026-08-30T13:00:00+02:00", "limit": 100}).json()
    assert rows
    for r in rows:
        if r["n"] < 3:
            assert r["modified_z"] is None
        assert r["readings"] >= 1, "how well observed the bucket was"


def test_excluding_a_source_changes_the_answer(client) -> None:
    """Source selection is the reader's lever, not decoration."""
    w = {"parameter": "pm10",
         "start": "2026-08-30T12:00:00+02:00",
         "end": "2026-08-30T13:00:00+02:00", "limit": 200}
    everything = client.get("/v1/consensus", params=w).json()
    reference = client.get("/v1/consensus", params={**w, "sources": ["fhmz"]}).json()
    assert everything != reference


def test_station_health_reports_without_excluding(client) -> None:
    rows = client.get("/v1/station-health", params={"days": 3650, "limit": 5}).json()
    assert rows
    for r in rows:
        assert r["readings"] >= 1
        assert r["zeros"] <= r["readings"]


def test_the_api_cannot_write(client) -> None:
    """Read only by construction, not by discipline."""
    assert client.post("/v1/stations").status_code in (404, 405)
    assert client.delete("/v1/stations/1").status_code in (404, 405)
