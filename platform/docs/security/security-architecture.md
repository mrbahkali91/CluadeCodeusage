# Security Architecture

---

## 1. What we are actually protecting

Three assets, in order of consequence if lost:

1. **Integrity of the numbers.** An attacker who can move a valuation or a score does more
   commercial damage than one who reads our database. Someone with an unsold unit has a direct
   financial motive to make it score well — and the input path runs through text we ingest from
   outside. **This makes prompt injection a business-logic attack, not a novelty.**
2. **Personal data.** Submitter identities, user watchlists (which reveal investment strategy),
   and any personal data incidentally present in ingested content. PDPL applies, with fines up
   to SAR 5M and enforcement active since September 2024.
3. **Availability and reputation.** A wrong "verified" badge is worse than downtime.

## 2. Identity and access

- **OIDC/OAuth2** against an external IdP (Keycloak self-hosted, or managed). We do not store
  passwords. Enterprise SSO and SCIM-ready from the start.
- **RBAC:** `viewer · analyst · admin · org_admin · platform_admin`. Permissions are checked in
  a decorator at the route layer *and* enforced by Postgres RLS at the row layer.
- **Organisation isolation:** `organization_id` on every tenant-scoped table, RLS policies bound
  to a per-request session variable set from the verified token. Cross-tenant tests are part of
  the integration suite, not a manual check.
- **Service credentials:** short-lived, workload-identity based. No static cloud keys in the cluster.
- **MFA** required for `admin` and above.

## 3. Application security

| Control | Implementation |
|---|---|
| Input validation | Pydantic at every boundary; strict types; no `dict[str, Any]` crossing a module edge |
| Output encoding | React escapes by default; no `dangerouslySetInnerHTML` — the agents produce Markdown, which is sanitised and rendered, never raw HTML |
| CSRF | SameSite=Lax cookies + double-submit token on state-changing routes |
| Cookies | `Secure`, `HttpOnly`, `SameSite`, host-prefixed |
| CSP | Strict, nonce-based; no `unsafe-inline`; map tiles from an explicit allowlist |
| Rate limiting | Per-IP, per-user, per-org at the edge and in Redis; **stricter limits on any route that can cause LLM spend** |
| SQL injection | Parameterised queries only; the NL-search agent emits a validated filter object, never SQL text |
| SSRF | Outbound fetches restricted to an allowlist of registered source hosts; no user-supplied URL is fetched by the server |
| File upload | Type sniffing, size caps, AV scan, stored in an isolated bucket, never served from the app origin |
| Dependencies | Lockfiles, SCA on every build, automated update PRs |

## 4. Data protection and PDPL

**Data minimisation is the primary control**, because the cheapest way to survive a personal-data
incident is not to hold the data:

- Seller and advertiser **contact details are never ingested.** Phone numbers, emails and
  national IDs are stripped by a redaction filter at the ingestion boundary — before storage,
  before indexing, before any LLM call.
- Deed-holder identity is not collected. We analyse assets, not people.
- Submitter identity is held only where a person chose to submit an opportunity, with consent
  and a stated retention period.

| Requirement | Approach |
|---|---|
| Lawful basis | Recorded per source in `sources.legal_access_method`; a source cannot be enabled without one |
| Residency | Personal data stored in-Kingdom. Backups in-Kingdom |
| **Cross-border transfer** | **The LLM call is the transfer risk.** Payloads are redacted before leaving the region and a transfer assessment is recorded per provider. If assessment or policy forbids it, the provider abstraction (ADR-006) lets us move to an in-Kingdom or self-hosted model without a code change — this is a specific, concrete reason the abstraction exists |
| Encryption | TLS 1.3 in transit; AES-256 at rest for database, object storage and backups; KMS-managed keys with rotation |
| Subject rights | Access, correction, deletion supported via admin tooling; deletion cascades to derived artifacts and embeddings |
| Retention | Per-category policy, enforced by scheduled jobs, not by intention |
| Breach response | Documented runbook with SDAIA notification timelines |

