"""Document-intelligence tables.

Discovered automatically by `model_modules.load_all()`, so adding this feature
does not touch `models.py` -- and does not contend with any other feature
adding tables at the same time.

Two invariants are enforced by the database rather than by convention, for the
same reason `verification_checks` enforces its evidence requirement there:

  * **immutable storage.** `documents.content_sha256` is unique, so the same
    bytes cannot be stored twice and a stored document cannot be quietly
    replaced by different content under the same identity.
  * **no conclusion without a citation.** A row in `document_extractions` must
    carry a page number of at least 1 and a non-empty excerpt. A conclusion
    that cannot say which page it came from is not storable, so it cannot
    reach a user.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from sreoi_persistence.models import Base


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


def _now() -> datetime:
    return datetime.now(UTC)


class Document(Base):
    """One ingested document. Content-addressed and never updated in place.

    `pages` holds the per-page text *after* PII redaction, which is the only
    form of the text that exists here: the un-redacted bytes are hashed for
    identity and then not stored as text at all.
    """

    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("content_sha256", name="uq_documents_content_sha256"),
        CheckConstraint("page_count >= 1", name="ck_documents_page_count_positive"),
        CheckConstraint("byte_size > 0", name="ck_documents_byte_size_positive"),
        Index("ix_documents_class", "document_class"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    filename: Mapped[str] = mapped_column(String(260))
    mime_type: Mapped[str] = mapped_column(String(120))
    content_sha256: Mapped[str] = mapped_column(String(64))
    byte_size: Mapped[int] = mapped_column(Integer)
    page_count: Mapped[int] = mapped_column(Integer)
    document_class: Mapped[str] = mapped_column(String(32))
    classification_confidence: Mapped[float] = mapped_column(Numeric(5, 4), default=0)
    classification_evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # {"phone": 2, "email": 1, ...} -- what redaction removed, kept for audit.
    pii_removed: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    pages: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    source_record_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("source_records.id", name="fk_documents_source_record_id"), nullable=True
    )
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    extractions: Mapped[list[DocumentExtractionRow]] = relationship(
        back_populates="document", order_by="DocumentExtractionRow.page_number"
    )

    def page_text(self, page: int) -> str | None:
        for entry in self.pages:
            if int(entry.get("page", 0)) == page:
                text = entry.get("text")
                return str(text) if text is not None else None
        return None


class DocumentExtractionRow(Base):
    """One conclusion drawn from one page, with the excerpt that supports it."""

    __tablename__ = "document_extractions"
    __table_args__ = (
        # The product claim is "every conclusion traces to a page". These two
        # constraints are that claim, made unbypassable.
        CheckConstraint("page_number >= 1", name="ck_document_extractions_page_cited"),
        CheckConstraint("length(btrim(excerpt)) > 0", name="ck_document_extractions_excerpt"),
        Index("ix_document_extractions_doc", "document_id", "kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", name="fk_document_extractions_document_id")
    )
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_runs.id", name="fk_document_extractions_agent_run_id"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(40))  # auction_lot | valuation | term | ...
    group_key: Mapped[str | None] = mapped_column(String(60), nullable=True)  # e.g. lot number
    field_name: Mapped[str] = mapped_column(String(60))
    value_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    value_numeric: Mapped[Decimal | None] = mapped_column(Numeric(16, 4), nullable=True)
    unit: Mapped[str | None] = mapped_column(String(16), nullable=True)
    confidence: Mapped[float] = mapped_column(Numeric(5, 4), default=0)
    page_number: Mapped[int] = mapped_column(Integer)
    excerpt: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    document: Mapped[Document] = relationship(back_populates="extractions")
