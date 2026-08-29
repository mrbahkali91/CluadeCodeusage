# Saudi Real Estate Data Source Matrix

**Status:** Phase 0 discovery output. **Date of research:** 2026-08-29.

## How to read the availability labels

| Label | Meaning | Permitted to build against? |
|---|---|---|
| **CONFIRMED** | I made a live request (or read first-party documentation) and observed the interface working, unauthenticated or with a documented self-service key. | Yes — build now. |
| **REQUIRES VALIDATION** | Strong evidence the data exists and is public, but I could **not** verify the machine interface from this environment. Access method, schema, licence and rate limits are unproven. | Spike only. No delivery commitment until validated. |
| **PARTNERSHIP REQUIRED** | The data exists but lawful programmatic access needs a contract, licence, or regulated status (broker/operator/integrator). | Business development first. Build the connector interface, not the connector. |
| **NOT RECOMMENDED** | Access would require unauthorised scraping, ToS violation, bypassing authentication, or would ingest personal data without a lawful basis. | **Prohibited.** Rejected on purpose, not overlooked. |

> **Honesty note.** Several government portals (`open.data.gov.sa`, `api.address.gov.sa`)
> reset the connection from this build environment, and `rega.gov.sa/open-data` returned
> HTTP 503. That is evidence about *my egress*, not proof the services are unavailable.
> Nothing in this matrix is upgraded to CONFIRMED on the basis of a blog post, a
> vendor marketing page, or a third-party wrapper. See
> [verification-log.md](verification-log.md) for the raw evidence.

---

## Layer A — Official / Truth Layer

These establish ground truth. Only this layer may set `verification_status = VERIFIED`.

### A1. KAPSARC Data Portal — GASTAT Real Estate Price Indices

| | |
|---|---|
| **Owner** | KAPSARC, republishing GASTAT (General Authority for Statistics) indices |
| **Data available** | Real Estate Price Index quarterly by sector and by region; residential/commercial/agricultural; sub-types (apartment, villa, residential plot, commercial plot, building, house); construction cost indices; base 2023=100 and legacy 2014=100 series |
| **Public/private** | Public |
| **Authentication** | **None** |
| **Official API** | **Yes** — Opendatasoft Explore API v2.1 at `https://data.kapsarc.org/api/explore/v2.1/catalog/datasets/{dataset_id}/records` |
| **Update frequency** | Quarterly, following GASTAT publication |
| **Legal / ToS** | Open data, attribution expected. Confirm commercial redistribution terms with KAPSARC before republishing raw series. |
| **Integration approach** | Direct HTTP pull, nightly. Cache locally; series are small (hundreds of rows). |
| **MVP priority** | **P0** |
| **Availability** | **CONFIRMED** |
| **Confidence** | **High** — verified live: HTTP 200, `application/json`, `total_count: 680`, records shaped `{periodicity, year, quarter, measure, sector, value, date}`. Catalog search returned 4 real-estate datasets. |

**Why this matters more than it looks.** This is the *time-adjustment* input for the
comparable engine. A sale from Q1 2024 cannot be compared to today's asking price without
indexing it forward. Without a defensible index, every "discount" number we publish is wrong
by an unknown amount. This is the one P0 source I can guarantee today.

---

### A2. Saudi Open Data Portal — Ministry of Justice real-estate transactions

