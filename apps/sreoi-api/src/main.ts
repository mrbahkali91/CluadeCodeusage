/**
 * Bootstrap.
 *
 * Two checks run before the server accepts a connection, and both are
 * refusals rather than warnings: configuration must be valid, and the database
 * role must not be able to bypass row-level security. A service that starts
 * anyway and serves data is worse than one that does not start.
 */

import type { NestExpressApplication } from '@nestjs/platform-express';
import process from 'node:process';

import { Logger } from '@nestjs/common';
import { NestFactory } from '@nestjs/core';
import { AppModule } from './app.module.ts';
import { loadConfig } from './config.ts';
import { DbService } from './db/db.service.ts';

import 'reflect-metadata';

export async function bootstrap(): Promise<void> {
	const logger = new Logger('bootstrap');
	const config = loadConfig();

	const app = await NestFactory.create<NestExpressApplication>(AppModule, {
		logger: ['error', 'warn', 'log'],
	});
	// No global ValidationPipe: it requires class-validator, and nothing here
	// uses DTO classes. Query and path input is validated explicitly at each
	// boundary (parseFilters, parseBbox, the uuid check) and configuration
	// through zod, so adding two dependencies to validate nothing would only
	// widen the supply-chain surface.
	app.enableCors({ origin: config.corsOrigin, credentials: true });
	// Behind Cloudflare (or any reverse proxy) the connection to this process is
	// plain HTTP, so without this `req.secure` is false and `req.ip` is the
	// proxy. The session cookie would then be re-emitted WITHOUT `Secure` on a
	// public HTTPS site, and every client would share one throttle bucket.
	// `trustProxy` counts hops rather than trusting the whole chain: an
	// arbitrary `X-Forwarded-For` from the internet must not be able to
	// impersonate an address and evict someone else's lockout.
	app.getHttpAdapter().getInstance().set('trust proxy', config.trustProxy);
	app.enableShutdownHooks();

	await app.get(DbService).assertNotSuperuser();

	// Loopback by default. Binding 0.0.0.0 must be a deliberate act, because
	// this deployment's identity provider may be the development password
	// issuer.
	await app.listen(config.port, config.host);
	/*
	 * A loopback development server serves plain HTTP. This line said
	 * `https://` purely to satisfy `no-http-url`, which sent anyone who
	 * clicked it to a TLS handshake error -- a lint rule is not worth a false
	 * URL in an operator-facing log.
	 */
	// eslint-disable-next-line ryoppippi/no-http-url -- see above
	logger.log(`listening on http://${config.host}:${config.port}`);
	logger.log(`valuation engine at ${config.engineUrl}`);
}

if (process.env.NODE_ENV !== 'test' && import.meta.vitest == null) {
	bootstrap().catch((error: unknown) => {
		// startup refusal must be visible on stderr or the operator sees nothing.
		console.error(error instanceof Error ? error.message : error);
		process.exit(1);
	});
}
