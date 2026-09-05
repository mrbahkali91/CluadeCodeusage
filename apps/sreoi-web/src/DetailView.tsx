import type { DetailResponse } from './api.ts';
import type { Locale } from './i18n.ts';
import { useEffect, useState } from 'react';
import { ApiError, fetchOpportunity } from './api.ts';
import { digits, formatNumber, formatPercent, t } from './i18n.ts';
import { classificationTone } from './OpportunityCard.tsx';

/**
 * The page the product's credibility rests on.
 *
 * A score on its own is a number a user has to take on trust. Everything here
 * exists so they do not have to: the derivation with each dimension's weight
 * and contribution, every cost line with the basis it was established on, the
 * comparables with their individual weights, and -- the part that matters most
 * -- the verification checks that could NOT be performed, shown as plainly as
 * the ones that passed. A page that displayed only successful checks would be
 * claiming a level of verification the platform has not achieved.
 */

const VERIFIED = new Set(['VERIFIED', 'PASSED', 'OK']);
const UNAVAILABLE = new Set(['UNAVAILABLE', 'NOT_APPLICABLE', 'NOT_PERFORMED']);

export function verificationTone(status: string): 'good' | 'warn' | 'bad' {
	if (VERIFIED.has(status)) {
		return 'good';
	}
	if (UNAVAILABLE.has(status)) {
		// Neither a pass nor a failure: nobody checked. Rendering this as a
		// failure would overstate what is known; rendering it as a pass would be
		// a lie.
		return 'warn';
	}
	return 'bad';
}

/**
 * ACTUAL is observed, RULE is derived from a published rate, ESTIMATE and
 * UNKNOWN are neither -- and an UNKNOWN material line is what makes the
 * discount refuse.
 */
export function basisTone(basis: string): 'good' | 'warn' | 'bad' {
	switch (basis) {
		case 'ACTUAL':
			return 'good';
		case 'RULE':
			return 'warn';
		default:
			return 'bad';
	}
}

