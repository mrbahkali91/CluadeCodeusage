# Source Verification Log

Raw evidence behind the availability labels in [matrix.md](matrix.md). Recorded so that a
reviewer can distinguish what was **observed** from what was **read about**, and so the
labels can be re-tested later.

**Environment caveat:** probes ran from a cloud build container with egress through an agent
proxy, outside Saudi Arabia. Several Saudi government endpoints appear to reject or geo-filter
such traffic. A connection reset here is **not** evidence that a service is unavailable — it is
evidence that *this* egress cannot reach it. All such probes must be repeated from a
KSA-resident network before any label is changed.

## Live probes — 2026-08-29

| Target | Result | Interpretation |
|---|---|---|
| `data.kapsarc.org/api/explore/v2.1/catalog/datasets/real-estate-price-index-by-sector-2023-100/records?limit=2` | **HTTP 200**, `application/json`, 1.07s | **CONFIRMED.** Returned `total_count: 680` and records `{periodicity:"Quarterly", year:"2021", quarter:"Q1", measure:"Index", sector:"Residential: Residential Plot", value:80.6, date:"2021-03"}`. No auth header sent. |
| `data.kapsarc.org/api/explore/v2.1/catalog/datasets?where=search(title,'real estate')` | **HTTP 200** | Catalog contains 4 real-estate datasets: `real-estate-price-index-by-sector-2023-100`, `real-estate-indices-by-regions`, `real-estate-indices`, `real-estate-indices-by-regions-2023-100`. |
| `infath.gov.sa/en/auctions` | **HTTP 200**, `text/html`, 9.8s | Public auction browse confirmed reachable without login. Page exposes auction name, opportunity count, status, start/end datetime, region, city, asset type, auction type. **No API, RSS, or export offered.** Latency ~10s is a note for connector design. |
| `open.data.gov.sa/en/datasets` | **Connection reset by peer** after 12.2s | Unreachable from this egress. Cannot verify MOJ transaction datasets, schema, or licence. **Blocks Assumption A-01.** |
| `open.data.gov.sa/ar/pages/developers-api` and the dataset API path `c4eb90eb-de47-4996-9eba-8ae503980bcf` (re-probed 2026-09-04, after the portal was raised directly) | **Connection reset by peer** after ~12.3s on every path tried | **Still blocked, and now diagnosed.** DNS resolves (78.93.109.61), TCP connects, the TLS *Client Hello* is sent, then the peer resets — before any HTTP request is seen. A browser `User-Agent` and `Accept-Language: ar` change nothing, and `data.kapsarc.org` answers in 1.3s from the same egress, so the egress is healthy. Signature of an IP-reputation or geo block on a foreign datacenter address, **not** an unavailable service. Nine candidate API paths were tried; all reset identically. A-01 therefore remains **UNVALIDATED**, and no line of code in this platform reads from this portal. `tools/validate_open_data.py` exists to settle it from a Saudi-resident egress in one command. |
| `api.address.gov.sa` | **Connection reset by peer** after 7.1s | Unreachable from this egress. Developer portal existence is documented; terms unverified. |
| `rega.gov.sa/en/open-data/` | **HTTP 503** | Service unavailable at probe time. Retry required. |
| `rega.gov.sa/en/rega-services/platforms/` | Retrieved | Twelve REGA platforms enumerated (Aqari, FAL, Real Estate Contributions, Real Estate Indicators, Ejar, Wafi off-plan, Geospatial Real Estate Portal, Real Estate Registry, Saudi Real Estate Institute, Arbitration Center, Mullak, non-Saudi ownership). **No mention of any API, open data repository, or third-party integration mechanism.** |

## Documentary evidence (read, not observed)

| Claim | Source of claim | Weight |
|---|---|---|
| MOJ publishes transaction-level quarterly sale CSVs (≈1.4M rows 2020–2025) and ≈6M operations records, via unauthenticated open-data download URLs, under the KSA Open Data License | Independent open-source dataset project documenting its own extraction method | **Medium.** Third-party, but specific, reproducible, and states plainly that no keys or special access were required. Must be reproduced first-hand. |
| `srem.moj.gov.sa` requires national ID + Nafath verification | Government and press announcements of the platform launch | High |
| Ejar has registered >10M rental contracts; integrations exist for licensed operators | REGA announcements (contract volume); vendor blog (integration claim) | Volume: high. **Integration claim: low — vendor marketing, not first-party documentation.** |
| Wafi is the sole lawful off-plan sale route; licensed projects are publicly verifiable | REGA platform documentation and regulatory summaries | High |
| Wasalt holds an Infath licence for electronic and hybrid auctions; Alio operates Infath real-estate auctions | Multiple independent trade press reports | High |
| SPL National Address API is commercial, capped per month and per second, with a strict approval process | SPL/vendor documentation | Medium-high |
| PDPL (Royal Decree M/19, implementing regulations 2023, enforcement from 14 Sep 2024) applies to processing of Saudi residents' personal data regardless of processor location; SDAIA enforces; fines up to SAR 5M | SDAIA and multiple independent legal summaries | High |

## Explicitly rejected access paths

Recorded so that no future contributor mistakes an omission for an oversight.

| Path | Why rejected |
|---|---|
| Commercial scraper marketplace actors for `sa.aqar.fm`, PropertyFinder.sa and Saudi auction listings | Unauthorised access to proprietary content. The existence of a paid scraper is not a licence. |
| "Unofficial Bayut API" services | An unauthorised interface to a private system; using it is reverse-engineering by proxy. |
| Automating `srem.moj.gov.sa` with a user's Nafath session | Credential-mediated access to a government system; outside any reasonable user authorisation. |
| Bulk enumeration of the REGA advertisement-licence inquiry service | A consumer verification endpoint used as a bulk data source is abuse, regardless of whether it is technically open. |
| Ingesting Haraj posts | ToS plus no lawful PDPL basis for processing seller personal data at scale. |

## Re-validation schedule

| When | Action |
|---|---|
| Week 1, from KSA egress | Re-probe A2, A4, A5, A6, A11, A12. **A2 is a go/no-go gate for the MVP scope.** |
| Week 1 | Counsel review of the KSA Open Data License text for commercial derivative use |
| Week 2 | Written data-sharing enquiries: Infath, NHC, REGA |
| Week 2 | Commercial enquiries: Wasalt (rank 1), SPL, one commercial transaction-data vendor as A2 fallback |
| Monthly thereafter | Automated source health checks re-run every probe in this table and alert on label-invalidating changes (see the admin source-health dashboard, Epic E7) |
