"""The listing-extraction agent (agent-architecture §3.1).

Listing text is the rawest, most hostile input the platform takes: it is written
by whoever is selling, in Arabic, English or both, with Eastern Arabic numerals,
Hijri dates, amounts written in words, and a vocabulary in which one word decides
whether a number means "the price of the property" or "the part of the price the
seller has paid so far".

That word is **تنازل** (assignment). An assignment sells a *position* in an
off-plan purchase: the buyer pays the seller's premium and then inherits the
remaining installments owed to the developer. Read as a plain sale, a 120,000 SAR
تنازل on a 720,000 SAR unit looks like an 87% discount. It is not a discount at
all. So `remaining_installments` is extracted whenever the text supports it, and
the opportunity type is set to ASSIGNMENT, not RESALE.

Three rules are structural here rather than advisory:

  * **no evidence, no value.** Every field carries `evidence_span` -- character
    offsets into the canonical text -- and a field whose span does not actually
    contain its excerpt is discarded, not trusted. Bedrooms are never guessed
    from area.
  * **range validation after the model, never clamping.** An out-of-range value
    becomes `null` plus a quality flag that records what was rejected and why.
    Clamping would turn a bad input into a plausible-looking output.
  * **the responder is not a model and is not described as one.** There are no
    LLM credentials in this environment. Extraction runs on a rule-based
    responder behind `DeterministicProvider`, recorded as
    `provider="deterministic-offline"` on every run.

Offsets are defined against the *canonical* text produced by
`canonicalise()` -- PII-redacted and digit-normalised. `normalize_digits` is a
1:1 character translation so Eastern Arabic numerals do not move any offset;
redaction does change lengths, which is exactly why the canonical text is what
gets stored, cited and returned to callers.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from sreoi_agents.provider import LLMRequest, ModelTier
from sreoi_agents.runtime import Agent, AgentContext, AgentResult, AgentRuntime
from sreoi_sources.redaction import redact

PROMPT_VERSION = "extraction-prompt-v1"
METHOD_VERSION = "extraction-rules-v1"

# ---------------------------------------------------------------------------
# Ranges. Out of range means null plus a flag -- never a clamped value.

AREA_MIN, AREA_MAX = 20.0, 10_000.0
PRICE_MIN, PRICE_MAX = Decimal("10000"), Decimal("500000000")
FLOOR_MIN, FLOOR_MAX = -3, 80
ROOM_MIN, ROOM_MAX = 0, 30
YEAR_MIN = 1900


def _year_max() -> int:
    return date.today().year + 3


# Confidence is a property of how the evidence was found, not a guess.
CONF_LABELLED = 0.95  # explicit label immediately before the value
CONF_UNIT = 0.88  # value carrying its unit ("150 م²")
CONF_KEYWORD = 0.90  # a term that is itself the value ("شقة" -> APARTMENT)
CONF_WEAK = 0.70  # plausible but unlabelled
AMBIGUITY_PENALTY = 0.20  # applied when candidates disagree


class QualityCode(StrEnum):
    OUT_OF_RANGE = "OUT_OF_RANGE"
    AMBIGUOUS = "AMBIGUOUS"
    EVIDENCE_MISMATCH = "EVIDENCE_MISMATCH"
    UNPARSEABLE = "UNPARSEABLE"
    HIJRI_APPROXIMATE = "HIJRI_APPROXIMATE"


# ---------------------------------------------------------------------------
# Output schema. Every field carries value + confidence + evidence_span.


class EvidencedField(BaseModel):
    """Common metadata: every extracted value carries these three things.

    `value` is declared here so the metadata and the value are never separable;
    subclasses narrow it to the type their field actually holds.
    """

    value: Any = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence_span: tuple[int, int] | None = None
    excerpt: str | None = None


class TextField(EvidencedField):
    value: str | None = None


class IntField(EvidencedField):
    value: int | None = None


class NumberField(EvidencedField):
    value: float | None = None


class MoneyField(EvidencedField):
    value: Decimal | None = None
    currency: str = "SAR"


class SignalTag(BaseModel):
    tag: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_span: tuple[int, int]
    excerpt: str


class QualityFlag(BaseModel):
    field_name: str
    code: QualityCode
    detail: str


class ListingExtraction(BaseModel):
    """One listing, field by field, each with the span that justifies it."""

    property_class: TextField = Field(default_factory=TextField)
    city: TextField = Field(default_factory=TextField)
    district: TextField = Field(default_factory=TextField)
    area_sqm: NumberField = Field(default_factory=NumberField)
    land_area_sqm: NumberField = Field(default_factory=NumberField)
    bedrooms: IntField = Field(default_factory=IntField)
    bathrooms: IntField = Field(default_factory=IntField)
    floor: IntField = Field(default_factory=IntField)
    build_year: IntField = Field(default_factory=IntField)
    asking_price: MoneyField = Field(default_factory=MoneyField)
    seller_payment: MoneyField = Field(default_factory=MoneyField)
    remaining_installments: MoneyField = Field(default_factory=MoneyField)
    opportunity_type: TextField = Field(default_factory=TextField)
    advertisement_licence: TextField = Field(default_factory=TextField)
    hijri_date: TextField = Field(default_factory=TextField)
    gregorian_date: TextField = Field(default_factory=TextField)
    signals: list[SignalTag] = Field(default_factory=list)
    quality_flags: list[QualityFlag] = Field(default_factory=list)
    language: str = "unknown"
    method_version: str = METHOD_VERSION

    def scalar_fields(self) -> dict[str, EvidencedField]:
        return {
            name: value
            for name, value in ((n, getattr(self, n)) for n in type(self).model_fields)
            if isinstance(value, EvidencedField)
        }

    @property
    def signal_tags(self) -> tuple[str, ...]:
        return tuple(s.tag for s in self.signals)


# ---------------------------------------------------------------------------
# Canonical text


@dataclass(frozen=True, slots=True)
class ListingText:
    """The exact string all evidence spans refer to."""

    text: str
    raw_sha256: str
    pii_removed: tuple[tuple[str, int], ...] = ()
    language_hint: str | None = None
    subject_id: UUID | None = None


def canonicalise(raw: str, *, subject_id: UUID | None = None) -> ListingText:
    """Redact PII and normalise numerals before anything reads the text."""
    cleaned, counts = redact(raw or "")
    return ListingText(
        text=cleaned,
        raw_sha256=hashlib.sha256((raw or "").encode()).hexdigest(),
        pii_removed=tuple(sorted((k, v) for k, v in counts.items() if v)),
        subject_id=subject_id,
    )


# ---------------------------------------------------------------------------
# Arabic vocabulary. Letter variants are matched with character classes rather
# than normalised away, because normalising would move the evidence offsets.

_A = "[اأإآ]"  # alef with and without hamza
_H = "[ةه]"  # ta marbuta / ha
_Y = "[يىی]"  # ya / alef maqsura

_UNIT = r"(?:م2|م²|متر\s*مربع|متر|sq\.?\s?m|sqm|m2|m²)"
_NUM = r"(\d[\d.,\u066b\u066c]*)"
_MULT = r"(?:\s*(ألف|الف|آلاف|مليون|ملايين|thousand|million|k|K|m|M))?"
_CURRENCY = r"(?:\s*(?:ريال|ر\.?\s?س|SAR|sar|SR))"

_MULTIPLIERS: dict[str, Decimal] = {
    "ألف": Decimal(1000),  # ألف
    "الف": Decimal(1000),  # الف
    "آلاف": Decimal(1000),  # آلاف
    "thousand": Decimal(1000),
    "k": Decimal(1000),
    "K": Decimal(1000),
    "مليون": Decimal(1_000_000),  # مليون
    "ملايين": Decimal(1_000_000),  # ملايين
    "million": Decimal(1_000_000),
    "m": Decimal(1_000_000),
    "M": Decimal(1_000_000),
}

_PROPERTY_CLASS_PATTERNS: tuple[tuple[str, str], ...] = (
    # Order matters: a villa standing on a plot is a villa, not land, and an
    # apartment "in the third floor" is an apartment, not a floor.
    ("VILLA", rf"(?:ف{_Y}?ل{_A}|ف{_Y}ل{_H}|فل{_H}|villa)"),
    ("DUPLEX", r"(?:دوبلكس|دوبليكس|duplex)"),
    (
        "APARTMENT",
        rf"(?:شق{_H}|شقق|استود{_Y}و|apartment|flat|studio)",
    ),
    (
        "FLOOR",
        r"(?:دور\s*(?:كامل|مستقل|علوي)"
        r"|دور\s+للبيع|للبيع\s+دور)",
    ),
    (
        "BUILDING",
        rf"(?:عمار{_H}|بناي{_H}|building)",
    ),
    (
        "RESIDENTIAL_PLOT",
        rf"(?:قطع{_H}\s+{_A}رض|{_A}رض\s*سكن{_Y}{_H}"
        rf"|{_A}رض|land|plot)",
    ),
)

_OPPORTUNITY_TYPE_PATTERNS: tuple[tuple[str, str], ...] = (
    # تنازل first and unconditionally: mis-typing an assignment as a resale is
    # the single most expensive extraction error this module can make.
    ("ASSIGNMENT", r"(?:تنازل|assignment)"),
    ("AUCTION", rf"(?:مزاد|مزايد{_H}|auction)"),
    (
        "OFF_PLAN_RESALE",
        r"(?:على\s+الخارطة"
        r"|على\s+الخريطة|off[\s-]?plan)",
    ),
    (
        "DEVELOPER_INVENTORY",
        r"(?:من\s+المطور"
        r"|المطور\s+مباشر"
        r"|from\s+the\s+developer|developer\s+inventory)",
    ),
    (
        "RESALE",
        r"(?:للبيع|للبيعة|for\s+sale|resale)",
    ),
)

_SIGNAL_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        "URGENT",
        r"(?:عاجل|بسرعة"
        r"|بيع\s+سريع|urgent|quick\s+sale)",
    ),
    ("AUCTION", rf"(?:مزاد|مزايد{_H}|auction)"),
    ("ASSIGNMENT", r"(?:تنازل|assignment)"),
    (
        "IMMEDIATE_TRANSFER",
        rf"(?:{_A}فراغ\s+فور{_Y}"
        rf"|ال{_A}فراغ\s+فور{_Y}"
        r"|immediate\s+transfer)",
    ),
    (
        "STREET_FACING",
        r"(?:على\s+(?:ال)?شارع"
        r"|واجهة\s+على|street[\s-]facing)",
    ),
    (
        "STAIRCASE_DUPLEX",
        r"(?:درج|دوبلكس|duplex)",
    ),
    (
        "PRICE_REDUCED",
        r"(?:تخفيض|خصم"
        r"|انخفض\s+السعر"
        r"|price\s+(?:drop|reduc)|reduced)",
    ),
    (
        "NEGOTIABLE",
        r"(?:قابل\s+للتفاوض"
        r"|للتفاوض|negotiable)",
    ),
    (
        "OFF_PLAN",
        r"(?:على\s+الخارطة"
        r"|على\s+الخريطة|off[\s-]?plan)",
    ),
    (
        "FINANCING",
        r"(?:تمويل|financing|mortgage)",
    ),
    (
        "LIVING_ROOM",
        rf"(?:صال{_H}|مجلس|living\s+room)",
    ),
    (
        "CORNER",
        rf"(?:زاو{_Y}{_H}|ناص{_Y}{_H}|corner)",
    ),
)

# Ordinal floors, Arabic and English.
_ARABIC_ORDINALS: dict[str, int] = {
    "الأرضي": 0,
    "الارضي": 0,
    "أرضي": 0,
    "ارضي": 0,
    "الأول": 1,
    "الاول": 1,
    "أول": 1,
    "اول": 1,
    "الثاني": 2,
    "ثاني": 2,
    "الثالث": 3,
    "ثالث": 3,
    "الرابع": 4,
    "رابع": 4,
    "الخامس": 5,
    "خامس": 5,
    "السادس": 6,
    "سادس": 6,
    "السابع": 7,
    "سابع": 7,
    "الثامن": 8,
    "ثامن": 8,
    "التاسع": 9,
    "تاسع": 9,
    "العاشر": 10,
    "عاشر": 10,
}

# Counts written as words: "ثلاث غرف" is three rooms.
_ARABIC_COUNT_WORDS: dict[str, int] = {
    "ثلاث": 3,
    "أربع": 4,
    "اربع": 4,
    "خمس": 5,
    "ست": 6,
    "سبع": 7,
    "ثمان": 8,
    "تسع": 9,
    "عشر": 10,
}

_HIJRI_MONTHS: dict[str, int] = {
    "محرم": 1,
    "صفر": 2,
    "ربيع الأول": 3,
    "ربيع الاول": 3,
    "ربيع الثاني": 4,
    "جمادى الأولى": 5,
    "جمادى الآخرة": 6,
    "رجب": 7,
    "شعبان": 8,
    "رمضان": 9,
    "شوال": 10,
    "ذو القعدة": 11,
    "ذو الحجة": 12,
}

_CITIES: tuple[tuple[str, str], ...] = (
    ("Riyadh", r"(?:الرياض|riyadh)"),
    ("Jeddah", r"(?:جدة|jeddah)"),
    ("Dammam", r"(?:الدمام|dammam)"),
    ("Khobar", r"(?:الخبر|khobar)"),
    ("Makkah", r"(?:مكة|makkah|mecca)"),
    ("Madinah", r"(?:المدينة|madinah|medina)"),
)


# ---------------------------------------------------------------------------
# Hijri conversion


def hijri_to_gregorian(year: int, month: int, day: int) -> date:
    """Arithmetic (tabular) Hijri conversion.

    Accurate to about a day against the Umm al-Qura calendar, which is
    observation-based. The result therefore carries a
    `HIJRI_APPROXIMATE` quality flag rather than being presented as exact --
    an auction close date that is silently one day out is worse than one
    labelled approximate.
    """
    jdn = (11 * year + 3) // 30 + 354 * year + 30 * month - (month - 1) // 2 + day + 1_948_440 - 386
    length = jdn + 68_569
    century = (4 * length) // 146_097
    length -= (146_097 * century + 3) // 4
    approx_year = (4000 * (length + 1)) // 1_461_001
    length = length - (1461 * approx_year) // 4 + 31
    approx_month = (80 * length) // 2447
    g_day = length - (2447 * approx_month) // 80
    length = approx_month // 11
    g_month = approx_month + 2 - 12 * length
    g_year = 100 * (century - 49) + approx_year + length
    return date(g_year, g_month, g_day)


# ---------------------------------------------------------------------------
# Matching primitives


@dataclass(frozen=True, slots=True)
class _Hit:
    raw: str
    span: tuple[int, int]
    confidence: float


def _hits(pattern: str, text: str, confidence: float, group: int = 0) -> list[_Hit]:
    out: list[_Hit] = []
    for match in re.finditer(pattern, text, re.IGNORECASE):
        if group and match.group(group) is None:
            continue
        out.append(
            _Hit(
                raw=match.group(group),
                span=(match.start(group), match.end(group)),
                confidence=confidence,
            )
        )
    return out


def _excerpt(text: str, span: tuple[int, int], pad: int = 24) -> str:
    start = max(0, span[0] - pad)
    end = min(len(text), span[1] + pad)
    return text[start:end].strip()


def _clean_numeral(raw: str) -> str:
    """Arabic thousands (\u066c) and decimal (\u066b) separators are punctuation, not digits."""
    return raw.replace(",", "").replace("\u066c", "").replace("\u066b", ".").strip(".")


def _parse_number(raw: str) -> float | None:
    cleaned = _clean_numeral(raw)
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_amount(raw: str, multiplier: str | None) -> Decimal | None:
    cleaned = _clean_numeral(raw)
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    if multiplier:
        value *= _MULTIPLIERS.get(multiplier, _MULTIPLIERS.get(multiplier.lower(), Decimal(1)))
    return value


# ---------------------------------------------------------------------------
# The rule-based extractor


class _Builder:
    """Accumulates fields, ambiguity flags and range rejections for one text."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.flags: list[QualityFlag] = []

    def flag(self, field_name: str, code: QualityCode, detail: str) -> None:
        self.flags.append(QualityFlag(field_name=field_name, code=code, detail=detail))

    def _pick(self, field_name: str, candidates: list[tuple[Any, _Hit]]) -> tuple[Any, _Hit] | None:
        """First candidate wins; disagreement lowers confidence and is flagged.

        Ambiguity matters for injection resistance: an attacker who appends
        "المساحة 9000 م²" to a genuine listing does not silently replace the
        real area, they produce a recorded AMBIGUOUS flag.
        """
        if not candidates:
            return None
        distinct = {c[0] for c in candidates}
        value, hit = candidates[0]
        if len(distinct) > 1:
            self.flag(
                field_name,
                QualityCode.AMBIGUOUS,
                f"{len(distinct)} conflicting candidates: {sorted(map(str, distinct))[:5]}",
            )
            hit = _Hit(hit.raw, hit.span, max(0.0, hit.confidence - AMBIGUITY_PENALTY))
        return value, hit

    def text_field(self, field_name: str, candidates: list[tuple[str, _Hit]]) -> TextField:
        chosen = self._pick(field_name, list(candidates))
        if chosen is None:
            return TextField()
        value, hit = chosen
        return TextField(
            value=value,
            confidence=hit.confidence,
            evidence_span=hit.span,
            excerpt=_excerpt(self.text, hit.span),
        )

    def int_field(
        self, field_name: str, candidates: list[tuple[int, _Hit]], low: int, high: int
    ) -> IntField:
        chosen = self._pick(field_name, list(candidates))
        if chosen is None:
            return IntField()
        value, hit = chosen
        excerpt = _excerpt(self.text, hit.span)
        if not low <= value <= high:
            self.flag(
                field_name,
                QualityCode.OUT_OF_RANGE,
                f"{value} is outside [{low}, {high}]; dropped rather than clamped",
            )
            return IntField(confidence=0.0, evidence_span=hit.span, excerpt=excerpt)
        return IntField(
            value=value, confidence=hit.confidence, evidence_span=hit.span, excerpt=excerpt
        )

    def number_field(
        self, field_name: str, candidates: list[tuple[float, _Hit]], low: float, high: float
    ) -> NumberField:
        chosen = self._pick(field_name, list(candidates))
        if chosen is None:
            return NumberField()
        value, hit = chosen
        excerpt = _excerpt(self.text, hit.span)
        if not low <= value <= high:
            self.flag(
                field_name,
                QualityCode.OUT_OF_RANGE,
                f"{value} is outside [{low}, {high}]; dropped rather than clamped",
            )
            return NumberField(confidence=0.0, evidence_span=hit.span, excerpt=excerpt)
        return NumberField(
            value=value, confidence=hit.confidence, evidence_span=hit.span, excerpt=excerpt
        )

    def money_field(self, field_name: str, candidates: list[tuple[Decimal, _Hit]]) -> MoneyField:
        chosen = self._pick(field_name, list(candidates))
        if chosen is None:
            return MoneyField()
        value, hit = chosen
        excerpt = _excerpt(self.text, hit.span)
        if not PRICE_MIN <= value <= PRICE_MAX:
            self.flag(
                field_name,
                QualityCode.OUT_OF_RANGE,
                f"{value} SAR is outside [{PRICE_MIN}, {PRICE_MAX}]; dropped rather than clamped",
            )
            return MoneyField(confidence=0.0, evidence_span=hit.span, excerpt=excerpt)
        return MoneyField(
            value=value, confidence=hit.confidence, evidence_span=hit.span, excerpt=excerpt
        )


