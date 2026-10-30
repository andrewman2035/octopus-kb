import sys
from pathlib import Path

import pytest

from octopus.media import MediaError
from octopus.media.pdf import pdf_text

FIX = Path(__file__).parent / "fixtures"
FONT = Path("/System/Library/Fonts/STHeiti Medium.ttc")
needs_vision = pytest.mark.skipif(
    sys.platform != "darwin" or not FONT.exists(), reason="needs macOS Vision and a CJK font"
)


def build_pdf(path, *pages):
    """Merge pages: each is a pypdf page object."""
    from pypdf import PdfWriter

    w = PdfWriter()
    for page in pages:
        w.add_page(page)
    with open(path, "wb") as f:
        w.write(f)
    return path


def text_page():
    from pypdf import PdfReader

    return PdfReader(str(FIX / "text.pdf")).pages[0]


def image_page(tmp_path, name="img", image=None):
    from pypdf import PdfReader
    from PIL import Image

    p = tmp_path / f"{name}.pdf"
    (image or Image.new("RGB", (400, 300), "white")).save(p)
    return PdfReader(str(p)).pages[0]


def test_text_pdf_without_ocr():
    calls = []
    text = pdf_text(FIX / "text.pdf", ocr=lambda img: calls.append(img) or "OCR")
    assert text == "Compound interest needs time and patience."
    assert calls == []


def test_scanned_pdf_falls_back_to_ocr(tmp_path):
    from PIL import Image

    path = tmp_path / "scan.pdf"
    Image.new("RGB", (400, 300), "white").save(path)
    pages = []
    text = pdf_text(path, ocr=lambda img: pages.append(img.size) or "扫描页文字")
    assert text == "扫描页文字"
    assert len(pages) == 1


def test_mixed_pdf_ocrs_only_the_image_page(tmp_path):
    path = build_pdf(tmp_path / "mixed.pdf", text_page(), image_page(tmp_path))
    pages = []
    text = pdf_text(path, ocr=lambda img: pages.append(img.size) or "扫描页文字")
    assert text == "Compound interest needs time and patience.\n\n扫描页文字"
    assert len(pages) == 1


def test_short_multipage_text_pdf_is_not_ocred(tmp_path):
    path = build_pdf(tmp_path / "two.pdf", text_page(), text_page())
    calls = []
    text = pdf_text(path, ocr=lambda img: calls.append(img) or "OCR")
    assert text == "Compound interest needs time and patience.\n\nCompound interest needs time and patience."
    assert calls == []


def test_ocr_text_is_nfkc_normalised(tmp_path):
    path = build_pdf(tmp_path / "scan.pdf", image_page(tmp_path))
    assert pdf_text(path, ocr=lambda img: "耐⼼") == "耐心"


def test_ocr_page_cap(tmp_path):
    path = build_pdf(tmp_path / "scan3.pdf", *[image_page(tmp_path, f"p{i}") for i in range(3)])
    calls = []
    text = pdf_text(path, ocr=lambda img: calls.append(1) or "页", max_ocr_pages=2)
    assert len(calls) == 2
    assert text == "页\n\n页\n\n（后面还有 1 页没有识别）"


def test_pdfium_document_closed(tmp_path, monkeypatch):
    import pypdfium2 as pdfium

    closed = []
    orig = pdfium.PdfDocument.close
    monkeypatch.setattr(pdfium.PdfDocument, "close", lambda self: closed.append(1) or orig(self))
    path = build_pdf(tmp_path / "scan.pdf", image_page(tmp_path))
    pdf_text(path, ocr=lambda img: "x")
    assert closed == [1]


@needs_vision
def test_real_ocr_on_image_only_pdf(tmp_path):
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (900, 200), "white")
    ImageDraw.Draw(img).text((20, 40), "复利需要时间和耐心", fill="black", font=ImageFont.truetype(str(FONT), 56))
    path = tmp_path / "scan.pdf"
    img.save(path)
    assert "复利需要时间和耐心" in pdf_text(path)


def test_nfkc_normalisation(monkeypatch):
    class FakePage:
        def extract_text(self):
            return "耐⼼" * 30  # Kangxi radical U+2F3C

    class FakeReader:
        is_encrypted = False
        pages = [FakePage()]

        def __init__(self, path):
            pass

    monkeypatch.setattr("octopus.media.pdf.PdfReader", FakeReader)
    assert pdf_text(Path("x.pdf")) == "耐心" * 30


def test_broken_pdf(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf")
    with pytest.raises(MediaError, match="PDF"):
        pdf_text(bad, ocr=lambda img: "")
