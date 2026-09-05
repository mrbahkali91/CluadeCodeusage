import type { QualityReport, SourceRow } from './api.ts';
import type { Locale } from './i18n.ts';
import { useEffect, useState } from 'react';
import { ApiError, fetchQuality, fetchSources } from './api.ts';
import { BarRow, Meter, StackedBar } from './charts.tsx';
import { formatNumber, formatPercent, t } from './i18n.ts';

/**
 * Operational dashboards.
 *
 * Both read the engine's own versioned report rather than recomputing anything,
 * and both lead with the synthetic-evidence caveat: a data-quality page is
 * exactly where someone would mistake fixture data for a measurement of the
 * Saudi market.
 *
 * The metric that matters most on this page is the one that looks worst.
 * Refusal rates are high by design -- the engine declining to state a value it
 * cannot support is the product working, not failing -- so those rows say so in
 * place of being quietly coloured green.
 */

type Tab = 'quality' | 'sources';

export function AdminView({ locale }: { locale: Locale }): React.JSX.Element {
	const [tab, setTab] = useState<Tab>('quality');
	const [quality, setQuality] = useState<QualityReport | null>(null);
	const [sources, setSources] = useState<SourceRow[] | null>(null);
	const [status, setStatus] = useState<'loading' | 'ready' | 'forbidden' | 'error'>('loading');

	// Both payloads are fetched once on mount rather than per tab, because they
	// are small, the tabs are toggled freely, and refetching on every toggle
	// would make an operational page feel slower than the system it reports on.
	useEffect(() => {
		let cancelled = false;
		Promise.all([fetchQuality(), fetchSources()])
			.then(([q, s]) => {
				if (!cancelled) {
					setQuality(q);
					setSources(s);
					setStatus('ready');
				}
			})
			.catch((error: unknown) => {
				if (cancelled) {
					return;
				}
				setStatus(error instanceof ApiError && error.status === 403 ? 'forbidden' : 'error');
			});
		return () => {
			cancelled = true;
		};
	}, []);

	if (status === 'loading') {
		return <p className="muted">{t(locale, 'loading')}</p>;
	}
	if (status === 'forbidden') {
		return <div className="refused">{t(locale, 'admin.forbidden')}</div>;
	}
	if (status === 'error' || quality === null || sources === null) {
		return <div className="refused">{t(locale, 'error')}</div>;
	}

	return (
		<>
			<nav className="tabs">
				<button
					type="button"
					className={tab === 'quality' ? 'on' : ''}
					onClick={() => setTab('quality')}
				>
					{t(locale, 'admin.quality')}
				</button>
				<button
					type="button"
					className={tab === 'sources' ? 'on' : ''}
					onClick={() => setTab('sources')}
				>
					{t(locale, 'admin.sources')}
				</button>
			</nav>

			{tab === 'quality' ? <Quality report={quality} locale={locale} /> : null}
			{tab === 'sources' ? <Sources rows={sources} locale={locale} /> : null}
		</>
	);
}

function statusTone(value: string): 'good' | 'warn' | 'bad' {
	switch (value.toUpperCase()) {
		case 'OK':
		case 'HEALTHY':
		case 'CONFIRMED':
			return 'good';
		case 'WARN':
		case 'UNKNOWN':
		case 'REQUIRES_VALIDATION':
			return 'warn';
		default:
			// FAIL, STALE, NOT_RECOMMENDED, and anything unrecognised.
			return 'bad';
	}
}

