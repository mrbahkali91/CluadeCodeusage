/**
 * Sign-in against this API's own origin.
 *
 * WHY THIS EXISTS
 * ---------------
 * The engine owns credentials -- Argon2 verification, the `memberships` lookup
 * that makes the database authoritative over a token's role claim, and the
 * out-of-band audit entry that survives a rollback. None of that is
 * reimplemented here and none of it should be.
 *
 * But the engine set the session cookie for ITS OWN host. In development that
 * was invisible: the engine and the client both answered on 127.0.0.1, and
 * cookies ignore the port, so signing in on the engine's page authenticated
 * the client too. Deployed, the browser only ever talks to the web origin and
 * never sees the engine at all -- so there was no way to sign in from the
 * deployed client, and no page anywhere that offered to.
 *
 * This relays the credential exchange and re-emits `Set-Cookie` on the origin
 * the browser is actually using. It verifies nothing itself; it decides
 * nothing; a wrong password is still the engine's 401.
 *
 * THESE ROUTES ARE PUBLIC BY NECESSITY. They are how a caller obtains a
 * credential, so they cannot require one. That makes them the only
 * unauthenticated write path in the service, and it is why the throttle below
 * exists.
 */

import type { Response } from 'express';
import type { AuthedRequest } from './auth.guard.ts';
import { Body, Controller, Post, Req, Res } from '@nestjs/common';
import { EngineService } from '../engine/engine.service.ts';
import { Public } from './auth.guard.ts';
import { LoginThrottle } from './throttle.ts';

/**
 * What is accepted in a login body.
 *
 * Only these three keys are forwarded. Passing the request body through
 * verbatim would let a caller smuggle any other field the engine's schema
 * happens to accept now or later, and the shape of that surface would be
 * decided by whatever the engine adds rather than by this boundary.
 */
export function loginBody(raw: unknown): string {
	const source = (typeof raw === 'object' && raw !== null ? raw : {}) as Record<string, unknown>;
	const body: Record<string, string> = {
		email: typeof source.email === 'string' ? source.email : '',
		password: typeof source.password === 'string' ? source.password : '',
	};
	if (typeof source.organization === 'string' && source.organization !== '') {
		body.organization = source.organization;
	}
	return JSON.stringify(body);
}

/**
 * Rewrite a cookie the engine issued for its own host so it works on this one.
 *
 * `Domain` is dropped, which scopes the cookie to whatever host served the
 * response -- correct in every deployment, and the only option that does not
 * require this service to know its own public hostname.
 *
 * `Secure` is added when the request arrived over HTTPS. Behind Cloudflare the
 * connection to this process is plain HTTP, so `req.secure` is only true when
 * Express trusts `X-Forwarded-Proto`; that is why `trust proxy` is set at
 * bootstrap. Without both, a session cookie for a public HTTPS site would be
 * sent without Secure and could leak over a downgraded request.
 */
export function rewriteCookie(cookie: string, secure: boolean): string {
	const parts = cookie
		.split(';')
		.map(part => part.trim())
		.filter(part => part !== '' && !/^domain=/i.test(part) && !/^secure$/i.test(part));
	if (secure) {
		parts.push('Secure');
	}
	return parts.join('; ');
}

@Controller('auth')
export class AuthController {
	private readonly throttle = new LoginThrottle();

	constructor(private readonly engine: EngineService) {}

	@Public()
	@Post('login')
	async login(
		@Body() body: unknown,
		@Req() request: AuthedRequest,
		@Res() response: Response,
	): Promise<void> {
		// Throttled before the engine is called, so a password-guessing run
		// cannot use this endpoint to drive Argon2 verifications on the engine.
		// Argon2 is deliberately expensive; unthrottled, that cost becomes the
		// attacker's denial-of-service lever rather than their obstacle.
		const decision = this.throttle.check(clientKey(request));
		if (!decision.allowed) {
			response
				.status(429)
				.set('retry-after', String(decision.retryAfterSeconds))
				.json({ message: 'too many sign-in attempts', statusCode: 429 });
			return;
		}

		const relayed = await this.engine.relayPost('/auth/login', loginBody(body), {});
		if (relayed.status >= 400) {
			this.throttle.recordFailure(clientKey(request));
		}
		else {
			this.throttle.recordSuccess(clientKey(request));
		}
		this.send(relayed, request, response);
	}

