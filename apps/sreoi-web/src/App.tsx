import type { Facets, Opportunity } from './api.ts';
import type { Locale } from './i18n.ts';
import { useCallback, useEffect, useState } from 'react';
import { ApiError, fetchFacets, searchOpportunities } from './api.ts';
import { direction, formatNumber, t } from './i18n.ts';
import { MapView } from './MapView.tsx';
import { OpportunityCard } from './OpportunityCard.tsx';

type View = 'list' | 'map';

export function App(): React.JSX.Element {
	const [locale, setLocale] = useState<Locale>('en');
	const [view, setView] = useState<View>('list');
	const [facets, setFacets] = useState<Facets | null>(null);
	const [rows, setRows] = useState<Opportunity[]>([]);
	const [count, setCount] = useState(0);
	const [status, setStatus] = useState<'loading' | 'ready' | 'error' | 'unauthorised'>('loading');

	const [district, setDistrict] = useState('');
	const [type, setType] = useState('');
	const [minScore, setMinScore] = useState('');
	const [sort, setSort] = useState('score');

	// Direction and lang live on <html>, so the whole document flips rather than
	// a container inside it -- scrollbars, form controls and text selection
	// included.
	useEffect(() => {
		document.documentElement.lang = locale;
		document.documentElement.dir = direction(locale);
	}, [locale]);

	const load = useCallback(async () => {
		setStatus('loading');
		try {
			const response = await searchOpportunities({
				districts: district === '' ? [] : [district],
				types: type === '' ? [] : [type],
				minScore: minScore === '' ? null : Number(minScore),
				sort,
				limit: 200,
			});
			setRows(response.results);
			setCount(response.count);
			setStatus('ready');
		}
		catch (error) {
			// 401 is a different message from a broken API: one tells the reader to
			// sign in, the other that something is down. Collapsing them would send
			// people to debug the wrong thing.
			setStatus(error instanceof ApiError && error.status === 401 ? 'unauthorised' : 'error');
		}
	}, [district, type, minScore, sort]);

	// Re-runs whenever a filter changes, because `load` is memoised on exactly
	// those values. Fetching in an effect rather than in the change handler
	// keeps one code path for the initial load and every subsequent filter
	// edit, so the two cannot drift apart.
	useEffect(() => {
		void load();
	}, [load]);

	// Facet lists are fetched once on mount: districts and opportunity types
	// change when the corpus is reloaded, not while someone is looking at the
	// page. A failure here is swallowed on purpose -- the filters degrade to
	// empty dropdowns, which is better than an unusable page.
	useEffect(() => {
		fetchFacets()
			.then(setFacets)
			.catch(() => {
				// Deliberately ignored; see above.
			});
	}, []);

	return (
		<>
			<header>
				<div>
					<div className="brand">{t(locale, 'brand')}</div>
					<div className="sub">{t(locale, 'tagline')}</div>
				</div>
				<nav>
					<button
						type="button"
						className={view === 'list' ? 'on' : ''}
						onClick={() => setView('list')}
					>
						{t(locale, 'nav.opportunities')}
					</button>
					<button
						type="button"
						className={view === 'map' ? 'on' : ''}
						onClick={() => setView('map')}
					>
						{t(locale, 'nav.map')}
					</button>
					<button
						type="button"
						className="pill"
						style={{ borderColor: 'rgba(255,255,255,.5)' }}
						onClick={() => setLocale(locale === 'ar' ? 'en' : 'ar')}
					>
						{locale === 'ar' ? 'English' : 'العربية'}
					</button>
				</nav>
			</header>

			<main>
				<div className="banner" role="alert">
					{t(locale, 'synthetic')}
				</div>

				{view === 'list' && (
					<>
						<section className="panel">
							<h2>{t(locale, 'filters.heading')}</h2>
							<div className="filters">
								<div>
									<label htmlFor="district">{t(locale, 'filters.district')}</label>
									<select
										id="district"
										value={district}
										onChange={e => setDistrict(e.target.value)}
									>
										<option value="">—</option>
										{facets?.districts.map(d => (
											<option key={d.nameEn} value={d.nameEn}>
												{locale === 'ar' ? d.nameAr : d.nameEn}
											</option>
										))}
									</select>
								</div>
								<div>
									<label htmlFor="type">{t(locale, 'filters.type')}</label>
									<select id="type" value={type} onChange={e => setType(e.target.value)}>
										<option value="">—</option>
										{facets?.types.map(v => (
											<option key={v} value={v}>
												{v}
											</option>
										))}
									</select>
								</div>
								<div>
									<label htmlFor="minScore">{t(locale, 'filters.minScore')}</label>
									<input
										id="minScore"
										type="number"
										min={0}
										max={100}
										value={minScore}
										onChange={e => setMinScore(e.target.value)}
									/>
								</div>
								<div>
									<label htmlFor="sort">{t(locale, 'filters.sort')}</label>
									<select id="sort" value={sort} onChange={e => setSort(e.target.value)}>
										<option value="score">{t(locale, 'sort.score')}</option>
										<option value="discount">{t(locale, 'sort.discount')}</option>
										<option value="newest">{t(locale, 'sort.newest')}</option>
										<option value="confidence">{t(locale, 'sort.confidence')}</option>
									</select>
								</div>
								<div>
									<button
										type="button"
										className="ghost"
										onClick={() => {
											setDistrict('');
											setType('');
											setMinScore('');
											setSort('score');
										}}
									>
										{t(locale, 'filters.clear')}
									</button>
								</div>
							</div>
						</section>

						{status === 'loading' && <p className="muted">{t(locale, 'loading')}</p>}
						{status === 'error' && <div className="refused">{t(locale, 'error')}</div>}
						{status === 'unauthorised' && (
							<div className="refused">{t(locale, 'signin.required')}</div>
						)}

						{status === 'ready' && (
							<>
								<p className="muted">
									{formatNumber(count, locale)}
									{' '}
									{t(locale, 'list.results')}
								</p>
								{rows.length === 0
									? (
											<p className="muted">{t(locale, 'list.empty')}</p>
										)
									: (
											<div className="cards">
												{rows.map(row => (
													<OpportunityCard key={row.id} opportunity={row} locale={locale} />
												))}
											</div>
										)}
							</>
						)}
					</>
				)}

				{view === 'map' && (
					<section className="panel">
						<MapView locale={locale} />
						<div className="legend">
							<span>
								<i style={{ background: '#136f3f' }} />
								≥ 80
							</span>
							<span>
								<i style={{ background: '#b8860b' }} />
								60–79
							</span>
							<span>
								<i style={{ background: '#a02128' }} />
								&lt; 60 / insufficient
							</span>
						</div>
					</section>
				)}
			</main>
		</>
	);
}
