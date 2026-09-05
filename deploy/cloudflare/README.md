# Deploying to Cloudflare at `bahkali-tek.com`

## What Cloudflare can and cannot run

Cloudflare cannot host this platform. Workers and Pages Functions run V8
isolates with no filesystem and no long-lived TCP; this stack is a Python
FastAPI engine, a NestJS API and PostgreSQL 16 with PostGIS. D1 is SQLite and
has no spatial types, so `ST_Intersects`, `ST_AsGeoJSON` and the district
geometry the map is drawn from have nowhere to run.

So the deployment is split, and the split is the point:

```
browser ──► bahkali-tek.com                    Cloudflare Pages
              │  static SPA (dist/)
              │  /api/* and /auth/*  ──────►   Pages Function (functions/)
              │                                  adds Access service token
              ▼
            origin.bahkali-tek.com             Cloudflare Tunnel
              │                                  ▲ outbound only
              ▼                                  │
            your server:  api ─► engine ─► PostGIS
                          (no published ports at all)
```

Two properties follow from that shape and are worth stating explicitly.

**The browser only ever talks to one origin.** `/api/*` and `/auth/*` are
proxied by a Pages Function rather than pointed at a second hostname. That
removes CORS preflights, keeps the session cookie first-party and
`SameSite=Lax` instead of `SameSite=None`, and — the one that actually breaks
the naive version — means Cloudflare Access does not answer an XHR with a login
redirect that `fetch` cannot follow. It also means there is no `API_BASE_URL`
anywhere in the client, because there does not need to be.

**The server publishes nothing.** `cloudflared` dials *out* to Cloudflare and
receives requests over that connection. No inbound firewall rule, no public IP
exposure, no TLS certificate on the box, and a port scan of it finds nothing.

---

## 1. The origin server

Any Linux box with Docker. 2 vCPU / 4 GB is comfortable.

```bash
git clone -b claude/saudi-realestate-opportunity-platform-n0bn70 \
  https://github.com/mrbahkali91/CluadeCodeusage
cd CluadeCodeusage/deploy/origin
cp .env.example .env
```

Fill in `.env`. Generate the two database passwords with
`openssl rand -base64 32`; leave `CLOUDFLARE_TUNNEL_TOKEN` until step 2.

Decide authentication now rather than later. For anything beyond a demo,
configure `SREOI_OIDC_*` and leave `SREOI_AUTH_DEV_MODE` unset — the
application refuses to start with both. The development password issuer is
acceptable **only** behind Cloudflare Access (step 3), because Access
authenticates every visitor before a request reaches this server at all.

```bash
docker compose up -d --build
docker compose logs -f engine       # note the generated admin password
```

The engine migrates to head on start and prints the first user's password once
if you left `ADMIN_PASSWORD` empty. It is not recoverable afterwards.

Seed demonstration data, or skip this and ingest real transactions instead:

```bash
docker compose exec engine python -m sreoi_pipeline.cli reset-and-demo
```

## 2. The tunnel

Cloudflare dashboard → Zero Trust → Networks → Tunnels → **Create a tunnel** →
Cloudflared → Docker. Copy the token out of the command it shows into
`CLOUDFLARE_TUNNEL_TOKEN` in `.env`, then:

- **Public hostname:** `origin.bahkali-tek.com`
- **Service:** `http://api:3000` — the API container, by its compose service
  name. Not the engine: the API is what the client talks to, and it is what
  applies the tenant scoping.

```bash
docker compose up -d cloudflared
curl -I https://origin.bahkali-tek.com/health     # expect 200
```

## 3. Lock the origin down with a service token

Right now `origin.bahkali-tek.com` is public. It should only be reachable by
the Pages Function.

Zero Trust → Access → Service auth → **Create service token**. Copy both halves
once; the secret is not shown again.

Zero Trust → Access → Applications → **Add a self-hosted application**:

- Domain: `origin.bahkali-tek.com`
- Policy: *Service Auth* → include → the service token you just created

`curl https://origin.bahkali-tek.com/health` should now fail. That failure is
the control working.

## 4. The client on Pages

```bash
cd apps/sreoi-web
pnpm install
pnpm run build
pnpm dlx wrangler@4 pages project create sreoi-web --production-branch main
pnpm dlx wrangler@4 pages deploy dist --project-name sreoi-web
```

Then the three runtime secrets. **None of them belong in git** — that is why
`wrangler.toml` names them and does not contain them:

```bash
pnpm dlx wrangler@4 pages secret put ORIGIN_URL --project-name sreoi-web
# https://origin.bahkali-tek.com
pnpm dlx wrangler@4 pages secret put ORIGIN_ACCESS_CLIENT_ID --project-name sreoi-web
pnpm dlx wrangler@4 pages secret put ORIGIN_ACCESS_CLIENT_SECRET --project-name sreoi-web
```

Redeploy after setting them — a Pages Function reads its environment at
deploy time, so secrets added afterwards are not picked up until the next
deploy.

Pages project → Custom domains → add `bahkali-tek.com`.

## 5. Access in front of the site

Zero Trust → Access → Applications → **Add a self-hosted application**:

- Domain: `bahkali-tek.com`
- Policy: include → Emails → your address (or Email domain → your org)

Now nobody reaches the site without authenticating to Cloudflare first, and the
application's own sign-in sits behind that. Two prompts, deliberately: Access
decides *who may reach the deployment*, the application decides *which
organisation's opportunities they see*. The second is what row-level security
enforces in the database, and Access cannot express it.

---

## What is still true after all of this

**The data is synthetic.** Comparable transactions come from a generated
fixture corpus, and the banner across the top of every page says so. The engine
is real — the valuations, the confidence gate, the cost invariant are all
genuine computations — but they are computed over invented evidence. Ingest
real transactions before showing anyone a number as if it meant something:
`python -m sreoi_pipeline.cli opendata --dry-run` (see the repository README).

**The development password issuer is not an identity provider.** It signs HS256
tokens with a shared secret and has never been validated against a real OIDC
issuer. Cloudflare Access is what makes it acceptable here; remove Access and
it is not.

**Sign-in is throttled per address, in one process.** Five failures in fifteen
minutes locks that address out for fifteen. The counter lives in memory, so it
resets on restart and does not span replicas. Scale the API past one container
and it needs a shared store.

**`docker compose up` for this stack has not been executed by its author.** The
database initialisation SQL has been run and verified against a real PostgreSQL
16 + PostGIS 3.4 cluster, and every service in it runs natively via
`./run-local.sh`, which has been executed end to end many times. The
orchestration itself is unverified. Expect to fix something on the first run.