def _detect_language(text: str) -> str:
    arabic = sum(1 for ch in text if "؀" <= ch <= "ۿ")
    latin = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    if arabic and latin and min(arabic, latin) / max(arabic, latin) > 0.25:
        return "mixed"
    if arabic > latin:
        return "ar"
    if latin:
        return "en"
    return "unknown"


def _amount_candidates(text: str, labels: str, confidence: float) -> list[tuple[Decimal, _Hit]]:
    """Amounts introduced by one of `labels`, e.g. السعر / المتبقي للمطور."""
    pattern = rf"(?:{labels})[^\d\n]{{0,24}}{_NUM}{_MULT}{_CURRENCY}?"
    out: list[tuple[Decimal, _Hit]] = []
    for match in re.finditer(pattern, text, re.IGNORECASE):
        value = _parse_amount(match.group(1), match.group(2))
        if value is None:
            continue
        out.append((value, _Hit(match.group(0), (match.start(), match.end()), confidence)))
    return out


def _bare_amount_candidates(text: str) -> list[tuple[Decimal, _Hit]]:
    """A number carrying a currency: "٩٠٠ ألف ريال"."""
    pattern = rf"{_NUM}{_MULT}{_CURRENCY}"
    out: list[tuple[Decimal, _Hit]] = []
    for match in re.finditer(pattern, text, re.IGNORECASE):
        value = _parse_amount(match.group(1), match.group(2))
        if value is None:
            continue
        out.append((value, _Hit(match.group(0), (match.start(), match.end()), CONF_WEAK)))
    return out