| | |
|---|---|
| **Owner** | SDAIA (portal), Ministry of Justice (publisher) |
| **Data available** | Reported as **transaction-level** quarterly sale records — price, area, price/m², location to district level, property classification, reference number; plus a much larger "operations" series covering mortgages, seizures, transfers and enforcement actions |
| **Public/private** | Public open data |
| **Authentication** | Reported as none required |
| **Official API** | Portal exposes dataset resources over predictable download URLs (`/odp-public/{ORG_ID}/{DATASET_ID}/v{N}/{RESOURCE}.csv`) |
| **Update frequency** | Quarterly |
| **Legal / ToS** | KSA Open Data License — redistribution with attribution, derivative works permitted; commercial use not expressly prohibited. **Must be read in full by counsel before we build a commercial product on it.** |
| **Integration approach** | Scheduled bulk download → staging → normalise → load into `transactions`. Batch, not streaming. |
| **MVP priority** | **P0 — highest value source in the entire matrix** |
| **Availability** | **REQUIRES VALIDATION** |
| **Confidence** | **Medium.** Coverage (≈1.4M transaction-level sales 2020–2025, ≈6M operations records) is documented by an independent open-source project that states no keys or special access were needed. I could not reach `open.data.gov.sa` from this environment (connection reset by peer), so I have **not** personally verified schema, granularity, licence text, or that the resource URLs are still live. |

**This is Assumption A-01.** Every headline claim the product makes — "19% below market",
"17 comparable transactions", "estimated market value SAR 890k–930k" — depends on
transaction-level records with location and area. If this source turns out to be
aggregate-only, or district-only without area, or paywalled, the product thesis changes and
the MVP must be rescoped. **Validate from a Saudi-resident egress in week 1.**

---

### A3. Real Estate Market — `srem.moj.gov.sa`

| | |
|---|---|
| **Owner** | Ministry of Justice |
| **Data available** | Transaction execution and browsing: transaction value, property location, price/m², area, timestamp; deed transfer services |
| **Public/private** | Service is citizen-facing and gated |
| **Authentication** | **Nafath** (national SSO) with national ID |
| **Official API** | None documented publicly |
| **Update frequency** | Real time |
| **Legal / ToS** | Authenticated citizen service. Automating access using a user's Nafath identity would be credential-mediated access to a government system. |
| **Integration approach** | **Do not automate.** If we ever surface this, it is as a deep link the user follows themselves, under their own identity. |
| **MVP priority** | Out of MVP |
| **Availability** | **NOT RECOMMENDED** (for automated ingestion) |
| **Confidence** | High that it is gated; the platform's own onboarding describes ID + Nafath verification. |

---

### A4. REGA — Real Estate Indicators & Open Data

| | |
|---|---|
| **Owner** | Real Estate General Authority |
| **Data available** | Aggregate indicators: average/min/max price per m², deed counts, rental metrics, by region and quarter; rental indicators via the REGA rental index portal |
| **Public/private** | Public |
| **Authentication** | Unknown |
| **Official API** | Unknown. REGA's own platforms page names twelve platforms and mentions **no** API or third-party integration mechanism. |
| **Update frequency** | Quarterly |
| **Legal / ToS** | Government open data, terms unverified |
| **Integration approach** | Bulk download if resources are addressable; otherwise treat as a manual quarterly analyst import |
| **MVP priority** | **P1** — valuable as a cross-check on A2 and as the rental-yield prior |
| **Availability** | **REQUIRES VALIDATION** |
| **Confidence** | **Medium.** `rega.gov.sa/en/open-data/` returned HTTP 503 to my probe. Independent work reports REGA sales indicators 2024–2025 and rental indicators 2019–2024 obtainable from open data channels. |

---

### A5. REGA — Real Estate Advertisement Licence Inquiry (FAL regime)

| | |
|---|---|
| **Owner** | REGA |
| **Data available** | Validity of a real-estate advertisement licence number; advertiser status |
| **Public/private** | Public single-record lookup service |
| **Authentication** | None documented for the inquiry UI |
| **Official API** | Not documented |
| **Update frequency** | Real time |
| **Legal / ToS** | Designed for one-at-a-time consumer verification. **Bulk automated enumeration would abuse it and is prohibited by our own source policy** (see ADR-008). |
| **Integration approach** | Per-property, on demand, rate-limited, cached with TTL, only for properties a user is actively evaluating. Never a crawl. |
| **MVP priority** | **P1** — this is the cheapest, highest-signal trust feature we have. Saudi ad regulation requires listings to carry a licence; an unlicensed ad is itself a risk signal. |
| **Availability** | **REQUIRES VALIDATION** |
| **Confidence** | Medium-high that the service exists (REGA announced it and documents it as an e-service); low on machine interface. |

