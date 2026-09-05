/** Typed client for the NestJS API. */

import type { FeatureCollection } from 'geojson';

export interface Opportunity {
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

export interface SearchResponse {
	evidence_is_synthetic: boolean;
	caveat: string;
	organization: string | null;
	count: number;
	results: Opportunity[];
}

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

export interface Valuation {
	fairValueLow: number | null;
	fairValueBase: number | null;
	fairValueHigh: number | null;
	basePricePerSqm: number | null;
	comparableCount: number | null;
	effectiveN: number | null;
	comparableQuality: number | null;
	confidence: number | null;
	indexTier: string | null;
	methodVersion: string | null;
}

export interface DetailResponse {
	evidence_is_synthetic: boolean;
	caveat: string;
	opportunity: Opportunity;
	score: {
		components: ScoreComponent[];
		weightProfileVersion: string | null;
		methodVersion: string | null;
	};
	valuation: Valuation | null;
	cost: { total: number | null; isComplete: boolean | null; lines: CostLine[] } | null;
	comparables: Comparable[];
	verification: VerificationCheck[];
	timeline: TimelineEvent[];
}

export interface QualityFlag {
	key: string;
	label: string;
	severity: string;
	value: number;
	warn_at: number | null;
	fail_at: number | null;
	note: string | null;
}

export interface ConfidenceBucket {
	label: string;
	lower: number;
	upper: number;
	count: number;
	share: number;
}

export interface CheckStats {
	applicable: number;
	verified: number;
	pass_rate: number;
}

export interface QualityReport {
	method_version: string;
	captured_at: string | null;
	evidence_is_synthetic: boolean;
	overall_status: string;
	counts: { opportunities: number; properties: number };
	field_completeness: { overall: number; by_field: Record<string, number> };
	confidence_distribution: {
		data_confidence: { count: number; mean: number; buckets: ConfidenceBucket[] };
	};
	provenance: { by_basis: Record<string, number>; total: number; unknown_share: number };
	verification: { by_check_type: Record<string, CheckStats> };
	flags: QualityFlag[];
}

export interface SourceRow {
	key: string;
	name: string;
	legal_access_method: string;
	data_license: string | null;
	availability_label: string;
	source_confidence: number;
	is_synthetic: boolean;
	enabled: boolean;
	record_count: number;
}

export interface Facets {
	districts: { nameEn: string; nameAr: string }[];
	types: string[];
}

export class ApiError extends Error {
	constructor(
		readonly status: number,
		message: string,
	) {
		super(message);
	}
}

async function get<T>(path: string): Promise<T> {
	// `same-origin` and the Vite proxy together mean the session cookie the
	// engine set is sent without a CORS credential dance in development.
	const response = await fetch(path, {
		credentials: 'same-origin',
		headers: { accept: 'application/json' },
	});
	if (!response.ok) {
		throw new ApiError(response.status, `${path} returned ${response.status}`);
	}
	return response.json() as Promise<T>;
}

/**
 * Exchange a password for a session, against this origin.
 *
 * Deliberately posts to this app's own `/auth/login` rather than to the
 * engine's. The engine sets the session cookie for ITS host; in development
 * both answered on 127.0.0.1 and cookies ignore the port, so signing in on the
 * engine's own page happened to authenticate this client too. Deployed, the
 * browser never sees the engine, so that coincidence disappears -- and there
 * was no sign-in form here at all. The API relays the exchange and re-emits
 * the cookie on the origin the browser is actually using.
 *
 * Returns the failure rather than throwing it: a wrong password is an expected
 * answer this form has to render, not an exception.
 */
export async function signIn(
	email: string,
	password: string,
): Promise<{ ok: true } | { ok: false; status: number }> {
	const response = await fetch('/auth/login', {
		method: 'POST',
		credentials: 'same-origin',
		headers: { 'content-type': 'application/json', 'accept': 'application/json' },
		body: JSON.stringify({ email, password }),
	});
	// The body carries the token too, and is deliberately not read or stored:
	// the session cookie is HttpOnly, so keeping a copy in JavaScript would
	// hand any injected script the credential the cookie exists to protect.
	return response.ok ? { ok: true } : { ok: false, status: response.status };
}

export async function signOut(): Promise<void> {
	await fetch('/auth/logout', { method: 'POST', credentials: 'same-origin' });
}

export interface SearchParams {
	districts?: string[];
	types?: string[];
	minScore?: number | null;
	sort?: string;
	limit?: number;
}

export function buildSearchQuery(params: SearchParams): string {
	const query = new URLSearchParams();
	for (const district of params.districts ?? []) {
		query.append('district', district);
	}
	for (const type of params.types ?? []) {
		query.append('type', type);
	}
	if (params.minScore != null) {
		query.set('min_score', String(params.minScore));
	}
	if (params.sort != null && params.sort !== '') {
		query.set('sort', params.sort);
	}
	if (params.limit != null) {
		query.set('limit', String(params.limit));
	}
	const rendered = query.toString();
	return rendered === '' ? '' : `?${rendered}`;
}

export async function searchOpportunities(params: SearchParams): Promise<SearchResponse> {
	return get<SearchResponse>(`/api/v1/search/opportunities${buildSearchQuery(params)}`);
}

export async function fetchOpportunity(id: string): Promise<DetailResponse> {
	return get<DetailResponse>(`/api/v1/opportunities/${encodeURIComponent(id)}`);
}

export async function fetchQuality(): Promise<QualityReport> {
	return get<QualityReport>('/api/v1/admin/quality');
}

export async function fetchSources(): Promise<SourceRow[]> {
	return get<SourceRow[]>('/api/v1/admin/sources');
}

export async function fetchFacets(): Promise<Facets> {
	return get<Facets>('/api/v1/facets');
}

export async function fetchMapDistricts(): Promise<FeatureCollection> {
	return get<FeatureCollection>('/api/v1/map/districts');
}

export async function fetchMapOpportunities(): Promise<FeatureCollection> {
	return get<FeatureCollection>('/api/v1/map/opportunities?limit=1000');
}

if (import.meta.vitest != null) {
	describe('buildSearchQuery', () => {
		it('repeats a parameter per value rather than joining with commas', () => {
			// The API reads repeated parameters; a comma-joined string would be
			// treated as one district named "Sidrah,Qurtubah".
			expect(buildSearchQuery({ districts: ['Sidrah', 'Qurtubah'] })).toBe(
				'?district=Sidrah&district=Qurtubah',
			);
		});

		it('is empty when nothing is filtered, so the URL stays clean', () => {
			expect(buildSearchQuery({})).toBe('');
		});

		it('omits a null score instead of sending min_score=null', () => {
			expect(buildSearchQuery({ minScore: null })).toBe('');
			expect(buildSearchQuery({ minScore: 0 })).toBe('?min_score=0');
		});

		it('encodes values that need it', () => {
			expect(buildSearchQuery({ districts: ['Al Munsiyah'] })).toBe('?district=Al+Munsiyah');
		});
	});
}