export function DetailView({
	id,
	locale,
	onBack,
}: {
	id: string;
	locale: Locale;
	onBack: () => void;
}): React.JSX.Element {
	const [data, setData] = useState<DetailResponse | null>(null);
	const [status, setStatus] = useState<'loading' | 'ready' | 'error' | 'unauthorised'>('loading');

	// Refetches whenever the selected opportunity changes. The locale is not a
	// dependency: nothing here is translated server-side, so switching language
	// re-renders from the data already held rather than issuing a request.
	useEffect(() => {
		let cancelled = false;
		setStatus('loading');
		fetchOpportunity(id)
			.then((response) => {
				if (!cancelled) {
					setData(response);
					setStatus('ready');
				}
			})
			.catch((error: unknown) => {
				if (!cancelled) {
					setStatus(
						error instanceof ApiError && error.status === 401 ? 'unauthorised' : 'error',
					);
				}
			});
		return () => {
			// Guards against a slow response for a previously selected opportunity
			// arriving after the user has moved on and overwriting the new one.
			cancelled = true;
		};
	}, [id]);

	if (status === 'loading') {
		return <p className="muted">{t(locale, 'loading')}</p>;
	}
	if (status === 'unauthorised') {
		return <div className="refused">{t(locale, 'signin.required')}</div>;
	}
	if (status === 'error' || data === null) {
		return <div className="refused">{t(locale, 'error')}</div>;
	}

	const { opportunity, score, valuation, cost, comparables, verification, timeline } = data;
	const district = locale === 'ar'
		? opportunity.districtAr ?? opportunity.district
		: opportunity.district;

	return (
		<>
			<button type="button" className="ghost" onClick={onBack}>
				{t(locale, 'detail.back')}
			</button>

			<h1 style={{ marginTop: 14 }}>{opportunity.title}</h1>
			<p className="muted">
				{district ?? '—'}
				{' · '}
				{opportunity.opportunityType}
				{' · '}
				{formatNumber(opportunity.areaSqm, locale, 1)}
				{' m²'}
			</p>

			<section className="panel">
				<h2>{t(locale, 'detail.assessment')}</h2>
				<span className={`pill ${classificationTone(opportunity.classification)}`}>
					{opportunity.classification ?? 'INSUFFICIENT_DATA'}
				</span>
				{opportunity.capped === true && (
					<span className="pill warn" style={{ marginInlineStart: 6 }}>
						{t(locale, 'card.capped')}
					</span>
				)}
				<div className="stats" style={{ marginTop: 12, gridTemplateColumns: 'repeat(4,1fr)' }}>
					<Stat k={t(locale, 'card.score')} v={formatNumber(opportunity.totalScore, locale, 1)} />
					<Stat
						k={t(locale, 'detail.marketValue')}
						v={formatNumber(valuation?.fairValueBase ?? null, locale)}
						n={valuation === null
							? undefined
							: `${formatNumber(valuation.fairValueLow, locale)} – ${formatNumber(valuation.fairValueHigh, locale)}`}
					/>
					<Stat
						k={t(locale, 'detail.trueCost')}
						v={formatNumber(cost?.total ?? null, locale)}
						n={cost?.isComplete === false ? t(locale, 'detail.costIncomplete') : undefined}
					/>
					<Stat
						k={t(locale, 'card.discount')}
						v={formatPercent(opportunity.discountFraction, locale)}
					/>
				</div>
				{opportunity.discountRefusedReason !== null && (
					<div className="refused">
						{t(locale, 'card.refused')}
						{': '}
						{opportunity.discountRefusedReason}
					</div>
				)}
			</section>

			<section className="panel">
				<h2>{t(locale, 'detail.derivation')}</h2>
				<p className="muted" style={{ marginTop: -6 }}>
					{t(locale, 'detail.derivationNote')}
					{' '}
					<code>{score.weightProfileVersion ?? '—'}</code>
					{' / '}
					<code>{score.methodVersion ?? '—'}</code>
				</p>
				<div className="scroll">
					<table>
						<thead>
							<tr>
								<th>{t(locale, 'detail.dimension')}</th>
								<th className="num">{t(locale, 'detail.raw')}</th>
								<th className="num">{t(locale, 'detail.normalised')}</th>
								<th className="num">{t(locale, 'detail.weight')}</th>
								<th className="num">{t(locale, 'detail.contribution')}</th>
							</tr>
						</thead>
						<tbody>
							{score.components.map(c => (
								<tr key={c.dimension}>
									<td>{c.dimension}</td>
									<td className="num">{formatNumber(c.rawValue, locale, 4)}</td>
									<td className="num">{formatNumber(c.normalizedScore, locale, 1)}</td>
									<td className="num">{formatNumber(c.weight, locale, 2)}</td>
									<td className="num">{formatNumber(c.contribution, locale, 2)}</td>
								</tr>
							))}
						</tbody>
					</table>
				</div>
			</section>

			{cost !== null && (
				<section className="panel">
					<h2>{t(locale, 'detail.trueCost')}</h2>
					<div className="scroll">
						<table>
							<thead>
								<tr>
									<th>{t(locale, 'detail.lineItem')}</th>
									<th className="num">{t(locale, 'detail.amount')}</th>
									<th>{t(locale, 'detail.basis')}</th>
									<th>{t(locale, 'detail.material')}</th>
								</tr>
							</thead>
							<tbody>
								{cost.lines.map(line => (
									<tr key={`${line.kind}-${line.basis}`}>
										<td>{line.kind}</td>
										<td className="num">{formatNumber(line.amount, locale)}</td>
										<td>
											<span className={`pill ${basisTone(line.basis)}`}>{line.basis}</span>
										</td>
										<td>{line.material ? t(locale, 'detail.yes') : t(locale, 'detail.no')}</td>
									</tr>
								))}
							</tbody>
						</table>
					</div>
				</section>
			)}

			{valuation !== null && (
				<section className="panel">
					<h2>{t(locale, 'detail.evidence')}</h2>
					<div className="stats" style={{ gridTemplateColumns: 'repeat(4,1fr)', marginBottom: 12 }}>
						<Stat
							k={t(locale, 'detail.comparablesUsed')}
							v={formatNumber(valuation.comparableCount, locale)}
							n={`${t(locale, 'detail.effectiveN')} ${formatNumber(valuation.effectiveN, locale, 2)}`}
						/>
						<Stat
							k={t(locale, 'detail.quality')}
							v={formatNumber(valuation.comparableQuality, locale, 2)}
						/>
						<Stat
							k={t(locale, 'card.confidence')}
							v={formatPercent(valuation.confidence, locale)}
						/>
						<Stat k={t(locale, 'detail.indexTier')} v={valuation.indexTier ?? '—'} />
					</div>
					<div className="scroll">
						<table>
							<thead>
								<tr>
									<th>{t(locale, 'detail.transaction')}</th>
									<th className="num">{t(locale, 'detail.adjusted')}</th>
									<th className="num">{t(locale, 'detail.distance')}</th>
									<th className="num">{t(locale, 'detail.weight')}</th>
									<th>{t(locale, 'detail.status')}</th>
								</tr>
							</thead>
							<tbody>
								{comparables.map(c => (
									<tr key={c.transactionId}>
										<td><code>{c.transactionId.slice(0, 8)}</code></td>
										<td className="num">{formatNumber(c.adjustedPricePerSqm, locale)}</td>
										<td className="num">{formatNumber(c.distanceM, locale)}</td>
										<td className="num">{formatNumber(c.weight, locale, 3)}</td>
										<td>
											{c.excludedReason === null
												? <span className="muted">{t(locale, 'detail.used')}</span>
												: <span className="pill bad">{c.excludedReason}</span>}
										</td>
									</tr>
								))}
							</tbody>
						</table>
					</div>
				</section>
			)}

			<section className="panel">
				<h2>{t(locale, 'detail.verification')}</h2>
				<div className="scroll">
					<table>
						<thead>
							<tr>
								<th>{t(locale, 'detail.status')}</th>
								<th>{t(locale, 'detail.check')}</th>
								<th>{t(locale, 'detail.finding')}</th>
							</tr>
						</thead>
						<tbody>
							{verification.map(check => (
								<tr key={check.checkType}>
									<td>
										<span className={`pill ${verificationTone(check.status)}`}>
											{check.status}
										</span>
									</td>
									<td>{check.checkType}</td>
									{/* dir="auto" so the browser resolves direction from the text
									    itself. Verification findings are written by the engine in
									    English; inside an RTL page a string like "134 m² is
									    plausible..." otherwise renders with its leading number
									    thrown to the far end, which reads as a different claim. */}
									<td className="muted" dir="auto">{findingOf(check.evidence)}</td>
								</tr>
							))}
						</tbody>
					</table>
				</div>
			</section>

			<section className="panel">
				<h2>{t(locale, 'detail.timeline')}</h2>
				<p className="muted" style={{ marginTop: -6 }}>{t(locale, 'detail.timelineNote')}</p>
				<ul className="tl">
					{timeline.map(event => (
						<li key={`${event.eventType}-${event.occurredAt ?? ''}`}>
							<span className="when">
								{digits((event.occurredAt ?? '').slice(0, 16).replace('T', ' '), locale)}
							</span>
							<span className="what">
								<span className="kind">{event.eventType}</span>
								<br />
								<span dir="auto">{event.summary ?? '—'}</span>
							</span>
						</li>
					))}
				</ul>
			</section>
		</>
	);
}

