"""Document intelligence: accept, hash, store, classify, extract, cite, persist.

An auction brochure is the most valuable and the most dangerous input the
platform takes. Valuable because it carries the lot schedule -- opening prices,
areas, deposit terms -- that nobody publishes as structured data. Dangerous
because it is attacker-authored text arriving with the authority of a PDF, and
because a conclusion drawn from page 7 of a 40-page brochure is worthless to an
investor who cannot check page 7.

So the pipeline has one non-negotiable output rule: **every conclusion carries
`{page, excerpt}`, and a conclusion whose citation does not resolve against the
stored page text is discarded, not stored.** The database enforces the same
thing from below (`ck_document_extractions_page_cited`,
`ck_document_extractions_excerpt`), because a validation rule in application
code is a rule until someone adds a second write path.

Storage is content-addressed and immutable: re-ingesting the same bytes is a
no-op that returns the existing document rather than a second copy with a
different id. PII is redacted at the boundary, before the text is stored and
before any agent sees it -- the un-redacted bytes are hashed for identity and
then never persisted as text.

As in `extraction.py`, no model is called. The document agent runs on a
rule-based responder behind `DeterministicProvider` and is recorded as
`provider="deterministic-offline"`.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from sreoi_agents.extraction import (
    QualityCode,
    QualityFlag,
    extract_listing_fields,
)
from sreoi_agents.provider import LLMRequest, ModelTier
from sreoi_agents.runtime import Agent, AgentContext, AgentRuntime
from sreoi_persistence.models_documents import Document, DocumentExtractionRow
from sreoi_sources.redaction import redact

PROMPT_VERSION = "document-extraction-prompt-v1"
METHOD_VERSION = "document-rules-v1"


class DocumentError(RuntimeError):
    """The document could not be accepted."""


class DocumentClass(StrEnum):
    AUCTION_BROCHURE = "AUCTION_BROCHURE"
    VALUATION_REPORT = "VALUATION_REPORT"
    TERMS_AND_CONDITIONS = "TERMS_AND_CONDITIONS"
    OTHER = "OTHER"


# ---------------------------------------------------------------------------
# Output schema


class PageCitation(BaseModel):
    """Where in the document a conclusion came from. Never optional."""

    page: int = Field(ge=1)
    excerpt: str = Field(min_length=1, max_length=600)


class DocumentConclusion(BaseModel):
    kind: str
    field_name: str
    citation: PageCitation
    group_key: str | None = None
    value_text: str | None = None
    value_numeric: Decimal | None = None
    unit: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class DocumentExtraction(BaseModel):
    document_class: DocumentClass = DocumentClass.OTHER
    classification_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    classification_evidence: list[PageCitation] = Field(default_factory=list)
    conclusions: list[DocumentConclusion] = Field(default_factory=list)
    quality_flags: list[QualityFlag] = Field(default_factory=list)
    method_version: str = METHOD_VERSION

    def by_group(self, kind: str) -> dict[str, dict[str, DocumentConclusion]]:
        """Conclusions of one kind, keyed by group then field -- e.g. lots."""
        grouped: dict[str, dict[str, DocumentConclusion]] = {}
        for conclusion in self.conclusions:
            if conclusion.kind != kind or conclusion.group_key is None:
                continue
            grouped.setdefault(conclusion.group_key, {})[conclusion.field_name] = conclusion
        return grouped


# ---------------------------------------------------------------------------
# Page text


@dataclass(frozen=True, slots=True)
class DocumentPayload:
    """Redacted page text, which is the only form the agent ever sees."""

    filename: str
    content_sha256: str
    pages: tuple[str, ...]
    subject_id: UUID | None = None

    @property
    def page_count(self) -> int:
        return len(self.pages)


def extract_pdf_pages(content: bytes) -> list[str]:
    """Per-page text from a PDF.

    `pdfminer.six` is imported here rather than at module scope so that this
    module still imports in an environment without it -- and so the failure,
    when it happens, names the missing dependency instead of breaking every
    import of `sreoi_agents`.
    """
    try:
        from pdfminer.high_level import extract_text
    except ImportError as exc:  # pragma: no cover -- dependency-shape guard
        raise DocumentError(
            "PDF text extraction needs pdfminer.six, which is not installed."
        ) from exc

    import io

    from pdfminer.pdfpage import PDFPage

    stream = io.BytesIO(content)
    total = len(list(PDFPage.get_pages(stream)))
    pages: list[str] = []
    for index in range(total):
        stream.seek(0)
        pages.append(extract_text(stream, page_numbers=[index]))
    return pages


# ---------------------------------------------------------------------------
# Classification. Deterministic: a keyword vote with the evidence recorded.

_CLASS_TERMS: dict[DocumentClass, tuple[str, ...]] = {
    DocumentClass.AUCTION_BROCHURE: (
        r"مزاد",
        r"كراسة\s+المزاد",
        r"جدول\s+الأصول",
        r"رقم\s+الأصل",
        r"السعر\s+الافتتاحي",
        r"auction",
        r"lot\s+schedule",
        r"opening\s+price",
    ),
    DocumentClass.VALUATION_REPORT: (
        r"تقرير\s+تقييم",
        r"القيمة\s+السوقية",
        r"تقييم\s+عقاري",
        r"المقيم\s+المعتمد",
        r"valuation\s+report",
        r"market\s+value",
        r"certified\s+valuer",
    ),
    DocumentClass.TERMS_AND_CONDITIONS: (
        r"الشروط\s+والأحكام",
        r"شروط\s+المزاد",
        r"العربون",
        r"terms\s+and\s+conditions",
        r"conditions\s+of\s+sale",
    ),
}


def classify(pages: tuple[str, ...]) -> tuple[DocumentClass, float, list[PageCitation]]:
    """Vote on the document class and keep the lines that produced the vote."""
    scores: dict[DocumentClass, int] = {}
    evidence: dict[DocumentClass, list[PageCitation]] = {}
    for doc_class, terms in _CLASS_TERMS.items():
        for term in terms:
            for page_index, text in enumerate(pages):
                match = re.search(term, text, re.IGNORECASE)
                if match is None:
                    continue
                scores[doc_class] = scores.get(doc_class, 0) + 1
                evidence.setdefault(doc_class, []).append(
                    PageCitation(page=page_index + 1, excerpt=_line_around(text, match.start()))
                )
                break
    if not scores:
        return DocumentClass.OTHER, 0.0, []
    best = max(scores.items(), key=lambda item: item[1])
    doc_class, hits = best
    rivals = sum(count for cls, count in scores.items() if cls is not doc_class)
    # Confidence is the winner's share of all matched terms, so a brochure that
    # also contains the terms-and-conditions vocabulary is less certain, honestly.
    confidence = round(hits / (hits + rivals), 4)
    return doc_class, confidence, evidence[doc_class][:6]


def _line_around(text: str, offset: int) -> str:
    start = text.rfind("\n", 0, offset) + 1
    end = text.find("\n", offset)
    if end == -1:
        end = len(text)
    return text[start:end].strip()[:600] or text[offset : offset + 80].strip()


# ---------------------------------------------------------------------------
# Rule-based conclusion builders

_LOT_ANCHOR = re.compile(
    r"(?:رقم\s+الأصل|الأصل\s+رقم|رقم\s+العقار|بند\s+رقم|lot\s+(?:no\.?|number)?)"
    r"\s*[:\-]?\s*(\d{1,4})",
    re.IGNORECASE,
)
_MARKET_VALUE = re.compile(
    r"(?:القيمة\s+السوقية|market\s+value)[^\d\n]{0,24}(\d[\d.,]*)\s*"
    r"(ألف|مليون|million|thousand)?",
    re.IGNORECASE,
)
_PERCENT_TERMS: tuple[tuple[str, str], ...] = (
    ("deposit_percent", r"(?:العربون|عربون|deposit)[^\d\n]{0,24}(\d{1,2}(?:\.\d+)?)\s*%"),
    ("commission_percent", r"(?:عمولة|commission)[^\d\n]{0,24}(\d{1,2}(?:\.\d+)?)\s*%"),
)
_MULTIPLIER = {
    "ألف": Decimal(1000),
    "thousand": Decimal(1000),
    "مليون": Decimal(1_000_000),
    "million": Decimal(1_000_000),
}


def _lot_conclusions(pages: tuple[str, ...]) -> list[DocumentConclusion]:
    """One auction lot per anchored line, parsed with the listing extractor.

    Reusing `extract_listing_fields` here is deliberate: a lot line is a
    listing in miniature, and the Arabic vocabulary, numeral normalisation and
    range validation are all the same problem. Anything the listing extractor
    rejects as out of range is rejected here too.
    """
    out: list[DocumentConclusion] = []
    for page_index, text in enumerate(pages):
        page = page_index + 1
        for line in text.splitlines():
            anchor = _LOT_ANCHOR.search(line)
            if anchor is None:
                continue
            lot = anchor.group(1)
            citation = PageCitation(page=page, excerpt=line.strip()[:600])
            fields = extract_listing_fields(line)
            for name in ("property_class", "district", "city"):
                extracted = getattr(fields, name)
                if extracted.value is not None:
                    out.append(
                        DocumentConclusion(
                            kind="auction_lot",
                            group_key=lot,
                            field_name=name,
                            value_text=str(extracted.value),
                            confidence=extracted.confidence,
                            citation=citation,
                        )
                    )
            for name, unit in (("area_sqm", "sqm"), ("land_area_sqm", "sqm")):
                area = getattr(fields, name)
                if area.value is not None:
                    out.append(
                        DocumentConclusion(
                            kind="auction_lot",
                            group_key=lot,
                            field_name=name,
                            value_numeric=Decimal(str(area.value)),
                            unit=unit,
                            confidence=area.confidence,
                            citation=citation,
                        )
                    )
            if fields.asking_price.value is not None:
                out.append(
                    DocumentConclusion(
                        kind="auction_lot",
                        group_key=lot,
                        field_name="opening_price",
                        value_numeric=fields.asking_price.value,
                        unit="SAR",
                        confidence=fields.asking_price.confidence,
                        citation=citation,
                    )
                )
            out.append(
                DocumentConclusion(
                    kind="auction_lot",
                    group_key=lot,
                    field_name="lot_number",
                    value_text=lot,
                    confidence=0.95,
                    citation=citation,
                )
            )
    return out


def _valuation_conclusions(pages: tuple[str, ...]) -> list[DocumentConclusion]:
    out: list[DocumentConclusion] = []
    for page_index, text in enumerate(pages):
        for match in _MARKET_VALUE.finditer(text):
            amount = Decimal(match.group(1).replace(",", ""))
            if match.group(2):
                amount *= _MULTIPLIER.get(match.group(2).lower(), Decimal(1))
            out.append(
                DocumentConclusion(
                    kind="valuation",
                    field_name="market_value",
                    value_numeric=amount,
                    unit="SAR",
                    confidence=0.9,
                    citation=PageCitation(
                        page=page_index + 1, excerpt=_line_around(text, match.start())
                    ),
                )
            )
    return out


def _term_conclusions(pages: tuple[str, ...]) -> list[DocumentConclusion]:
    out: list[DocumentConclusion] = []
    for page_index, text in enumerate(pages):
        for field_name, pattern in _PERCENT_TERMS:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                out.append(
                    DocumentConclusion(
                        kind="term",
                        field_name=field_name,
                        value_numeric=Decimal(match.group(1)),
                        unit="percent",
                        confidence=0.9,
                        citation=PageCitation(
                            page=page_index + 1, excerpt=_line_around(text, match.start())
                        ),
                    )
                )
    return out


def extract_document_fields(pages: tuple[str, ...]) -> DocumentExtraction:
    """The rule-based document extractor. Regex over redacted page text."""
    doc_class, confidence, evidence = classify(pages)
    conclusions: list[DocumentConclusion] = []
    conclusions.extend(_lot_conclusions(pages))
    conclusions.extend(_valuation_conclusions(pages))
    conclusions.extend(_term_conclusions(pages))
    return DocumentExtraction(
        document_class=doc_class,
        classification_confidence=confidence,
        classification_evidence=evidence,
        conclusions=conclusions,
    )


# ---------------------------------------------------------------------------
# The agent


class DocumentExtractionAgent(Agent[DocumentPayload, DocumentExtraction]):
    """Reads document pages and returns cited conclusions. No tools."""

    name = "document_extraction"
    prompt_version = PROMPT_VERSION
    tier = ModelTier.STANDARD
    output_model = DocumentExtraction
    call_budget_usd = Decimal("0.20")
    uses_tools = False

    def system_prompt(self) -> str:
        return (
            "You read Saudi real-estate documents -- auction brochures, valuation "
            "reports, terms and conditions -- in Arabic or English, and return "
            "structured conclusions. Every conclusion MUST carry the page number "
            "it came from and a verbatim excerpt from that page. A conclusion you "
            "cannot cite must be omitted entirely rather than reported without a "
            "citation. Do not aggregate across pages, do not restate a figure you "
            "cannot quote, and do not clamp implausible numbers. Respond with JSON "
            "matching the schema."
        )

    def user_prompt(self, payload: DocumentPayload) -> str:
        return (
            f"Document {payload.filename!r} has {payload.page_count} pages, supplied "
            "in order in the untrusted block below, separated by the block's own "
            "delimiter. Page numbers are 1-based in that order."
        )

    def untrusted_content(self, payload: DocumentPayload) -> list[str]:
        return list(payload.pages)

    def subject(self, payload: DocumentPayload) -> tuple[str, UUID | None]:
        return ("document", payload.subject_id)

    def input_fingerprint(self, payload: DocumentPayload) -> Any:
        return {
            "content_sha256": payload.content_sha256,
            "page_count": payload.page_count,
            "method_version": METHOD_VERSION,
        }

    def validate_output(
        self, output: DocumentExtraction, payload: DocumentPayload
    ) -> DocumentExtraction:
        """Drop every conclusion whose citation does not resolve. No exceptions.

        This is the layer that makes the product claim true rather than
        aspirational: a page number outside the document, or an excerpt that is
        not actually on the page it names, means the conclusion is discarded and
        the discard is recorded as a quality flag.
        """
        kept: list[DocumentConclusion] = []
        flags = list(output.quality_flags)
        for conclusion in output.conclusions:
            page = conclusion.citation.page
            if page > payload.page_count:
                flags.append(
                    QualityFlag(
                        field_name=conclusion.field_name,
                        code=QualityCode.EVIDENCE_MISMATCH,
                        detail=f"cites page {page} of a {payload.page_count}-page document",
                    )
                )
                continue
            page_text = payload.pages[page - 1]
            excerpt = conclusion.citation.excerpt.strip()
            if _collapse(excerpt) not in _collapse(page_text):
                flags.append(
                    QualityFlag(
                        field_name=conclusion.field_name,
                        code=QualityCode.EVIDENCE_MISMATCH,
                        detail=f"excerpt is not present on page {page}",
                    )
                )
                continue
            kept.append(conclusion)
        return output.model_copy(update={"conclusions": kept, "quality_flags": flags})


def _collapse(text: str) -> str:
    """Whitespace-insensitive containment check for citations.

    PDF text extraction inserts and drops whitespace unpredictably, so an
    excerpt is required to be present modulo whitespace runs -- not byte-exact.
    """
    return re.sub(r"[ \t]+", " ", text)


def deterministic_document_responder(request: LLMRequest) -> str:
    """Offline stand-in for document extraction. **Not a model.**

    Reads the pages from `request.untrusted_blocks`, in order, which is exactly
    what the runtime frames as data, so page numbers line up with what the
    caller supplied.
    """
    return extract_document_fields(tuple(request.untrusted_blocks)).model_dump_json()


# ---------------------------------------------------------------------------
# The pipeline


@dataclass(frozen=True, slots=True)
class DocumentIngestResult:
    document: Document
    created: bool
    extraction: DocumentExtraction | None
    run_id: UUID | None
    stored_conclusions: int = 0

    @property
    def is_duplicate(self) -> bool:
        return not self.created


def ingest_document(
    session: Session,
    *,
    filename: str,
    content: bytes,
    context: AgentContext,
    mime_type: str = "application/pdf",
    pages: list[str] | None = None,
    source_record_id: UUID | None = None,
) -> DocumentIngestResult:
    """accept -> hash -> store -> classify -> extract with citations -> persist.

    Re-ingesting identical content is a no-op: the existing document is
    returned untouched, with no second extraction and no second agent run.
    """
    if not content:
        raise DocumentError("empty document")

    digest = hashlib.sha256(content).hexdigest()
    existing = session.scalar(select(Document).where(Document.content_sha256 == digest))
    if existing is not None:
        return DocumentIngestResult(document=existing, created=False, extraction=None, run_id=None)

    raw_pages = pages if pages is not None else extract_pdf_pages(content)
    if not raw_pages:
        raise DocumentError(f"no page text could be extracted from {filename!r}")

    # PII leaves at the boundary: everything downstream, storage and agent
    # alike, only ever sees the redacted text.
    redacted: list[str] = []
    removed: dict[str, int] = {}
    for text in raw_pages:
        clean, counts = redact(text)
        redacted.append(clean)
        for key, value in counts.items():
            removed[key] = removed.get(key, 0) + value

    payload = DocumentPayload(
        filename=filename,
        content_sha256=digest,
        pages=tuple(redacted),
    )
    doc_class, confidence, evidence = classify(payload.pages)

    document = Document(
        filename=filename,
        mime_type=mime_type,
        content_sha256=digest,
        byte_size=len(content),
        page_count=payload.page_count,
        document_class=doc_class.value,
        classification_confidence=confidence,
        classification_evidence={"citations": [citation.model_dump() for citation in evidence]},
        pii_removed={key: value for key, value in removed.items() if value},
        pages=[
            {"page": index + 1, "text": text, "chars": len(text)}
            for index, text in enumerate(payload.pages)
        ],
        source_record_id=source_record_id,
    )
    session.add(document)
    session.flush()

    result = AgentRuntime(context).run(DocumentExtractionAgent(), payload)
    extraction = result.output

    for conclusion in extraction.conclusions:
        session.add(
            DocumentExtractionRow(
                document_id=document.id,
                agent_run_id=result.run_id,
                kind=conclusion.kind,
                group_key=conclusion.group_key,
                field_name=conclusion.field_name,
                value_text=conclusion.value_text,
                value_numeric=conclusion.value_numeric,
                unit=conclusion.unit,
                confidence=conclusion.confidence,
                page_number=conclusion.citation.page,
                excerpt=conclusion.citation.excerpt,
            )
        )
    session.flush()

    return DocumentIngestResult(
        document=document,
        created=True,
        extraction=extraction,
        run_id=result.run_id,
        stored_conclusions=len(extraction.conclusions),
    )