---

### A6. Wafi — Off-plan sales & lease programme

| | |
|---|---|
| **Owner** | REGA |
| **Data available** | Licensed off-plan projects, project licence numbers, registered developers, escrow-controlled projects |
| **Public/private** | Public verification of licensed projects via REGA/Wafi |
| **Authentication** | None documented for lookup |
| **Official API** | Not documented |
| **Update frequency** | Continuous as licences are issued |
| **Legal / ToS** | Unverified |
| **Integration approach** | Per-project verification lookup, cached. Same rate discipline as A5. |
| **MVP priority** | **P1** — required to score off-plan assignment (تنازل) opportunities honestly. An assignment on an unlicensed project is a red flag, not a bargain. |
| **Availability** | **REQUIRES VALIDATION** |
| **Confidence** | High that Wafi is the sole lawful off-plan route and that project verification is publicly offered; low on machine interface. |

---

### A7. Ejar — Rental contracts network

| | |
|---|---|
| **Owner** | REGA / Ministry of Municipal, Rural Affairs and Housing |
| **Data available** | Registered rental contracts (>10M since launch); a rental index used for compliant rent benchmarking |
| **Public/private** | **Contract records are private** — they identify landlords and tenants and are personal data under PDPL |
| **Authentication** | Yes; integration positioned at licensed brokers and property operators |
| **Official API** | Vendor blogs assert integration APIs exist for licensed operators. **Unverified, and not first-party documentation.** |
| **Update frequency** | Continuous (~19k contracts/day reported) |
| **Legal / ToS** | Contract-level data must never be ingested without a lawful basis and a data-sharing agreement. Aggregate index data may be obtainable separately. |
| **Integration approach** | Pursue aggregate/index access only. Contract-level access is out of scope for an intelligence product that is not a party to the tenancy. |
| **MVP priority** | P2 |
| **Availability** | **PARTNERSHIP REQUIRED** |
| **Confidence** | Low on any specific interface. Treat every third-party "Ejar API" claim as unverified. |

---

### A8. Infath — Entrustment and Liquidation Center (judicial auctions)

| | |
|---|---|
| **Owner** | Ministry of Justice initiative; independent government centre |
| **Data available** | Auction name, number of opportunities in the auction, status (upcoming/ongoing/ended), start and end date-times, region, city, asset type (property/vehicle/movable), auction type (electronic), link to auction detail; asset detail and brochures on the auction page |
| **Public/private** | **Public — browsable without login** |
| **Authentication** | None to browse; account needed to bid |
| **Official API** | **None found.** No API, RSS, or machine-readable export is offered on the auctions page. |
| **Update frequency** | Continuous; auctions are announced with lead time and have hard deadlines |
| **Legal / ToS** | Government publication of judicial auctions is public-interest information. Automated collection still needs a ToS/robots review and, preferably, a data-sharing request. |
| **Integration approach** | **Preferred:** formal data-sharing request to Infath, or ingestion via a licensed e-auction operator partner (A9). **Interim MVP:** analyst-assisted structured entry with document attachment — the auction volume (reported ~75 auctions / ~950 assets in an early-2026 period) is small enough that this is genuinely tractable and gives us a real product while the agreement is negotiated. |
| **MVP priority** | **P0 for the opportunity type; P2 for automated ingestion** |
| **Availability** | **REQUIRES VALIDATION** (public web verified reachable: HTTP 200) / **PARTNERSHIP REQUIRED** (structured feed) |
| **Confidence** | High on public availability and field set; **zero** on any API. |

**Design consequence:** because the highest-signal opportunity type has no feed, the ingestion
layer must treat *manual and analyst-assisted entry as a first-class connector*, not a
fallback hack. See ADR-008.

