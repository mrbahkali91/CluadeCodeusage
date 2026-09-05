# Deploying to Cloudflare at `bahkali-tek.com`

No Docker. The origin runs natively under systemd.

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
            your server:  sreoi-api.service ─► sreoi-engine.service ─► PostGIS
                          (nothing listens on a public interface)
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
receives requests over that connection. The engine binds `127.0.0.1:8000`, the
API `127.0.0.1:3000`, PostgreSQL its unix socket. No inbound firewall rule, no
public IP exposure, no TLS certificate on the box, and a port scan of it finds
nothing. `install.sh` prints the listening sockets at the end so that claim is
checkable rather than asserted.

---

## 1. The origin server

Any systemd Linux box. 2 vCPU / 4 GB is comfortable. Debian 12 or Ubuntu 24.04
if you want the `--install-packages` shortcut to work.

```bash
git clone -b claude/saudi-realestate-opportunity-platform-n0bn70 \
  https://github.com/mrbahkali91/CluadeCodeusage
cd CluadeCodeusage/deploy/origin
cp .env.example .env
```

Fill in `.env` — the database password, `PUBLIC_ORIGIN`, and an authentication
mode. Leave `CLOUDFLARE_TUNNEL_TOKEN` until step 2. Then:

```bash
sudo ./install.sh --check              # verifies everything, changes nothing
sudo ./install.sh --install-packages   # first run on a fresh box
sudo ./install.sh --seed               # ...and load the demo corpus
```

Decide authentication now rather than later. For anything beyond a demo,
configure `SREOI_OIDC_*` and leave `SREOI_AUTH_DEV_MODE` unset — `install.sh`
refuses both (the engine would not start) and refuses neither (the app would
serve nothing). The development password issuer is acceptable **only** behind
Cloudflare Access (step 5), because Access authenticates every visitor before a
request reaches this server at all.

What the script does, all of it re-runnable:

- creates the `sreoi` system account (no home, no login shell) and
  `/var/lib/sreoi` for the runtimes' caches
- creates the `sreoi` database role **`NOSUPERUSER NOBYPASSRLS`** — this is
  load-bearing, not hygiene: PostgreSQL exempts superusers from row-level
  security unconditionally, and even `FORCE` does not apply to them, so a
  superuser application role would leave all sixteen tenant policies in place
  and enforced against nobody
- installs PostGIS and `pg_trgm` as the cluster superuser, then hands the
  schema to the app role
- writes `/etc/sreoi/engine.env` and `/etc/sreoi/api.env` at `0640 root:sreoi`
  — a systemd unit is world-readable (`systemctl cat` prints it for any user),
  so secrets never go in the unit
- builds the Python venv, installs API dependencies, migrates to head
- installs and starts `sreoi-engine.service` and `sreoi-api.service`, then
  health-checks both

To redeploy after a `git pull`: `sudo ./install.sh`. Logs:
`journalctl -u sreoi-engine -u sreoi-api -f`.

## 2. The tunnel

Cloudflare dashboard → Zero Trust → Networks → Tunnels → **Create a tunnel** →
Cloudflared. Copy the token out of the command it shows into
`CLOUDFLARE_TUNNEL_TOKEN` in `.env`, then re-run `sudo ./install.sh` — it
installs the `cloudflared` binary and its service for you.

In the dashboard, give the tunnel a public hostname:

- **Public hostname:** `origin.bahkali-tek.com`
- **Service:** `http://127.0.0.1:3000` — the API. Not the engine: the API is
  what the client talks to, and it is what applies the tenant scoping.

```bash
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

Redeploy after setting them — a Pages Function reads its environment at deploy
time, so secrets added afterwards are not picked up until the next deploy.

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

## What was actually verified, and what was not

**Verified by running it**, on a machine without systemd as PID 1 (so
`systemctl` was stubbed and the services were then started by hand with the
units' exact user, `EnvironmentFile` and `ExecStart`):

- every configuration guard refuses correctly — empty secrets, a dev secret
  under 32 characters, both auth modes at once, neither, a missing `.env`
- the database role is created `NOSUPERUSER NOBYPASSRLS` (confirmed by querying
  `pg_roles` afterwards), PostGIS and `pg_trgm` install, migrations reach head
- the venv builds, API dependencies install, and the API's module graph loads
- both services start under the `sreoi` account from `/opt/sreoi` and answer
  their health checks
- through the real API: sign-in returns 200 with a session cookie, the map
  endpoint returns 56 features, search returns 56 opportunities
- through the Pages Function proxy against that origin: sign-in relays 200 with
  the cookie, an authenticated read returns 56 features, a wrong password
  relays 401 rather than 502, and a forged
  `cf-access-authenticated-user-email` header buys nothing
- both unit files pass `systemd-analyze verify`

**Not verified:** `systemctl enable/start` under real systemd, `cloudflared
service install`, and everything on the Cloudflare side — the tunnel, the
Access policies, the Pages deploy. Those need your account. Expect to fix
something on the first real run.

## What is still true after all of this

**The data is synthetic.** Comparable transactions come from a generated
fixture corpus, and the banner across the top of every page says so. The engine
is real — the valuations, the confidence gate, the cost invariant are all
genuine computations — but they are computed over invented evidence. Ingest
real transactions before showing anyone a number as if it meant something:
`python -m sreoi_pipeline.cli opendata --dry-run` (see `platform/README.md`).

**The development password issuer is not an identity provider.** It signs HS256
tokens with a shared secret and has never been validated against a real OIDC
issuer. Cloudflare Access is what makes it acceptable here; remove Access and
it is not.

**Sign-in is throttled per address, in one process.** Five failures in fifteen
minutes locks that address out for fifteen. The counter lives in memory, so it
resets on restart and does not span replicas. Scale the API past one process
and it needs a shared store.

**`install.sh` uses `--ignore-scripts` for the API's dependencies.** Partly out
of necessity — the workspace root's `prepare` script runs `git config`, and the
deployed copy has no `.git`, so it exits 128 and aborts the install — and
partly on purpose: install scripts are arbitrary code from every transitive
dependency, running on the host that holds the database credentials. The API's
own dependencies need none of them.
