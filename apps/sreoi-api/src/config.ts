/**
 * Configuration, resolved once and validated at startup.
 *
 * This fails closed for the same reason the Python service does: a deployment
 * with no identity configuration must refuse every request rather than serve
 * opportunity data to anyone who can reach the port.
 */

import process from 'node:process';
import { z } from 'zod';

const schema = z.object({
	port: z.coerce.number().int().min(1).max(65535).default(3000),
	host: z.string().default('127.0.0.1'),
	databaseUrl: z.string().min(1),
	/**
	 * The Python valuation engine. It stays authoritative for anything that
	 * produces a number and for credential verification; see engine/README
	 * reasoning in engine.service.ts.
	 */
	engineUrl: z.string().url().default('http://127.0.0.1:8000'),
	engineTimeoutMs: z.coerce.number().int().positive().default(10_000),
	corsOrigin: z.string().default('http://127.0.0.1:5173'),
	// How many reverse proxies sit in front of this process. 0 means none, and
	// is the only safe default: with `trust proxy` on, an `X-Forwarded-For`
	// header from anyone becomes `req.ip`, so a client could name any address
	// it liked and evict another client's sign-in lockout. Set it to the real
	// hop count for the deployment -- 1 behind a single Cloudflare tunnel.
	trustProxy: z.coerce.number().int().min(0).max(8).default(0),
});

export type Config = z.infer<typeof schema>;

export class ConfigurationError extends Error {}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): Config {
	const parsed = schema.safeParse({
		port: env.SREOI_API_PORT,
		host: env.SREOI_API_HOST,
		databaseUrl: env.SREOI_DATABASE_URL_PG ?? toLibpq(env.SREOI_DATABASE_URL),
		engineUrl: env.SREOI_ENGINE_URL,
		engineTimeoutMs: env.SREOI_ENGINE_TIMEOUT_MS,
		corsOrigin: env.SREOI_CORS_ORIGIN,
		trustProxy: env.SREOI_TRUST_PROXY,
	});
	if (!parsed.success) {
		throw new ConfigurationError(
			`refusing to start: ${parsed.error.issues.map(i => `${i.path.join('.')}: ${i.message}`).join('; ')}`,
		);
	}
	return parsed.data;
}

/**
 * The Python side stores its URL in SQLAlchemy's dialect form
 * (`postgresql+psycopg://`), which libpq does not understand. Accepting it and
 * translating means one variable configures both services instead of two that
 * can silently drift apart and point at different databases.
 */
function toLibpq(url: string | undefined): string | undefined {
	if (url === undefined) {
		return undefined;
	}
	return url.replace(/^postgresql\+\w+:\/\//, 'postgresql://');
}

if (import.meta.vitest != null) {
	describe('loadConfig', () => {
		const base = { SREOI_DATABASE_URL: 'postgresql://u:p@h:5432/d' } as NodeJS.ProcessEnv;

		it('refuses to start without a database url', () => {
			expect(() => loadConfig({})).toThrow(ConfigurationError);
		});

		it('translates the SQLAlchemy dialect form so one variable serves both services', () => {
			const config = loadConfig({ SREOI_DATABASE_URL: 'postgresql+psycopg://u:p@h:5432/d' });
			expect(config.databaseUrl).toBe('postgresql://u:p@h:5432/d');
		});

		it('leaves a plain libpq url alone', () => {
			expect(loadConfig(base).databaseUrl).toBe('postgresql://u:p@h:5432/d');
		});

		it('rejects a port outside the valid range', () => {
			expect(() => loadConfig({ ...base, SREOI_API_PORT: '70000' })).toThrow(ConfigurationError);
		});

		it('defaults to loopback, never 0.0.0.0', () => {
			expect(loadConfig(base).host).toBe('127.0.0.1');
		});
	});
}
