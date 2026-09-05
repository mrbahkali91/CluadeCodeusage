/** The contract the client consumes. */

import type { AuthedRequest } from '../auth/auth.guard.ts';
import type { OpportunityFilters, SortKey } from './opportunities.service.ts';
import {
	BadRequestException,
	Controller,
	Get,
	NotFoundException,
	Param,
	Query,
	Req,
} from '@nestjs/common';
import { DetailService } from './detail.service.ts';
import { isSortKey, OpportunitiesService } from './opportunities.service.ts';

const MAX_LIMIT = 200;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

@Controller('api/v1')
export class OpportunitiesController {
	constructor(
		private readonly opportunities: OpportunitiesService,
		private readonly evidence: DetailService,
	) {}

	@Get('search/opportunities')
	async search(
		@Query() query: Record<string, string | string[] | undefined>,
		@Req() request: AuthedRequest,
	): Promise<unknown> {
		const filters = parseFilters(query);
		const page = await this.opportunities.search(filters);
		return {
			// Leading with the provenance, not appending it: a client cannot render
			// these numbers without having received the caveat first.
			evidence_is_synthetic: true,
			caveat:
				'Comparable transactions come from a synthetic fixture corpus, not '
				+ 'registered Saudi sales. The engine is real; the evidence is generated.',
			organization: request.principal?.organization ?? null,
			count: page.count,
			filters_applied: filters,
			results: page.results,
		};
	}

	@Get('opportunities/:id')
	async detail(@Param('id') id: string): Promise<unknown> {
		if (!UUID.test(id)) {
			// Rejected before it reaches the database so a malformed id is a 400
			// rather than a driver-level type error surfacing as a 500.
			throw new BadRequestException('id must be a uuid');
		}
		const row = await this.opportunities.byId(id);
		if (row === null) {
			throw new NotFoundException('no such opportunity');
		}
		const evidence = await this.evidence.evidence(id);
		return {
			// The caveat leads, as it does on the list: a client cannot render the
			// derivation without having received the provenance first.
			evidence_is_synthetic: true,
			caveat:
				'Comparable transactions come from a synthetic fixture corpus, not '
				+ 'registered Saudi sales. The engine is real; the evidence is generated.',
			opportunity: row,
			...evidence,
		};
	}

	@Get('facets')
	async facets(): Promise<unknown> {
		const [districts, types] = await Promise.all([
			this.opportunities.districts(),
			this.opportunities.types(),
		]);
		return { districts, types };
	}
}

export function parseFilters(
	query: Record<string, string | string[] | undefined>,
): OpportunityFilters {
	const sortRaw = single(query.sort) ?? 'score';
	if (!isSortKey(sortRaw)) {
		throw new BadRequestException(`sort must be one of score, discount, newest, confidence`);
	}
	return {
		districts: many(query.district),
		types: many(query.type),
		minScore: bounded(single(query.min_score), 0, 100),
		maxPrice: bounded(single(query.max_price), 0, Number.MAX_SAFE_INTEGER),
		sort: sortRaw satisfies SortKey,
		limit: bounded(single(query.limit), 1, MAX_LIMIT) ?? 50,
		offset: bounded(single(query.offset), 0, Number.MAX_SAFE_INTEGER) ?? 0,
	};
}

function single(value: string | string[] | undefined): string | undefined {
	if (Array.isArray(value)) {
		return value[0];
	}
	return value;
}

/**
 * Repeated query parameters (`?district=A&district=B`) arrive as an array and a
 * single one as a string. Normalising here rather than at each call site is
 * what keeps a one-value filter from being silently ignored -- a bug the Python
 * side shipped once, where `district=Sidrah` returned all 56 rows.
 */
function many(value: string | string[] | undefined): string[] {
	if (value === undefined) {
		return [];
	}
	const list = Array.isArray(value) ? value : [value];
	return list.filter(v => typeof v === 'string' && v.trim() !== '').map(v => v.trim());
}

function bounded(value: string | undefined, min: number, max: number): number | null {
	if (value === undefined || value.trim() === '') {
		return null;
	}
	const parsed = Number(value);
	if (!Number.isFinite(parsed)) {
		throw new BadRequestException(`${value} is not a number`);
	}
	if (parsed < min || parsed > max) {
		throw new BadRequestException(`value must be between ${min} and ${max}`);
	}
	return parsed;
}

if (import.meta.vitest != null) {
	describe('parseFilters', () => {
		it('defaults to score order with a bounded page', () => {
			const filters = parseFilters({});
			expect(filters.sort).toBe('score');
			expect(filters.limit).toBe(50);
			expect(filters.offset).toBe(0);
			expect(filters.districts).toEqual([]);
		});

		it('accepts one district as well as several -- the single-value case regressed once', () => {
			expect(parseFilters({ district: 'Sidrah' }).districts).toEqual(['Sidrah']);
			expect(parseFilters({ district: ['Sidrah', 'Qurtubah'] }).districts).toEqual([
				'Sidrah',
				'Qurtubah',
			]);
		});

		it('drops blank values instead of filtering on an empty string', () => {
			expect(parseFilters({ district: ['', '  ', 'Sidrah'] }).districts).toEqual(['Sidrah']);
		});

		it('refuses an unknown sort key rather than falling back silently', () => {
			expect(() => parseFilters({ sort: 'total_score' })).toThrow(BadRequestException);
		});

		it('caps the page size so one request cannot ask for the whole table', () => {
			expect(() => parseFilters({ limit: '5000' })).toThrow(BadRequestException);
			expect(parseFilters({ limit: '200' }).limit).toBe(200);
		});

		it('rejects a non-numeric score instead of coercing it to NaN', () => {
			expect(() => parseFilters({ min_score: 'abc' })).toThrow(BadRequestException);
		});

		it('rejects a score outside 0-100', () => {
			expect(() => parseFilters({ min_score: '120' })).toThrow(BadRequestException);
			expect(() => parseFilters({ min_score: '-1' })).toThrow(BadRequestException);
		});
	});
}