**Never logged:** passwords, tokens, API keys, full request bodies on auth routes, raw LLM
payloads containing user text. A structured-logging redaction filter runs by default and is
tested — a redactor nobody tests is a redactor that silently stopped working.

## 5. Prompt injection — the defining AI-security problem here

Every listing description, auction brochure and PDF is attacker-controlled text. Someone who
wants their property to score well can write instructions into it. Defence is layered, because
no single layer is sufficient:

1. **Structural separation.** External content is never concatenated into an instruction.
   It is passed in a delimited, labelled data block with a standing system instruction that
   content inside it is data to be analysed and never instruction to be followed.
2. **No tools on untrusted paths.** The extraction agent has **no tool access at all.** It reads
   text and returns a schema. There is nothing for an injected instruction to actuate.
3. **Structured output as a containment boundary.** The response must validate against a
   Pydantic schema. Prose smuggled out of a schema-constrained response has nowhere to land.
4. **Range and sanity validation after the model.** Even a "successful" injection claiming a
   200 m² area is 2,000 m² is rejected by range checks. **The model is not the last line of defence.**
5. **Deterministic supremacy.** Scores and valuations are computed from validated fields. An
   injection can at worst corrupt an extracted *input*, which is then subject to cross-source
   agreement checks — it can never directly set an output.
6. **Detection and quarantine.** Heuristics for injection patterns ("ignore previous", "system:",
   role markers, unusual instruction density) flag the source record; repeat offenders
   quarantine the source and alert an admin.
7. **Least privilege on agents that do have tools.** The verification agent may only call an
   allowlist of registered official-source clients with fixed parameter shapes. It cannot
   construct an arbitrary URL.

**Explicit trust boundary:** everything from Layer B is untrusted. Everything from Layer A is
untrusted *content* from a trusted *origin* — an official PDF is still parsed under the same
rules, because a trusted source can still carry attacker-authored text.

## 6. Supply chain and infrastructure

- Pinned dependencies with lockfiles; SBOM per build; container images signed and admission-verified.
- Base images minimal and rebuilt on a schedule; containers run non-root, read-only root
  filesystem, dropped capabilities.
- Kubernetes: NetworkPolicies default-deny with explicit allows; secrets from an external
  secret manager via CSI, never in manifests or environment files committed to git.
- Terraform state encrypted and locked; IaC scanning in CI; plan review required for production.
- CI: SAST, dependency scan, container scan, IaC scan, secret scanning on every merge request.
  **A failing security gate blocks merge** — it is not an advisory annotation.

## 7. Audit

Immutable, append-only `audit_events` covering authentication, authorisation decisions, data
access on sensitive entities, ingestion runs, agent runs, score changes, and every admin action.
Written in the same transaction as the action so an action cannot succeed unaudited.

Retention aligned to regulatory expectations; export supported for compliance review.

## 8. Threat model summary

| Threat | Likelihood | Impact | Primary mitigation |
|---|---|---|---|
| Prompt injection to inflate a score | **High** | **High** | §5 layers 2, 4, 5 — deterministic scoring is the structural answer |
| Cross-tenant data leak | Medium | High | RLS + app scoping + explicit tests |
| Credential compromise (admin) | Medium | High | MFA, short sessions, audited admin actions |
| Scraping of our own analysis | High | Medium | Rate limits, auth-gated detail views, watermarked exports |
| PDPL violation via ingested PII | **Medium** | **High** | Boundary redaction + source policy + minimisation |
| Cost exhaustion via LLM-triggering routes | Medium | Medium | Per-route, per-org budgets and ceilings |
| Poisoned source (fake listings to move district metrics) | Medium | High | Robust statistics, outlier rejection, source confidence weighting, cross-source agreement |
| Supply-chain compromise | Low | High | Pinning, SBOM, signed images, scanning gates |
| Model provider outage | Medium | Low | Provider failover; deterministic pipeline degrades gracefully |
