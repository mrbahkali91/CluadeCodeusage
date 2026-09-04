# Slice: authentication, authorisation and tenant isolation

Closes the largest gap in every prior slice: the application served all data to
anyone who could reach the port.

## What this slice does

- **Fails closed.** With no identity configuration the app refuses every
  request with 503 rather than serving data. Enforcement is middleware with a
  default-deny posture; exposure is an explicit entry in `PUBLIC_PATHS`, and
  that list is nine lines long.
- **Two credential types.** OIDC bearer tokens verified against the provider's
  JWKS, and Argon2id-hashed API keys. A development password issuer exists for
  local use and is opt-in; the app **refuses to start** if it is enabled
  alongside a configured OIDC issuer.
- **The database is authoritative over the token.** A token's `role` claim is
  never trusted: the role comes from the `memberships` row. A forged claim
  cannot escalate, and an API key cannot exceed the role of whoever minted it.
- **Tenant isolation in PostgreSQL, not in application code.** Eight
  customer-data tables carry `organization_id` with `ENABLE ROW LEVEL SECURITY`
  and `FORCE ROW LEVEL SECURITY`; each request runs
  `set_config('app.organization_id', ..., true)` and the policies key on it. The
  isolation tests issue queries with **no application-level filter at all** and
  assert the other tenant's rows are invisible, so they test the control rather
  than the caller.
- **Audit trail.** Logins, refusals and key minting are recorded. Security
  failures are committed out of band so a rolled-back request cannot erase the
  record of its own refusal. No credential ever reaches a `detail` field.

## Decisions worth knowing

**The app role must not be a superuser.** PostgreSQL exempts superusers from
row-level security unconditionally, and `FORCE` does not apply to them either.
Before this was fixed, all sixteen policies were decorative — present in
`pg_policies`, enforced against nobody. `deploy-local.sh` now creates the role
`NOSUPERUSER NOBYPASSRLS`, re-asserts it on every run, and has a superuser
install the extensions instead.

**RLS lives in `sreoi_persistence/rls.py`, not only in the migration.** Tests
build their schema with `create_all`, so a policy defined only in migration SQL
would never be exercised by any test. Both paths now call the same function.

**Tenancy is per-table, not global.** Market data (properties, transactions,
comparables, price indices) is shared: it is observed fact and every tenant
values against the same evidence. Customer-created data (watchlists, alerts,
memos, documents, feedback) is tenant-scoped. Platform operations (audit,
source health, quality snapshots) is neither.

**Auth settings are read per request.** An earlier version froze them at import
time, which made configuration untestable without `importlib.reload` — and a
reloaded module leaked its state into every test that imported it afterwards
(44 failures in five unrelated modules). The middleware now reads the
environment on each request; `main` calls `load_settings()` once at import
purely to fail fast on contradictory configuration.

**Credential hashing lives in `sreoi_persistence`, not the API layer.** The
hash *is* the stored column, so the algorithm and the schema have to change
together. This also keeps the layering contract intact: the pipeline's
bootstrap command needs to hash a password and must not import the API.

## Findings from running it

Two defects that the test suite could not have caught, because both were in the
deployment path:

1. `LoginRequest.email` was `EmailStr`, which rejects addresses without a dot in
   the host. The deploy script's own default account, `admin@localhost`, could
   therefore never sign in. An endpoint stricter than its identity store makes
   accounts unreachable, and the 422-vs-401 difference also answered a question
   the endpoint deliberately refuses to answer. It now accepts any address the
   store can hold.
2. A stale server from an earlier session held port 8000, so a re-run of
   `deploy-local.sh` failed to bind and its verification silently exercised the
   *previous, pre-auth* build — which is exactly what a passing anonymous
   request looked like. Verified again against a confirmed-fresh process.

## Running it on a laptop

Prerequisites: Python 3.11, PostgreSQL 16 with PostGIS 3.4 and `pg_trgm`, and
`uv`.

```bash
make install
make deploy            # or: RESET=1 ./deploy-local.sh to rebuild the schema
```

The script creates the role and database, installs the extensions as a
superuser, migrates, seeds, bootstraps an organisation and first user, prints
the credentials once, and serves on <http://127.0.0.1:8000>. Override
`ADMIN_EMAIL` / `ADMIN_PASSWORD` to choose them; omit `ADMIN_PASSWORD` and one
is generated and printed once.

Sign in at `/auth/signin`. An unauthenticated browser request to any page
redirects there with its destination preserved; an API client gets 401 with
`WWW-Authenticate`, because a redirect to HTML reads as success to anything
checking only `response.ok`. A `POST` is never redirected — bouncing it would
discard the body and look like it succeeded.

## Verified

- 512 tests pass under randomised ordering; `ruff`, `mypy --strict` and the
  import-linter contracts are clean.
- `alembic downgrade base` → `upgrade head` round-trips, and `alembic check`
  reports no drift.
- End-to-end against a fresh deployment: anonymous browser redirected,
  anonymous API 401, sign-in, all six pages, API key minted and used, key
  cannot exceed its minter's role, sign-out invalidates, `pg_roles` confirms
  the app role is neither superuser nor bypassrls, 16 policies present.

## Not done

- No refresh-token rotation or session revocation list: a dev token is valid
  for its 12 hours. With OIDC the provider owns this.
- No rate limiting on `/auth/login`. Argon2id makes each attempt expensive, but
  that is not a substitute.
- No OIDC provider has actually been configured against this code. The JWKS
  path is unit-tested with a locally generated key; it is **REQUIRES
  VALIDATION** against a real issuer.