_PRICE_LABELS = (
    r"السعر\s+الافتتاحي"
    r"|السعر\s+الأفتتاحي"
    r"|سعر\s+البيع|السعر"
    r"|بسعر|مطلوب|المطلوب"
    r"|السوم|opening\s+price|asking\s+price|asking|price"
)
_SELLER_PAYMENT_LABELS = (
    r"مبلغ\s+التنازل"
    r"|قيمة\s+التنازل"
    r"|مقابل\s+التنازل"
    r"|المبلغ\s+المدفوع"
    r"|المدفوع|المسدد"
    r"|دفعات\s+مسددة"
    r"|تنازل\s+بمبلغ"
    r"|assignment\s+(?:fee|premium)|seller\s+payment|amount\s+paid|paid\s+to\s+date"
)
_REMAINING_LABELS = (
    r"المتبقي\s+للمطور"
    r"|متبقي\s+للمطور"
    r"|المبلغ\s+المتبقي"
    r"|باقي\s+الأقساط"
    r"|الأقساط\s+المتبقية"
    r"|الدفعات\s+المتبقية"
    r"|الدفعة\s+المتبقية"
    r"|المتبقي"
    r"|remaining\s+(?:installments?|to\s+developer|balance)"
    r"|balance\s+to\s+(?:the\s+)?developer|outstanding\s+installments?"
)
# "حي" also occurs inside unrelated words (الافتتاحي), so the anchor needs a
# token boundary; Arabic has no \b that regex can use here.
_DISTRICT_PATTERN = (
    r"(?:^|[\s،,.\n])(?:ب?حي|مخطط|district|neighbou?rhood)\s+"
    r"([^\s،,.\n]{2,}(?:\s+(?:ال|عبد|بن)[^\s،,.\n]+|\s+[A-Z][A-Za-z]+)?)"
)

