/**
 * Reverse proxy from Cloudflare Pages to the origin API.
 *
 * WHY A PROXY AND NOT A CROSS-ORIGIN FETCH
 * ----------------------------------------
 * The obvious deployment is a client on `bahkali-tek.com` calling an API on
 * `api.bahkali-tek.com`. That is cross-site, and it costs three things at once:
 * CORS preflights on every request; a session cookie that must become
 * `SameSite=None; Secure` and is therefore sent on every cross-site request a
 * browser can be tricked into making; and — the one that actually breaks it —
 * Cloudflare Access on the API hostname, which answers an unauthenticated XHR
 * with a redirect to a login page that `fetch` cannot usefully follow. The
 * request fails as an opaque CORS error and there is nothing in the console
 * that says why.
 *
 * Proxying through the same origin removes all three. The browser only ever
 * talks to `bahkali-tek.com`, the cookie stays `SameSite=Lax` and first-party,
 * one Access application covers the whole site, and the client's existing
 * relative-path fetches work unchanged — there is no `API_BASE_URL` anywhere
 * in the client, because there does not need to be.
 *
 * The origin is reached with an Access SERVICE TOKEN rather than left open:
 * the browser is authenticated by Access at the edge, and this worker is
 * authenticated to the origin by a token only Cloudflare holds. The origin
 * server publishes no port to the internet at all — it reaches Cloudflare
 * outbound through a tunnel — so this proxy is the only path to it.
 */

/**
 * The slice of a Pages Function's context this proxy uses.
 *
 * Declared here rather than pulled from `@cloudflare/workers-types`. That
 * package exists mainly to supply the `PagesFunction` global, and adding a
 * dependency to this workspace's pinned catalog to name one function type is a
 * poor trade -- especially when the alternative is that `functions/` stays
 * OUTSIDE tsconfig's include and deploys untypechecked, which is how it started.
 * Everything else here (Request, Response, Headers, fetch) is DOM lib and needs
 * no declaration.
 */
export interface PagesContext<E> {
	request: Request;
	env: E;
}

export interface ProxyEnv {
	/** Origin base, e.g. https://origin.bahkali-tek.com. No trailing path. */
	ORIGIN_URL?: string;
	/** Cloudflare Access service token. Both or neither. */
	ORIGIN_ACCESS_CLIENT_ID?: string;
	ORIGIN_ACCESS_CLIENT_SECRET?: string;
}

/**
 * Headers that must not be forwarded.
 *
 * `host` would make the origin believe it is this hostname. The `cf-*` set is
 * added by Cloudflare and includes `cf-connecting-ip` and the Access identity
 * JWT: forwarding them from an inbound request would let a caller assert their
 * own values, and Cloudflare adds the real ones again on the outbound hop.
 * `content-length` is dropped because the body is re-encoded here and a stale
 * length is worse than none.
 */
const STRIPPED = new Set([
	'host',
	'connection',
	'keep-alive',
	'transfer-encoding',
	'upgrade',
	'content-length',
	'cf-connecting-ip',
	'cf-ipcountry',
	'cf-ray',
	'cf-visitor',
	'cf-access-jwt-assertion',
	'cf-access-authenticated-user-email',
	'cf-access-client-id',
	'cf-access-client-secret',
]);

export function forwardHeaders(incoming: Headers, env: ProxyEnv): Headers {
	const headers = new Headers();
	for (const [name, value] of incoming) {
		if (!STRIPPED.has(name.toLowerCase())) {
			headers.set(name, value);
		}
	}
	// Service-token authentication to an Access-protected origin. Set only when
	// BOTH halves are present: half a credential is not a weaker credential, it
	// is a request that fails at the origin with an error naming neither half.
	const id = env.ORIGIN_ACCESS_CLIENT_ID;
	const secret = env.ORIGIN_ACCESS_CLIENT_SECRET;
	if (id !== undefined && id !== '' && secret !== undefined && secret !== '') {
		headers.set('cf-access-client-id', id);
		headers.set('cf-access-client-secret', secret);
	}
	return headers;
}

/** The origin URL for an inbound request, preserving path and query. */
export function originUrl(request: Request, origin: string): string {
	const inbound = new URL(request.url);
	const target = new URL(origin);
	// Path is taken from the inbound URL verbatim. It has already been resolved
	// by the runtime, so `..` cannot climb out of the origin's own host.
	target.pathname = inbound.pathname;
	target.search = inbound.search;
	return target.toString();
}

