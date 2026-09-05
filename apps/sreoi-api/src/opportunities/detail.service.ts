/**
 * The evidence behind one opportunity.
 *
 * This is the endpoint the product's credibility rests on. A score on its own
 * is a number someone has to take on trust; the specification's position is
 * that every published figure must be traceable to the evidence and the
 * version of the method that produced it. So this returns the derivation, not
 * a summary of it: each score dimension with its raw value, normalisation,
 * weight and contribution; every cost line with the basis it was established
 * on; the comparables with their individual weights; and the verification
 * checks including the ones that could not be performed.
 *
 * Nothing here computes. Every number is read as the engine stored it.
 */

import { Injectable } from '@nestjs/common';
import { DbService } from '../db/db.service.ts';

export interface ScoreComponent {
	dimension: string;
	rawValue: number | null;
	normalizedScore: number | null;
	weight: number | null;
	contribution: number | null;
}

export interface CostLine {
	kind: string;
	amount: number | null;
	basis: string;
	material: boolean;
	note: string | null;
}

export interface Comparable {
	transactionId: string;
	weight: number | null;
	distanceM: number | null;
	adjustedPricePerSqm: number | null;
	excludedReason: string | null;
}

export interface VerificationCheck {
	checkType: string;
	status: string;
	evidence: unknown;
	checkedAt: string | null;
}

export interface TimelineEvent {
	eventType: string;
	summary: string | null;
	occurredAt: string | null;
}

@Injectable()
export class DetailService {
	constructor(private readonly db: DbService) {}

	async evidence(opportunityId: string): Promise<{
		score: {
			components: ScoreComponent[];
			weightProfileVersion: string | null;
			methodVersion: string | null;
		};
		valuation: Record<string, unknown> | null;
		cost: { total: number | null; isComplete: boolean | null; lines: CostLine[] } | null;
		comparables: Comparable[];
		verification: VerificationCheck[];
		timeline: TimelineEvent[];
	}> {
		return this.db.withoutTenant(async (client) => {
			const score = await client.query(
				`SELECT sc.dimension, sc.raw_value, sc.normalized_score, sc.weight,
				        s.weight_profile_version, s.method_version
				 FROM opportunity_scores s
				 JOIN score_components sc ON sc.score_id = s.id
				 WHERE s.opportunity_id = $1 AND s.superseded_at IS NULL
				 ORDER BY sc.weight DESC NULLS LAST`,
				[opportunityId],
			);

			// The valuation is append-only like the cost, so take the newest.
			const valuation = await client.query(
				`SELECT id, fair_value_low, fair_value_base, fair_value_high,
				        base_price_per_sqm, comparable_count, effective_n,
				        comparable_quality, confidence, index_tier, method_version
				 FROM valuations
				 WHERE opportunity_id = $1
				 ORDER BY computed_at DESC
				 LIMIT 1`,
				[opportunityId],
			);
			const valuationRow = valuation.rows[0] as Record<string, unknown> | undefined;

			const cost = await client.query(
				`SELECT c.id, c.total, c.is_complete
				 FROM true_acquisition_costs c
				 WHERE c.opportunity_id = $1
				 ORDER BY c.computed_at DESC
				 LIMIT 1`,
				[opportunityId],
			);
			const costRow = cost.rows[0] as Record<string, unknown> | undefined;

			const lines = costRow === undefined
				? { rows: [] as Record<string, unknown>[] }
				: await client.query(
						`SELECT kind, amount, basis, material, note
					 FROM cost_line_items WHERE cost_id = $1
					 ORDER BY material DESC, amount DESC NULLS LAST`,
						[costRow.id],
					);

			const comparables = valuationRow === undefined
				? { rows: [] as Record<string, unknown>[] }
				: await client.query(
						`SELECT transaction_id, weight, distance_m, adjusted_price_per_sqm,
					        excluded_reason
					 FROM valuation_comparables WHERE valuation_id = $1
					 ORDER BY weight DESC NULLS LAST`,
						[valuationRow.id],
					);

			// Append-only with no batch id, so "the current verdict" is the newest
			// row per check_type. A time-window approach was tried on the Python
			// side and reported a phantom disagreement rate, because several
			// opportunities are re-evaluated within the same second.
			const verification = await client.query(
				`SELECT DISTINCT ON (check_type) check_type, status, evidence, checked_at
				 FROM verification_checks
				 WHERE opportunity_id = $1
				 ORDER BY check_type, checked_at DESC`,
				[opportunityId],
			);

			const timeline = await client.query(
				`SELECT t.event_type, t.summary, t.occurred_at
				 FROM property_timeline t
				 JOIN opportunities o ON o.property_id = t.property_id
				 WHERE o.id = $1
				 ORDER BY t.occurred_at DESC
				 LIMIT 50`,
				[opportunityId],
			);

			const first = score.rows[0] as Record<string, unknown> | undefined;
			return {
				score: {
					components: score.rows.map((r: Record<string, unknown>) => {
						const weight = num(r.weight);
						const normalized = num(r.normalized_score);
						return {
							dimension: String(r.dimension),
							rawValue: num(r.raw_value),
							normalizedScore: normalized,
							weight,
							// Read, not recomputed, except for this product which the
							// engine does not store as its own column. It is the one
							// arithmetic here, and it is a presentation convenience:
							// weight x normalised is what the derivation table shows.
							contribution: weight === null || normalized === null
								? null
								: Math.round(weight * normalized * 1000) / 1000,
						};
					}),
					weightProfileVersion: first?.weight_profile_version == null
						? null
						: String(first.weight_profile_version),
					methodVersion: first?.method_version == null
						? null
						: String(first.method_version),
				},
				valuation: valuationRow === undefined ? null : mapValuation(valuationRow),
				cost: costRow === undefined
					? null
					: {
							total: num(costRow.total),
							isComplete: costRow.is_complete == null ? null : Boolean(costRow.is_complete),
							lines: lines.rows.map((r: Record<string, unknown>) => ({
								kind: String(r.kind),
								amount: num(r.amount),
								basis: String(r.basis),
								material: Boolean(r.material),
								note: r.note == null ? null : String(r.note),
							})),
						},
				comparables: comparables.rows.map((r: Record<string, unknown>) => ({
					transactionId: String(r.transaction_id),
					weight: num(r.weight),
					distanceM: num(r.distance_m),
					adjustedPricePerSqm: num(r.adjusted_price_per_sqm),
					excludedReason: r.excluded_reason == null ? null : String(r.excluded_reason),
				})),
				verification: verification.rows.map((r: Record<string, unknown>) => ({
					checkType: String(r.check_type),
					status: String(r.status),
					evidence: r.evidence,
					checkedAt: iso(r.checked_at),
				})),
				timeline: timeline.rows.map((r: Record<string, unknown>) => ({
					eventType: String(r.event_type),
					summary: r.summary == null ? null : String(r.summary),
					occurredAt: iso(r.occurred_at),
				})),
			};
		});
	}
}