_LICENCE_LABELS = (
    r"رقم\s+الإعلان"
    r"|رقم\s+الاعلان"
    r"|رخصة\s+الإعلان"
    r"|ترخيص\s+الإعلان"
    r"|رقم\s+الترخيص"
    r"|رقم\s+الرخصة"
    r"|advertis\w*\s+licen[cs]e|ad\s+licen[cs]e|licen[cs]e\s+(?:no\.?|number)"
)


def _area_candidates(text: str) -> list[tuple[float, _Hit]]:
    labels = (
        r"مساحة\s+البناء"
        r"|المساحة|مساحة"
        r"|built[\s-]?up\s+area|area"
    )
    out: list[tuple[float, _Hit]] = []
    land = _land_spans(text)
    for match in re.finditer(rf"(?:{labels})[^\d\n]{{0,16}}{_NUM}\s*{_UNIT}?", text, re.I):
        if any(s <= match.start() < e for s, e in land):
            continue
        value = _parse_number(match.group(1))
        if value is not None:
            out.append((value, _Hit(match.group(0), (match.start(), match.end()), CONF_LABELLED)))
    for match in re.finditer(rf"{_NUM}\s*{_UNIT}", text, re.I):
        if any(s <= match.start() < e for s, e in land):
            continue
        if any(h.span[0] <= match.start() < h.span[1] for _, h in out):
            continue
        value = _parse_number(match.group(1))
        if value is not None:
            out.append((value, _Hit(match.group(0), (match.start(), match.end()), CONF_UNIT)))
    return out


