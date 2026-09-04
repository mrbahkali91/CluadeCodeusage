import { Controller, Get, Query } from '@nestjs/common';
import { MapService, parseBbox } from './map.service.ts';

@Controller('api/v1/map')
export class MapController {
	constructor(private readonly map: MapService) {}

	@Get('districts')
	async districts(): Promise<unknown> {
		return this.map.districts();
	}

	@Get('opportunities')
	async opportunities(
		@Query('bbox') bbox?: string,
		@Query('limit') limit?: string,
	): Promise<unknown> {
		const parsed = Number(limit ?? '500');
		const bounded = Number.isFinite(parsed) ? Math.min(Math.max(parsed, 1), 2000) : 500;
		return this.map.opportunities(parseBbox(bbox), bounded);
	}
}