function Stat({ k, v, n }: { k: string; v: string; n?: string | undefined }): React.JSX.Element {
	return (
		<div className="stat">
			<div className="k">{k}</div>
			<div className="v">{v}</div>
			{n !== undefined && <div className="n muted">{n}</div>}
		</div>
	);
}

/**
 * Verification evidence is a free-form object from the engine. It is rendered
 * as text, never as markup, and never interpreted: it can carry text that
 * originated in an external listing, which the specification treats as
 * untrusted throughout.
 */
export function findingOf(evidence: unknown): string {
	if (evidence === null || evidence === undefined) {
		return '—';
	}
	if (typeof evidence === 'string') {
		return evidence;
	}
	if (typeof evidence === 'object') {
		const record = evidence as Record<string, unknown>;
		// `summary` first, because that is the key the verification agent actually
		// writes. An earlier version of this list omitted it -- guessed rather than
		// checked -- so every row fell through to JSON.stringify and the page
		// rendered raw objects at the reader.
		for (const key of ['summary', 'finding', 'reason', 'message', 'note', 'detail']) {
			const value = record[key];
			if (typeof value === 'string' && value !== '') {
				return value;
			}
		}
		return JSON.stringify(evidence);
	}
	return String(evidence);
}

if (import.meta.vitest != null) {
	describe('verificationTone', () => {
		it('separates verified, unavailable and failed', () => {
			expect(verificationTone('VERIFIED')).toBe('good');
			// Not checked is not the same as failed, and must not read as passed.
			expect(verificationTone('UNAVAILABLE')).toBe('warn');
			expect(verificationTone('NOT_APPLICABLE')).toBe('warn');
			expect(verificationTone('FAILED')).toBe('bad');
		});

		it('treats an unrecognised status as a failure, never as a pass', () => {
			expect(verificationTone('SOMETHING_NEW')).toBe('bad');
		});
	});

	describe('basisTone', () => {
		it('marks ACTUAL as observed and anything unknown as not', () => {
			expect(basisTone('ACTUAL')).toBe('good');
			expect(basisTone('RULE')).toBe('warn');
			expect(basisTone('ESTIMATE')).toBe('bad');
			expect(basisTone('UNKNOWN')).toBe('bad');
		});
	});

	describe('findingOf', () => {
		it('prefers a human-readable field over dumping json', () => {
			expect(findingOf({ finding: 'area is plausible' })).toBe('area is plausible');
			expect(findingOf({ reason: 'no register integrated' })).toBe('no register integrated');
		});

		it('reads `summary`, which is the key the engine actually writes', () => {
			// The real shape: {detail: {...}, summary: '...', check_class: '...'}.
			// Omitting `summary` from the preferred keys dumped the whole object.
			expect(findingOf({
				detail: { area_sqm: 134.4 },
				summary: '134 m² is plausible for 3 bedrooms',
				check_class: 'INTERNAL',
			})).toBe('134 m² is plausible for 3 bedrooms');
		});

		it('does not return a non-string field as if it were prose', () => {
			expect(findingOf({ detail: { a: 1 } })).toBe('{"detail":{"a":1}}');
		});

		it('falls back to json rather than rendering [object Object]', () => {
			expect(findingOf({ a: 1 })).toBe('{"a":1}');
		});

		it('handles absent evidence', () => {
			expect(findingOf(null)).toBe('—');
			expect(findingOf(undefined)).toBe('—');
		});

		it('returns a plain string unchanged, and returns text rather than markup', () => {
			// Rendered through React as a text child, so any markup in external
			// listing text is escaped rather than interpreted.
			expect(findingOf('<b>hi</b>')).toBe('<b>hi</b>');
		});
	});
}
