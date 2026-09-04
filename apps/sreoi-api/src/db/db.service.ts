/**
 * PostgreSQL access, with tenant isolation preserved.
 *
 * The single most important thing in this file is `withTenant`. Eight
 * customer-data tables carry `organization_id` under PostgreSQL
 * `ENABLE`/`FORCE ROW LEVEL SECURITY`, and every policy keys on
 * `current_setting('app.organization_id')`. That setting is *transaction-local*.
 * A query issued outside a transaction that set it sees nothing at all -- which
 * fails safe -- but a query that reuses a pooled connection where some earlier
 * request set a different tenant would see the wrong tenant's rows, which does
 * not. So the binding and the query must be the same transaction, and this
 * class is the only place that opens one.
 *
 * Two rules follow, and neither is optional:
 *   1. Never expose the raw pool. Callers get `withTenant` or `withoutTenant`.
 *   2. The connecting role must not be a PostgreSQL superuser. Superusers are
 *      exempt from row-level security unconditionally and `FORCE` does not
 *      apply to them either, so a superuser role would leave all sixteen
 *      policies enforced against nobody. `assertNotSuperuser` checks this at
 *      startup rather than trusting the deployment.
 */

import type { OnApplicationShutdown } from '@nestjs/common';
import type { PoolClient } from 'pg';
import { Injectable } from '@nestjs/common';
import { Pool } from 'pg';
import { loadConfig } from '../config.ts';

export type TenantId = string;

@Injectable()
export class DbService implements OnApplicationShutdown {
	private readonly pool: Pool;

	constructor() {
		const config = loadConfig();
		this.pool = new Pool({
			connectionString: config.databaseUrl,
			max: 10,
			idleTimeoutMillis: 30_000,
			// A query that hangs holds a connection and its tenant binding.
			statement_timeout: 15_000,
		});
	}

	async onApplicationShutdown(): Promise<void> {
		await this.pool.end();
	}

	/**
	 * Run `fn` in a transaction with the tenant bound, so row-level security
	 * applies. `set_config(..., true)` is the local form: it is scoped to this
	 * transaction and cannot leak to the next borrower of this connection.
	 */
	async withTenant<T>(
		organizationId: TenantId,
		fn: (client: PoolClient) => Promise<T>,
	): Promise<T> {
		const client = await this.pool.connect();
		try {
			await client.query('BEGIN');
			await client.query('SELECT set_config($1, $2, true)', [
				'app.organization_id',
				organizationId,
			]);
			const result = await fn(client);
			await client.query('COMMIT');
			return result;
		}
		catch (error) {
			await client.query('ROLLBACK').catch(() => {
				// A failed rollback must not mask the original error.
			});
			throw error;
		}
		finally {
			client.release();
		}
	}

	/**
	 * For market data only -- properties, transactions, comparables, districts,
	 * price indices -- which is deliberately shared across tenants because it is
	 * observed fact and every tenant values against the same evidence. Using
	 * this for a table that carries `organization_id` would bypass the isolation
	 * the policies exist to provide, so the name is blunt on purpose.
	 */
	async withoutTenant<T>(fn: (client: PoolClient) => Promise<T>): Promise<T> {
		const client = await this.pool.connect();
		try {
			return await fn(client);
		}
		finally {
			client.release();
		}
	}

	/** Startup guard. See the class docstring for why this is not paranoia. */
	async assertNotSuperuser(): Promise<void> {
		const { rows } = await this.pool.query<{ rolsuper: boolean; rolbypassrls: boolean }>(
			'SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user',
		);
		const role = rows[0];
		if (role === undefined) {
			throw new Error('cannot determine the current database role');
		}
		if (role.rolsuper || role.rolbypassrls) {
			throw new Error(
				'refusing to start: the application database role is a superuser or has '
				+ 'BYPASSRLS, so every row-level security policy is enforced against nobody. '
				+ 'Run: ALTER ROLE <role> NOSUPERUSER NOBYPASSRLS;',
			);
		}
	}
}
