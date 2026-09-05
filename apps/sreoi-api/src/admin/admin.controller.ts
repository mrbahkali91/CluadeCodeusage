/**
 * Admin dashboards: data quality and source health.
 *
 * Both payloads are FORWARDED from the Python engine, not recomputed here. The
 * quality report is a versioned artefact (`quality-v1`) with its own thresholds
 * and its own snapshot table; a second implementation in SQL would drift from
 * the first, and the two would disagree about whether the platform is healthy.
 * The engine computes, this layer authorises and forwards.
 *
 * Both require ADMIN. They expose operational posture -- which sources are
 * stale, how often the engine refuses, where provenance is UNKNOWN -- which is
 * exactly what an attacker probing the platform would like to read, and is of
 * no use to an ordinary analyst.
 */

import type { AuthedRequest } from '../auth/auth.guard.ts';
import { Controller, ForbiddenException, Get, Req } from '@nestjs/common';
import { extractCredential } from '../auth/auth.guard.ts';
import { atLeast } from '../auth/principal.ts';
import { EngineService } from '../engine/engine.service.ts';

@Controller('api/v1/admin')
export class AdminController {
	constructor(private readonly engine: EngineService) {}

	@Get('quality')
	async quality(@Req() request: AuthedRequest): Promise<unknown> {
		return this.forward(request, '/api/v1/admin/quality');
	}

	@Get('sources')
	async sources(@Req() request: AuthedRequest): Promise<unknown> {
		return this.forward(request, '/api/v1/admin/sources');
	}

	private async forward(request: AuthedRequest, path: string): Promise<unknown> {
		const principal = request.principal;
		if (principal === undefined || !atLeast(principal.role, 'ADMIN')) {
			throw new ForbiddenException('admin role required');
		}
		// The caller's own credential is carried through rather than a service
		// account, so the engine applies the same checks and writes the same audit
		// entry it would for a direct call. This layer cannot widen access.
		const credential = extractCredential(request);
		if (credential === null) {
			throw new ForbiddenException('a credential is required');
		}
		return this.engine.get(path, credential);
	}
}
