# @sreoi/api

NestJS API for the Saudi Real Estate Opportunity Intelligence platform.

## Where this sits

```
  @sreoi/web  (React + Vite, :5173)
       |  /api, /auth  -- proxied same-origin in dev
  @sreoi/api  (NestJS, :3000)     <- auth, tenancy, query, client contract
       |                    \
  PostgreSQL + PostGIS       Python engine (:8000)
   (reads, under RLS)         <- every number, and credential verification
```

## Two things stay in Python, deliberately

**Money.** Fair value, the interval, true acquisition cost, discount and the
opportunity score come from ~2,600 lines of pure deterministic domain logic
with 515 tests pinning it — weighted median and quantiles, Kish effective
sample size, IQR outlier rejection, field-level provenance, and the invariant
that an unknown material cost line _refuses_ the discount rather than
estimating it. Porting that to TypeScript is where silent numeric drift would
come from, and the specification requires a `method_version` change for any
alteration to a published figure. This service transports numbers; it never
computes one.

**Credentials.** The Python side owns Argon2 hashing, the `memberships` lookup
that makes the database authoritative over a token's role claim, and the
out-of-band audit trail that survives a request rollback. Verifying credentials
here as well would mean two implementations of one security control, and the
weaker would decide.

## Running it

```bash
# 1. Python engine (owns scoring and credentials)
cd platform && ./deploy-local.sh

# 2. This API
SREOI_DATABASE_URL=postgresql://sreoi:sreoi@127.0.0.1:5432/sreoi \
SREOI_ENGINE_URL=http://127.0.0.1:8000 \
  pnpm --filter @sreoi/api start

# 3. The client
pnpm --filter @sreoi/web dev     # http://127.0.0.1:5173
```

`SREOI_DATABASE_URL` accepts SQLAlchemy's `postgresql+psycopg://` form and
translates it, so one variable configures both services rather than two that
can drift apart and point at different databases.

## Things that will bite you

**The database role must not be a superuser.** PostgreSQL exempts superusers
from row-level security unconditionally, and `FORCE` does not apply to them
either, so a superuser role leaves all sixteen tenant policies enforced against
nobody. `DbService.assertNotSuperuser` refuses to start rather than trusting the
deployment.

**`ts/consistent-type-imports` is off, and must stay off.** NestJS resolves
dependencies from `design:paramtypes`, which `emitDecoratorMetadata` writes from
constructor parameter types. Those parameters are the only syntactic use of an
injected class, so the rule rewrites the import to `import type` and erases the
metadata the container reads. Running `lint --fix` did exactly that once and the
application stopped starting — while typecheck and tests still passed, because
nothing was wrong with the types.

**Bun, not `node --experimental-strip-types`.** Type stripping erases types but
does not transform decorators, so Node cannot run this. `bun -b` handles both,
and matches what `apps/mcp` already does.
