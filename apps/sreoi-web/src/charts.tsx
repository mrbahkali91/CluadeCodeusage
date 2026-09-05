/**
 * The chart primitives this dashboard needs, built in plain SVG/HTML.
 *
 * Colour follows the job, not taste. Both bar charts carry a SINGLE series, so
 * they use one sequential hue and no legend -- the title names the series.
 * The provenance stack is four ordered classes, so it uses an ordinal ramp
 * (strongest evidence darkest) with direct labels and a legend, never four
 * arbitrary hues. Status colours are reserved for status and always ship with
 * the word written next to them, never colour alone.
 *
 * The ramps below were validated with the dataviz palette validator rather
 * than chosen by eye: the ordinal ramp passes lightness monotonicity, adjacent
 * ΔL and light-end contrast on both surfaces, and the bar hue clears 3:1
 * against the chart surface.
 */

/** Sequential single hue (blue 400). 3.54:1 on the light surface. */
const BAR = '#3987e5';

/**
 * Ordinal ramp, strongest evidence darkest. Light-mode steps 650/500/350/250 --
 * the light end is step 250 because an ordinal ramp's lightest step must still
 * clear 2:1 against the surface, unlike a continuous sequential ramp.
 */
export const ORDINAL = ['#104281', '#256abf', '#5598e7', '#86b6ef'] as const;

export function BarRow({
	label,
	value,
	max,
	display,
	title,
}: {
	label: string;
	value: number;
	max: number;
	display: string;
	title?: string | undefined;
}): React.JSX.Element {
	const pct = max <= 0 ? 0 : Math.max(0, Math.min(1, value / max)) * 100;
	return (
		<div className="barrow" title={title ?? `${label}: ${display}`}>
			<div className="barrow-label" dir="auto">{label}</div>
			<div className="barrow-track">
				{/* Anchored to the baseline with a rounded data-end, thin mark. */}
				<div className="barrow-fill" style={{ width: `${pct}%`, background: BAR }} />
			</div>
			<div className="barrow-value">{display}</div>
		</div>
	);
}

/**
 * Part-to-whole across four ordered classes. Direct labels are mandatory at
 * four series, and a 2px surface gap separates adjacent segments so the
 * boundaries read without relying on hue difference alone.
 */
export function StackedBar({
	segments,
}: {
	segments: { label: string; value: number; display: string }[];
}): React.JSX.Element {
	const total = segments.reduce((sum, s) => sum + s.value, 0);
	return (
		<>
			<div className="stack">
				{segments.map((segment, index) => {
					const pct = total <= 0 ? 0 : (segment.value / total) * 100;
					return (
						<div
							key={segment.label}
							className="stack-seg"
							style={{ width: `${pct}%`, background: ORDINAL[index] ?? ORDINAL[ORDINAL.length - 1] }}
							title={`${segment.label}: ${segment.display}`}
						/>
					);
				})}
			</div>
			<div className="legend">
				{segments.map((segment, index) => (
					<span key={segment.label}>
						<i style={{ background: ORDINAL[index] ?? ORDINAL[ORDINAL.length - 1], borderRadius: 2 }} />
						{segment.label}
						{' '}
						<strong>{segment.display}</strong>
					</span>
				))}
			</div>
		</>
	);
}

/**
 * A single value against its warn and fail limits.
 *
 * NOT hard-coded to a 0-1 track. Most quality flags are ratios, but
 * `stalest_source_age_days` carries limits of 7 and 30 DAYS -- clamping that to
 * [0,1] piled both ticks at the far right and drew 0.47 days as 47% of the
 * track, which tells the reader nothing about how close the source is to
 * stale. The scale is therefore derived from the value and its own limits.
 *
 * Ratios keep a literal 0-100% track so they stay comparable row to row; any
 * flag whose numbers exceed 1 gets its own range with headroom, so the limit
 * ticks land somewhere legible instead of on the end cap.
 */
export function meterScale(value: number, warn: number | null, fail: number | null): number {
	const points = [value, warn ?? 0, fail ?? 0];
	const largest = Math.max(...points);
	return largest <= 1 ? 1 : largest * 1.15;
}

export function Meter({
	value,
	warn,
	fail,
	higherIsBetter,
}: {
	value: number;
	warn: number | null;
	fail: number | null;
	higherIsBetter: boolean;
}): React.JSX.Element {
	const scale = meterScale(value, warn, fail);
	const pos = (v: number): number => Math.max(0, Math.min(1, v / scale)) * 100;
	return (
		<div className="meter" aria-hidden="true">
			<div className="meter-fill" style={{ width: `${pos(value)}%` }} />
			{warn !== null && <i className="meter-tick warn" style={{ insetInlineStart: `${pos(warn)}%` }} />}
			{fail !== null && <i className="meter-tick fail" style={{ insetInlineStart: `${pos(fail)}%` }} />}
			<span className="sr-only">
				{higherIsBetter ? 'higher is better' : 'lower is better'}
			</span>
		</div>
	);
}

if (import.meta.vitest != null) {
	describe('meterScale', () => {
		it('keeps a literal 0-100% track for ratios, so rows stay comparable', () => {
			expect(meterScale(0.97, 0.7, 0.5)).toBe(1);
			expect(meterScale(0, 0.02, 0.1)).toBe(1);
		});

		it('rescales a flag whose limits are not ratios', () => {
			// stalest_source_age_days: warn 7 days, fail 30 days. Clamping this to
			// [0,1] put both ticks on the end cap and drew 0.47 days as 47%.
			const scale = meterScale(0.467, 7, 30);
			expect(scale).toBeCloseTo(34.5, 1);
			// The fail tick must land inside the track, not on its end.
			expect((30 / scale) * 100).toBeLessThan(100);
			expect((7 / scale) * 100).toBeGreaterThan(0);
		});

		it('leaves headroom so the largest limit is never flush with the end', () => {
			expect(meterScale(50, 10, 20)).toBeGreaterThan(50);
		});
	});

	describe('ordinal ramp', () => {
		it('runs darkest to lightest, so strongest evidence reads heaviest', () => {
			expect(ORDINAL[0]).toBe('#104281');
			expect(ORDINAL[ORDINAL.length - 1]).toBe('#86b6ef');
		});

		it('has exactly one step per provenance basis', () => {
			// ACTUAL, RULE, ESTIMATE, UNKNOWN. A fifth basis must extend the ramp
			// deliberately rather than wrap around and reuse a colour.
			expect(ORDINAL).toHaveLength(4);
		});

		it('holds only distinct steps', () => {
			expect(new Set(ORDINAL).size).toBe(ORDINAL.length);
		});
	});
}
