"""Readable text from fetched menu documents."""

import pymupdf

from menudb.normalize import html_to_text, tidy

# Below this many characters per page, treat a PDF as scanned/image-only.
_MIN_CHARS_PER_PAGE = 40


def pdf_text(data: bytes) -> str:
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return tidy("\n".join(page.get_text() for page in doc))


def is_image_only_pdf(data: bytes) -> bool:
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        chars = sum(len(page.get_text().strip()) for page in doc)
        return chars < _MIN_CHARS_PER_PAGE * max(doc.page_count, 1)


def document_kind(content_type: str, url: str, data: bytes) -> str:
    """html | pdf | image"""
    if content_type == "application/pdf" or data[:5] == b"%PDF-" or url.lower().endswith(".pdf"):
        return "pdf"
    if content_type.startswith("image/"):
        return "image"
    return "html"


def document_text(kind: str, data: bytes) -> str | None:
    """Text for hashing/extraction, or None when the document has no text layer
    (image-only PDFs and images go to vision extraction and hash by raw bytes)."""
    if kind == "html":
        return html_to_text(data.decode(errors="replace"))
    if kind == "pdf" and not is_image_only_pdf(data):
        return pdf_text(data)
    return None