def _land_spans(text: str) -> list[tuple[int, int]]:
    """Where the text is talking about land area rather than built area."""
    labels = (
        rf"مساحة\s+ال{_A}?رض"
        rf"|على\s+{_A}رض|{_A}رض\s+مساحة"
        rf"|قطعة\s+{_A}رض"
        r"|land\s+area|plot\s+area"
    )
    return [
        (m.start(), m.end())
        for m in re.finditer(rf"(?:{labels})[^\d\n]{{0,16}}{_NUM}\s*{_UNIT}?", text, re.I)
    ]


def _land_area_candidates(text: str) -> list[tuple[float, _Hit]]:
    out: list[tuple[float, _Hit]] = []
    for start, end in _land_spans(text):
        match = re.search(rf"{_NUM}", text[start:end])
        if match is None:
            continue
        value = _parse_number(match.group(1))
        if value is not None:
            out.append((value, _Hit(text[start:end], (start, end), CONF_LABELLED)))
    return out


def _room_candidates(
    text: str, arabic: str, english: str, duals: tuple[str, ...] = ()
) -> list[tuple[int, _Hit]]:
    out: list[tuple[int, _Hit]] = []
    patterns = (
        rf"(\d+)\s*(?:{arabic})",
        rf"(?:{arabic})\s*[:]?\s*(\d+)",
        rf"(\d+)\s*(?:{english})",
        rf"(?:{english})\s*[:]?\s*(\d+)",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.I):
            if any(h.span[0] <= match.start() < h.span[1] for _, h in out):
                continue
            out.append(
                (
                    int(match.group(1)),
                    _Hit(match.group(0), (match.start(), match.end()), CONF_LABELLED),
                )
            )
    # Counts written as words: "ثلاث غرف" is three rooms.
    words = "|".join(sorted(_ARABIC_COUNT_WORDS, key=len, reverse=True))
    for match in re.finditer(rf"({words})\s+(?:{arabic})", text):
        out.append(
            (
                _ARABIC_COUNT_WORDS[match.group(1)],
                _Hit(match.group(0), (match.start(), match.end()), CONF_LABELLED),
            )
        )
    # Dual forms carry the count inside the noun: غرفتين is two rooms.
    for dual in duals:
        for match in re.finditer(re.escape(dual), text):
            out.append((2, _Hit(match.group(0), (match.start(), match.end()), CONF_KEYWORD)))
    return out


