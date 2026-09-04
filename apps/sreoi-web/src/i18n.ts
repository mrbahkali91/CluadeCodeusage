/**
 * Arabic and English, with the parts that actually matter for an Arabic reader:
 * direction, Arabic-Indic digits, and real estate vocabulary rather than
 * transliteration.
 */

export type Locale = 'en' | 'ar';

const STRINGS: Record<Locale, Record<string, string>> = {
	en: {
		'brand': 'Saudi Real Estate Opportunity Intelligence',
		'tagline': 'Discover mispriced real estate, not listings',
		'nav.opportunities': 'Opportunities',
		'nav.map': 'Map',
		'filters.heading': 'Filters',
		'filters.district': 'District',
		'filters.type': 'Type',
		'filters.minScore': 'Minimum score',
		'filters.sort': 'Sort by',
		'filters.apply': 'Apply',
		'filters.clear': 'Clear',
		'sort.score': 'Score',
		'sort.discount': 'Discount',
		'sort.newest': 'Newest',
		'sort.confidence': 'Confidence',
		'list.results': 'opportunities',
		'list.empty': 'No opportunities match these filters.',
		'card.score': 'Score',
		'card.discount': 'Discount',
		'card.confidence': 'Confidence',
		'card.area': 'Area',
		'card.capped': 'capped by confidence gate',
		'card.refused': 'Discount refused',
		'synthetic':
			'Demonstration data. Comparable transactions come from a synthetic fixture corpus, not real registered sales. The engine is real; the evidence is generated.',
		'loading': 'Loading…',
		'error': 'Could not reach the API.',
		'signin.required': 'Sign in through the engine to view opportunities.',
	},
	ar: {
		'brand': 'منصة اكتشاف الفرص العقارية في السعودية',
		'tagline': 'اكتشف العقارات المُسعّرة بأقل من قيمتها، لا مجرد إعلانات',
		'nav.opportunities': 'الفرص',
		'nav.map': 'الخريطة',
		'filters.heading': 'المرشحات',
		'filters.district': 'الحي',
		'filters.type': 'النوع',
		'filters.minScore': 'أدنى درجة',
		'filters.sort': 'الترتيب حسب',
		'filters.apply': 'تطبيق',
		'filters.clear': 'مسح',
		'sort.score': 'الدرجة',
		'sort.discount': 'نسبة الخصم',
		'sort.newest': 'الأحدث',
		'sort.confidence': 'الثقة',
		'list.results': 'فرصة',
		'list.empty': 'لا توجد فرص مطابقة لهذه المرشحات.',
		'card.score': 'الدرجة',
		'card.discount': 'الخصم',
		'card.confidence': 'الثقة',
		'card.area': 'المساحة',
		'card.capped': 'مُقيّدة ببوابة الثقة',
		'card.refused': 'تم رفض احتساب الخصم',
		'synthetic':
			'بيانات تجريبية. الصفقات المقارنة مأخوذة من مجموعة بيانات اصطناعية، وليست صفقات مسجلة حقيقية. المحرك حقيقي، أما الأدلة فمولّدة.',
		'loading': 'جارٍ التحميل…',
		'error': 'تعذّر الوصول إلى الواجهة البرمجية.',
		'signin.required': 'سجّل الدخول عبر المحرك لعرض الفرص.',
	},
};

export function t(locale: Locale, key: string): string {
	return STRINGS[locale][key] ?? key;
}

export function direction(locale: Locale): 'rtl' | 'ltr' {
	return locale === 'ar' ? 'rtl' : 'ltr';
}

const ARABIC_INDIC = ['٠', '١', '٢', '٣', '٤', '٥', '٦', '٧', '٨', '٩'] as const;

/**
 * Arabic-Indic digits for Arabic. A number rendered in Western digits inside
 * otherwise-Arabic text reads as foreign, and this is a product for Saudi
 * users rather than a translated English one.
 */
export function digits(value: string, locale: Locale): string {
	if (locale !== 'ar') {
		return value;
	}
	return value.replace(/\d/g, d => ARABIC_INDIC[Number(d)] ?? d);
}

export function formatNumber(value: number | null, locale: Locale, fractionDigits = 0): string {
	if (value === null || !Number.isFinite(value)) {
		return '—';
	}
	const formatted = new Intl.NumberFormat(locale === 'ar' ? 'ar-SA' : 'en-US', {
		minimumFractionDigits: fractionDigits,
		maximumFractionDigits: fractionDigits,
	}).format(value);
	return digits(formatted, locale);
}

export function formatPercent(value: number | null, locale: Locale): string {
	if (value === null || !Number.isFinite(value)) {
		return '—';
	}
	return digits(`${(value * 100).toFixed(1)}%`, locale);
}

if (import.meta.vitest != null) {
	describe('i18n', () => {
		it('renders Arabic-Indic digits in Arabic and Western digits in English', () => {
			expect(digits('2026', 'ar')).toBe('٢٠٢٦');
			expect(digits('2026', 'en')).toBe('2026');
		});

		it('sets direction from the locale', () => {
			expect(direction('ar')).toBe('rtl');
			expect(direction('en')).toBe('ltr');
		});

		it('shows an em dash for an absent number rather than 0', () => {
			// An unknown score and a score of zero mean very different things.
			expect(formatNumber(null, 'en')).toBe('—');
			expect(formatPercent(null, 'en')).toBe('—');
		});

		it('formats a percentage from a fraction', () => {
			expect(formatPercent(0.357, 'en')).toBe('35.7%');
		});

		it('falls back to the key rather than rendering undefined', () => {
			expect(t('en', 'no.such.key')).toBe('no.such.key');
		});

		it('has the same keys in both locales, so nothing falls back silently', () => {
			expect(Object.keys(STRINGS.ar).sort()).toEqual(Object.keys(STRINGS.en).sort());
		});
	});
}
