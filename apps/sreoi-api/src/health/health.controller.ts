import type { AuthedRequest } from '../auth/auth.guard.ts';
import { Controller, Get, Req } from '@nestjs/common';
import { Public } from '../auth/auth.guard.ts';

@Controller()
export class HealthController {
	/** Liveness only. Carries no data, which is why it is public. */
	@Public()
	@Get('health')
	health(): { status: string } {
		return { status: 'ok' };
	}

	/** Who the caller is, according to the database rather than their token. */
	@Get('auth/me')
	me(@Req() request: AuthedRequest): unknown {
		return request.principal ?? null;
	}
}
