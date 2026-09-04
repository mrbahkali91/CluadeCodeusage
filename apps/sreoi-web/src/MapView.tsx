import type { Locale } from './i18n.ts';
import maplibregl from 'maplibre-gl';
import { useEffect, useRef } from 'react';
import { fetchMapDistricts, fetchMapOpportunities } from './api.ts';

/**
 * The map deliberately has no third-party basemap.
 *
 * Tiles from a public provider would send every viewport this platform's users
 * pan to -- effectively, which districts an investor is studying -- to a third
 * party, and the platform's own Content-Security-Policy allows no external
 * origins. District polygons come from the platform's own PostGIS geometry
 * instead, which is enough to orient the reader and leaks nothing.
 */
const EMPTY_STYLE: maplibregl.StyleSpecification = {
	version: 8,
	sources: {},
	// `glyphs` is deliberately ABSENT rather than set to undefined. MapLibre
	// validates the style object and rejects an explicit `glyphs: undefined`
	// with "glyphs: string expected, undefined found", which aborts the whole
	// style load and leaves a blank canvas with only a console error. Omitting
	// the key is what "no glyph server" actually means.
	//
	// Because there are no glyphs, labels must be DOM markers rather than
	// symbol layers -- a symbol layer renders nothing at all without them.
	layers: [
		{
			id: 'background',
			type: 'background',
			paint: { 'background-color': '#eef1f5' },
		},
	],
};

export function scoreColour(score: number | null): string {
	if (score === null || !Number.isFinite(score)) {
		return '#a02128';
	}
	if (score >= 80) {
		return '#136f3f';
	}
	if (score >= 60) {
		return '#b8860b';
	}
	return '#a02128';
}

export function MapView({ locale }: { locale: Locale }): React.JSX.Element {
	const container = useRef<HTMLDivElement | null>(null);
	const mapRef = useRef<maplibregl.Map | null>(null);

	// Owns the MapLibre instance for the lifetime of this component. MapLibre is
	// imperative and holds a WebGL context, so it cannot live in React state:
	// it is created here and torn down in the returned cleanup, which also
	// removes every marker. Skipping that teardown leaks a GL context per
	// mount, and browsers cap how many may exist at once.
	useEffect(() => {
		if (container.current === null) {
			return;
		}
		const map = new maplibregl.Map({
			container: container.current,
			style: EMPTY_STYLE,
			center: [46.79, 24.83],
			zoom: 11,
			attributionControl: false,
		});
		mapRef.current = map;
		map.addControl(new maplibregl.NavigationControl(), 'top-right');

		const markers: maplibregl.Marker[] = [];

		map.on('load', () => {
			void (async () => {
				const [districts, opportunities] = await Promise.all([
					fetchMapDistricts(),
					fetchMapOpportunities(),
				]);

				map.addSource('districts', { type: 'geojson', data: districts });
				map.addLayer({
					id: 'district-fill',
					type: 'fill',
					source: 'districts',
					paint: { 'fill-color': '#5fae86', 'fill-opacity': 0.28 },
				});
				map.addLayer({
					id: 'district-line',
					type: 'line',
					source: 'districts',
					paint: { 'line-color': '#2f7d5b', 'line-width': 1 },
				});

				for (const feature of opportunities.features) {
					if (feature.geometry.type !== 'Point') {
						continue;
					}
					const [lon, lat] = feature.geometry.coordinates as [number, number];
					const properties = feature.properties ?? {};
					const element = document.createElement('div');
					element.className = 'marker';
					element.style.background = scoreColour(
						properties.score == null ? null : Number(properties.score),
					);
					element.title = String(properties.title ?? '');
					markers.push(new maplibregl.Marker({ element }).setLngLat([lon, lat]).addTo(map));
				}
			})();
		});

		return () => {
			for (const marker of markers) {
				marker.remove();
			}
			map.remove();
			mapRef.current = null;
		};
	}, [locale]);

	return <div id="map" ref={container} />;
}

if (import.meta.vitest != null) {
	describe('scoreColour', () => {
		it('bands scores the same way the legend claims', () => {
			expect(scoreColour(85)).toBe('#136f3f');
			expect(scoreColour(80)).toBe('#136f3f');
			expect(scoreColour(70)).toBe('#b8860b');
			expect(scoreColour(60)).toBe('#b8860b');
			expect(scoreColour(59)).toBe('#a02128');
		});

		it('treats an absent score as insufficient rather than as zero-but-valid', () => {
			expect(scoreColour(null)).toBe('#a02128');
			expect(scoreColour(Number.NaN)).toBe('#a02128');
		});
	});
}
