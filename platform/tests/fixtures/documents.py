"""A generated multi-page Arabic auction brochure, and its labels.

The PDF is built at test time rather than committed as a binary so the expected
lot data and the bytes that produce it cannot drift apart. DejaVu Sans is used
because it is the only font in this image with Arabic coverage; the visual
shaping is not what is under test -- the extracted text and its page numbers
are.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

ARABIC_FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")

PAGE_1 = (
    "كراسة المزاد العلني",
    "شركة المزادات العقارية - الرياض",
    "جدول الأصول المعروضة للبيع بالمزاد العلني",
    "تاريخ المزاد: 12/03/1446هـ",
    "للاستفسار: 0551234567",
)

PAGE_2 = (
    "جدول الأصول - صفحة 1 من 2",
    "رقم الأصل: 1 - شقة في حي النرجس، المساحة 145 م2، السعر الافتتاحي 850,000 ريال",
    "رقم الأصل: 2 - فيلا في حي الياسمين، مساحة الأرض 500 م2، السعر الافتتاحي 2,750,000 ريال",
)

PAGE_3 = (
    "جدول الأصول - صفحة 2 من 2",
    "رقم الأصل: 3 - أرض سكنية في حي القيروان، المساحة 620 م2، السعر الافتتاحي 1,900,000 ريال",
    "الشروط والأحكام: العربون 5% من قيمة السوم، عمولة 2.5% على المشتري",
)

PAGES: tuple[tuple[str, ...], ...] = (PAGE_1, PAGE_2, PAGE_3)


@dataclass(frozen=True, slots=True)
class ExpectedLot:
    lot_number: str
    page: int
    property_class: str
    district: str
    area_field: str
    area_sqm: Decimal
    opening_price: Decimal


EXPECTED_LOTS: tuple[ExpectedLot, ...] = (
    ExpectedLot("1", 2, "APARTMENT", "النرجس", "area_sqm", Decimal("145"), Decimal("850000")),
    ExpectedLot("2", 2, "VILLA", "الياسمين", "land_area_sqm", Decimal("500"), Decimal("2750000")),
    ExpectedLot(
        "3",
        3,
        "RESIDENTIAL_PLOT",
        "القيروان",
        "area_sqm",
        Decimal("620"),
        Decimal("1900000"),
    ),
)


def build_auction_pdf() -> bytes:
    """A three-page brochure: cover, two lot-schedule pages, terms on the last."""
    from fpdf import FPDF

    pdf = FPDF(orientation="L", format="A4")
    pdf.set_auto_page_break(auto=False)
    pdf.add_font("arabic", "", str(ARABIC_FONT))
    for page in PAGES:
        pdf.add_page()
        pdf.set_font("arabic", size=10)
        for line in page:
            # One physical line per logical line: a wrapped lot row would split
            # the price away from its lot anchor, which is a property of the
            # fixture, not of the extractor.
            pdf.cell(0, 8, line, new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())
