/**
 * Geospatial reads for the map.
 *
 * Geometry is converted to GeoJSON in PostgreSQL rather than in Node. The
 * Python side learned this the hard way: binding a geography column as a
 * parameter produced `st_asbinary(character varying)` errors, and doing the
 * conversion in SQL keeps the cast explicit and the driver out of it.
 */

import { BadRequestException, Injectable } from '@nestjs/common';
import { DbService } from '../db/db.service.ts';

export interface Bbox {
	west: number;
	south: number;
	east: number;
	north: number;
}

@Injectable()
export class MapService {
	constructor(private readonly db: DbService) {}

	async districts(): Promise<unknown> {
		return this.db.withoutTenant(async (client) => {
			const { rows } = await client.query<{ feature: unknown }>(
				`SELECT jsonb_build_object(
					'type', 'Feature',
					'geometry', ST_AsGeoJSON(ST_Transform(boundary::geometry, 4326))::jsonb,
					'properties', jsonb_build_object(
						'name_en', name_en,
						'name_ar', name_ar,
						'precision', boundary_precision,
						'liquidity', liquidity_score,
						'location', location_score
					)
				) AS feature
				FROM districts
				WHERE boundary IS NOT NULL`,
			);
			return { type: 'FeatureCollection', features: rows.map(r => r.feature) };
		});
	}

	async opportunities(bbox: Bbox | null, limit: number): Promise<unknown> {
		return this.db.withoutTenant(async (client) => {
			const params: unknown[] = [];
			let filter = '';
			if (bbox !== null) {
				params.push(bbox.west, bbox.south, bbox.east, bbox.north);
				filter = `AND ST_Intersects(
					p.location::geometry,
					ST_MakeEnvelope($1, $2, $3, $4, 4326)
				)`;
			}
			params.push(limit);
			const { rows } = await client.query<{ feature: unknown }>(
				`SELECT jsonb_build_object(
					'type', 'Feature',
					'geometry', ST_AsGeoJSON(p.location::geometry)::jsonb,
					'properties', jsonb_build_object(
						'id', o.id,
						'title', o.title,
						'type', o.opportunity_type,
						'score', s.total_score,
						'classification', s.classification,
						'confidence', s.data_confidence
					)
				) AS feature
				FROM opportunities o
				JOIN properties p ON p.id = o.property_id
				LEFT JOIN opportunity_scores s
					ON s.opportunity_id = o.id AND s.superseded_at IS NULL
				WHERE p.location IS NOT NULL ${filter}
				LIMIT $${params.length}`,
				params,
			);
			return { type: 'FeatureCollection', features: rows.map(r => r.feature) };
		});
	}
}

export function parseBbox(raw: string | undefined): Bbox | null {
	if (raw === undefined || raw.trim() === '') {
		return null;
	}
	const parts = raw.split(',').map(p => Number(p.trim()));
	if (parts.length !== 4 || parts.some(p => !Number.isFinite(p))) {
		throw new BadRequestException('bbox must be four numbers: west,south,east,north');
	}
	const [west, south, east, north] = parts as [number, number, number, number];
	// Checked rather than silently normalised: a caller who swapped the corners
	// has a bug, and a quietly reordered envelope hides it behind plausible
	// results.
	if (west >= east || south >= north) {
		throw new BadRequestException('bbox must satisfy west < east and south < north');
	}
	if (west < -180 || east > 180 || south < -90 || north > 90) {
		throw new BadRequestException('bbox is outside valid lon/lat bounds');
	}
	return { west, south, east, north };
}

if (import.meta.vitest != null) {
	describe('parseBbox', () => {
		it('parses a well-formed bbox', () => {
			expect(parseBbox('46.6,24.7,46.9,24.9')).toEqual({
				west: 46.6,
				south: 24.7,
				east: 46.9,
				north: 24.9,
			});
		});

		it('treats an absent bbox as no filter rather than an error', () => {
			expect(parseBbox(undefined)).toBeNull();
			expect(parseBbox('')).toBeNull();
		});

		it('rejects the wrong number of components', () => {
			expect(() => parseBbox('46.6,24.7,46.9')).toThrow(BadRequestException);
		});

		it('rejects inverted corners instead of normalising them', () => {
			expect(() => parseBbox('46.9,24.7,46.6,24.9')).toThrow(BadRequestException);
			expect(() => parseBbox('46.6,24.9,46.9,24.7')).toThrow(BadRequestException);
		});

		it('rejects coordinates outside lon/lat range', () => {
			expect(() => parseBbox('-200,24.7,46.9,24.9')).toThrow(BadRequestException);
			expect(() => parseBbox('46.6,-95,46.9,24.9')).toThrow(BadRequestException);
		});

		it('rejects non-numeric components rather than passing NaN to PostGIS', () => {
			expect(() => parseBbox('a,b,c,d')).toThrow(BadRequestException);
		});
	});
}
