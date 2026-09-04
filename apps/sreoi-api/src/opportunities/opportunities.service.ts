/**
 * Opportunity reads.
 *
 * Queries are parameterised throughout, and the sort column is resolved
 * through a lookup rather than interpolated: a sort key is the one filter
 * users expect to name a column, which is exactly what makes it the usual
 * injection route.
 *
 * Nothing here computes a number. Scores, valuations and costs are read as
 * the engine stored them.
 */

import { Injectable } from '@nestjs/common';
import { DbService } from '../db/db.service.ts';

export interface OpportunityFilters {
	districts: string[];
	types: string[];
	minScore: number | null;
	maxPrice: number | null;
	sort: SortKey;
	limit: number;
	offset: number;
}

export const SORT_COLUMNS = {
	score: 's.total_score DESC NULLS LAST',
	discount: 's.discount_fraction DESC NULLS LAST',
	newest: 'o.created_at DESC',
	confidence: 's.data_confidence DESC NULLS LAST',
} as const;

export type SortKey = keyof typeof SORT_COLUMNS;

export function isSortKey(value: string): value is SortKey {
	return Object.hasOwn(SORT_COLUMNS, value);
}

export interface OpportunityRow {
	id: string;
	title: string;
	opportunityType: string;
	status: string;
	district: string | null;
	districtAr: string | null;
	areaSqm: number | null;
	totalScore: number | null;
	classification: string | null;
	dataConfidence: number | null;
	capped: boolean | null;
	discountFraction: number | null;
	discountRefusedReason: string | null;
	methodVersion: string | null;
}

@Injectable()
export class OpportunitiesService {
	constructor(private readonly db: DbService) {}

	async search(filters: OpportunityFilters): Promise<{ count: number; results: OpportunityRow[] }> {
		// Market data is shared across tenants by design, so this reads without a
		// tenant binding. See DbService.withoutTenant for why that is safe here
		// and would not be for a watchlist or an alert.
		return this.db.withoutTenant(async (client) => {
			const where: string[] = ['s.superseded_at IS NULL'];
			const params: unknown[] = [];

			if (filters.districts.length > 0) {
				params.push(filters.districts);
				where.push(`d.name_en = ANY($${params.length}::text[])`);
			}
			if (filters.types.length > 0) {
				params.push(filters.types);
				where.push(`o.opportunity_type = ANY($${params.length}::text[])`);
			}
			if (filters.minScore !== null) {
				params.push(filters.minScore);
				where.push(`s.total_score >= $${params.length}`);
			}
			if (filters.maxPrice !== null) {
				params.push(filters.maxPrice);
				where.push(`c.total <= $${params.length}`);
			}

			const clause = where.join(' AND ');
			// `true_acquisition_costs` is append-only -- re-evaluation inserts a new
			// row rather than updating one, because the sequence of what the
			// platform claimed over time is itself evidence. A plain LEFT JOIN
			// therefore fans out: 81 cost rows for 56 opportunities produced 81
			// results, silently duplicating listings in the client. The lateral
			// takes the current row per opportunity and leaves history intact.
			//
			// Scores need no equivalent because they carry `superseded_at`, which
			// the WHERE clause already filters on.
			const from = `
				FROM opportunities o
				JOIN properties p ON p.id = o.property_id
				LEFT JOIN districts d ON d.id = p.district_id
				LEFT JOIN opportunity_scores s ON s.opportunity_id = o.id
				LEFT JOIN LATERAL (
					SELECT tc.total
					FROM true_acquisition_costs tc
					WHERE tc.opportunity_id = o.id
					ORDER BY tc.computed_at DESC
					LIMIT 1
				) c ON true
				WHERE ${clause}`;

			const counted = await client.query<{ count: string }>(
				`SELECT count(*)::text AS count ${from}`,
				params,
			);

			params.push(filters.limit, filters.offset);
			const rows = await client.query(
				`SELECT
					o.id, o.title, o.opportunity_type, o.status,
					d.name_en AS district, d.name_ar AS district_ar,
					p.built_area_sqm AS area_sqm,
					s.total_score, s.classification, s.data_confidence, s.capped,
					s.discount_fraction, s.discount_refused_reason, s.method_version
				${from}
				ORDER BY ${SORT_COLUMNS[filters.sort]}
				LIMIT $${params.length - 1} OFFSET $${params.length}`,
				params,
			);

			return {
				count: Number(counted.rows[0]?.count ?? 0),
				results: rows.rows.map(mapRow),
			};
		});
	}

