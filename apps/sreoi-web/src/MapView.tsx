import type { Locale } from './i18n.ts';
import maplibregl from 'maplibre-gl';
import { useEffect, useRef } from 'react';
import { fetchMapDistricts, fetchMapOpportunities } from './api.ts';

/**
 * The map has no third-party basemap by default.
 *
 * Tiles from a public provider would send every viewport this platform's users
 * pan to -- effectively, which districts an investor is studying -- to a third
 * party, and the platform's own Content-Security-Policy allows no external
 * origins.
 *
 * That decision was right and the first implementation of it was still a bad
 * map: four translucent rectangles and a scatter of dots on an empty grey
 * field, at a hard-coded centre and zoom that put them in one corner. Nothing
 * named a district, nothing gave a distance, and a reader could not tell where
 * in Riyadh they were looking. A map that leaks nothing but orients nobody is
 * not a working map.
 *
 * So the reference geography is drawn from data we already hold, rather than
 * borrowed from a tile server:
 *   - the view is fitted to the data, so it fills the frame;
 *   - each district polygon carries its own name as a DOM label;
 *   - a scale bar gives absolute distance;
 *   - a coordinate grid gives orientation.
 *
 * A real basemap is a one-line opt-in for whoever accepts the trade: set
 * VITE_MAP_TILE_URL to an XYZ raster template and add that origin to the CSP.
 * Unset -- the default -- the map makes no external request at all.
 */

/** An XYZ raster template, e.g. `https://tiles.example.com/{z}/{x}/{y}.png`. */
const TILE_URL: string | undefined = import.meta.env.VITE_MAP_TILE_URL as string | undefined;
const TILE_ATTRIBUTION: string = (import.meta.env.VITE_MAP_TILE_ATTRIBUTION as string) ?? '';

export function buildStyle(
	tileUrl: string | undefined,
	attribution: string,
): maplibregl.StyleSpecification {
	// `glyphs` is deliberately ABSENT rather than set to undefined. MapLibre
	// validates the style object and rejects an explicit `glyphs: undefined`
	// with "glyphs: string expected, undefined found", which aborts the whole
	// style load and leaves a blank canvas with only a console error. Omitting
	// the key is what "no glyph server" actually means.
	//
	// Because there are no glyphs, labels must be DOM markers rather than
	// symbol layers -- a symbol layer renders nothing at all without them.
	const style: maplibregl.StyleSpecification = {
		version: 8,
		sources: {},
		layers: [
			{
				id: 'background',
				type: 'background',
				paint: { 'background-color': '#eef1f5' },
			},
		],
	};
	if (tileUrl === undefined || tileUrl === '') {
		return style;
	}
	style.sources.basemap = {
		type: 'raster',
		tiles: [tileUrl],
		tileSize: 256,
		// Attribution is the tile licence's requirement, not a decoration. A
		// provider configured without one is surfaced as an explicit gap rather
		// than silently rendered unattributed.
		attribution: attribution === '' ? 'basemap attribution not configured' : attribution,
	};
	style.layers.push({ id: 'basemap', type: 'raster', source: 'basemap' });
	return style;
}

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

interface Bounds {
	west: number;
	south: number;
	east: number;
	north: number;
}

/**
 * The extent of every coordinate in both layers.
 *
 * Returns null for no coordinates at all, so the caller keeps its default view
 * instead of fitting to an empty box -- MapLibre reads a degenerate bounds as
 * infinite zoom and renders nothing.
 */
export function dataBounds(geometries: GeoJSON.Geometry[]): Bounds | null {
	let west = Infinity;
	let south = Infinity;
	let east = -Infinity;
	let north = -Infinity;

	const visit = (coordinates: unknown): void => {
		if (!Array.isArray(coordinates)) {
			return;
		}
		if (typeof coordinates[0] === 'number' && typeof coordinates[1] === 'number') {
			const [lon, lat] = coordinates as [number, number];
			if (!Number.isFinite(lon) || !Number.isFinite(lat)) {
				return;
			}
			west = Math.min(west, lon);
			east = Math.max(east, lon);
			south = Math.min(south, lat);
			north = Math.max(north, lat);
			return;
		}
		for (const child of coordinates) {
			visit(child);
		}
	};

	for (const geometry of geometries) {
		if (geometry.type === 'GeometryCollection') {
			continue;
		}
		visit(geometry.coordinates);
	}

	if (!Number.isFinite(west) || !Number.isFinite(south)) {
		return null;
	}
	return { west, south, east, north };
}

