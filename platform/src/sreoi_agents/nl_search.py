"""The natural-language search agent.

Two things make this safe rather than clever.

**It cannot reach the database.** The agent emits a structured *intent*: a set
of keyword arguments for the existing `OpportunityFilters` dataclass, which the
deterministic search layer executes as bound parameters. No fragment of user
text ever becomes SQL, so prompt injection has nowhere to land. The mapping
from intent to filters is a single constructor call in the API router; the
agent layer sits beneath the API layer and never imports it (see
`.importlinter`), so `SORT_KEYS` here mirrors `sreoi_api.search.SORTS` and a
test asserts the two agree.

**It says what it decided.** A search tool that quietly reinterprets a request
is worse than one that refuses: the user cannot correct a filter they cannot
see. So every compilation returns, alongside the filters:

  * `interpreted` -- each vague term with the explicit criterion it became;
  * `unmapped` -- every fragment that was not understood, never dropped;
  * `not_enforced` -- criteria we understood but the search layer cannot yet
    apply, with the reason. Two of the brief's own example terms land here,
    because rental yield and per-dimension legal risk are not computed by this
    platform yet. Silently ignoring them would be the dishonest option.

Arabic is a first-class input: Eastern Arabic numerals are normalised through
`sreoi_sources.redaction.normalize_digits`, and ألف / مليون amounts, Arabic
district names and Arabic vague terms are all recognised.

No model is called. The provider is the offline `DeterministicProvider` with
the rule-based responder below, recorded as `provider="deterministic-offline"`.
The rules are the compiler; a real model would replace the responder and would
still have to pass the same post-model validation.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from sreoi_agents.provider import ModelTier
from sreoi_agents.runtime import Agent, AgentContext, AgentRuntime
from sreoi_agents.untrusted import scan
from sreoi_persistence.models import District
from sreoi_sources.redaction import normalize_digits

PROMPT_VERSION = "nl-search-prompt-v1"

# Mirrors sreoi_api.search.SORTS. Asserted equal by tests/test_nl_search.py --
# the agent layer must not import the API layer.
SORT_KEYS = ("score", "discount", "cost", "newest")
PROPERTY_CLASSES = ("APARTMENT", "VILLA", "RESIDENTIAL_PLOT")
OPPORTUNITY_TYPES = (
    "AUCTION",
    "ASSIGNMENT",
    "RESALE",
    "OFF_PLAN_RESALE",
    "DEVELOPER_INVENTORY",
)

DEFAULT_LIMIT = 100
MAX_LIMIT = 200
# A budget above this is not a budget. Bounding it keeps a mistyped or
# adversarial amount from producing an effectively unfiltered query.
MAX_COST_CEILING = Decimal("500000000")
# Amounts below this with no ألف / مليون / k / m multiplier are ambiguous
# (SAR 1.2? 1.2 million?) and are reported unmapped rather than guessed.
AMBIGUOUS_AMOUNT_BELOW = Decimal("10000")

# What "a bargain" is asserted to mean, made explicit and editable.
BARGAIN_MIN_DISCOUNT_PCT = 10.0
# What "good rental demand" would mean once the rental engine exists.
GOOD_YIELD_PCT = 6.0

_NO_EXCLUSION_REASON = (
    "the search layer supports inclusion filters only, so an exclusion cannot "
    "be applied; it is declared here rather than applied inverted"
)


class NLSearchError(RuntimeError):
    """The request could not be compiled."""


# --------------------------------------------------------------------------
# Structured output


class Unenforceable(BaseModel):
    """A criterion we understood but the deterministic search cannot apply."""

    term: str = Field(max_length=120)
    meaning: str = Field(max_length=200)
    reason: str = Field(max_length=240)


class SearchIntent(BaseModel):
    """Filter arguments plus the reasoning the user is entitled to see."""

    districts: list[str] = Field(default_factory=list, max_length=40)
    opportunity_types: list[str] = Field(default_factory=list, max_length=8)
    property_class: str | None = Field(default=None, max_length=32)
    max_cost: float | None = None
    min_discount_pct: float | None = None
    min_score: float | None = None
    exclude_insufficient: bool = False
    sort: str = Field(default="score", max_length=16)
    limit: int = DEFAULT_LIMIT
    interpreted: dict[str, str] = Field(default_factory=dict)
    unmapped: list[str] = Field(default_factory=list, max_length=40)
    not_enforced: list[Unenforceable] = Field(default_factory=list, max_length=12)
    confidence: float = 0.0
    refusal_reason: str | None = Field(default=None, max_length=300)

    @property
    def refused(self) -> bool:
        return self.refusal_reason is not None

    def filter_kwargs(self) -> dict[str, Any]:
        """Exactly the constructor arguments of `OpportunityFilters`."""
        return {
            "districts": list(self.districts),
            "opportunity_types": list(self.opportunity_types),
            "property_class": self.property_class,
            "max_cost": None if self.max_cost is None else Decimal(str(self.max_cost)),
            "min_discount_pct": self.min_discount_pct,
            "min_score": self.min_score,
            "include_insufficient": not self.exclude_insufficient,
            "sort": self.sort,
            "limit": self.limit,
        }

    def describe(self) -> dict[str, Any]:
        """The compiled filters, shaped for display before any results."""
        out: dict[str, Any] = {}
        if self.districts:
            out["districts"] = list(self.districts)
        if self.opportunity_types:
            out["opportunity_types"] = list(self.opportunity_types)
        if self.property_class:
            out["property_class"] = self.property_class
        if self.max_cost is not None:
            out["max_true_acquisition_cost"] = self.max_cost
        if self.min_discount_pct is not None:
            out["min_discount_pct"] = self.min_discount_pct
        if self.min_score is not None:
            out["min_opportunity_score"] = self.min_score
        if self.exclude_insufficient:
            out["exclude_insufficient_data"] = True
        out["sort"] = self.sort
        out["limit"] = self.limit
        return out


@dataclass(frozen=True, slots=True)
class CompiledSearch:
    intent: SearchIntent
    query: str
    injection_flagged: bool
    injection_summary: str
    run_id: UUID | None


# --------------------------------------------------------------------------
# District vocabulary


@dataclass(frozen=True, slots=True)
class DistrictTerm:
    """One district and every surface form we accept for it."""

    name_en: str
    variants: tuple[str, ...]


# Alef and ya variants folded to one form so "أقل" and "اقل" are one term.
_LETTER_FOLD = str.maketrans("أإآٱى", "ااااي")


def _fold(text: str) -> str:
    """Normalise for matching: ASCII digits, lowercase, folded Arabic letters.

    Deliberately close to length-preserving so that fragments reported back as
    `unmapped` still read like what the user typed.
    """
    folded = normalize_digits(text).lower()
    folded = re.sub(r"[ً-ْـٰ]", "", folded)
    return re.sub(r"\s+", " ", folded.translate(_LETTER_FOLD)).strip()


def _district_variants(name_en: str, name_ar: str) -> tuple[str, ...]:
    forms = {_fold(name_en), _fold(name_ar)}
    english = _fold(name_en)
    if english.startswith("al "):
        forms.add(english[3:])
        forms.add("al-" + english[3:])
    else:
        forms.add("al " + english)
    arabic = _fold(name_ar)
    if arabic.startswith("ال"):
        forms.add(arabic[2:])
    return tuple(sorted((f for f in forms if f), key=len, reverse=True))


def district_vocabulary(session: Session) -> tuple[DistrictTerm, ...]:
    """Districts we actually hold. The agent cannot invent one."""
    rows = session.execute(
        select(District.name_en, District.name_ar).order_by(District.name_en)
    ).all()
    return tuple(
        DistrictTerm(name_en=name_en, variants=_district_variants(name_en, name_ar))
        for name_en, name_ar in rows
    )


# --------------------------------------------------------------------------
# Vocabularies

_CLASS_TERMS: dict[str, tuple[str, ...]] = {
    "APARTMENT": ("apartments", "apartment", "flats", "flat", "شقق", "شقة", "شقه"),
    "VILLA": ("villas", "villa", "فلل", "فيلات", "فيلا"),
    "RESIDENTIAL_PLOT": (
        "residential plots",
        "residential plot",
        "plots",
        "plot",
        "land",
        "اراضي سكنية",
        "ارض سكنية",
        "اراضي",
        "ارض",
        "قطعة ارض",
    ),
}

_TYPE_TERMS: dict[str, tuple[str, ...]] = {
    "OFF_PLAN_RESALE": (
        "off-plan resale",
        "off plan resale",
        "off-plan",
        "off plan",
        "على الخارطة",
        "على الخريطة",
    ),
    "DEVELOPER_INVENTORY": ("developer inventory", "مخزون المطور", "مخزون مطور"),
    "AUCTION": ("auctions", "auction", "مزادات", "مزاد", "infath", "انفاذ"),
    "ASSIGNMENT": ("assignments", "assignment", "تنازلات", "تنازل"),
    "RESALE": ("resales", "resale", "اعادة بيع", "إعادة بيع"),
}

_CITY_TERMS = ("riyadh", "الرياض")

_MULTIPLIERS: tuple[tuple[str, Decimal], ...] = (
    ("million", Decimal("1000000")),
    ("مليون", Decimal("1000000")),
    ("thousand", Decimal("1000")),
    ("الف", Decimal("1000")),
    ("mn", Decimal("1000000")),
    ("m", Decimal("1000000")),
    ("k", Decimal("1000")),
)

_MAX_COST = re.compile(
    r"(?:under|below|less\s+than|up\s+to|max(?:imum)?|at\s+most|no\s+more\s+than|<=?|"
    r"تحت|اقل\s+من|دون|حتى|بحد\s+اقصى|لا\s+يزيد\s+عن|لا\s+تزيد\s+عن)"
    r"\s*(?:sar|sr|ريال)?\s*"
    r"(\d+(?:[.,]\d+)*)\s*"
    r"(million|thousand|مليون|الف|mn|m|k)?\s*(?:sar|sr|ريال)?",
    re.I,
)

_MAX_COST_BARE = re.compile(
    r"(?:under|below|less\s+than|up\s+to|max(?:imum)?|at\s+most|"
    r"تحت|اقل\s+من|دون|حتى|بحد\s+اقصى)"
    r"\s*(?:a|one|نصف)?\s*(million|thousand|مليون|الف)\s*(?:sar|sr|ريال)?",
    re.I,
)

_MIN_COST = re.compile(
    r"(?:above|over|at\s+least|more\s+than|from|>=?|"
    r"اكثر\s+من|فوق|بحد\s+ادني|لا\s+يقل\s+عن)"
    r"\s*(?:sar|sr|ريال)?\s*"
    r"(\d+(?:[.,]\d+)*)\s*"
    r"(million|thousand|مليون|الف|mn|m|k)\s*(?:sar|sr|ريال)?",
    re.I,
)

_DISCOUNT = re.compile(
    r"(?:(?:at\s+least|minimum|min|over|above|more\s+than|>=?)\s*)?"
    r"(\d+(?:\.\d+)?)\s*(?:%|٪|percent|per\s+cent)\s*(?:or\s+more\s+)?"
    r"(?:discount|off\b|below\s+(?:fair|market))"
    r"|(?:discount|off)\s*(?:of\s+)?(?:at\s+least\s+|minimum\s+|min\s+|>=?\s*)?"
    r"(\d+(?:\.\d+)?)\s*(?:%|٪|percent|per\s+cent)"
    r"|(?:خصم|تخفيض)[^\d\n]{0,25}?(\d+(?:\.\d+)?)\s*(?:%|٪)?"
    r"|(\d+(?:\.\d+)?)\s*(?:%|٪)[^\d\n]{0,15}?(?:خصم|تخفيض)",
    re.I,
)

_MIN_SCORE = re.compile(
    r"(?:score|rating|درجة|تقييم)\s*"
    r"(?:above|over|at\s+least|of\s+at\s+least|>=?|min(?:imum)?|اعلي\s+من|فوق|لا\s+تقل\s+عن)?"
    r"\s*(\d+(?:\.\d+)?)",
    re.I,
)

_LIMIT = re.compile(r"(?:top|first|best)\s+(\d{1,3})\b|اول\s+(\d{1,3})|افضل\s+(\d{1,3})", re.I)

# Strong SQL shapes only. A bare semicolon or apostrophe is not evidence of an
# attack and refusing on one would break ordinary punctuation.
_SQL_SHAPES = re.compile(
    r"--\s|--$|/\*|\*/|\bunion\s+(?:all\s+)?select\b|\bdrop\s+(?:table|database)\b"
    r"|\bdelete\s+from\b|\binsert\s+into\b|\bupdate\s+\w+\s+set\b|\btruncate\s+table\b"
    r"|\balter\s+table\b|\bselect\b[\s\S]{0,40}\bfrom\b|\bor\s+1\s*=\s*1\b|\bxp_cmdshell\b"
    r"|\bpg_sleep\b|\bsleep\s*\(",
    re.I,
)


# "not auctions" must never compile to "only auctions". `OpportunityFilters`
# has no exclusion field, so a negated term is declared unenforceable rather
# than applied inverted -- the one failure mode worse than dropping a term is
# applying its opposite.
_NEGATIONS = frozenset(
    {
        "not",
        "no",
        "without",
        "exclude",
        "excluding",
        "except",
        "بدون",
        "بلا",
        "غير",
        "ليس",
        "ماعدا",
        "عدا",
    }
)


@dataclass(frozen=True, slots=True)
class VagueTerm:
    """A soft phrase, the hard criterion it becomes, and whether we can apply it."""

    key: str
    terms: tuple[str, ...]
    meaning: str
    enforceable: bool
    reason: str = ""


_VAGUE_TERMS: tuple[VagueTerm, ...] = (
    VagueTerm(
        key="rental_demand",
        terms=(
            "good rental demand",
            "strong rental demand",
            "high rental demand",
            "good rental yield",
            "strong rental yield",
            "rental demand",
            "طلب ايجاري جيد",
            "طلب ايجاري مرتفع",
            "عائد ايجاري جيد",
            "طلب ايجاري",
        ),
        meaning=f"min_gross_yield >= {GOOD_YIELD_PCT:.0f}% (district median)",
        enforceable=False,
        reason=(
            "gross yield is computed and stored per opportunity, but "
            "OpportunityFilters exposes no yield filter, so the deterministic "
            "search cannot apply it yet; it is shown here rather than silently "
            "dropped"
        ),
    ),
    VagueTerm(
        key="legal_complexity",
        terms=(
            "no major legal complexity",
            "no legal complexity",
            "no legal complications",
            "no legal issues",
            "clean title",
            "بدون تعقيدات قانونية",
            "بلا تعقيدات قانونية",
            "بدون مشاكل قانونية",
            "بدون تعقيدات",
        ),
        meaning="legal_risk <= LOW",
        enforceable=False,
        reason=(
            "per-dimension risk levels are computed but not persisted, so legal "
            "risk cannot be filtered on yet; only the aggregate risk score reaches "
            "the database"
        ),
    ),
    VagueTerm(
        key="well_evidenced",
        terms=(
            "high confidence",
            "reliable data",
            "well evidenced",
            "solid data",
            "بيانات موثوقة",
            "ثقة عالية",
        ),
        meaning="exclude_insufficient_data = true",
        enforceable=True,
    ),
    VagueTerm(
        key="bargain",
        terms=("bargain", "bargains", "mispriced", "undervalued", "مسعرة دون قيمتها", "صفقة رابحة"),
        meaning=f"min_discount_pct >= {BARGAIN_MIN_DISCOUNT_PCT:.0f}",
        enforceable=True,
    ),
    VagueTerm(
        key="sort_best",
        terms=(
            "best opportunities",
            "top opportunities",
            "strongest opportunities",
            "افضل الفرص",
        ),
        meaning="sort = highest opportunity score",
        enforceable=True,
    ),
    VagueTerm(
        key="sort_cheapest",
        terms=("cheapest", "lowest cost", "الارخص", "ارخص", "اقل تكلفة"),
        meaning="sort = lowest acquisition cost",
        enforceable=True,
    ),
    VagueTerm(
        key="sort_discount",
        terms=(
            "biggest discount",
            "largest discount",
            "deepest discount",
            "اكبر خصم",
            "اعلي خصم",
        ),
        meaning="sort = largest discount",
        enforceable=True,
    ),
    VagueTerm(
        key="sort_newest",
        terms=("newest", "most recent", "latest", "الاحدث", "احدث"),
        meaning="sort = most recently added",
        enforceable=True,
    ),
    VagueTerm(
        key="liquidity",
        terms=("liquid", "easy to sell", "quick to sell", "سهل البيع", "سيولة عالية"),
        meaning="district liquidity_score high",
        enforceable=False,
        reason=(
            "district liquidity is an input to the score but is not exposed as a "
            "filter, so it cannot be applied on its own"
        ),
    ),
)

_STOPWORD_LINES = (
    "without exclude excluding except بدون بلا غير ليس ماعدا عدا",
    "الفرص الفرصه العقار العقارات الوحدات الوحدة الاسعار السعر التكلفة الميزانية",
    "a an the and or with for to from of in on at any all no not is are there that this these",
    "those show me find get list please i we want looking need only some more most just about",
    "around near nearby close by property properties opportunity opportunities unit units",
    "real estate deal deals sar sr riyal riyals price prices priced cost costs budget under",
    "below less than up max maximum least min minimum over above discount off score rating",
    "city district districts area sqm buy purchase purchasing invest investment sale sales",
    "market value good great nice available في من على مع الي الى او و ب بـ حول قرب بجوار بحي",
    "حي احياء ريال ريالا تحت اقل دون حتي حتى بحد اقصي اقصى ادني ادنى اكثر فوق لا يزيد تزيد",
    "يقل تقل عن ابحث اعرض اريد ارغب عقار عقارات فرص فرصة فرصه التي هي مع ذلك هذه هذا جيد جيدة",
    "جيده افضل فقط مليون الف خصم تخفيض درجة تقييم سعر اسعار تكلفة ميزانية شراء استثمار قيمة",
    "سوق متوفرة متاحة",
)
_STOPWORDS = frozenset(" ".join(_STOPWORD_LINES).split())


# --------------------------------------------------------------------------
# The compiler


@dataclass(slots=True)
class _Consumption:
    """Tracks which characters of the query were actually understood."""

    text: str
    mask: list[bool]

    def overlaps(self, start: int, end: int) -> bool:
        return any(self.mask[start:end])

    def take(self, start: int, end: int) -> None:
        for i in range(start, end):
            self.mask[i] = True

    def find(self, term: str) -> tuple[int, int] | None:
        """First unconsumed occurrence of a term, folded the same way as the text.

        Folding the term here rather than at the call sites is what keeps the
        vocabularies readable: `"على الخارطة"` can be written the way it is
        actually spelled even though the matcher works on folded text.
        """
        term = _fold(term)
        if not term:
            return None
        start = 0
        while True:
            index = self.text.find(term, start)
            if index < 0:
                return None
            end = index + len(term)
            if not self.overlaps(index, end) and self._bounded(index, end):
                return index, end
            start = index + 1

    def _bounded(self, start: int, end: int) -> bool:
        """Latin terms must match whole words; Arabic tolerates clitics."""
        term = self.text[start:end]
        if not re.match(r"^[a-z0-9]", term):
            return True
        before = self.text[start - 1] if start else " "
        after = self.text[end] if end < len(self.text) else " "
        return not (before.isalnum() or after.isalnum())

    def negated(self, start: int) -> bool:
        """Whether the term starting here is preceded by a negation."""
        window = self.text[max(0, start - 24) : start]
        words = re.findall(r"[\w؀-ۿ]+", window)
        return any(word in _NEGATIONS for word in words[-2:])

    def absorb_clitics(self) -> None:
        """Count a single-letter Arabic clitic as understood with its stem.

        Arabic attaches و / ب / ل / ف / ك directly to a word, so "وسدرة" is the
        conjunction plus a district we did match. Without this the token reads
        as half-understood and depresses the reported confidence for exactly
        the queries the market actually types.
        """
        for index, char in enumerate(self.text):
            if self.mask[index] or char not in "وبلفك":
                continue
            before_ok = index == 0 or not self.text[index - 1].isalnum()
            after_ok = index + 1 < len(self.mask) and self.mask[index + 1]
            if before_ok and after_ok:
                self.mask[index] = True

    def remainder_phrases(self) -> list[str]:
        phrases: list[str] = []
        current: list[str] = []
        for token, start, end in _tokens(self.text):
            trivial = len(token) < 2 and not token.isdigit()
            if self.overlaps(start, end) or token in _STOPWORDS or trivial:
                if current:
                    phrases.append(" ".join(current))
                    current = []
                continue
            current.append(token)
        if current:
            phrases.append(" ".join(current))
        return phrases


def _tokens(text: str) -> list[tuple[str, int, int]]:
    return [(m.group(0), m.start(), m.end()) for m in re.finditer(r"[\w؀-ۿ.]+", text)]


def _amount(digits: str, multiplier: str | None) -> Decimal | None:
    """Parse an amount, refusing to guess when the magnitude is ambiguous."""
    raw = digits.replace(",", "")
    if raw.count(".") > 1:
        raw = raw.replace(".", "")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    if multiplier:
        for name, factor in _MULTIPLIERS:
            if multiplier.lower() == name:
                value *= factor
                break
    elif value < AMBIGUOUS_AMOUNT_BELOW:
        # "under 1.2" could be SAR 1.2 or SAR 1.2 million. Do not guess.
        return None
    return value


def compile_intent(query: str, districts: tuple[DistrictTerm, ...]) -> SearchIntent:
    """Turn a request into filter arguments plus its own explanation.

    Pure: no I/O, no database, no model. Deterministic for a given query and
    district vocabulary, which is what makes the labelled corpus meaningful.
    """
    folded = _fold(query)
    if not folded:
        return SearchIntent(refusal_reason="empty request", confidence=0.0, limit=DEFAULT_LIMIT)

    sql = _SQL_SHAPES.search(folded)
    if sql is not None:
        return SearchIntent(
            unmapped=[sql.group(0).strip()],
            confidence=0.0,
            limit=DEFAULT_LIMIT,
            refusal_reason=(
                "the request contains SQL-shaped text and was refused. Search "
                "terms are compiled into filters, never into a query, so this "
                "could not have executed; refusing makes the attempt visible."
            ),
        )

    injection = scan(query)
    if injection.suspicious:
        return SearchIntent(
            unmapped=sorted({f"instruction-like text: {f.pattern}" for f in injection.findings}),
            confidence=0.0,
            limit=DEFAULT_LIMIT,
            refusal_reason=(
                f"the request tries to instruct the system rather than describe "
                f"a search ({injection.summary}); no filters were compiled"
            ),
        )

    state = _Consumption(text=folded, mask=[False] * len(folded))
    interpreted: dict[str, str] = {}
    not_enforced: list[Unenforceable] = []
    unmapped: list[str] = []

    districts_hit: list[str] = []
    opportunity_types: list[str] = []
    property_class: str | None = None
    max_cost: Decimal | None = None
    min_discount_pct: float | None = None
    min_score: float | None = None
    exclude_insufficient = False
    sort = "score"
    limit = DEFAULT_LIMIT

    # 1. Numeric constraints first: "at least 15% discount" must not be eaten
    #    by the "at least <amount>" budget pattern.
    for match in _DISCOUNT.finditer(folded):
        if state.overlaps(match.start(), match.end()):
            continue
        digits = next((g for g in match.groups() if g), None)
        if digits is None:
            continue
        value = float(digits)
        if not 0.0 <= value <= 100.0:
            unmapped.append(match.group(0).strip())
            state.take(match.start(), match.end())
            continue
        min_discount_pct = value if min_discount_pct is None else max(min_discount_pct, value)
        state.take(match.start(), match.end())
        interpreted[match.group(0).strip()] = f"min_discount_pct = {value:g}"

    for match in _MIN_SCORE.finditer(folded):
        if state.overlaps(match.start(), match.end()):
            continue
        value = float(match.group(1))
        state.take(match.start(), match.end())
        if 0.0 <= value <= 100.0:
            min_score = value if min_score is None else max(min_score, value)
            interpreted[match.group(0).strip()] = f"min_opportunity_score = {value:g}"
        else:
            unmapped.append(match.group(0).strip())

    for match in _MAX_COST.finditer(folded):
        if state.overlaps(match.start(), match.end()):
            continue
        amount = _amount(match.group(1), match.group(2))
        state.take(match.start(), match.end())
        if amount is None or amount <= 0 or amount > MAX_COST_CEILING:
            unmapped.append(match.group(0).strip())
            continue
        max_cost = amount if max_cost is None else min(max_cost, amount)
        interpreted[match.group(0).strip()] = (
            f"max_true_acquisition_cost = {amount:,.0f} "
            "(measured on the true acquisition cost, never on the advertised price)"
        )

    if max_cost is None:
        for match in _MAX_COST_BARE.finditer(folded):
            if state.overlaps(match.start(), match.end()):
                continue
            amount = _amount("1", match.group(1))
            if amount is None:
                continue
            half = "نصف" in match.group(0)
            if half:
                amount /= 2
            state.take(match.start(), match.end())
            max_cost = amount
            interpreted[match.group(0).strip()] = (
                f"max_true_acquisition_cost = {amount:,.0f} "
                "(measured on the true acquisition cost, never on the advertised price)"
            )
            break

    for match in _MIN_COST.finditer(folded):
        if state.overlaps(match.start(), match.end()):
            continue
        amount = _amount(match.group(1), match.group(2))
        if amount is None:
            continue
        state.take(match.start(), match.end())
        not_enforced.append(
            Unenforceable(
                term=match.group(0).strip(),
                meaning=f"min_true_acquisition_cost = {amount:,.0f}",
                reason=(
                    "the search layer has no minimum-cost filter; only an upper bound is supported"
                ),
            )
        )

    for match in _LIMIT.finditer(folded):
        if state.overlaps(match.start(), match.end()):
            continue
        digits = next((g for g in match.groups() if g), None)
        if digits is None:
            continue
        state.take(match.start(), match.end())
        limit = max(1, min(int(digits), MAX_LIMIT))
        interpreted[match.group(0).strip()] = f"limit = {limit}"

    # 2. Vague terms, longest surface form first.
    vague_pairs = sorted(
        ((term, vague) for vague in _VAGUE_TERMS for term in vague.terms),
        key=lambda pair: len(pair[0]),
        reverse=True,
    )
    for term, vague in vague_pairs:
        span = state.find(term)
        if span is None:
            continue
        state.take(*span)
        interpreted[term] = vague.meaning
        if not vague.enforceable:
            not_enforced.append(
                Unenforceable(term=term, meaning=vague.meaning, reason=vague.reason)
            )
            continue
        if vague.key == "well_evidenced":
            exclude_insufficient = True
        elif vague.key == "bargain":
            min_discount_pct = (
                BARGAIN_MIN_DISCOUNT_PCT
                if min_discount_pct is None
                else max(min_discount_pct, BARGAIN_MIN_DISCOUNT_PCT)
            )
        elif vague.key == "sort_best":
            sort = "score"
        elif vague.key == "sort_cheapest":
            sort = "cost"
        elif vague.key == "sort_discount":
            sort = "discount"
        elif vague.key == "sort_newest":
            sort = "newest"

    # 3. Property class and opportunity type, longest form first.
    class_pairs = sorted(
        ((term, name) for name, terms in _CLASS_TERMS.items() for term in terms),
        key=lambda pair: len(pair[0]),
        reverse=True,
    )
    for term, name in class_pairs:
        span = state.find(term)
        if span is None:
            continue
        if state.negated(span[0]):
            state.take(*span)
            not_enforced.append(
                Unenforceable(
                    term=f"not {term}",
                    meaning=f"property_class != {name}",
                    reason=_NO_EXCLUSION_REASON,
                )
            )
            continue
        state.take(*span)
        if property_class is None:
            property_class = name
            interpreted[term] = f"property_class = {name}"
        elif property_class != name:
            unmapped.append(term)

    type_pairs = sorted(
        ((term, name) for name, terms in _TYPE_TERMS.items() for term in terms),
        key=lambda pair: len(pair[0]),
        reverse=True,
    )
    for term, name in type_pairs:
        span = state.find(term)
        if span is None:
            continue
        if state.negated(span[0]):
            state.take(*span)
            not_enforced.append(
                Unenforceable(
                    term=f"not {term}",
                    meaning=f"opportunity_type != {name}",
                    reason=_NO_EXCLUSION_REASON,
                )
            )
            continue
        state.take(*span)
        if name not in opportunity_types:
            opportunity_types.append(name)
            interpreted[term] = f"opportunity_type includes {name}"

    # 4. Districts, from the vocabulary we actually hold.
    district_pairs = sorted(
        ((variant, term.name_en) for term in districts for variant in term.variants),
        key=lambda pair: len(pair[0]),
        reverse=True,
    )
    district_positions: dict[str, int] = {}
    for variant, name_en in district_pairs:
        span = state.find(variant)
        if span is None:
            continue
        state.take(*span)
        if state.negated(span[0]):
            not_enforced.append(
                Unenforceable(
                    term=f"not {variant}",
                    meaning=f"district != {name_en}",
                    reason=_NO_EXCLUSION_REASON,
                )
            )
            continue
        district_positions.setdefault(name_en, span[0])
    # Echo the districts in the order the user named them.
    districts_hit = sorted(district_positions, key=lambda name: district_positions[name])

    # 5. City. There is no city filter because coverage is one city; saying so
    #    is more useful than pretending the term did something.
    for city in _CITY_TERMS:
        span = state.find(city)
        if span is None:
            continue
        state.take(*span)
        interpreted[city] = (
            "city = Riyadh, which is the only city with coverage, so no city filter is applied"
        )

    state.absorb_clitics()
    unmapped.extend(state.remainder_phrases())

    tokens = [t for t, _s, _e in _tokens(folded)]
    content = [
        (t, s, e)
        for t, s, e in _tokens(folded)
        if t not in _STOPWORDS and (len(t) >= 2 or t.isdigit())
    ]
    understood = sum(1 for _t, s, e in content if all(state.mask[s:e]))
    confidence = round(understood / len(content), 3) if content else (1.0 if tokens else 0.0)

    return SearchIntent(
        districts=districts_hit,
        opportunity_types=opportunity_types,
        property_class=property_class,
        max_cost=float(max_cost) if max_cost is not None else None,
        min_discount_pct=min_discount_pct,
        min_score=min_score,
        exclude_insufficient=exclude_insufficient,
        sort=sort,
        limit=limit,
        interpreted=interpreted,
        unmapped=_dedupe(unmapped),
        not_enforced=not_enforced,
        confidence=confidence,
    )


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        cleaned = value.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
    return out


# --------------------------------------------------------------------------
# The agent


class NaturalLanguageSearchAgent(Agent[dict[str, Any], SearchIntent]):
    """Compiles a request into filter arguments. Cannot query anything."""

    name = "nl_search"
    prompt_version = PROMPT_VERSION
    tier = ModelTier.SMALL
    output_model = SearchIntent
    call_budget_usd = Decimal("0.02")
    uses_tools = False  # the query is user-controlled text

    def system_prompt(self) -> str:
        return (
            "You translate a property search request into a structured filter "
            "object. You never write SQL and never query anything: you emit "
            "filter fields only, and another component executes them. Allowed "
            f"sorts: {', '.join(SORT_KEYS)}. Allowed property classes: "
            f"{', '.join(PROPERTY_CLASSES)}. Allowed opportunity types: "
            f"{', '.join(OPPORTUNITY_TYPES)}. Districts must come from the "
            "supplied list. Put every vague phrase in `interpreted` with the "
            "explicit criterion it becomes, and every fragment you did not "
            "understand in `unmapped`. Never drop a term silently. Respond "
            "with JSON matching the schema."
        )

    def user_prompt(self, payload: dict[str, Any]) -> str:
        return (
            "Known districts:\n"
            f"```json\n{json.dumps(payload['known_districts'], ensure_ascii=False)}\n```\n"
            "Compile the request in the untrusted block below into filters."
        )

    def untrusted_content(self, payload: dict[str, Any]) -> list[str]:
        return [str(payload["query"])]

    def subject(self, payload: dict[str, Any]) -> tuple[str, UUID | None]:
        return ("search", None)

    def input_fingerprint(self, payload: dict[str, Any]) -> Any:
        return {"query": payload["query"], "districts": payload["known_districts"]}

    def validate_output(self, output: SearchIntent, payload: dict[str, Any]) -> SearchIntent:
        """Clamp and vocabulary-check. The model is not the last line of defence.

        Nothing here raises: an out-of-vocabulary value is moved to `unmapped`
        so the user sees that it was not applied, which is more useful than an
        error page and strictly safer than passing it through.
        """
        known = {d["name_en"] for d in payload["known_districts"]}
        unmapped = list(output.unmapped)

        districts: list[str] = []
        for name in output.districts:
            if name in known and name not in districts:
                districts.append(name)
            elif name not in known:
                unmapped.append(f"unknown district: {name}")

        types: list[str] = []
        for name in output.opportunity_types:
            if name in OPPORTUNITY_TYPES and name not in types:
                types.append(name)
            elif name not in OPPORTUNITY_TYPES:
                unmapped.append(f"unknown opportunity type: {name}")

        property_class = output.property_class
        if property_class is not None and property_class not in PROPERTY_CLASSES:
            unmapped.append(f"unknown property class: {property_class}")
            property_class = None

        max_cost = output.max_cost
        if max_cost is not None and not 0 < max_cost <= float(MAX_COST_CEILING):
            unmapped.append(f"out-of-range budget: {max_cost:g}")
            max_cost = None

        min_discount = output.min_discount_pct
        if min_discount is not None and not 0.0 <= min_discount <= 100.0:
            unmapped.append(f"out-of-range discount: {min_discount:g}")
            min_discount = None

        min_score = output.min_score
        if min_score is not None and not 0.0 <= min_score <= 100.0:
            unmapped.append(f"out-of-range score: {min_score:g}")
            min_score = None

        refused = output.refusal_reason is not None
        return output.model_copy(
            update={
                "districts": [] if refused else districts,
                "opportunity_types": [] if refused else types,
                "property_class": None if refused else property_class,
                "max_cost": None if refused else max_cost,
                "min_discount_pct": None if refused else min_discount,
                "min_score": None if refused else min_score,
                "exclude_insufficient": False if refused else output.exclude_insufficient,
                "sort": output.sort if output.sort in SORT_KEYS else "score",
                "limit": max(1, min(output.limit, MAX_LIMIT)),
                "unmapped": _dedupe(unmapped),
                "confidence": min(1.0, max(0.0, output.confidence)),
            }
        )


def deterministic_nl_responder(request: Any) -> str:
    """Offline stand-in: the rule-based compiler, reading the same prompt."""
    match = re.search(r"```json\n(.*?)\n```", request.user, re.S)
    known = json.loads(match.group(1)) if match else []
    districts = tuple(
        DistrictTerm(name_en=d["name_en"], variants=tuple(d["variants"])) for d in known
    )
    query = "\n".join(request.untrusted_blocks)
    return compile_intent(query, districts).model_dump_json()


def compile_search(
    *,
    query: str,
    districts: tuple[DistrictTerm, ...],
    context: AgentContext,
) -> CompiledSearch:
    """Run the agent and return the compiled intent with its audit context."""
    payload: dict[str, Any] = {
        "query": query,
        "known_districts": [
            {"name_en": d.name_en, "variants": list(d.variants)} for d in districts
        ],
    }
    result = AgentRuntime(context).run(NaturalLanguageSearchAgent(), payload)
    return CompiledSearch(
        intent=result.output,
        query=query,
        injection_flagged=result.injection_flagged,
        injection_summary=result.injection.summary,
        run_id=result.run_id,
    )
