"""Arabic and English, with real RTL.

Arabic is the primary language of this market, so it is a first-class locale
rather than a translation layer bolted on later: the document direction, the
number formatting and the district names all change with it.
"""

from __future__ import annotations

from typing import Any

DEFAULT_LOCALE = "en"
LOCALES = ("en", "ar")

_ARABIC_INDIC = "٠١٢٣٤٥٦٧٨٩"

CATALOGUE: dict[str, dict[str, str]] = {
    "en": {
        "brand": "Saudi Real Estate Opportunity Intelligence",
        "tagline": "Discover mispriced real estate, not listings",
        "nav.opportunities": "Opportunities",
        "nav.map": "Map",
        "nav.admin": "Sources",
        "nav.api": "API docs",
        "list.title": "Opportunities",
        "list.subtitle": "Ranked by opportunity score. Below 60% data confidence no recommendation is shown at all.",
        "list.empty": "No opportunities match these filters.",
        "list.showing": "showing",
        "list.of": "of",
        "filters.title": "Filters",
        "filters.district": "District",
        "filters.type": "Opportunity type",
        "filters.max_cost": "Max acquisition cost (SAR)",
        "filters.min_discount": "Min discount %",
        "filters.min_score": "Min score",
        "filters.sort": "Sort by",
        "filters.hide_insufficient": "Hide insufficient-data opportunities",
        "filters.apply": "Apply",
        "filters.reset": "Reset",
        "filters.interpreted": "Filters applied",
        "field.cost": "True acquisition cost",
        "field.value": "Estimated market value",
        "field.discount": "Discount",
        "field.ppsqm": "Price / m²",
        "field.confidence": "confidence",
        "field.area": "Area",
        "field.score": "Opportunity score",
        "field.refused": "Refused",
        "detail.assessment": "Assessment",
        "detail.cost": "True acquisition cost",
        "detail.score": "Score derivation — reproducible",
        "detail.evidence": "Valuation evidence",
        "detail.timeline": "Timeline",
        "detail.provenance": "Provenance",
        "detail.no_recommendation": "No recommendation is shown.",
        "detail.comparables_used": "Comparables used",
        "detail.comparable_quality": "Comparable quality",
        "detail.valuation_confidence": "Valuation confidence",
        "detail.index_tier": "Index tier",
        "detail.line_item": "Line item",
        "detail.amount": "Amount",
        "detail.basis": "Basis",
        "detail.material": "Material",
        "detail.total": "Total",
        "detail.unknown": "UNKNOWN",
        "detail.confidence_gaps": "What is holding confidence down",
        "map.title": "Map",
        "map.subtitle": "Draw or pan to filter. District shading is median price per m² from registered transactions.",
        "map.districts": "District median SAR/m²",
        "admin.title": "Source health",
        "admin.subtitle": "A silently dead connector is the failure that matters most.",
        "admin.state": "State",
        "admin.source": "Source",
        "admin.legal": "Legal basis",
        "admin.licence": "Licence",
        "admin.records": "Records",
        "admin.latency": "Latency",
        "admin.last_record": "Last record",
        "admin.checked": "Checked",
        "banner.synthetic": "Demonstration data. Comparable transactions come from a synthetic fixture corpus, not real registered sales. The engine is real; the evidence is generated.",
        "sort.score": "Highest opportunity score",
        "sort.discount": "Largest discount",
        "sort.cost": "Lowest acquisition cost",
        "sort.newest": "Most recently added",
        "otype.AUCTION": "Auction",
        "otype.ASSIGNMENT": "Assignment",
        "otype.RESALE": "Resale",
        "otype.OFF_PLAN_RESALE": "Off-plan resale",
        "otype.DEVELOPER_INVENTORY": "Developer inventory",
        "pclass.APARTMENT": "Apartment",
        "pclass.VILLA": "Villa",
        "pclass.RESIDENTIAL_PLOT": "Residential plot",
        "detail.verification": "Verification",
        "verif.score": "Verification score",
        "verif.internal": "Internal coherence",
        "verif.official": "Official confirmation",
        "verif.check": "Check",
        "verif.status": "Status",
        "verif.class": "Class",
        "verif.finding": "Finding",
        "verif.capped": "Capped",
        "class.EXCEPTIONAL": "Exceptional",
        "class.STRONG": "Strong",
        "class.WORTH_REVIEWING": "Worth reviewing",
        "class.WATCHLIST": "Watchlist",
        "class.WEAK": "Weak opportunity",
        "class.INSUFFICIENT_DATA": "Insufficient data",
    },
    "ar": {
        "brand": "منصة ذكاء الفرص العقارية السعودية",
        "tagline": "اكتشف العقارات المُسعّرة دون قيمتها، لا مجرد الإعلانات",
        "nav.opportunities": "الفرص",
        "nav.map": "الخريطة",
        "nav.admin": "المصادر",
        "nav.api": "توثيق الواجهة",
        "list.title": "الفرص",
        "list.subtitle": "مرتبة حسب درجة الفرصة. عند انخفاض ثقة البيانات دون ٦٠٪ لا تُعرض أي توصية.",
        "list.empty": "لا توجد فرص مطابقة لهذه المرشحات.",
        "list.showing": "عرض",
        "list.of": "من",
        "filters.title": "المرشحات",
        "filters.district": "الحي",
        "filters.type": "نوع الفرصة",
        "filters.max_cost": "أقصى تكلفة استحواذ (ريال)",
        "filters.min_discount": "أدنى خصم ٪",
        "filters.min_score": "أدنى درجة",
        "filters.sort": "الترتيب",
        "filters.hide_insufficient": "إخفاء الفرص ذات البيانات غير الكافية",
        "filters.apply": "تطبيق",
        "filters.reset": "إعادة تعيين",
        "filters.interpreted": "المرشحات المطبقة",
        "field.cost": "التكلفة الفعلية للاستحواذ",
        "field.value": "القيمة السوقية التقديرية",
        "field.discount": "الخصم",
        "field.ppsqm": "السعر / م²",
        "field.confidence": "الثقة",
        "field.area": "المساحة",
        "field.score": "درجة الفرصة",
        "field.refused": "مرفوض",
        "detail.assessment": "التقييم",
        "detail.cost": "التكلفة الفعلية للاستحواذ",
        "detail.score": "اشتقاق الدرجة — قابل لإعادة الإنتاج",
        "detail.evidence": "أدلة التقييم",
        "detail.timeline": "الخط الزمني",
        "detail.provenance": "مصدر البيانات",
        "detail.no_recommendation": "لا تُعرض أي توصية.",
        "detail.comparables_used": "الصفقات المقارنة المستخدمة",
        "detail.comparable_quality": "جودة المقارنات",
        "detail.valuation_confidence": "ثقة التقييم",
        "detail.index_tier": "مستوى المؤشر",
        "detail.line_item": "البند",
        "detail.amount": "المبلغ",
        "detail.basis": "الأساس",
        "detail.material": "جوهري",
        "detail.total": "الإجمالي",
        "detail.unknown": "غير معروف",
        "detail.confidence_gaps": "ما الذي يخفض الثقة",
        "map.title": "الخريطة",
        "map.subtitle": "حرّك الخريطة للتصفية. تظليل الأحياء يمثل وسيط سعر المتر من الصفقات المسجلة.",
        "map.districts": "وسيط سعر المتر للحي",
        "admin.title": "صحة المصادر",
        "admin.subtitle": "المصدر المتوقف بصمت هو الخلل الأخطر.",
        "admin.state": "الحالة",
        "admin.source": "المصدر",
        "admin.legal": "الأساس النظامي",
        "admin.licence": "الترخيص",
        "admin.records": "السجلات",
        "admin.latency": "زمن الاستجابة",
        "admin.last_record": "آخر سجل",
        "admin.checked": "آخر فحص",
        "banner.synthetic": "بيانات توضيحية. الصفقات المقارنة مولّدة اصطناعياً وليست صفقات مسجلة حقيقية. المحرك حقيقي، والأدلة مولّدة.",
        "sort.score": "الأعلى درجة",
        "sort.discount": "الأكبر خصماً",
        "sort.cost": "الأقل تكلفة",
        "sort.newest": "الأحدث إضافة",
        "otype.AUCTION": "مزاد",
        "otype.ASSIGNMENT": "تنازل",
        "otype.RESALE": "إعادة بيع",
        "otype.OFF_PLAN_RESALE": "إعادة بيع على الخارطة",
        "otype.DEVELOPER_INVENTORY": "مخزون المطور",
        "pclass.APARTMENT": "شقة",
        "pclass.VILLA": "فيلا",
        "pclass.RESIDENTIAL_PLOT": "أرض سكنية",
        "detail.verification": "التحقق",
        "verif.score": "درجة التحقق",
        "verif.internal": "الاتساق الداخلي",
        "verif.official": "التأكيد الرسمي",
        "verif.check": "الفحص",
        "verif.status": "الحالة",
        "verif.class": "النوع",
        "verif.finding": "النتيجة",
        "verif.capped": "محدود",
        "class.EXCEPTIONAL": "استثنائية",
        "class.STRONG": "قوية",
        "class.WORTH_REVIEWING": "تستحق المراجعة",
        "class.WATCHLIST": "قائمة المتابعة",
        "class.WEAK": "فرصة ضعيفة",
        "class.INSUFFICIENT_DATA": "بيانات غير كافية",
    },
}