def _floor_candidates(text: str) -> list[tuple[int, _Hit]]:
    out: list[tuple[int, _Hit]] = []
    ordinals = "|".join(sorted(_ARABIC_ORDINALS, key=len, reverse=True))
    anchor = r"(?:الدور|الطابق|دور|طابق)"
    for match in re.finditer(rf"{anchor}\s*(?:رقم\s*)?({ordinals}|-?\d{{1,2}})", text):
        token = match.group(1)
        value = _ARABIC_ORDINALS.get(token)
        if value is None:
            value = int(token)
        out.append((value, _Hit(match.group(0), (match.start(), match.end()), CONF_LABELLED)))
    for match in re.finditer(r"(?:بدروم|قبو|basement)", text, re.I):
        out.append((-1, _Hit(match.group(0), (match.start(), match.end()), CONF_KEYWORD)))
    for match in re.finditer(r"ground\s+floor", text, re.I):
        out.append((0, _Hit(match.group(0), (match.start(), match.end()), CONF_KEYWORD)))
    for match in re.finditer(r"(?:floor|level)\s*[:#]?\s*(-?\d{1,2})", text, re.I):
        out.append(
            (int(match.group(1)), _Hit(match.group(0), (match.start(), match.end()), CONF_LABELLED))
        )
    for match in re.finditer(r"(\d{1,2})(?:st|nd|rd|th)\s+floor", text, re.I):
        out.append(
            (int(match.group(1)), _Hit(match.group(0), (match.start(), match.end()), CONF_LABELLED))
        )
    return out


def _build_year_candidates(text: str) -> list[tuple[int, _Hit]]:
    labels = (
        r"سنة\s+البناء"
        r"|تاريخ\s+البناء"
        r"|بنيت\s+عام|بني\s+عام"
        r"|موديل|build\s+year|built\s+in|year\s+built"
    )
    out: list[tuple[int, _Hit]] = []
    for match in re.finditer(rf"(?:{labels})[^\d\n]{{0,12}}(\d{{4}})", text, re.I):
        year = int(match.group(1))
        # A four-digit 13xx/14xx build year is Hijri, not a 700-year-old flat.
        if 1300 <= year <= 1500:
            year = hijri_to_gregorian(year, 1, 1).year
        out.append((year, _Hit(match.group(0), (match.start(), match.end()), CONF_LABELLED)))
    return out


def _hijri_candidates(text: str) -> list[tuple[tuple[int, int, int], _Hit]]:
    out: list[tuple[tuple[int, int, int], _Hit]] = []
    for match in re.finditer(
        r"(\d{1,2})\s*[/\-]\s*(\d{1,2})\s*[/\-]\s*(1[34]\d{2})\s*(?:هـ|ه)?", text
    ):
        day, month, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
        if 1 <= month <= 12 and 1 <= day <= 30:
            out.append(
                (
                    (year, month, day),
                    _Hit(match.group(0), (match.start(), match.end()), CONF_LABELLED),
                )
            )
    for match in re.finditer(
        r"(1[34]\d{2})\s*[/\-]\s*(\d{1,2})\s*[/\-]\s*(\d{1,2})\s*(?:هـ|ه)?", text
    ):
        year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
        if 1 <= month <= 12 and 1 <= day <= 30:
            out.append(
                (
                    (year, month, day),
                    _Hit(match.group(0), (match.start(), match.end()), CONF_LABELLED),
                )
            )
    months = "|".join(sorted(_HIJRI_MONTHS, key=len, reverse=True))
    for match in re.finditer(rf"(\d{{1,2}})\s+({months})\s+(1[34]\d{{2}})", text):
        day, month, year = int(match.group(1)), _HIJRI_MONTHS[match.group(2)], int(match.group(3))
        out.append(
            ((year, month, day), _Hit(match.group(0), (match.start(), match.end()), CONF_LABELLED))
        )
    return out


