# ADR-008: Source ingestion policy enforced in code and CI

**Status:** Accepted · **Date:** 2026-08-29

## Problem
Most Saudi listing platforms have no public API. Scraping them is technically trivial and
commercially tempting — third-party scrapers for Aqar, PropertyFinder.sa and Saudi auction
listings are sold openly. Ingesting them would expose us to ToS claims, IP claims, PDPL
liability, and reputational damage with the government bodies we most need as partners. A
policy in a document does not prevent an engineer under deadline from adding a connector.

## Options considered
1. **Policy document + code review** — rely on people.
2. **Policy enforced by the type system and CI** — a connector cannot exist without a recorded
   legal basis, and prohibited hosts fail the build.
3. **Ingest broadly, remove on complaint** — rejected on ethics and on strategy: we would be
   negotiating partnerships with organisations whose ToS we were violating.

## Decision
Option 2.

1. Every `PropertySource` must declare `legal_access_method ∈ {OFFICIAL_API, OPEN_DATA,
   PUBLIC_WEB_PERMITTED, LICENSED_API, PARTNERSHIP, USER_AUTHORIZED, MANUAL_UPLOAD}` and a
   `data_license`. These are required constructor arguments — a connector without them does not
   type-check.
2. A CI check asserts every registered source has a corresponding entry in the
   [data source matrix](../data-sources/matrix.md) with a non-`NOT RECOMMENDED` label.
3. A denylist of hosts (Aqar, Haraj, Bayut and any unofficial-API wrapper domains) fails the
   build if referenced from connector code.
4. `robots.txt` is honoured by the fetch client at runtime, not merely at review time.
5. Rate limits are per-source and conservative; official lookup endpoints (REGA advertisement
   licence, Wafi) are called **per property on demand only**, never enumerated.
6. Enabling a source in production requires an admin action recorded in `audit_events`.

## Why
- **The partnership strategy depends on it.** Wasalt, NHC, Infath and REGA are the sources that
  matter most, and they are counterparties. An organisation caught scraping does not get a
  data-sharing agreement.
- Legal exposure is asymmetric: the upside of scraping is faster MVP supply; the downside is an
  existential legal and reputational problem.
- Making compliance a build-time property means it survives staff turnover and deadline pressure.

## Trade-offs
- **Accepted:** slower opportunity supply at MVP. Mitigated by first-party ingestion (matrix B8)
  and analyst-assisted Infath entry, which is tractable at the actual auction volume.
- **Accepted:** some competitors will scrape and will have more listings. We compete on the
  quality and defensibility of the analysis, which is the durable position anyway.

## Revisit when
A source's terms change to permit automated access, or a licence is signed — either of which is
a matrix update plus a source record, not a code change.