---

### A9. Infath-licensed e-auction operators (Wasalt Auctions, Alio, others)

| | |
|---|---|
| **Owner** | Private, licensed by Infath |
| **Data available** | Auction catalogues, asset detail, opening prices, bid state, outcomes |
| **Public/private** | Public catalogues; bid data varies |
| **Authentication** | Varies |
| **Official API** | Not published |
| **Update frequency** | Real time during auctions |
| **Legal / ToS** | Commercial platforms; ingestion requires agreement |
| **Integration approach** | Commercial data agreement. **Wasalt is the single highest-value partner target**: PIF-owned, a large listings portal, *and* Infath-licensed for electronic and hybrid auctions — one agreement covers both the truth layer and the signal layer. |
| **MVP priority** | P1 (business development), P2 (build) |
| **Availability** | **PARTNERSHIP REQUIRED** |
| **Confidence** | High that Wasalt and Alio hold Infath auction licences; none on data terms. |

---

### A10. NHC / Sakani

| | |
|---|---|
| **Owner** | National Housing Company (NHC); Sakani is its citizen platform |
| **Data available** | Project inventory, unit types, developer prices, off-plan launches; NHC also operates several other real-estate platforms |
| **Public/private** | Consumer-facing catalogue is public; eligibility/financing flows are gated |
| **Authentication** | Gated for transactional flows |
| **Official API** | Not documented |
| **Update frequency** | Continuous |
| **Legal / ToS** | Unverified |
| **Integration approach** | Data-sharing agreement preferred. NHC's developer-price list is the *reference price* against which assignment (تنازل) opportunities are judged, so this materially improves scoring quality for a very common Saudi opportunity type. |
| **MVP priority** | P1 |
| **Availability** | **REQUIRES VALIDATION** → likely **PARTNERSHIP REQUIRED** |
| **Confidence** | Low on interface; high on relevance (NHC reported ~62% off-plan market share and a 600k-unit mandate to 2030). |

---

### A11. Saudi National Address — SPL (`api.address.gov.sa`)

| | |
|---|---|
| **Owner** | Saudi Post / SPL |
| **Data available** | Address geocoding, reverse geocoding, free-text and short-address resolution, district identifiers, postal codes |
| **Public/private** | Commercial |
| **Authentication** | API key, approved account |
| **Official API** | **Yes** — a documented developer portal exists |
| **Update frequency** | Continuous |
| **Legal / ToS** | Paid tiers with monthly and per-second call caps; approval process reported as strict |
| **Integration approach** | Subscribe; wrap behind our `GeocodingProvider` port so it is swappable |
| **MVP priority** | **P1** |
| **Availability** | **PARTNERSHIP REQUIRED** (commercial subscription) |
| **Confidence** | High that the API exists and is paid; unverified from here (connection reset). |

**Fallback if procurement is slow:** MVP can run on district-polygon containment plus
listing-declared district, with `location_precision = DISTRICT` recorded honestly in
provenance. Comparable selection degrades but does not break.

---

### A12. REGA Geospatial Real Estate Portal / district boundaries

| | |
|---|---|
| **Owner** | REGA; boundary data also associated with SPL/National Address ArcGIS services |
| **Data available** | Interactive maps, locations, points of interest; potentially district and municipal boundary polygons |
| **Public/private** | Public viewer |
| **Authentication** | Unknown |
| **Official API** | Unknown; an ArcGIS service endpoint for Saudi National Address is referenced in third-party data catalogues |
| **Update frequency** | Infrequent |
| **Legal / ToS** | Unverified |
| **Integration approach** | One-time boundary import into PostGIS, refreshed rarely |
| **MVP priority** | **P0 for the boundaries themselves** — the district is the primary comparable-selection unit in Riyadh |
| **Availability** | **REQUIRES VALIDATION** |
| **Confidence** | Medium. If unavailable, boundaries can be constructed from open sources (e.g. OpenStreetMap admin relations) and analyst-curated for the ~15 target Riyadh districts. That fallback is entirely sufficient for MVP. |

