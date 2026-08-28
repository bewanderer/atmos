# Atmos

An open archive of air quality and weather measurements for Bosnia and Herzegovina.

Atmos collects published environmental measurements from as many sources as it can reach,
keeps the original responses, and records every reading with enough context that anyone can
trace it back to where it came from.

**Status: early development.** Nothing here is production ready yet.

## What it does

**Collects widely.** Official monitoring networks, cantonal and municipal networks, community
sensors, satellite retrievals and atmospheric models. Where several sources publish readings
for the same place, all of them are kept. None is merged away.

**Keeps the original.** Every request is archived exactly as received, with a timestamp and a
checksum. Parsing happens separately, over the archive. If a parser breaks because a source
changes layout, no data is lost and the archive is simply reprocessed once the parser is fixed.

**Never overwrites.** Observations are append only, and that is enforced by database grants
rather than by convention. If a source later publishes a different value for the same station,
parameter and hour, the new value is stored as an additional revision alongside the original.
The first value stays canonical, both are visible, and the change is recorded permanently.
A value that disappears upstream is recorded too, as a withdrawal.

Several sources state openly that their published data is unvalidated and may be amended
later. Revisions are therefore expected and normal. Atmos makes them visible and countable
rather than characterising them.

**Records provenance.** Every reading carries the station and its coordinates, the instrument
and method where the operator discloses it, the source and its operator, the exact URL
fetched, the fetch time, the parser version, and a link to the archived response. Nothing is
published that cannot be traced this way.

**Compares sources.** Where several sources report the same parameter for the same place and
hour, Atmos computes robust statistics across them, so agreement and disagreement are both
visible. Sources can be included or excluded and the statistics recompute.

## Design notes

**Concentrations, not indices.** Only measured concentrations are stored as observations.
Air quality indices are derived, versioned, and labelled with the scale used, because
different publishers apply different index standards to the same underlying measurements and
arrive at different numbers. An index published by a source is recorded as that source's
claim, never as a measurement.

**Aggregators are never a source of record.** Republished data is collected and clearly
labelled, but a primary measurement always takes precedence.

**Measured, modelled and derived are never mixed.** Every value carries a tier, visible in the
interface and filterable in the API.

## Running your own instance

Atmos is designed to be self hosted, and running your own instance is encouraged. It collects
independently, keeps its own archive, and shares nothing with any other deployment. There is
no configuration that points at someone else's infrastructure, by design.

Independent archives of the same public sources make the overall record stronger.

## Licensing

- **Code:** AGPL-3.0-or-later
- **Our data contribution:** CC BY 4.0

Atmos does not own the underlying measurements. Each source keeps its own licence, terms and
attribution, which are recorded against the source and travel with the data through the API
and every export. The CC BY grant covers our own contribution: the collection record,
harmonisation, quality flags, revision history and derived products.

## Contributing

Contributions are welcome, particularly new source connectors. A connector is a small module
that declares what to fetch and how to parse it. Parsers are pure functions over archived
bytes, so they are straightforward to test against stored fixtures.

Code contributions only. Measurement data is never accepted through pull requests.