function mapValuation(row: Record<string, unknown>): Record<string, unknown> {
	return {
		fairValueLow: num(row.fair_value_low),
		fairValueBase: num(row.fair_value_base),
		fairValueHigh: num(row.fair_value_high),
		basePricePerSqm: num(row.base_price_per_sqm),
		comparableCount: num(row.comparable_count),
		effectiveN: num(row.effective_n),
		comparableQuality: num(row.comparable_quality),
		confidence: num(row.confidence),
		indexTier: row.index_tier == null ? null : String(row.index_tier),
		methodVersion: row.method_version == null ? null : String(row.method_version),
	};
}

/**
 * `pg` returns timestamptz as a JavaScript Date, whose default string form is
 * "Fri Sep 04 2026 23:19:11 GMT+0000 (Coordinated Universal Time)" -- a
 * runtime artefact, not a wire format. Emitting ISO 8601 keeps the API
 * contract stable and leaves formatting to the client, which is the only layer
 * that knows the reader's locale and calendar (Hijri included).
 */
function iso(value: unknown): string | null {
	if (value === null || value === undefined) {
		return null;
	}
	if (value instanceof Date) {
		return Number.isNaN(value.getTime()) ? null : value.toISOString();
	}
	const parsed = new Date(String(value));
	return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toISOString();
}

/**
 * See opportunities.service.ts: `numeric` arrives as a string, and an absent
 * value must stay absent rather than becoming 0.
 */
function num(value: unknown): number | null {
	if (value === null || value === undefined) {
		return null;
	}
	// `Number('')` and `Number('   ')` are 0, not NaN. Without this guard an
	// empty amount reads as *free*, which is the exact failure the true-cost
	// invariant exists to prevent: an unknown material line must refuse the
	// discount, never make the acquisition look cheaper. Caught by its own test.
	if (typeof value === 'string' && value.trim() === '') {
		return null;
	}
	const parsed = Number(value);
	return Number.isFinite(parsed) ? parsed : null;
}

if (import.meta.vitest != null) {
	describe('timestamps', () => {
		it('emits ISO 8601, not a JavaScript Date toString', () => {
			// "Fri Sep 04 2026 23:19:11 GMT+0000 (Coordinated Universal Time)" is
			// what leaked into the client before this existed.
			expect(iso(new Date('2026-09-04T23:19:11Z'))).toBe('2026-09-04T23:19:11.000Z');
		});

		it('passes through an absent timestamp', () => {
			expect(iso(null)).toBeNull();
			expect(iso(undefined)).toBeNull();
		});

		it('keeps an unparseable value rather than inventing a date', () => {
			expect(iso('not a date')).toBe('not a date');
			expect(iso(new Date('nonsense'))).toBeNull();
		});
	});

	describe('numeric handling', () => {
		it('keeps an absent amount absent rather than reading as free', () => {
			// A cost line with an UNKNOWN amount is what makes the discount refuse.
			// Turning it into 0 would silently make the acquisition look cheaper.
			expect(num(null)).toBeNull();
			expect(num(undefined)).toBeNull();
			expect(num('')).toBeNull();
		});

		it('parses the strings pg returns for numeric columns', () => {
			expect(num('560700.00')).toBe(560700);
			expect(num('0.3574')).toBe(0.3574);
		});
	});
}
