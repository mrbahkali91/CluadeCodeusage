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