def extract_listing_fields(text: str) -> ListingExtraction:
    """The rule-based extractor. Regex and normalisation, no model.

    Everything it produces is anchored to a span in `text`; anything it cannot
    anchor it leaves as `None`.
    """
    builder = _Builder(text)

    property_class: list[tuple[str, _Hit]] = []
    for label, pattern in _PROPERTY_CLASS_PATTERNS:
        found = _hits(pattern, text, CONF_KEYWORD)
        if found:
            property_class.append((label, found[0]))
            break

    opportunity_type: list[tuple[str, _Hit]] = []
    for label, pattern in _OPPORTUNITY_TYPE_PATTERNS:
        found = _hits(pattern, text, CONF_KEYWORD)
        if found:
            opportunity_type.append((label, found[0]))
            break

    city: list[tuple[str, _Hit]] = []
    for label, pattern in _CITIES:
        found = _hits(pattern, text, CONF_KEYWORD)
        if found:
            city.append((label, found[0]))
            break

    district: list[tuple[str, _Hit]] = []
    for match in re.finditer(_DISTRICT_PATTERN, text, re.IGNORECASE):
        name = match.group(1).strip("،, .")
        if not name or name[0].isdigit():
            continue
        district.append(
            (
                name,
                _Hit(match.group(0), (match.start(1), match.start(1) + len(name)), CONF_LABELLED),
            )
        )
        break

    signals: list[SignalTag] = []
    seen_tags: set[str] = set()
    for tag, pattern in _SIGNAL_PATTERNS:
        found = _hits(pattern, text, CONF_KEYWORD)
        if found and tag not in seen_tags:
            seen_tags.add(tag)
            signals.append(
                SignalTag(
                    tag=tag,
                    confidence=found[0].confidence,
                    evidence_span=found[0].span,
                    excerpt=_excerpt(text, found[0].span),
                )
            )

    seller_payment = builder.money_field(
        "seller_payment", _amount_candidates(text, _SELLER_PAYMENT_LABELS, CONF_LABELLED)
    )
    remaining = builder.money_field(
        "remaining_installments", _amount_candidates(text, _REMAINING_LABELS, CONF_LABELLED)
    )

    price_candidates = _amount_candidates(text, _PRICE_LABELS, CONF_LABELLED)
    if not price_candidates:
        price_candidates = [
            candidate
            for candidate in _bare_amount_candidates(text)
            if not _overlaps(candidate[1].span, seller_payment.evidence_span)
            and not _overlaps(candidate[1].span, remaining.evidence_span)
        ]
    asking_price = builder.money_field("asking_price", price_candidates)

    # A REGA advertisement licence is a 10-digit number, and the PII redactor
    # treats any 10-digit number starting 1 or 2 as a national ID. When that
    # collision happens the number is already gone by the time we see the text,
    # so the loss is flagged rather than passed off as "no licence stated".
    for match in re.finditer(rf"(?:{_LICENCE_LABELS})\D{{0,6}}\[REDACTED_ID\]", text, re.I):
        builder.flag(
            "advertisement_licence",
            QualityCode.UNPARSEABLE,
            "a licence number was removed by PII redaction (national-ID false "
            f"positive) at offset {match.start()}",
        )

    licence = builder.text_field(
        "advertisement_licence",
        [
            (match.group(1), _Hit(match.group(0), (match.start(), match.end()), CONF_LABELLED))
            for match in re.finditer(rf"(?:{_LICENCE_LABELS})\D{{0,14}}(\d{{6,14}})", text, re.I)
        ],
    )

    hijri = builder.text_field(
        "hijri_date",
        [(f"{y:04d}-{m:02d}-{d:02d}", hit) for (y, m, d), hit in _hijri_candidates(text)],
    )
    gregorian = TextField()
    if hijri.value is not None:
        year, month, day = (int(part) for part in hijri.value.split("-"))
        converted = hijri_to_gregorian(year, month, day)
        gregorian = TextField(
            value=converted.isoformat(),
            confidence=round(hijri.confidence * 0.95, 4),
            evidence_span=hijri.evidence_span,
            excerpt=hijri.excerpt,
        )
        builder.flag(
            "gregorian_date",
            QualityCode.HIJRI_APPROXIMATE,
            "tabular Hijri conversion, accurate to about one day against Umm al-Qura",
        )

    result = ListingExtraction(
        property_class=builder.text_field("property_class", property_class),
        city=builder.text_field("city", city),
        district=builder.text_field("district", district),
        area_sqm=builder.number_field("area_sqm", _area_candidates(text), AREA_MIN, AREA_MAX),
        land_area_sqm=builder.number_field(
            "land_area_sqm", _land_area_candidates(text), AREA_MIN, AREA_MAX
        ),
        bedrooms=builder.int_field(
            "bedrooms",
            _room_candidates(
                text,
                r"غرفة\s*نوم|غرف\s*نوم|غرفة|غرف",
                r"bedrooms?|beds?|b/?r",
                ("غرفتين", "غرفتان"),
            ),
            ROOM_MIN,
            ROOM_MAX,
        ),
        bathrooms=builder.int_field(
            "bathrooms",
            _room_candidates(
                text,
                r"حمامات|حمام|دورات\s*مياه|دورة\s*مياه",
                r"bathrooms?|baths?|wc",
                ("حمامين", "حمامان"),
            ),
            ROOM_MIN,
            ROOM_MAX,
        ),
        floor=builder.int_field("floor", _floor_candidates(text), FLOOR_MIN, FLOOR_MAX),
        build_year=builder.int_field(
            "build_year", _build_year_candidates(text), YEAR_MIN, _year_max()
        ),
        asking_price=asking_price,
        seller_payment=seller_payment,
        remaining_installments=remaining,
        opportunity_type=builder.text_field("opportunity_type", opportunity_type),
        advertisement_licence=licence,
        hijri_date=hijri,
        gregorian_date=gregorian,
        signals=signals,
        quality_flags=builder.flags,
        language=_detect_language(text),
    )
    return result


def _overlaps(span: tuple[int, int], other: tuple[int, int] | None) -> bool:
    if other is None:
        return False
    return span[0] < other[1] and other[0] < span[1]


# ---------------------------------------------------------------------------
# The agent


