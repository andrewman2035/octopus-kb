"""PDF text extraction with an OCR fallback for scanned documents."""
from __future__ import annotations

import unicodedata
from pathlib import Path
from typing import Any, Callable

from pypdf import PdfReader

from octopus.i18n import t
from octopus.media import MediaError
from octopus.media.ocr import ocr_image


MIN_CHARS_PER_PAGE = 20
MAX_OCR_PAGES = 200


def pdf_text(
    path: Path,
    ocr: Callable[[Any], str] = ocr_image,
    min_chars_per_page: int = MIN_CHARS_PER_PAGE,
    max_ocr_pages: int = MAX_OCR_PAGES,
) -> str:
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted and not reader.decrypt(""):
            raise MediaError(t("media.pdf_password"))
        pages = [(p.extract_text() or "").strip() for p in reader.pages]
    except MediaError:
        raise
    except Exception as e:
        raise MediaError(t("media.pdf_failed", error=e)) from e
    needs_ocr = [i for i, page in enumerate(pages) if len(page) < min_chars_per_page]
    skipped = max(0, len(needs_ocr) - max_ocr_pages)
    if needs_ocr:
        doc = None
        try:
            import pypdfium2 as pdfium

            doc = pdfium.PdfDocument(str(path))
            for i in needs_ocr[:max_ocr_pages]:
                ocred = ocr(doc[i].render(scale=2).to_pil()).strip()
                if ocred:
                    pages[i] = ocred
        except MediaError:
            raise
        except Exception as e:
            raise MediaError(t("media.pdf_ocr_failed", error=e)) from e
        finally:
            if doc is not None:
                doc.close()
    text = unicodedata.normalize("NFKC", "\n\n".join(page for page in pages if page))
    if skipped:  # appended after NFKC so the full-width brackets survive
        text += "\n\n" + t("note.pdf_skipped", n=skipped)
    return text
