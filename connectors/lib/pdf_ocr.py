"""
PDF text extraction and OCR utilities.

Generic — no connector or domain knowledge.

``extract_text_from_pdf`` is the preferred entry point: it tries pdfplumber
(fast, no system dependencies) and falls back to tesseract OCR only when the
PDF has no usable text layer.
"""

from __future__ import annotations

import io
import threading
import time
from typing import Callable

# PSM 4: single column of text of variable sizes. A certificate is one
# column of numbered fields, not a single uniform block (PSM 6) and not a
# newspaper (PSM 3).
_OCR_CONFIG = "--psm 4"
_DEFAULT_DPI = 300
_PDFPLUMBER_MIN_CHARS = 80  # below this, assume no real text layer and fall back to OCR


class RateLimiter:
    """Process-wide minimum interval between acquisitions.

    Sleep happens outside the lock so one slow waiter does not block the
    bookkeeping, but the next-allowed timestamp is reserved under the lock.
    ``workers`` therefore cannot multiply the request rate: four threads
    with ``sleep_s=1`` still issue one download per second.
    """

    def __init__(
        self,
        min_interval_s: float,
        *,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self.min_interval_s = max(0.0, float(min_interval_s))
        self._clock = clock or time.monotonic
        self._sleep = sleeper or time.sleep
        self._lock = threading.Lock()
        self._next = 0.0

    def acquire(self) -> None:
        if self.min_interval_s <= 0:
            return
        with self._lock:
            now = self._clock()
            wait = self._next - now
            if wait < 0:
                wait = 0.0
            base = now if wait == 0 else self._next
            self._next = base + self.min_interval_s
        if wait > 0:
            self._sleep(wait)


def _extract_text_pdfplumber(pdf_bytes: bytes) -> str:
    """Extract text layer from PDF using pdfplumber. Returns empty string if
    pdfplumber is unavailable or the PDF has no text layer worth using."""
    try:
        import pdfplumber
    except ImportError:
        return ""
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            pages = [page.extract_text() or "" for page in pdf.pages]
        return "\n\n".join(pages).strip()
    except Exception:
        return ""


def ocr_pdf(pdf_bytes: bytes, *, dpi: int = _DEFAULT_DPI) -> str:
    """Render every page and OCR it. Page 1 usually holds fields 1–8; later
    pages are still read because filings do not share a page count.

    Imports pdf2image and pytesseract lazily so unit tests of the parser
    do not require a tesseract binary or poppler.
    """
    try:
        from pdf2image import convert_from_bytes
        import pytesseract
    except ImportError as exc:
        raise RuntimeError(
            "OCR dependencies are not installed (pdf2image, pytesseract) "
            "or poppler/tesseract is missing from the image"
        ) from exc

    images = convert_from_bytes(pdf_bytes, dpi=dpi)
    if not images:
        return ""
    pages = []
    for image in images:
        page_text = pytesseract.image_to_string(
            image, lang="eng", config=_OCR_CONFIG,
        )
        pages.append(page_text or "")
    return "\n\n".join(pages).strip()


def extract_text_from_pdf(pdf_bytes: bytes, *, dpi: int = _DEFAULT_DPI) -> str:
    """Try pdfplumber first; fall back to OCR when the text layer is sparse."""
    extracted = _extract_text_pdfplumber(pdf_bytes)
    if len(extracted) >= _PDFPLUMBER_MIN_CHARS:
        return extracted
    return ocr_pdf(pdf_bytes, dpi=dpi)