def register_strings(locale: str, mapping: dict[str, str]) -> None:
    """Add translations from a feature module.

    Keeps the catalogue extensible without every feature editing this file.
    Call it at import time from the feature's own module.
    """
    CATALOGUE.setdefault(locale, {}).update(mapping)


def normalise_locale(value: str | None) -> str:
    if value and value.lower()[:2] in LOCALES:
        return value.lower()[:2]
    return DEFAULT_LOCALE


def direction(locale: str) -> str:
    return "rtl" if locale == "ar" else "ltr"


def translator(locale: str) -> Any:
    """Returns t(key) with an English fallback so a missing string is visible, not blank."""
    catalogue = CATALOGUE.get(locale, CATALOGUE[DEFAULT_LOCALE])
    fallback = CATALOGUE[DEFAULT_LOCALE]

    def t(key: str) -> str:
        return catalogue.get(key) or fallback.get(key) or key

    return t


def localise_digits(text: str, locale: str) -> str:
    """Render numerals in Arabic-Indic form for the Arabic locale."""
    if locale != "ar":
        return text
    return text.translate(str.maketrans("0123456789", _ARABIC_INDIC))


def format_number(value: float | int | None, locale: str, decimals: int = 0) -> str:
    if value is None:
        return "—"
    formatted = f"{value:,.{decimals}f}"
    return localise_digits(formatted, locale)