/**
 * A lon/lat grid covering `bounds`, as a GeoJSON line layer.
 *
 * Without a basemap this is the only absolute reference on the canvas: it is
 * what tells a reader that two clusters are 4km apart rather than 400m.
 */
export function graticule(bounds: Bounds, step: number): GeoJSON.FeatureCollection {
	const features: GeoJSON.Feature[] = [];
	// Stepped by integer index, never by `+= step`. Accumulating 0.02 in binary
	// floating point overshoots the upper bound by ~5e-15, which silently drops
	// the last grid line on one axis and not the other -- a grid missing its
	// right-hand edge, for no reason a reader could ever guess.
	const first = (v: number): number => Math.floor(v / step);
	const last = (v: number): number => Math.ceil(v / step);
	const south = first(bounds.south) * step;
	const north = last(bounds.north) * step;
	const west = first(bounds.west) * step;
	const east = last(bounds.east) * step;

	for (let i = first(bounds.west); i <= last(bounds.east); i += 1) {
		const lon = i * step;
		features.push({
			type: 'Feature',
			properties: {},
			geometry: { type: 'LineString', coordinates: [[lon, south], [lon, north]] },
		});
	}
	for (let i = first(bounds.south); i <= last(bounds.north); i += 1) {
		const lat = i * step;
		features.push({
			type: 'Feature',
			properties: {},
			geometry: { type: 'LineString', coordinates: [[west, lat], [east, lat]] },
		});
	}
	return { type: 'FeatureCollection', features };
}

