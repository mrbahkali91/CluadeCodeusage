"""Extraction and document-intelligence endpoints.

Discovered automatically by `routers.discover()`, so this feature registers
itself without editing `main.py`.

Two contracts are held to deliberately:

  * **evidence travels with the value.** Every extracted field serialises as
    `{value, confidence, evidence_span, excerpt}`, and the canonical text the
    spans index is returned alongside them, so a caller can verify a citation
    instead of trusting it. Money fields additionally carry the
    `ProvenancedMoney` envelope used everywhere else in the API (ADR-007), so
    an unattributed price cannot leave this router either.
  * **the provider is named on every response.** `provider` is
    `"deterministic-offline"` here because no model is called. It is reported
    rather than omitted, so a client can never mistake a rule-based extraction
    for model reasoning.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from collections.abc import Iterator
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from sreoi_agents.documents import (
    DocumentError,
    DocumentIngestResult,
    deterministic_document_responder,
    ingest_document,
)
from sreoi_agents.extraction import (
    EvidencedField,
    MoneyField,
    deterministic_extraction_responder,
    extract_listing,
)
from sreoi_agents.provider import DeterministicProvider
from sreoi_agents.runtime import AgentContext, AgentError
from sreoi_api.i18n import register_strings
from sreoi_api.schemas import ProvenancedMoney
from sreoi_persistence.db import get_session_factory
from sreoi_persistence.models_documents import Document, DocumentExtractionRow

API_PREFIX = "/api/v1"
MAX_TEXT_CHARS = 20_000
MAX_DOCUMENT_BYTES = 20 * 1024 * 1024

MONEY_FIELDS = ("asking_price", "seller_payment", "remaining_installments")

router = APIRouter(prefix=API_PREFIX, tags=["extraction"])


# ---------------------------------------------------------------- i18n

register_strings(
    "en",
    {
        "extract.title": "Listing extraction",
        "extract.field": "Field",
        "extract.value": "Value",
        "extract.confidence": "Confidence",
        "extract.evidence": "Evidence span",
        "extract.excerpt": "Excerpt",
        "extract.signals": "Signals",
        "extract.flags": "Quality flags",
        "extract.no_evidence": "No supporting text — left unset rather than inferred",
        "extract.out_of_range": "Out of range — rejected, not clamped",
        "extract.injection_flagged": "Possible prompt injection detected in this text",
        "extract.provider_offline": "Extracted by deterministic rules; no model was called",
        "doc.title": "Documents",
        "doc.class": "Document type",
        "doc.pages": "Pages",
        "doc.duplicate": "Identical content already stored — no new document created",
        "doc.citation": "Citation",
        "doc.page": "Page",
        "doc.lot": "Lot",
        "doc.conclusions": "Cited conclusions",
        "docclass.AUCTION_BROCHURE": "Auction brochure",
        "docclass.VALUATION_REPORT": "Valuation report",
        "docclass.TERMS_AND_CONDITIONS": "Terms and conditions",
        "docclass.OTHER": "Other",
    },
)
register_strings(
    "ar",
    {
        "extract.title": "استخراج بيانات الإعلان",
        "extract.field": "الحقل",
        "extract.value": "القيمة",
        "extract.confidence": "الثقة",
        "extract.evidence": "موضع الدليل",
        "extract.excerpt": "المقتطف",
        "extract.signals": "الإشارات",
        "extract.flags": "ملاحظات الجودة",
        "extract.no_evidence": "لا يوجد نص مؤيد — تُترك فارغة ولا تُستنتج",
        "extract.out_of_range": "خارج النطاق — مرفوضة وغير معدّلة",
        "extract.injection_flagged": "احتمال وجود محاولة تلاعب بالتعليمات في هذا النص",
        "extract.provider_offline": "استُخرجت بقواعد حتمية؛ لم يُستخدم أي نموذج",
        "doc.title": "المستندات",
        "doc.class": "نوع المستند",
        "doc.pages": "الصفحات",
        "doc.duplicate": "المحتوى مخزّن مسبقًا — لم يُنشأ مستند جديد",
        "doc.citation": "الاستشهاد",
        "doc.page": "صفحة",
        "doc.lot": "الأصل",
        "doc.conclusions": "النتائج الموثّقة",
        "docclass.AUCTION_BROCHURE": "كراسة مزاد",
        "docclass.VALUATION_REPORT": "تقرير تقييم",
        "docclass.TERMS_AND_CONDITIONS": "الشروط والأحكام",
        "docclass.OTHER": "أخرى",
    },
)


# ---------------------------------------------------------------- plumbing


def get_session() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


SessionDep = Annotated[Session, Depends(get_session)]


def _listing_context(session: Session) -> AgentContext:
    return AgentContext(
        session=session, provider=DeterministicProvider(deterministic_extraction_responder)
    )


def _document_context(session: Session) -> AgentContext:
    return AgentContext(
        session=session, provider=DeterministicProvider(deterministic_document_responder)
    )


# ---------------------------------------------------------------- schemas


class ExtractedFieldOut(BaseModel):
    """A value that carries its own justification, or no value at all."""

    value: Any | None = None
    confidence: float
    evidence_span: tuple[int, int] | None = None
    excerpt: str | None = None


class ProvenancedMoneyOut(ProvenancedMoney):
    """The standard money envelope, plus the span it was read from."""

    evidence_span: tuple[int, int] | None = None
    excerpt: str | None = None


class SignalOut(BaseModel):
    tag: str
    confidence: float
    evidence_span: tuple[int, int]
    excerpt: str


class QualityFlagOut(BaseModel):
    field_name: str
    code: str
    detail: str


class ListingExtractionIn(BaseModel):
    text: str = Field(min_length=1)
    language_hint: str | None = None


class ListingExtractionOut(BaseModel):
    agent_run_id: uuid.UUID
    provider: str
    provider_is_model: bool = Field(
        description="False whenever extraction came from deterministic rules"
    )
    cached: bool
    method_version: str
    language: str
    text_sha256: str
    # Returned so a caller can resolve every evidence_span itself. Spans index
    # this string, not the raw submission: redaction changes lengths.
    canonical_text: str
    pii_removed: dict[str, int]
    injection_flagged: bool
    injection_summary: str
    fields: dict[str, ExtractedFieldOut]
    money: dict[str, ProvenancedMoneyOut]
    signals: list[SignalOut]
    quality_flags: list[QualityFlagOut]


class DocumentIn(BaseModel):
    filename: str = Field(min_length=1, max_length=260)
    content_base64: str = Field(min_length=1)
    mime_type: str = "application/pdf"
    # Pre-extracted page text, for callers that already have it (or for plain
    # text). The bytes are still what the document is identified by.
    pages: list[str] | None = None


class DocumentOut(BaseModel):
    id: uuid.UUID
    filename: str
    mime_type: str
    content_sha256: str
    byte_size: int
    page_count: int
    document_class: str
    classification_confidence: float
    pii_removed: dict[str, int]
    duplicate: bool
    stored_conclusions: int
    agent_run_id: uuid.UUID | None
    provider: str


class CitationOut(BaseModel):
    page: int
    excerpt: str


class DocumentConclusionOut(BaseModel):
    kind: str
    group_key: str | None
    field_name: str
    value_text: str | None
    value_numeric: Decimal | None
    unit: str | None
    confidence: float
    citation: CitationOut
    money: ProvenancedMoney | None = None


class DocumentExtractionsOut(BaseModel):
    document_id: uuid.UUID
    document_class: str
    page_count: int
    conclusions: list[DocumentConclusionOut]
    # Auction lots, keyed by lot number, so the client does not have to regroup.
    lots: dict[str, dict[str, DocumentConclusionOut]]


# ---------------------------------------------------------------- serialisation


def _field_out(extracted: EvidencedField) -> ExtractedFieldOut:
    return ExtractedFieldOut(
        value=getattr(extracted, "value", None),
        confidence=extracted.confidence,
        evidence_span=extracted.evidence_span,
        excerpt=extracted.excerpt,
    )


def _money_out(money: MoneyField) -> ProvenancedMoneyOut:
    """Money always leaves with a basis. An unknown amount says so."""
    span = money.evidence_span
    return ProvenancedMoneyOut(
        value=money.value,
        unit=money.currency,
        basis="ACTUAL" if money.value is not None else "UNKNOWN",
        confidence=money.confidence,
        sources=(
            [f"listing_text[{span[0]}:{span[1]}]"]
            if money.value is not None and span is not None
            else []
        ),
        evidence_span=span,
        excerpt=money.excerpt,
    )


def _conclusion_out(row: DocumentExtractionRow, filename: str) -> DocumentConclusionOut:
    money: ProvenancedMoney | None = None
    if row.unit == "SAR":
        money = ProvenancedMoney(
            value=row.value_numeric,
            unit="SAR",
            basis="ACTUAL" if row.value_numeric is not None else "UNKNOWN",
            confidence=float(row.confidence),
            sources=[f"{filename} p.{row.page_number}"],
        )
    return DocumentConclusionOut(
        kind=row.kind,
        group_key=row.group_key,
        field_name=row.field_name,
        value_text=row.value_text,
        value_numeric=row.value_numeric,
        unit=row.unit,
        confidence=float(row.confidence),
        citation=CitationOut(page=row.page_number, excerpt=row.excerpt),
        money=money,
    )


def _document_out(result: DocumentIngestResult, provider: str) -> DocumentOut:
    document = result.document
    return DocumentOut(
        id=document.id,
        filename=document.filename,
        mime_type=document.mime_type,
        content_sha256=document.content_sha256,
        byte_size=document.byte_size,
        page_count=document.page_count,
        document_class=document.document_class,
        classification_confidence=float(document.classification_confidence),
        pii_removed={str(k): int(v) for k, v in (document.pii_removed or {}).items()},
        duplicate=result.is_duplicate,
        stored_conclusions=result.stored_conclusions,
        agent_run_id=result.run_id,
        provider=provider,
    )


# ---------------------------------------------------------------- endpoints


@router.post("/extraction/listing", response_model=ListingExtractionOut)
def extract_listing_endpoint(
    payload: ListingExtractionIn, session: SessionDep
) -> ListingExtractionOut:
    """Raw listing text in, structured fields with evidence out."""
    if len(payload.text) > MAX_TEXT_CHARS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"text exceeds {MAX_TEXT_CHARS} characters",
        )
    context = _listing_context(session)
    try:
        result, canonical = extract_listing(context, payload.text)
    except AgentError as exc:
        # Two schema failures is a real failure, not something to paper over.
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    extraction = result.output
    scalars = extraction.scalar_fields()
    return ListingExtractionOut(
        agent_run_id=result.run_id,
        provider=result.provider,
        provider_is_model=False,
        cached=result.cached,
        method_version=extraction.method_version,
        language=extraction.language,
        text_sha256=canonical.raw_sha256,
        canonical_text=canonical.text,
        pii_removed=dict(canonical.pii_removed),
        injection_flagged=result.injection_flagged,
        injection_summary=result.injection.summary,
        fields={
            name: _field_out(value) for name, value in scalars.items() if name not in MONEY_FIELDS
        },
        money={name: _money_out(getattr(extraction, name)) for name in MONEY_FIELDS},
        signals=[
            SignalOut(
                tag=signal.tag,
                confidence=signal.confidence,
                evidence_span=signal.evidence_span,
                excerpt=signal.excerpt,
            )
            for signal in extraction.signals
        ],
        quality_flags=[
            QualityFlagOut(field_name=flag.field_name, code=flag.code.value, detail=flag.detail)
            for flag in extraction.quality_flags
        ],
    )


@router.post("/documents", response_model=DocumentOut, status_code=status.HTTP_201_CREATED)
def create_document(payload: DocumentIn, session: SessionDep) -> DocumentOut:
    """Ingest a document. Identical content returns the existing record."""
    try:
        content = base64.b64decode(payload.content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "content_base64 is not valid base64"
        ) from exc
    if not content:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "empty document")
    if len(content) > MAX_DOCUMENT_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"document exceeds {MAX_DOCUMENT_BYTES} bytes",
        )

    pages = payload.pages
    if pages is None and payload.mime_type.startswith("text/"):
        pages = [content.decode("utf-8", errors="replace")]

    context = _document_context(session)
    try:
        result = ingest_document(
            session,
            filename=payload.filename,
            content=content,
            context=context,
            mime_type=payload.mime_type,
            pages=pages,
        )
    except DocumentError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except AgentError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    return _document_out(result, context.provider.name)


@router.get("/documents/{document_id}/extractions", response_model=DocumentExtractionsOut)
def document_extractions(document_id: uuid.UUID, session: SessionDep) -> DocumentExtractionsOut:
    """Every stored conclusion, each with the page and excerpt it came from."""
    document = session.get(Document, document_id)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")

    rows = session.scalars(
        select(DocumentExtractionRow)
        .where(DocumentExtractionRow.document_id == document_id)
        .order_by(
            DocumentExtractionRow.page_number,
            DocumentExtractionRow.group_key,
            DocumentExtractionRow.field_name,
        )
    ).all()

    conclusions = [_conclusion_out(row, document.filename) for row in rows]
    lots: dict[str, dict[str, DocumentConclusionOut]] = {}
    for conclusion in conclusions:
        if conclusion.kind == "auction_lot" and conclusion.group_key is not None:
            lots.setdefault(conclusion.group_key, {})[conclusion.field_name] = conclusion

    return DocumentExtractionsOut(
        document_id=document.id,
        document_class=document.document_class,
        page_count=document.page_count,
        conclusions=conclusions,
        lots=lots,
    )
