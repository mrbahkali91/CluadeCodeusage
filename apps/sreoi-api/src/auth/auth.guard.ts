/**
 * Default-deny authentication.
 *
 * Every route is protected unless it carries `@Public()`. That is the opposite
 * of the usual guard-per-controller arrangement, and it is deliberate: with
 * opt-in protection, a new controller added by someone who has not read this
 * file is exposed, and the failure is silent. With opt-out, the same mistake
 * yields a 401 that shows up the first time anyone calls it.
 *
 * The principal comes from the Python engine, which reads the role from the
 * `memberships` table rather than from the token's own claim. A token that
 * says `PLATFORM_ADMIN` therefore gets whatever the database says it gets.
 */

import type { CanActivate, ExecutionContext } from '@nestjs/common';
import type { Request } from 'express';
import type { Credential } from '../engine/engine.service.ts';
import type { Principal } from './principal.ts';
// NOTE: `Reflector` and `EngineService` MUST be value imports, not
// `import type`. They appear only in constructor parameter positions, so
// `consistent-type-imports` wants to convert them -- and doing so erases the
// `design:paramtypes` metadata that NestJS dependency injection reads at
// runtime. The autofix made exactly that change once and the application
// failed to start. The rule is disabled for this app in eslint.config.js; see the
// comment there.
import { Injectable, SetMetadata, UnauthorizedException } from '@nestjs/common';
import { Reflector } from '@nestjs/core';
import { EngineService } from '../engine/engine.service.ts';
import { isRole } from './principal.ts';

const PUBLIC_KEY = 'sreoi:public';

/** Marks a route reachable without a credential. Every use is a decision. */
export function Public(): MethodDecorator & ClassDecorator {
	return SetMetadata(PUBLIC_KEY, true);
}

export type AuthedRequest = Request & { principal?: Principal };

@Injectable()
export class AuthGuard implements CanActivate {
	constructor(
		private readonly reflector: Reflector,
		private readonly engine: EngineService,
	) {}

	async canActivate(context: ExecutionContext): Promise<boolean> {
		const isPublic = this.reflector.getAllAndOverride<boolean>(PUBLIC_KEY, [
			context.getHandler(),
			context.getClass(),
		]);
		if (isPublic === true) {
			return true;
		}

		const request = context.switchToHttp().getRequest<AuthedRequest>();
		const credential = extractCredential(request);
		if (credential === null) {
			throw new UnauthorizedException('a credential is required');
		}

		const resolved = await this.engine.resolvePrincipal(credential);
		if (resolved === null) {
			throw new UnauthorizedException('invalid credential');
		}
		if (!isRole(resolved.role)) {
			// An unrecognised role is refused rather than downgraded to VIEWER:
			// a role this service does not understand is a version mismatch
			// between the two halves, and guessing is how a privilege check
			// silently becomes wrong.
			throw new UnauthorizedException('unrecognised role');
		}
		if (resolved.organizationId === '') {
			throw new UnauthorizedException('no organisation membership');
		}

		request.principal = { ...resolved, role: resolved.role };
		return true;
	}
}

export function extractCredential(request: {
	headers: Record<string, unknown>;
	cookies?: Record<string, string> | undefined;
}): Credential | null {
	const authorization = header(request.headers.authorization);
	if (authorization !== undefined) {
		const [scheme, ...rest] = authorization.split(' ');
		const token = rest.join(' ').trim();
		if (scheme?.toLowerCase() === 'bearer' && token !== '') {
			return { kind: 'bearer', value: token };
		}
	}
	const apiKey = header(request.headers['x-api-key']);
	if (apiKey !== undefined && apiKey !== '') {
		return { kind: 'apiKey', value: apiKey };
	}
	const session = request.cookies?.sreoi_session ?? cookieFrom(header(request.headers.cookie));
	if (session !== undefined && session !== '') {
		return { kind: 'cookie', value: session };
	}
	return null;
}

function header(value: unknown): string | undefined {
	if (typeof value === 'string') {
		return value;
	}
	// Node lowercases header names but repeated headers arrive as an array.
	if (Array.isArray(value) && typeof value[0] === 'string') {
		return value[0];
	}
	return undefined;
}

function cookieFrom(cookieHeader: string | undefined): string | undefined {
	if (cookieHeader === undefined) {
		return undefined;
	}
	for (const part of cookieHeader.split(';')) {
		const index = part.indexOf('=');
		if (index === -1) {
			continue;
		}
		if (part.slice(0, index).trim() === 'sreoi_session') {
			return part.slice(index + 1).trim();
		}
	}
	return undefined;
}

if (import.meta.vitest != null) {
	describe('extractCredential', () => {
		it('reads a bearer token', () => {
			expect(extractCredential({ headers: { authorization: 'Bearer abc.def' } })).toEqual({
				kind: 'bearer',
				value: 'abc.def',
			});
		});

		it('is case-insensitive about the scheme but not about the header value', () => {
			expect(extractCredential({ headers: { authorization: 'bearer abc' } })).toEqual({
				kind: 'bearer',
				value: 'abc',
			});
		});

		it('ignores a non-bearer authorization scheme rather than treating it as a token', () => {
			// Passing a Basic credential through as a bearer token would send a
			// base64 password to the engine as if it were a JWT.
			expect(extractCredential({ headers: { authorization: 'Basic dXNlcjpwYXNz' } })).toBeNull();
		});

		it('reads an api key header', () => {
			expect(extractCredential({ headers: { 'x-api-key': 'sk_abc.def' } })).toEqual({
				kind: 'apiKey',
				value: 'sk_abc.def',
			});
		});

		it('parses the session cookie out of a crowded cookie header', () => {
			expect(
				extractCredential({
					headers: { cookie: 'other=1; sreoi_session=tok3n; lang=ar' },
				}),
			).toEqual({ kind: 'cookie', value: 'tok3n' });
		});

		it('does not match a cookie whose name merely ends with the session name', () => {
			expect(extractCredential({ headers: { cookie: 'not_sreoi_session=tok3n' } })).toBeNull();
		});

		it('returns null when nothing is presented, so the guard refuses', () => {
			expect(extractCredential({ headers: {} })).toBeNull();
			expect(extractCredential({ headers: { authorization: 'Bearer ' } })).toBeNull();
		});

		it('prefers a bearer token over a cookie when both are sent', () => {
			expect(
				extractCredential({
					headers: { authorization: 'Bearer explicit', cookie: 'sreoi_session=ambient' },
				}),
			).toEqual({ kind: 'bearer', value: 'explicit' });
		});
	});
}
