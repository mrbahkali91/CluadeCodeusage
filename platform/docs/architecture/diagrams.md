# Architecture Diagrams

All diagrams are Mermaid and render in GitHub, VitePress and the Artifacts viewer.

---

## 1. System context

Who and what the platform touches, and which trust layer each source belongs to.

```mermaid
graph TB
    subgraph Users
        INV["Individual investor"]
        OFF["Investment / family office"]
        ANL["Internal analyst"]
        ADM["Compliance / admin"]
    end

    PLAT["<b>Saudi Real Estate<br/>Opportunity Intelligence</b><br/>discover · verify · value · score · monitor"]

    subgraph Truth["Layer A — Official / Truth"]
        MOJ["MOJ open transactions<br/><i>REQUIRES VALIDATION</i>"]
        KAP["KAPSARC / GASTAT indices<br/><i>CONFIRMED</i>"]
        REGA["REGA indicators · ad licence · Wafi<br/><i>REQUIRES VALIDATION</i>"]
        INF["Infath auctions<br/><i>public web · no API</i>"]
        SPL["SPL National Address<br/><i>PARTNERSHIP</i>"]
        EJAR["Ejar aggregate index<br/><i>PARTNERSHIP</i>"]
    end

    subgraph Signal["Layer B — Opportunity Signal"]
        FIRST["First-party entry<br/>analyst · broker · user<br/><i>CONFIRMED</i>"]
        PART["Licensed feeds<br/>Wasalt · developers<br/><i>PARTNERSHIP</i>"]
        REJ["Aqar · Haraj · Bayut<br/><i>NOT RECOMMENDED</i>"]
    end

    subgraph Ext["Supporting services"]
        LLM["LLM providers<br/>PII-stripped payloads only"]
        NOTIF["Email · push · SMS"]
        IDP["OIDC identity provider"]
    end

    INV --> PLAT
    OFF --> PLAT
    ANL --> PLAT
    ADM --> PLAT

    MOJ --> PLAT
    KAP --> PLAT
    REGA --> PLAT
    INF --> PLAT
    SPL --> PLAT
    EJAR --> PLAT
    FIRST --> PLAT
    PART --> PLAT
    REJ -. "excluded by source policy<br/>ADR-008" .-> PLAT

    PLAT --> LLM
    PLAT --> NOTIF
    PLAT --> IDP

    classDef confirmed fill:#dcfce7,stroke:#15803d,color:#14532d
    classDef validate fill:#fef3c7,stroke:#b45309,color:#78350f
    classDef partner fill:#dbeafe,stroke:#1d4ed8,color:#1e3a8a
    classDef rejected fill:#fee2e2,stroke:#b91c1c,color:#7f1d1d,stroke-dasharray: 4 3
    classDef core fill:#1e293b,stroke:#0f172a,color:#f8fafc

    class KAP,FIRST confirmed
    class MOJ,REGA,INF validate
    class SPL,EJAR,PART partner
    class REJ rejected
    class PLAT core
```

---

## 2. Container architecture

Two deployables plus the web app. Modules inside the API/worker are compile-time boundaries,
not network hops.

```mermaid
graph TB
    subgraph Edge
        CDN["CDN / WAF"]
        GW["Ingress · TLS · rate limit"]
    end

    WEB["<b>apps/web</b><br/>Next.js · MapLibre<br/>AR/EN · RTL"]
    API["<b>apps/api</b><br/>FastAPI<br/>REST /api/v1 · OpenAPI"]
    WRK["<b>apps/worker</b><br/>ingestion · pipeline DAG<br/>agents · alerts"]

    subgraph Domain["packages/domain — pure, no I/O"]
        DPROP["property + resolution"]
        DVAL["valuation + comparables"]
        DCOST["true acquisition cost"]
        DRISK["risk"]
        DSCORE["scoring"]
        DPROV["provenance"]
    end

    subgraph Ports
        PSRC["PropertySource adapters"]
        PLLM["LLMProvider"]
        PGEO["GeocodingProvider"]
        PDOC["DocumentStore"]
        PNOT["NotificationChannel"]
    end

    subgraph Data
        PG[("PostgreSQL 16 + PostGIS<br/>+ pgvector + FTS")]
        RD[("Redis<br/>cache · rate limit · idempotency")]
        S3[("S3-compatible<br/>raw payloads · documents")]
    end

    OBS["OpenTelemetry → Prometheus · Loki · Tempo · Grafana"]

    CDN --> WEB --> GW --> API
    API --> Domain
    WRK --> Domain
    WRK --> Ports
    API --> PG
    API --> RD
    WRK --> PG
    WRK --> RD
    WRK --> S3
    PDOC --> S3
    API -. "enqueue job" .-> PG
    PG -. "FOR UPDATE SKIP LOCKED" .-> WRK
    API --> OBS
    WRK --> OBS

    classDef app fill:#1e293b,stroke:#0f172a,color:#f8fafc
    classDef store fill:#ede9fe,stroke:#6d28d9,color:#3b0764
    class WEB,API,WRK app
    class PG,RD,S3 store
```