	async byId(id: string): Promise<OpportunityRow | null> {
		return this.db.withoutTenant(async (client) => {
			const { rows } = await client.query(
				`SELECT
					o.id, o.title, o.opportunity_type, o.status,
					d.name_en AS district, d.name_ar AS district_ar,
					p.built_area_sqm AS area_sqm,
					s.total_score, s.classification, s.data_confidence, s.capped,
					s.discount_fraction, s.discount_refused_reason, s.method_version
				FROM opportunities o
				JOIN properties p ON p.id = o.property_id
				LEFT JOIN districts d ON d.id = p.district_id
				LEFT JOIN opportunity_scores s
					ON s.opportunity_id = o.id AND s.superseded_at IS NULL
				WHERE o.id = $1`,
				[id],
			);
			const row = rows[0];
			return row === undefined ? null : mapRow(row);
		});
	}

	async districts(): Promise<{ nameEn: string; nameAr: string }[]> {
		return this.db.withoutTenant(async (client) => {
			const { rows } = await client.query<{ name_en: string; name_ar: string }>(
				'SELECT name_en, name_ar FROM districts ORDER BY name_en',
			);
			return rows.map(r => ({ nameEn: r.name_en, nameAr: r.name_ar }));
		});
	}

	async types(): Promise<string[]> {
		return this.db.withoutTenant(async (client) => {
			const { rows } = await client.query<{ opportunity_type: string }>(
				'SELECT DISTINCT opportunity_type FROM opportunities ORDER BY opportunity_type',
			);
			return rows.map(r => r.opportunity_type);
		});
	}
}

/**
 * `numeric` arrives from `pg` as a string, because a JavaScript number cannot
 * represent every value it can hold. These are ratios and scores rather than
 * money -- money is never summed here, only displayed -- so converting is
 * safe, and `null` stays `null` rather than becoming 0: an absent score and a
 * score of zero mean very different things to a reader.
 */
function num(value: unknown): number | null {
	if (value === null || value === undefined) {
		return null;
	}
	const parsed = Number(value);
	return Number.isFinite(parsed) ? parsed : null;
}

function mapRow(row: Record<string, unknown>): OpportunityRow {
	return {
		id: String(row.id),
		title: String(row.title),
		opportunityType: String(row.opportunity_type),
		status: String(row.status),
		district: row.district == null ? null : String(row.district),
		districtAr: row.district_ar == null ? null : String(row.district_ar),
		areaSqm: num(row.area_sqm),
		totalScore: num(row.total_score),
		classification: row.classification == null ? null : String(row.classification),
		dataConfidence: num(row.data_confidence),
		capped: row.capped == null ? null : Boolean(row.capped),
		discountFraction: num(row.discount_fraction),
		discountRefusedReason:
			row.discount_refused_reason == null ? null : String(row.discount_refused_reason),
		methodVersion: row.method_version == null ? null : String(row.method_version),
	};
}

if (import.meta.vitest != null) {
	describe('sort keys', () => {
		it('accepts only the four declared keys, so a sort cannot name a column', () => {
			expect(isSortKey('score')).toBe(true);
			expect(isSortKey('discount')).toBe(true);
			// The attack this closes: ORDER BY interpolated from user input.
			expect(isSortKey('total_score; DROP TABLE opportunities')).toBe(false);
			expect(isSortKey('__proto__')).toBe(false);
			expect(isSortKey('constructor')).toBe(false);
		});

		it('maps every declared key to a fixed fragment', () => {
			for (const key of Object.keys(SORT_COLUMNS)) {
				expect(isSortKey(key)).toBe(true);
				expect(SORT_COLUMNS[key as SortKey]).toMatch(/^[\w. ]+$/);
			}
		});
	});

	describe('numeric conversion', () => {
		it('keeps an absent score absent rather than turning it into zero', () => {
			expect(num(null)).toBeNull();
			expect(num(undefined)).toBeNull();
			expect(num('not a number')).toBeNull();
		});

		it('parses the strings pg returns for numeric columns', () => {
			expect(num('82.725')).toBe(82.725);
			expect(num('0')).toBe(0);
		});
	});
}
