import { Module } from '@nestjs/common';
import { APP_GUARD } from '@nestjs/core';
import { AuthGuard } from './auth/auth.guard.ts';
import { DbService } from './db/db.service.ts';
import { EngineService } from './engine/engine.service.ts';
import { HealthController } from './health/health.controller.ts';
import { MapController } from './map/map.controller.ts';
import { MapService } from './map/map.service.ts';
import { OpportunitiesController } from './opportunities/opportunities.controller.ts';
import { OpportunitiesService } from './opportunities/opportunities.service.ts';

@Module({
	controllers: [HealthController, OpportunitiesController, MapController],
	providers: [
		DbService,
		EngineService,
		OpportunitiesService,
		MapService,
		// Registered globally so protection is the default and exposure is the
		// exception. See auth.guard.ts for why that direction matters.
		{ provide: APP_GUARD, useClass: AuthGuard },
	],
})
export class AppModule {}
