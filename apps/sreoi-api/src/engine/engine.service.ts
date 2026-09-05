/**
 * Client for the Python valuation engine.
 *
 * WHY THIS SERVICE EXISTS RATHER THAN A TYPESCRIPT PORT
 * ----------------------------------------------------
 * Two categories of work stay in Python deliberately.
 *
 * Money. Every published number -- fair value, the interval, true acquisition
 * cost, discount, opportunity score -- comes from ~2,600 lines of pure
 * deterministic domain logic with 515 tests pinning it: weighted median and
 * quantiles, Kish effective sample size, IQR outlier rejection, field-level
 * provenance, and the invariant that an unknown material cost line *refuses*
 * the discount rather than estimating it. Reimplementing that here would be
 * where silent numeric drift comes from, and the specification requires a
 * `method_version` change for any alteration to a published figure. This
 * service therefore transports numbers; it never computes one.
 *
 * Credentials. The Python side owns Argon2 hashing, the `memberships` lookup
 * that makes the database authoritative over a token's role claim, and the
 * out-of-band audit trail that survives a request rollback. Verifying
 * credentials here as well would mean two implementations of one security
 * control, and the weaker of the two would decide. So this asks the engine who
 * the caller is and believes the answer.
 */

import { Injectable, Logger, ServiceUnavailableException } from '@nestjs/common';
import { loadConfig } from '../config.ts';

export type Credential
	= | { kind: 'bearer'; value: string }
		| { kind: 'apiKey'; value: string }
		| { kind: 'cookie'; value: string };

export interface EnginePrincipal {
	subject: string;
	email: string | null;
	organizationId: string;
	organization: string;
	role: string;
	credential: string;
}

@Injectable()
export class EngineService {
	private readonly logger = new Logger(EngineService.name);
	private readonly config = loadConfig();

	/**
	 * Resolve a credential to a principal, or null if the engine rejects it.
	 *
	 * `null` means "not authenticated" and is the caller's cue to send 401.
	 * A transport failure is *not* null -- it throws -- because treating an
	 * unreachable authority as a rejection would be indistinguishable from a
	 * bad password, and treating it as success would be catastrophic.
	 */
	async resolvePrincipal(credential: Credential): Promise<EnginePrincipal | null> {
		const response = await this.call('/auth/me', {
			headers: this.credentialHeaders(credential),
		});
		if (response.status === 401 || response.status === 403) {
			return null;
		}
		if (!response.ok) {
			throw new ServiceUnavailableException(`identity service returned ${response.status}`);
		}
		const body = (await response.json()) as Record<string, unknown>;
		return {
			subject: String(body.subject ?? ''),
			email: body.email == null ? null : String(body.email),
			organizationId: String(body.organization_id ?? ''),
			organization: String(body.organization ?? ''),
			role: String(body.role ?? ''),
			credential: String(body.credential ?? ''),
		};
	}

	/** Proxy a GET to the engine, carrying the caller's own credential. */
	async get(path: string, credential: Credential): Promise<unknown> {
		const response = await this.call(path, { headers: this.credentialHeaders(credential) });
		if (!response.ok) {
			throw new ServiceUnavailableException(`engine ${path} returned ${response.status}`);
		}
		return response.json();
	}

	/**
	 * Relay a credential-issuing POST to the engine and hand back its raw parts.
	 *
	 * Unlike `get`, a non-2xx is NOT an error here. `/auth/login` answers 401
	 * for a wrong password and 403 when password login is disabled, and both
	 * are meaningful answers the browser must see. Mapping them to 503 would
	 * tell the user the service is down when in fact their password is wrong.
	 *
	 * `Set-Cookie` is returned for the caller to relay, because the engine sets
	 * the session cookie for ITS OWN host. Once the browser talks to the API
	 * origin instead, the cookie has to be re-emitted there or the session is
	 * established on a host the browser will never visit again.
	 */
	async relayPost(
		path: string,
		body: string,
		headers: Record<string, string>,
	): Promise<{ status: number; setCookie: string[]; body: string; contentType: string }> {
		const response = await this.call(path, {
			method: 'POST',
			headers: { 'content-type': 'application/json', ...headers },
			body,
		});
		return {
			status: response.status,
			// getSetCookie() rather than get('set-cookie'): a joined header string
			// cannot be split safely, because cookie Expires values contain commas.
			setCookie: response.headers.getSetCookie(),
			body: await response.text(),
			contentType: response.headers.get('content-type') ?? 'application/json',
		};
	}

	private credentialHeaders(credential: Credential): Record<string, string> {
		switch (credential.kind) {
			case 'bearer':
				return { authorization: `Bearer ${credential.value}` };
			case 'apiKey':
				return { 'x-api-key': credential.value };
			case 'cookie':
				return { cookie: `sreoi_session=${credential.value}` };
		}
	}

	private async call(path: string, init: RequestInit): Promise<Response> {
		const url = new URL(path, this.config.engineUrl);
		const signal = AbortSignal.timeout(this.config.engineTimeoutMs);
		try {
			return await fetch(url, { ...init, signal, redirect: 'manual' });
		}
		catch (cause) {
			// Deliberately does not include the credential or the full error, which
			// can carry request headers.
			this.logger.error(`engine unreachable at ${url.pathname}`);
			throw new ServiceUnavailableException('valuation engine is unreachable', {
				cause,
			});
		}
	}
}