function Quality({ report, locale }: { report: QualityReport; locale: Locale }): React.JSX.Element {
	const confidence = report.confidence_distribution.data_confidence;
	const provenance = report.provenance.by_basis;
	const verification = Object.entries(report.verification.by_check_type);
	// Ordered strongest evidence first, which is also the ramp's dark end.
	const basisOrder = ['ACTUAL', 'RULE', 'ESTIMATE', 'UNKNOWN'] as const;
	const maxBucket = Math.max(1, ...confidence.buckets.map(b => b.count));

	return (
		<>
			<section className="panel">
				<h2>{t(locale, 'admin.overall')}</h2>
				<span className={`pill ${statusTone(report.overall_status)}`}>
					{report.overall_status}
				</span>
				<span className="muted" style={{ marginInlineStart: 10 }}>
					<code>{report.method_version}</code>
					{' · '}
					{(report.captured_at ?? '').slice(0, 16).replace('T', ' ')}
				</span>
				<div className="stats" style={{ marginTop: 12, gridTemplateColumns: 'repeat(4,1fr)' }}>
					<Tile k={t(locale, 'admin.opportunities')} v={formatNumber(report.counts.opportunities, locale)} />
					<Tile k={t(locale, 'admin.properties')} v={formatNumber(report.counts.properties, locale)} />
					<Tile
						k={t(locale, 'admin.meanConfidence')}
						v={formatPercent(confidence.mean, locale)}
					/>
					<Tile
						k={t(locale, 'admin.completeness')}
						v={formatPercent(report.field_completeness.overall, locale)}
					/>
				</div>
			</section>

			<section className="panel">
				<h2>{t(locale, 'admin.flags')}</h2>
				<div className="scroll">
					<table>
						<thead>
							<tr>
								<th>{t(locale, 'detail.status')}</th>
								<th>{t(locale, 'admin.metric')}</th>
								<th className="num">{t(locale, 'admin.value')}</th>
								<th style={{ width: 160 }}>{t(locale, 'admin.thresholds')}</th>
							</tr>
						</thead>
						<tbody>
							{report.flags.map(flag => (
								<tr key={flag.key}>
									<td><span className={`pill ${statusTone(flag.severity)}`}>{flag.severity}</span></td>
									<td dir="auto">
										{flag.label}
										{flag.note !== null && <div className="muted" style={{ fontSize: 12 }}>{flag.note}</div>}
									</td>
									<td className="num">{formatNumber(flag.value, locale, 3)}</td>
									<td>
										<Meter
											value={typeof flag.value === 'number' ? flag.value : 0}
											warn={flag.warn_at ?? null}
											fail={flag.fail_at ?? null}
											higherIsBetter={(flag.warn_at ?? 0) > (flag.fail_at ?? 0)}
										/>
									</td>
								</tr>
							))}
						</tbody>
					</table>
				</div>
			</section>

			<section className="panel">
				<h2>{t(locale, 'admin.confidenceDist')}</h2>
				<p className="muted" style={{ marginTop: -6 }}>{t(locale, 'admin.confidenceNote')}</p>
				{confidence.buckets.map(bucket => (
					<BarRow
						key={bucket.label}
						label={bucket.label}
						value={bucket.count}
						max={maxBucket}
						display={`${formatNumber(bucket.count, locale)} · ${formatPercent(bucket.share, locale)}`}
					/>
				))}
			</section>

			<section className="panel">
				<h2>{t(locale, 'admin.verification')}</h2>
				{verification.map(([name, stats]) => (
					<BarRow
						key={name}
						label={name}
						value={stats.applicable === 0 ? 0 : stats.pass_rate}
						max={1}
						display={stats.applicable === 0
							? t(locale, 'admin.notApplicable')
							: `${formatPercent(stats.pass_rate, locale)} (${formatNumber(stats.verified, locale)}/${formatNumber(stats.applicable, locale)})`}
					/>
				))}
			</section>

			<section className="panel">
				<h2>{t(locale, 'admin.provenance')}</h2>
				<p className="muted" style={{ marginTop: -6 }}>{t(locale, 'admin.provenanceNote')}</p>
				<StackedBar
					segments={basisOrder.map(basis => ({
						label: basis,
						value: provenance[basis] ?? 0,
						display: formatNumber(provenance[basis] ?? 0, locale),
					}))}
				/>
			</section>
		</>
	);
}

function Sources({ rows, locale }: { rows: SourceRow[]; locale: Locale }): React.JSX.Element {
	return (
		<section className="panel">
			<h2>{t(locale, 'admin.sources')}</h2>
			<div className="scroll">
				<table>
					<thead>
						<tr>
							<th>{t(locale, 'admin.source')}</th>
							<th>{t(locale, 'admin.availability')}</th>
							<th>{t(locale, 'admin.access')}</th>
							<th>{t(locale, 'admin.licence')}</th>
							<th className="num">{t(locale, 'admin.records')}</th>
							<th className="num">{t(locale, 'card.confidence')}</th>
						</tr>
					</thead>
					<tbody>
						{rows.map(row => (
							<tr key={row.key}>
								<td>
									<div dir="auto">{row.name}</div>
									<code>{row.key}</code>
									{row.is_synthetic && (
										<span className="pill warn" style={{ marginInlineStart: 6 }}>
											{t(locale, 'admin.synthetic')}
										</span>
									)}
								</td>
								<td>
									<span className={`pill ${statusTone(row.availability_label)}`}>
										{row.availability_label}
									</span>
								</td>
								<td dir="auto">{row.legal_access_method}</td>
								<td className="muted" dir="auto">{row.data_license ?? '—'}</td>
								<td className="num">{formatNumber(row.record_count, locale)}</td>
								<td className="num">{formatPercent(row.source_confidence, locale)}</td>
							</tr>
						))}
					</tbody>
				</table>
			</div>
		</section>
	);
}

function Tile({ k, v }: { k: string; v: string }): React.JSX.Element {
	return (
		<div className="stat">
			<div className="k">{k}</div>
			<div className="v">{v}</div>
		</div>
	);
}

export { statusTone };

if (import.meta.vitest != null) {
	describe('statusTone', () => {
		it('maps the healthy states', () => {
			expect(statusTone('OK')).toBe('good');
			expect(statusTone('HEALTHY')).toBe('good');
			expect(statusTone('CONFIRMED')).toBe('good');
		});

		it('treats unknown and requires-validation as warnings, not passes', () => {
			expect(statusTone('UNKNOWN')).toBe('warn');
			expect(statusTone('REQUIRES_VALIDATION')).toBe('warn');
		});

		it('never renders an unrecognised state as healthy', () => {
			// A new status added engine-side must not arrive here looking fine.
			expect(statusTone('FAIL')).toBe('bad');
			expect(statusTone('NOT_RECOMMENDED')).toBe('bad');
			expect(statusTone('SOMETHING_NEW')).toBe('bad');
		});

		it('is case-insensitive, since these arrive from two different payloads', () => {
			expect(statusTone('ok')).toBe('good');
		});
	});
}