/** The centre of a polygon's extent — where its name label belongs. */
export function centroidOf(geometry: GeoJSON.Geometry): [number, number] | null {
	const bounds = dataBounds([geometry]);
	if (bounds === null) {
		return null;
	}
	return [(bounds.west + bounds.east) / 2, (bounds.south + bounds.north) / 2];
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
			style: buildStyle(TILE_URL, TILE_ATTRIBUTION),
			// Riyadh, used only until the data arrives and the view is fitted to it.
			center: [46.79, 24.83],
			zoom: 11,
			attributionControl: false,
		});
		mapRef.current = map;
		map.addControl(new maplibregl.NavigationControl(), 'top-right');
		// Absolute distance. Without a basemap nothing else on the canvas
		// supplies it, and a discount is a claim about a neighbourhood.
		map.addControl(new maplibregl.ScaleControl({ maxWidth: 120, unit: 'metric' }), 'bottom-left');
		if (TILE_URL !== undefined && TILE_URL !== '') {
			map.addControl(new maplibregl.AttributionControl({ compact: true }));
		}

		const markers: maplibregl.Marker[] = [];

		map.on('load', () => {
			void (async () => {
				const [districts, opportunities] = await Promise.all([
					fetchMapDistricts(),
					fetchMapOpportunities(),
				]);

				const bounds = dataBounds([
					...districts.features.map(f => f.geometry),
					...opportunities.features.map(f => f.geometry),
				]);

				// The grid goes underneath everything, so district fills read as
				// areas rather than as windows onto a grid.
				if (bounds !== null) {
					map.addSource('graticule', { type: 'geojson', data: graticule(bounds, 0.02) });
					map.addLayer({
						id: 'graticule-line',
						type: 'line',
						source: 'graticule',
						paint: { 'line-color': '#c9d2dd', 'line-width': 0.6 },
					});
				}

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

				// District names, as DOM labels because there is no glyph server.
				// Added AFTER the score markers on purpose: MapLibre appends every
				// marker to one container, so DOM order is paint order, and a name
				// half-hidden behind a dot is worse than no name at all.
				// An unnamed polygon is the difference between "somewhere in
				// Riyadh" and "Qurtubah".
				for (const feature of districts.features) {
					const centre = centroidOf(feature.geometry);
					if (centre === null) {
						continue;
					}
					const properties = feature.properties ?? {};
					const name = locale === 'ar'
						? (properties.name_ar ?? properties.name_en)
						: (properties.name_en ?? properties.name_ar);
					if (name == null || name === '') {
						continue;
					}
					const label = document.createElement('div');
					label.className = 'map-label';
					// `dir=auto` so an Arabic name is not reordered around its
					// surrounding punctuation.
					label.dir = 'auto';
					label.textContent = String(name);
					markers.push(
						new maplibregl.Marker({ element: label }).setLngLat(centre).addTo(map),
					);
				}

				// Fit last, once every layer exists. A hard-coded centre and zoom
				// left the whole corpus in one corner of the frame at a scale
				// nobody chose.
				if (bounds !== null && bounds.east > bounds.west && bounds.north > bounds.south) {
					map.fitBounds(
						[
							[bounds.west, bounds.south],
							[bounds.east, bounds.north],
						],
						{ padding: 56, duration: 0 },
					);
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

	describe('buildStyle', () => {
		it('makes no external request when no tile url is configured', () => {
			const style = buildStyle(undefined, '');
			expect(style.sources).toEqual({});
			expect(style.layers.map(l => l.id)).toEqual(['background']);
		});

		it('treats an empty tile url as absent rather than as a request to ""', () => {
			expect(buildStyle('', '').sources).toEqual({});
		});

		it('omits glyphs entirely rather than setting it undefined', () => {
			// An explicit `glyphs: undefined` fails MapLibre style validation and
			// blanks the canvas with only a console error.
			expect('glyphs' in buildStyle(undefined, '')).toBe(false);
		});

		it('adds a raster basemap when one is configured', () => {
			const style = buildStyle('https://t.example.com/{z}/{x}/{y}.png', '© Example');
			expect(style.layers.map(l => l.id)).toEqual(['background', 'basemap']);
			expect(style.sources.basemap).toMatchObject({
				type: 'raster',
				tiles: ['https://t.example.com/{z}/{x}/{y}.png'],
				attribution: '© Example',
			});
		});

		it('says so when a basemap is configured without attribution', () => {
			// Attribution is the tile licence's requirement. Rendering someone's
			// tiles unattributed is a licence breach, so the gap is visible.
			const style = buildStyle('https://t.example.com/{z}/{x}/{y}.png', '');
			expect(style.sources.basemap).toMatchObject({
				attribution: 'basemap attribution not configured',
			});
		});
	});

	describe('dataBounds', () => {
		it('covers points and polygons together', () => {
			const bounds = dataBounds([
				{ type: 'Point', coordinates: [46.9, 24.7] },
				{
					type: 'Polygon',
					coordinates: [[[46.7, 24.8], [46.8, 24.8], [46.8, 24.9], [46.7, 24.9], [46.7, 24.8]]],
				},
			]);
			expect(bounds).toEqual({ west: 46.7, south: 24.7, east: 46.9, north: 24.9 });
		});

		it('returns null for no geometry, so the caller keeps its default view', () => {
			// Fitting to a degenerate box makes MapLibre zoom to infinity and
			// render an empty canvas.
			expect(dataBounds([])).toBeNull();
		});

		it('ignores non-finite coordinates instead of poisoning the extent', () => {
			expect(dataBounds([
				{ type: 'Point', coordinates: [Number.NaN, 24.7] },
				{ type: 'Point', coordinates: [46.8, 24.8] },
			])).toEqual({ west: 46.8, south: 24.8, east: 46.8, north: 24.8 });
		});
	});

	describe('graticule', () => {
		it('spans the whole extent on grid multiples', () => {
			const grid = graticule({ west: 46.71, south: 24.71, east: 46.75, north: 24.75 }, 0.02);
			// 46.70, 46.72, 46.74, 46.76 and the same four latitudes. Stepping
			// by `+= 0.02` instead of by index drops the last of each to
			// floating-point overshoot, giving 7.
			expect(grid.features).toHaveLength(8);
		});

		it('still produces a grid for a single point', () => {
			const grid = graticule({ west: 46.7, south: 24.7, east: 46.7, north: 24.7 }, 0.02);
			expect(grid.features.length).toBeGreaterThan(0);
		});
	});

	describe('centroidOf', () => {
		it('centres a label inside its polygon', () => {
			expect(centroidOf({
				type: 'Polygon',
				coordinates: [[[46.7, 24.8], [46.8, 24.8], [46.8, 24.9], [46.7, 24.9], [46.7, 24.8]]],
			})).toEqual([46.75, 24.85]);
		});
	});
}