---

## 3. Data ingestion flow

The invariant to read off this diagram: **raw bytes are stored before anything interprets them**,
and interpretation never overwrites history.

```mermaid
flowchart TD
    START["Scheduled trigger · webhook · analyst submission"] --> LEGAL{"Source enabled<br/>with recorded<br/>legal basis?"}
    LEGAL -- no --> BLOCK["Refuse · alert admin<br/>ADR-008"]
    LEGAL -- yes --> DISC["discover(since)"]
    DISC --> FETCH["fetch(ref)"]
    FETCH --> HASH["hash payload"]
    HASH --> DUP{"content_hash<br/>seen before?"}
    DUP -- yes --> NOOP["No-op · record health check"]
    DUP -- no --> PII["Redact PII at boundary<br/>phone · national ID · email"]
    PII --> RAW[("source_records<br/><b>immutable raw</b>")]
    RAW --> NORM["normalize()"]
    NORM --> VAL{"validate()"}
    VAL -- fail --> QUAR["Quarantine + data-quality metric<br/>raw retained for replay"]
    VAL -- pass --> SNAP[("listing_snapshots<br/><b>append-only</b>")]
    SNAP --> PROV["Write field provenance<br/>value · confidence · source · evidence"]
    PROV --> RESOLVE["Entity resolution"]
    RESOLVE --> MATCH{"Existing property?"}
    MATCH -- "confidence ≥ 0.85" --> LINK["Link to property<br/>append timeline event"]
    MATCH -- "0.60–0.85" --> REVIEW["Queue for human review"]
    MATCH -- "< 0.60" --> NEW["Create new property"]
    LINK --> ENQ["Enqueue evaluation pipeline"]
    NEW --> ENQ
    REVIEW --> ENQ

    classDef bad fill:#fee2e2,stroke:#b91c1c,color:#7f1d1d
    classDef store fill:#ede9fe,stroke:#6d28d9,color:#3b0764
    class BLOCK,QUAR bad
    class RAW,SNAP store
```

---

## 4. Agent workflow

Deterministic nodes are dark; LLM-backed agents are amber. Note how few are amber, and that
none of them touch a money field directly.

```mermaid
flowchart TD
    IN["Normalized record"] --> EXT{"Unstructured<br/>content present?"}
    EXT -- yes --> A1["<b>Extraction Agent</b><br/>LLM · structured output<br/>every field: value+confidence+evidence"]
    EXT -- no --> GEO
    A1 --> SCHEMA{"Schema + range<br/>validation"}
    SCHEMA -- fail --> RETRY["Retry once · then quarantine<br/>never coerce"]
    SCHEMA -- pass --> GEO["Geolocate · deterministic"]
    GEO --> A2["<b>Property Resolution Agent</b><br/>deterministic candidates<br/>+ LLM adjudication on ties"]
    A2 --> COMP["Comparable selection · SQL"]
    COMP --> VALU["<b>Valuation</b> · deterministic<br/>weighted median · time-adjusted"]
    VALU --> COST["<b>True Cost</b> · deterministic<br/>itemised"]
    COST --> RENT["<b>Rental</b> · deterministic + priors"]
    RENT --> RISK["<b>Risk</b> · rules + evidence"]
    RISK --> SCORE["<b>Opportunity Score</b><br/>pure function · versioned weights"]
    SCORE --> GATE{"score ≥ 60?"}
    GATE -- no --> PARK["Store · monitor · no spend"]
    GATE -- yes --> A3["<b>Verification Agent</b><br/>official lookups<br/>evidence required for VERIFIED"]
    A3 --> CONF{"score ≥ 70<br/>AND confidence ≥ 0.60?"}
    CONF -- no --> INSUF["Show INSUFFICIENT DATA<br/>no recommendation"]
    CONF -- yes --> A4["<b>Investment Memo Agent</b><br/>LLM over computed numbers only<br/>fails closed on unresolved citation"]
    A4 --> PUB["Publish opportunity"]
    PUB --> A5["<b>Watch Agent</b><br/>re-evaluate on schedule + event"]
    A5 -.-> COMP

    classDef llm fill:#fef3c7,stroke:#b45309,color:#78350f
    classDef det fill:#1e293b,stroke:#0f172a,color:#f8fafc
    classDef warn fill:#fee2e2,stroke:#b91c1c,color:#7f1d1d
    class A1,A2,A3,A4 llm
    class GEO,COMP,VALU,COST,RENT,RISK,SCORE,A5 det
    class RETRY,INSUF warn
```

---

## 5. Opportunity evaluation flow

The money path, showing the two places the system refuses to produce a number.