---

### A13. SAMA — Saudi Central Bank

| | |
|---|---|
| **Owner** | SAMA |
| **Data available** | Residential mortgage volumes, lending statistics, rates — a leading indicator for liquidity and market direction |
| **Public/private** | Public statistical publications |
| **Authentication** | None |
| **Official API** | Not documented; published as reports/spreadsheets |
| **Update frequency** | Monthly / quarterly |
| **Legal / ToS** | Public statistics, attribution |
| **Integration approach** | Scheduled file ingest |
| **MVP priority** | P2 — feeds the market-risk component of the score, not the valuation |
| **Availability** | **REQUIRES VALIDATION** |
| **Confidence** | High that data is public; low on machine access. |

---

## Layer B — Opportunity Signal Layer

These generate candidates. **None of them may set `verification_status = VERIFIED`.**
A signal is a hypothesis until the truth layer confirms it.

### B1. Aqar (`sa.aqar.fm`)

| | |
|---|---|
| **Owner** | Aqar |
| **Data available** | Largest Saudi listings inventory: asking prices, area, district, property type, ad licence numbers, advertiser identity verified via Nafath |
| **Public/private** | Public website, proprietary content |
| **Authentication** | Public browse |
| **Official API** | None published |
| **Update frequency** | Continuous |
| **Legal / ToS** | Listings are the platform's and advertisers' content. Multiple commercial scrapers for Aqar exist on scraping marketplaces — **their existence is not permission.** |
| **Integration approach** | Commercial content licence, or nothing. |
| **MVP priority** | P2 (business development) |
| **Availability** | **NOT RECOMMENDED** for scraping / **PARTNERSHIP REQUIRED** for licensed feed |
| **Confidence** | High |

### B2. Haraj

| | |
|---|---|
| **Owner** | Haraj |
| **Data available** | Free-form classifieds; very high density of urgent-sale and assignment language, often with seller phone numbers |
| **Public/private** | Public UGC |
| **Official API** | None published |
| **Legal / ToS** | ToS restrictions plus a serious **PDPL** problem: posts routinely contain personal data (names, phone numbers) with no lawful basis for us to process at scale. |
| **Integration approach** | None. |
| **MVP priority** | Excluded |
| **Availability** | **NOT RECOMMENDED** |
| **Confidence** | High. This is the source most likely to be requested by stakeholders and it is the one we should most firmly refuse without a licence and a PDPL assessment. |

### B3. Bayut Saudi

| | |
|---|---|
| **Owner** | Bayut (Dubizzle group) |
| **Data available** | Structured listings with good metadata quality |
| **Official API** | No public API. Third-party sites advertise an "unofficial Bayut API" — **explicitly rejected**; it is an unauthorised interface to a private system. |
| **Legal / ToS** | Proprietary |
| **Integration approach** | Commercial agreement |
| **MVP priority** | P2 |
| **Availability** | **PARTNERSHIP REQUIRED**; unofficial wrappers **NOT RECOMMENDED** |
| **Confidence** | High |

### B4. Wasalt

See **A9**. PIF-owned, ~24k listings reported, and Infath-licensed for auctions.
**Rank #1 in the partnership pipeline.**
**Availability: PARTNERSHIP REQUIRED.**

### B5. Developer websites (ROSHN, NHC, private developers)

| | |
|---|---|
| **Data available** | Launch prices, remaining inventory, payment plans, handover dates — the reference price for launch-arbitrage and assignment scoring |
| **Official API** | Rare |
| **Legal / ToS** | Per-site. Assess `robots.txt` and ToS individually; prefer a direct data-sharing arrangement, which developers often welcome because it is distribution for them. |
| **Integration approach** | Per-developer connector, each with its own recorded legal assessment in `sources.legal_access_method` |
| **MVP priority** | P1 for 3–5 named Riyadh developers |
| **Availability** | **REQUIRES VALIDATION per developer** |
| **Confidence** | Medium |