export async function proxy(request: Request, env: ProxyEnv): Promise<Response> {
	const origin = env.ORIGIN_URL;
	if (origin === undefined || origin === '') {
		// A misconfiguration, said plainly. Falling through to the static asset
		// handler would answer an API call with index.html, and the client would
		// report "unexpected token < in JSON" — a message that sends whoever
		// reads it to debug the wrong layer entirely.
		return Response.json(
			{ message: 'ORIGIN_URL is not configured on this Pages project', statusCode: 502 },
			{ status: 502 },
		);
	}

	const response = await fetch(originUrl(request, origin), {
		method: request.method,
		headers: forwardHeaders(request.headers, env),
		body: request.method === 'GET' || request.method === 'HEAD' ? undefined : request.body,
		// Never follow: a 302 from the origin is an answer the browser must see,
		// and following one here would silently turn an Access login redirect
		// into a proxied login page served from this site's own origin.
		redirect: 'manual',
	});

	// Rebuilt rather than returned as-is: a Response from fetch has immutable
	// headers, and `set-cookie` has to be re-emitted for this hostname.
	const headers = new Headers(response.headers);
	headers.delete('content-encoding');
	headers.delete('content-length');
	return new Response(response.body, { status: response.status, headers });
}

if (import.meta.vitest != null) {
	describe('forwardHeaders', () => {
		it('drops Host so the origin does not believe it is this hostname', () => {
			const headers = forwardHeaders(new Headers({ host: 'bahkali-tek.com', accept: 'x' }), {});
			expect(headers.get('host')).toBeNull();
			expect(headers.get('accept')).toBe('x');
		});

		it('refuses a caller-supplied Access identity', () => {
			// These are added by Cloudflare on the outbound hop. Forwarding an
			// inbound copy would let anyone assert an authenticated email.
			const headers = forwardHeaders(
				new Headers({
					'cf-access-jwt-assertion': 'forged',
					'cf-access-authenticated-user-email': 'admin@example.com',
					'cf-connecting-ip': '1.2.3.4',
				}),
				{},
			);
			expect(headers.get('cf-access-jwt-assertion')).toBeNull();
			expect(headers.get('cf-access-authenticated-user-email')).toBeNull();
			expect(headers.get('cf-connecting-ip')).toBeNull();
		});

		it('refuses caller-supplied service-token headers', () => {
			// Otherwise a request could present its own token to the origin and
			// bypass the one this proxy is trusted to hold.
			const headers = forwardHeaders(
				new Headers({ 'cf-access-client-id': 'mine', 'cf-access-client-secret': 'mine' }),
				{},
			);
			expect(headers.get('cf-access-client-id')).toBeNull();
		});

		it('adds the service token when both halves are configured', () => {
			const headers = forwardHeaders(new Headers(), {
				ORIGIN_ACCESS_CLIENT_ID: 'id.access',
				ORIGIN_ACCESS_CLIENT_SECRET: 'secret',
			});
			expect(headers.get('cf-access-client-id')).toBe('id.access');
			expect(headers.get('cf-access-client-secret')).toBe('secret');
		});

		it('sends neither half when only one is configured', () => {
			// Half a credential is not a weaker credential; it is a request that
			// fails at the origin with an error naming neither half.
			const headers = forwardHeaders(new Headers(), { ORIGIN_ACCESS_CLIENT_ID: 'id.access' });
			expect(headers.get('cf-access-client-id')).toBeNull();
		});

		it('keeps the cookie, which is how the session survives the hop', () => {
			const headers = forwardHeaders(new Headers({ cookie: 'sreoi_session=t' }), {});
			expect(headers.get('cookie')).toBe('sreoi_session=t');
		});
	});

	describe('originUrl', () => {
		it('preserves path and query', () => {
			expect(originUrl(
				new Request('https://bahkali-tek.com/api/v1/map/opportunities?limit=1000'),
				'https://origin.example.com',
			)).toBe('https://origin.example.com/api/v1/map/opportunities?limit=1000');
		});

		it('cannot be walked out of the origin host', () => {
			// The runtime resolves `..` before this sees the URL, so a traversal
			// attempt lands on a path, never on another host.
			expect(originUrl(
				new Request('https://bahkali-tek.com/api/../../etc/passwd'),
				'https://origin.example.com',
			)).toBe('https://origin.example.com/etc/passwd');
		});

		it('ignores any path on the configured origin rather than doubling it', () => {
			expect(originUrl(
				new Request('https://bahkali-tek.com/api/v1/facets'),
				'https://origin.example.com/ignored',
			)).toBe('https://origin.example.com/api/v1/facets');
		});
	});

	describe('proxy', () => {
		it('fails loudly when the origin is not configured', async () => {
			// Falling through to the static handler would answer an API call with
			// index.html, and the client would report a JSON parse error that
			// sends the reader to debug the wrong layer.
			const response = await proxy(new Request('https://x/api/v1/facets'), {});
			expect(response.status).toBe(502);
			expect(await response.json()).toMatchObject({ statusCode: 502 });
		});
	});
}