class ListingExtractionAgent(Agent[ListingText, ListingExtraction]):
    """Reads listing text and returns a schema. No tools, by design.

    `uses_tools = False` is not a default we happened to inherit: the runtime
    refuses to run an agent that both reads untrusted content and holds tools,
    so an instruction injected into a listing has nothing to actuate.
    """

    name = "extraction"
    prompt_version = PROMPT_VERSION
    tier = ModelTier.STANDARD
    output_model = ListingExtraction
    call_budget_usd = Decimal("0.05")
    uses_tools = False

    def system_prompt(self) -> str:
        return (
            "You extract structured facts from Saudi property listings written in "
            "Arabic, English or both. For every field return value, confidence and "
            "evidence_span: the character offsets in the supplied text that justify "
            "the value. A field with no supporting span is null -- never infer, and "
            "never guess bedrooms from area. Do not clamp out-of-range numbers; "
            "return them and let validation reject them.\n"
            "Vocabulary that changes the meaning of the numbers:\n"
            "  تنازل is an ASSIGNMENT, not a sale. The seller's payment "
            "excludes the balance still owed to the developer, so extract "
            "remaining_installments (دفعة / المتبقي "
            "للمطور) whenever the text supports it and set "
            "opportunity_type = ASSIGNMENT.\n"
            "  مزاد = auction, عاجل = urgent, "
            "إفراغ فوري = immediate transfer, "
            "على الشارع = street-facing, "
            "درج = staircase/duplex, "
            "شقة/فيلا/دور/أرض = "
            "apartment/villa/floor/land.\n"
            "Respond with JSON matching the schema and nothing else."
        )

    def user_prompt(self, payload: ListingText) -> str:
        return (
            "Extract the fields for one listing from the untrusted block below. "
            f"Character offsets are into that block, which is {len(payload.text)} "
            "characters long."
        )

    def untrusted_content(self, payload: ListingText) -> list[str]:
        return [payload.text]

    def subject(self, payload: ListingText) -> tuple[str, UUID | None]:
        return ("listing_text", payload.subject_id)

    def input_fingerprint(self, payload: ListingText) -> Any:
        return {
            "text_sha256": hashlib.sha256(payload.text.encode()).hexdigest(),
            "language_hint": payload.language_hint,
            "method_version": METHOD_VERSION,
        }

    def validate_output(self, output: ListingExtraction, payload: ListingText) -> ListingExtraction:
        """Post-model validation. The model is not the last line of defence.

        Applied to every output regardless of who produced it, so switching to a
        real provider does not weaken any of it:

          * a span that does not resolve in the source text invalidates its
            field -- this is what stops a fabricated citation;
          * ranges are re-checked and violations become null plus a flag.
        """
        text = payload.text
        flags = list(output.quality_flags)
        updates: dict[str, EvidencedField] = {}

        for name, extracted in output.scalar_fields().items():
            if extracted.value is None:
                continue
            span = extracted.evidence_span
            if span is None or not (0 <= span[0] < span[1] <= len(text)):
                flags.append(
                    QualityFlag(
                        field_name=name,
                        code=QualityCode.EVIDENCE_MISMATCH,
                        detail=f"evidence_span {span} does not resolve in a "
                        f"{len(text)}-character source",
                    )
                )
                updates[name] = extracted.model_copy(update={"value": None, "confidence": 0.0})
                continue
            if extracted.excerpt and text[span[0] : span[1]] not in extracted.excerpt:
                flags.append(
                    QualityFlag(
                        field_name=name,
                        code=QualityCode.EVIDENCE_MISMATCH,
                        detail="excerpt does not match the text at evidence_span",
                    )
                )
                updates[name] = extracted.model_copy(update={"value": None, "confidence": 0.0})

        for signal in output.signals:
            start, end = signal.evidence_span
            if not (0 <= start < end <= len(text)):
                flags.append(
                    QualityFlag(
                        field_name="signals",
                        code=QualityCode.EVIDENCE_MISMATCH,
                        detail=f"signal {signal.tag} cites an unresolvable span",
                    )
                )

        candidate = output.model_copy(update={**updates, "quality_flags": flags})
        return _enforce_ranges(candidate)


def _enforce_ranges(extraction: ListingExtraction) -> ListingExtraction:
    """Range validation, applied after the model. Rejects, never clamps."""
    flags = list(extraction.quality_flags)
    updates: dict[str, EvidencedField] = {}

    def reject(name: str, extracted: EvidencedField, detail: str) -> None:
        flags.append(QualityFlag(field_name=name, code=QualityCode.OUT_OF_RANGE, detail=detail))
        updates[name] = extracted.model_copy(update={"value": None, "confidence": 0.0})

    for name in ("area_sqm", "land_area_sqm"):
        area: NumberField = getattr(extraction, name)
        if area.value is not None and not AREA_MIN <= area.value <= AREA_MAX:
            reject(name, area, f"{area.value} m² outside [{AREA_MIN}, {AREA_MAX}]")

    for name in ("asking_price", "seller_payment", "remaining_installments"):
        money: MoneyField = getattr(extraction, name)
        if money.value is not None and not PRICE_MIN <= money.value <= PRICE_MAX:
            reject(name, money, f"{money.value} SAR outside [{PRICE_MIN}, {PRICE_MAX}]")

    floor: IntField = extraction.floor
    if floor.value is not None and not FLOOR_MIN <= floor.value <= FLOOR_MAX:
        reject("floor", floor, f"{floor.value} outside [{FLOOR_MIN}, {FLOOR_MAX}]")

    for name in ("bedrooms", "bathrooms"):
        rooms: IntField = getattr(extraction, name)
        if rooms.value is not None and not ROOM_MIN <= rooms.value <= ROOM_MAX:
            reject(name, rooms, f"{rooms.value} outside [{ROOM_MIN}, {ROOM_MAX}]")

    year: IntField = extraction.build_year
    if year.value is not None and not YEAR_MIN <= year.value <= _year_max():
        reject("build_year", year, f"{year.value} outside [{YEAR_MIN}, {_year_max()}]")

    if not updates and len(flags) == len(extraction.quality_flags):
        return extraction
    return extraction.model_copy(update={**updates, "quality_flags": flags})


def deterministic_extraction_responder(request: LLMRequest) -> str:
    """Offline stand-in for the extraction step. **Not a model.**

    It reads the listing out of `request.untrusted_blocks`, which is the same
    text the runtime frames as data, so evidence offsets are exact. Being a
    regex extractor it is structurally incapable of following an instruction
    embedded in the listing -- useful for the injection tests, and honest about
    what it is: rules, recorded as `provider="deterministic-offline"`.
    """
    text = request.untrusted_blocks[0] if request.untrusted_blocks else ""
    return extract_listing_fields(text).model_dump_json()


def extract_listing(
    context: AgentContext, raw_text: str, *, subject_id: UUID | None = None
) -> tuple[AgentResult[ListingExtraction], ListingText]:
    """Canonicalise, then run the agent through the runtime.

    Returns the canonical text alongside the result because every evidence span
    is meaningless without the exact string it indexes.
    """
    payload = canonicalise(raw_text, subject_id=subject_id)
    result = AgentRuntime(context).run(ListingExtractionAgent(), payload)
    return result, payload