### B6. X / social media

| | |
|---|---|
| **Data available** | Broker and owner posts; genuinely early distress signals |
| **Official API** | Paid tiers exist |
| **Legal / ToS** | Paid API is lawful, but posts are authored by identifiable individuals — PDPL applies. Storing post text plus author handle needs a lawful basis and retention limits. |
| **Integration approach** | Defer. If built: paid API only, store the minimum, never store contact details, and treat every post as untrusted input to the prompt-injection boundary. |
| **MVP priority** | P3 — post-MVP |
| **Availability** | **REQUIRES VALIDATION** |
| **Confidence** | Medium |

### B7. Commercial property-data vendors

Aggregators and valuation-data vendors operating in KSA.
**PARTNERSHIP REQUIRED.** Evaluate in parallel with A2 validation: if A2 fails, a commercial
transaction dataset becomes the fallback backbone and materially changes unit economics.

### B8. First-party channels — analyst entry, user submission, broker portal

| | |
|---|---|
| **Owner** | **Us** |
| **Data available** | Everything a partner feed would give us, entered by an analyst, a broker we onboard, or a user forwarding an opportunity they were sent |
| **Authentication** | Our own auth |
| **Official API** | Ours |
| **Legal / ToS** | Clean, with submitter consent and clear terms |
| **Integration approach** | Same `PropertySource` interface as every other connector, so nothing downstream knows the difference |
| **MVP priority** | **P0** |
| **Availability** | **CONFIRMED** |
| **Confidence** | High |

**This is the strategic answer to the ingestion problem.** It de-risks the MVP: the
intelligence engine can be built, tested and demonstrated on real Riyadh opportunities from
day one while partnerships are negotiated. It also seeds the opportunity graph, which is the
long-term moat.

---

## Priority summary for MVP (Riyadh)

| Priority | Source | Purpose | Label |
|---|---|---|---|
| P0 | KAPSARC/GASTAT indices (A1) | Time-adjust comparables | CONFIRMED |
| P0 | MOJ open transactions (A2) | Comparable backbone | REQUIRES VALIDATION ← **gate** |
| P0 | District boundaries (A12, OSM fallback) | Geospatial unit of analysis | REQUIRES VALIDATION |
| P0 | First-party ingestion (B8) | Opportunity supply for MVP | CONFIRMED |
| P1 | REGA indicators (A4) | Cross-check, rental prior | REQUIRES VALIDATION |
| P1 | REGA ad licence (A5), Wafi (A6) | Verification agent | REQUIRES VALIDATION |
| P1 | SPL geocoding (A11) | Coordinate precision | PARTNERSHIP REQUIRED |
| P1 | NHC/developer prices (A10, B5) | Assignment reference price | REQUIRES VALIDATION |
| P2 | Infath structured (A8), operators (A9) | Auction automation | PARTNERSHIP REQUIRED |
| P2 | Ejar aggregate (A7), SAMA (A13) | Rental & market risk | PARTNERSHIP REQUIRED / VALIDATE |
| — | Aqar, Haraj, Bayut, unofficial APIs | — | **NOT RECOMMENDED** |

## Standing rules

1. No unauthorised scraping. No authentication bypass. No reverse-engineered private APIs.
   No use of third-party scraper marketplaces to launder the same act.
2. Every source carries a recorded `legal_access_method` and `data_license` before its
   connector is enabled in any environment. A connector with an unrecorded legal basis
   fails CI (see ADR-008).
3. A `robots.txt` disallow or a ToS prohibition is treated as a hard stop, not a risk to price in.
4. Personal data (names, phone numbers, national IDs, deed-holder identity) is not ingested
   from signal sources. Where it is unavoidably present in fetched text, it is redacted at
   the ingestion boundary before storage and before any LLM call.