```mermaid
flowchart LR
    subgraph Evidence
        TX["Comparable transactions<br/>time-adjusted"]
        RENTC["Rental comparables"]
        IDX["District price index"]
    end

    subgraph Valuation
        SEL["Select comps<br/>distance · area · age · type · recency"]
        WM["Weighted median SAR/m²<br/>IQR outlier rejection"]
        FV["fair_value low / base / high"]
    end

    subgraph Cost["True acquisition cost"]
        SP["Seller payment"]
        RI["Remaining installments"]
        FEES["Auction · brokerage · transfer<br/>VAT · registration"]
        RENO["Renovation estimate"]
        TAC["true_acquisition_cost"]
    end

    GUARD{"Any material<br/>line item UNKNOWN?"}
    REFUSE["<b>Refuse the discount</b><br/>show cost breakdown only"]
    DISC["discount = (fair_base − TAC) / fair_base"]

    subgraph Score["Opportunity score"]
        C1["Discount 30%"]
        C2["Liquidity 15%"]
        C3["Rental 15%"]
        C4["Location 10%"]
        C5["Developer/project 10%"]
        C6["Risk 10%"]
        C7["Confidence 10%"]
        SUM["Σ w·s → 0–100<br/>versioned weights"]
    end

    CGATE{"confidence<br/>≥ 0.60?"}
    INSUF["<b>INSUFFICIENT DATA</b>"]
    CLASS["Classify<br/>90+ Exceptional · 80+ Strong<br/>70+ Review · 60+ Watchlist"]

    TX --> SEL --> WM --> FV
    IDX --> WM
    SP --> TAC
    RI --> TAC
    FEES --> TAC
    RENO --> TAC
    TAC --> GUARD
    GUARD -- yes --> REFUSE
    GUARD -- no --> DISC
    FV --> DISC
    DISC --> C1
    RENTC --> C3
    C1 --> SUM
    C2 --> SUM
    C3 --> SUM
    C4 --> SUM
    C5 --> SUM
    C6 --> SUM
    C7 --> SUM
    SUM --> CGATE
    CGATE -- no --> INSUF
    CGATE -- yes --> CLASS

    classDef warn fill:#fee2e2,stroke:#b91c1c,color:#7f1d1d
    class REFUSE,INSUF warn
```

---

## 6. Deployment architecture

Cloud-agnostic Kubernetes. In-Kingdom region for anything touching personal data.

```mermaid
graph TB
    subgraph Internet
        U["Users"]
    end

    subgraph Region["Cloud region — in-Kingdom (PDPL residency)"]
        subgraph EdgeL["Edge"]
            WAF["WAF + DDoS"]
            LB["Load balancer · TLS"]
        end

        subgraph K8S["Kubernetes cluster"]
            subgraph NSApp["namespace: app"]
                WEBP["web · HPA 2–10"]
                APIP["api · HPA 3–20"]
            end
            subgraph NSWrk["namespace: workers"]
                WING["ingestion workers"]
                WPIPE["pipeline workers"]
                WAGENT["agent workers<br/>separate pool · LLM-latency bound"]
                WDOC["document workers<br/>CPU-heavy · own pool"]
                WCRON["scheduler"]
            end
            subgraph NSObs["namespace: observability"]
                OTEL["OTel collector"]
                PROM["Prometheus / Mimir"]
                LOKI["Loki"]
                TEMPO["Tempo"]
                GRAF["Grafana"]
            end
        end

        subgraph Managed["Managed data services"]
            PGM[("PostgreSQL 16 + PostGIS<br/>primary + replica<br/>PITR · encrypted at rest")]
            RDM[("Redis")]
            OBJ[("S3-compatible object store")]
            KMS["KMS / secret manager"]
        end
    end

    subgraph Outside["Outside Kingdom — no personal data"]
        LLMP["LLM providers<br/>redacted payloads only<br/>transfer assessment recorded"]
    end

    subgraph CI["GitLab-compatible CI/CD"]
        BUILD["build · test · SAST<br/>dep + container + IaC scan"]
        REG["image registry · signed"]
        TF["Terraform"]
    end

    U --> WAF --> LB --> WEBP
    LB --> APIP
    APIP --> PGM
    APIP --> RDM
    WING --> PGM
    WPIPE --> PGM
    WAGENT --> PGM
    WDOC --> OBJ
    WAGENT --> LLMP
    APIP --> KMS
    WING --> KMS
    APIP --> OTEL
    WPIPE --> OTEL
    OTEL --> PROM
    OTEL --> LOKI
    OTEL --> TEMPO
    PROM --> GRAF
    LOKI --> GRAF
    TEMPO --> GRAF
    BUILD --> REG --> K8S
    TF --> Region

    classDef store fill:#ede9fe,stroke:#6d28d9,color:#3b0764
    classDef ext fill:#fef3c7,stroke:#b45309,color:#78350f
    class PGM,RDM,OBJ store
    class LLMP ext
```
