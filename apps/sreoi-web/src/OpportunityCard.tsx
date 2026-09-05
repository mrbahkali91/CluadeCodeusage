import type { Opportunity } from './api.ts';
import type { Locale } from './i18n.ts';
import { formatNumber, formatPercent, t } from './i18n.ts';

/**
 * A score is shown with the two things that decide whether it can be trusted:
 * the confidence, and whether the confidence gate capped it. Presenting the
 * number alone would be the failure the specification warns about -- a figure
 * that invites trust it has not earned.
 */
export function classificationTone(classification: string | null): 'good' | 'warn' | 'bad' {
	switch (classification) {
		case 'STRONG_OPPORTUNITY':
			return 'good';
		case 'WORTH_REVIEWING':
			return 'warn';
		default:
			// INSUFFICIENT_DATA and anything unrecognised. Deliberately not
			// optimistic: an unknown classification must never render as a
			// recommendation.
			return 'bad';
	}
}

export function OpportunityCard({
	opportunity,
	locale,
	onOpen,
}: {
	opportunity: Opportunity;
	locale: Locale;
	onOpen: (id: string) => void;
}): React.JSX.Element {
	const district
		= locale === 'ar' ? (opportunity.districtAr ?? opportunity.district) : opportunity.district;
	const tone = classificationTone(opportunity.classification);

	return (
		<article className="card">
			{/* A button rather than a div with onClick, so the card is reachable by
			    keyboard and announced as actionable by a screen reader. */}
			<h3>
				<button type="button" className="cardlink" onClick={() => onOpen(opportunity.id)}>
					{opportunity.title}
				</button>
			</h3>
			<div className="meta">
				{district ?? '—'}
				{' · '}
				{opportunity.opportunityType}
				{' · '}
				{t(locale, 'card.area')}
				{' '}
				{formatNumber(opportunity.areaSqm, locale, 1)}
				{' m²'}
			</div>

			<span className={`pill ${tone}`}>{opportunity.classification ?? 'INSUFFICIENT_DATA'}</span>
			{opportunity.capped === true && (
				<span className="pill warn" style={{ marginInlineStart: 6 }}>
					{t(locale, 'card.capped')}
				</span>
			)}

			<div className="stats" style={{ marginTop: 12 }}>
				<div className="stat">
					<div className="k">{t(locale, 'card.score')}</div>
					<div className="v">{formatNumber(opportunity.totalScore, locale, 1)}</div>
				</div>
				<div className="stat">
					<div className="k">{t(locale, 'card.discount')}</div>
					<div className="v">{formatPercent(opportunity.discountFraction, locale)}</div>
				</div>
				<div className="stat">
					<div className="k">{t(locale, 'card.confidence')}</div>
					<div className="v">{formatPercent(opportunity.dataConfidence, locale)}</div>
				</div>
			</div>

			{opportunity.discountRefusedReason !== null && (
				<div className="refused">
					{t(locale, 'card.refused')}
					{': '}
					{opportunity.discountRefusedReason}
				</div>
			)}
		</article>
	);
}

if (import.meta.vitest != null) {
	describe('classificationTone', () => {
		it('maps the two positive classifications', () => {
			expect(classificationTone('STRONG_OPPORTUNITY')).toBe('good');
			expect(classificationTone('WORTH_REVIEWING')).toBe('warn');
		});

		it('never renders an unknown classification as a recommendation', () => {
			// A new classification added on the engine side must not arrive here
			// looking like a buy signal.
			expect(classificationTone('INSUFFICIENT_DATA')).toBe('bad');
			expect(classificationTone(null)).toBe('bad');
			expect(classificationTone('SOME_FUTURE_VALUE')).toBe('bad');
		});
	});
}
