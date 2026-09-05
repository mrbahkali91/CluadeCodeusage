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
		'signin.heading': 'Sign in',
		'signin.subtitle': 'Every view requires a credential. The organisation you belong to decides which opportunities you can see.',
		'signin.email': 'Email',
		'signin.password': 'Password',
		'signin.submit': 'Sign in',
		'signin.working': 'Signing in…',
		'signin.invalid': 'That email and password did not match. The same answer is given for an unknown address, so this does not confirm whether the account exists.',
		'signin.throttled': 'Too many sign-in attempts from this address. Wait a few minutes and try again.',
		'signin.disabled': 'This deployment authenticates through its identity provider, not a password.',
		'signin.unavailable': 'Could not reach the sign-in service.',
		'nav.signout': 'Sign out',
		'nav.admin': 'Admin',
		'admin.quality': 'Data quality',
		'admin.sources': 'Source health',
		'admin.overall': 'Overall status',
		'admin.opportunities': 'Opportunities',
		'admin.properties': 'Properties',
		'admin.meanConfidence': 'Mean data confidence',
		'admin.completeness': 'Field completeness',
		'admin.flags': 'Quality flags',
		'admin.metric': 'Metric',
		'admin.value': 'Value',
		'admin.thresholds': 'Warn / fail',
		'admin.confidenceDist': 'Data confidence distribution',
		'admin.confidenceNote': 'The gate holds a score below 0.60 at INSUFFICIENT_DATA and caps it below 0.75. A distribution weighted low means thin evidence, not a broken scorer.',
		'admin.verification': 'Verification pass rate by check',
		'admin.provenance': 'Provenance by basis',
		'admin.provenanceNote': 'ACTUAL is observed, RULE is derived from a published rate, ESTIMATE and UNKNOWN are neither. An UNKNOWN material cost line refuses the discount outright.',
		'admin.notApplicable': 'not applicable',
		'admin.source': 'Source',
		'admin.availability': 'Availability',
		'admin.access': 'Legal access',
		'admin.licence': 'Licence',
		'admin.records': 'Records',
		'admin.synthetic': 'synthetic',
		'admin.forbidden': 'These dashboards require the ADMIN role.',
		'detail.back': '← Back to opportunities',
		'detail.assessment': 'Assessment',
		'detail.marketValue': 'Estimated market value',
		'detail.trueCost': 'True acquisition cost',
		'detail.costIncomplete': 'material items unknown',
		'detail.derivation': 'Score derivation — reproducible',
		'detail.derivationNote': 'Same inputs and same versions always produce the identical score. Weights',
		'detail.dimension': 'Dimension',
		'detail.raw': 'Raw',
		'detail.normalised': 'Normalised',
		'detail.weight': 'Weight',
		'detail.contribution': 'Contribution',
		'detail.lineItem': 'Line item',
		'detail.amount': 'Amount',
		'detail.basis': 'Basis',
		'detail.material': 'Material',
		'detail.yes': 'yes',
		'detail.no': 'no',
		'detail.evidence': 'Valuation evidence',
		'detail.comparablesUsed': 'Comparables used',
		'detail.effectiveN': 'effective n',
		'detail.quality': 'Comparable quality',
		'detail.indexTier': 'Index tier',
		'detail.transaction': 'Transaction',
		'detail.adjusted': 'Adjusted SAR/m²',
		'detail.distance': 'Distance (m)',
		'detail.status': 'Status',
		'detail.used': 'used',
		'detail.verification': 'Verification',
		'detail.check': 'Check',
		'detail.finding': 'Finding',
		'detail.timeline': 'Timeline',
		'detail.timelineNote': 'Snapshots are append-only — a price change is a new row, never an overwrite. The sequence is itself the opportunity signal.',
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
		'signin.heading': 'تسجيل الدخول',
		'signin.subtitle': 'كل صفحة تتطلب بيانات اعتماد. والمؤسسة التي تنتمي إليها تحدد الفرص التي يمكنك رؤيتها.',
		'signin.email': 'البريد الإلكتروني',
		'signin.password': 'كلمة المرور',
		'signin.submit': 'تسجيل الدخول',
		'signin.working': 'جارٍ تسجيل الدخول…',
		'signin.invalid': 'البريد الإلكتروني أو كلمة المرور غير صحيحة. ويُعطى الجواب نفسه لعنوان غير مسجَّل، فهذا لا يؤكد وجود الحساب.',
		'signin.throttled': 'محاولات تسجيل دخول كثيرة من هذا العنوان. انتظر بضع دقائق ثم أعد المحاولة.',
		'signin.disabled': 'يستخدم هذا النشر مزود الهوية للمصادقة، لا كلمة مرور.',
		'signin.unavailable': 'تعذّر الوصول إلى خدمة تسجيل الدخول.',
		'nav.signout': 'تسجيل الخروج',
		'nav.admin': 'الإدارة',
		'admin.quality': 'جودة البيانات',
		'admin.sources': 'صحة المصادر',
		'admin.overall': 'الحالة العامة',
		'admin.opportunities': 'الفرص',
		'admin.properties': 'العقارات',
		'admin.meanConfidence': 'متوسط ثقة البيانات',
		'admin.completeness': 'اكتمال الحقول',
		'admin.flags': 'مؤشرات الجودة',
		'admin.metric': 'المقياس',
		'admin.value': 'القيمة',
		'admin.thresholds': 'تحذير / فشل',
		'admin.confidenceDist': 'توزيع ثقة البيانات',
		'admin.confidenceNote': 'تُبقي البوابة أي درجة دون ٠٫٦٠ عند «بيانات غير كافية»، وتُقيّدها دون ٠٫٧٥. التوزيع المنخفض يعني أدلة ضعيفة، لا خللًا في المُقيّم.',
		'admin.verification': 'نسبة نجاح التحقق حسب الفحص',
		'admin.provenance': 'مصدر البيانات حسب الأساس',
		'admin.provenanceNote': 'ACTUAL مرصود، وRULE مشتق من نسبة منشورة، أما ESTIMATE وUNKNOWN فليسا كذلك. أي بند تكلفة جوهري بأساس UNKNOWN يرفض احتساب الخصم.',
		'admin.notApplicable': 'غير منطبق',
		'admin.source': 'المصدر',
		'admin.availability': 'التوفر',
		'admin.access': 'الوصول النظامي',
		'admin.licence': 'الترخيص',
		'admin.records': 'السجلات',
		'admin.synthetic': 'اصطناعي',
		'admin.forbidden': 'تتطلب هذه اللوحات دور ADMIN.',
		'detail.back': '→ العودة إلى الفرص',
		'detail.assessment': 'التقييم',
		'detail.marketValue': 'القيمة السوقية المقدرة',
		'detail.trueCost': 'التكلفة الفعلية للاقتناء',
		'detail.costIncomplete': 'بنود جوهرية غير معروفة',
		'detail.derivation': 'اشتقاق الدرجة — قابل لإعادة الإنتاج',
		'detail.derivationNote': 'المدخلات نفسها والإصدارات نفسها تنتج دائمًا الدرجة ذاتها. الأوزان',
		'detail.dimension': 'البعد',
		'detail.raw': 'القيمة الخام',
		'detail.normalised': 'المعيارية',
		'detail.weight': 'الوزن',
		'detail.contribution': 'المساهمة',
		'detail.lineItem': 'البند',
		'detail.amount': 'المبلغ',
		'detail.basis': 'الأساس',
		'detail.material': 'جوهري',
		'detail.yes': 'نعم',
		'detail.no': 'لا',
		'detail.evidence': 'أدلة التقييم',
		'detail.comparablesUsed': 'المقارنات المستخدمة',
		'detail.effectiveN': 'العدد الفعلي',
		'detail.quality': 'جودة المقارنات',
		'detail.indexTier': 'مستوى المؤشر',
		'detail.transaction': 'الصفقة',
		'detail.adjusted': 'ريال/م² معدّل',
		'detail.distance': 'المسافة (م)',
		'detail.status': 'الحالة',
		'detail.used': 'مستخدمة',
		'detail.verification': 'التحقق',
		'detail.check': 'الفحص',
		'detail.finding': 'النتيجة',
		'detail.timeline': 'الخط الزمني',
		'detail.timelineNote': 'اللقطات تُضاف ولا تُستبدل — تغيير السعر صف جديد. التسلسل نفسه هو إشارة الفرصة.',
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