	@Public()
	@Post('logout')
	async logout(@Req() request: AuthedRequest, @Res() response: Response): Promise<void> {
		// Public, and intentionally so: an expired or malformed session must
		// still be clearable. Requiring a valid credential to log out would
		// leave a user holding a cookie they cannot get rid of.
		const relayed = await this.engine.relayPost('/auth/logout', '{}', {});
		this.send(relayed, request, response);
	}

	private send(
		relayed: { status: number; setCookie: string[]; body: string; contentType: string },
		request: AuthedRequest,
		response: Response,
	): void {
		for (const cookie of relayed.setCookie) {
			response.append('set-cookie', rewriteCookie(cookie, request.secure));
		}
		response.status(relayed.status).type(relayed.contentType);
		// The engine's 204 logout has no body, and writing '' to a 204 is a
		// protocol violation Express will warn about.
		if (relayed.body === '') {
			response.end();
			return;
		}
		response.send(relayed.body);
	}
}

/**
 * The throttle bucket for a request.
 *
 * `req.ip` behind a proxy is the proxy unless Express trusts the forwarding
 * header, in which case it is the client. Keying on a single shared value
 * would make one user's failures throttle everyone, so the deployment sets
 * `trust proxy` and this reads the resolved address.
 */
export function clientKey(request: AuthedRequest): string {
	return request.ip ?? 'unknown';
}

if (import.meta.vitest != null) {
	describe('loginBody', () => {
		it('forwards only the three fields the engine accepts', () => {
			expect(JSON.parse(loginBody({
				email: 'a@b.c',
				password: 'pw',
				organization: 'acme',
				role: 'PLATFORM_ADMIN',
			}))).toEqual({ email: 'a@b.c', password: 'pw', organization: 'acme' });
		});

		it('drops an empty organization rather than sending one', () => {
			// The engine treats an absent organization as "any membership" and an
			// empty string as a slug that matches nothing.
			expect(JSON.parse(loginBody({ email: 'a@b.c', password: 'pw', organization: '' })))
				.toEqual({ email: 'a@b.c', password: 'pw' });
		});

		it('coerces missing or non-string credentials to empty, never to undefined', () => {
			// The engine's schema requires both keys; omitting one turns a wrong
			// password into a 422 schema error, which reads as a broken client.
			expect(JSON.parse(loginBody({ email: 42 }))).toEqual({ email: '', password: '' });
			expect(JSON.parse(loginBody(null))).toEqual({ email: '', password: '' });
		});
	});

	describe('rewriteCookie', () => {
		it('drops Domain so the cookie belongs to whoever served it', () => {
			expect(rewriteCookie('sreoi_session=t; Domain=engine.internal; Path=/; HttpOnly', false))
				.toBe('sreoi_session=t; Path=/; HttpOnly');
		});

		it('adds Secure on an https request', () => {
			expect(rewriteCookie('sreoi_session=t; Path=/', true)).toBe('sreoi_session=t; Path=/; Secure');
		});

		it('does not duplicate Secure when the engine already set it', () => {
			expect(rewriteCookie('sreoi_session=t; Path=/; Secure', true))
				.toBe('sreoi_session=t; Path=/; Secure');
		});

		it('strips Secure when the request is plain http', () => {
			// Otherwise a local http deployment sets a cookie the browser will
			// never send back, and sign-in appears to succeed and then fail.
			expect(rewriteCookie('sreoi_session=t; Path=/; Secure', false)).toBe('sreoi_session=t; Path=/');
		});

		it('keeps HttpOnly, SameSite and Max-Age untouched', () => {
			expect(rewriteCookie('sreoi_session=t; HttpOnly; SameSite=Lax; Max-Age=43200; Path=/', false))
				.toBe('sreoi_session=t; HttpOnly; SameSite=Lax; Max-Age=43200; Path=/');
		});
	});
}
