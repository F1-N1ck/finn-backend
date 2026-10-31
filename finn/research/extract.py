"""PDF text extraction with page numbers and explicit failure states.

Page numbers are PDF page indexes (1 = first page of the file), NOT printed page numbers.
No OCR: a scanned PDF is reported as scanned_no_text instead of guessed at.
"""

import re
import time

MIN_TEXT_CHARS = 30


def _pdf_date(raw):
    if not raw:
        return None
    match = re.match(r"D:(\d{4})(\d{2})(\d{2})", raw)
    if match:
        return match.group(1) + "-" + match.group(2) + "-" + match.group(3)
    return None


def extract_pdf(data, max_pages=600, budget_s=90.0):
    """Return {"state", "pages", "page_count", ...}. Never raises.

    state: ok | partial | scanned_no_text | encrypted | malformed_pdf | empty | too_slow
    partial_reason (when partial): time_cap | page_cap | unreadable_pages
    """
    started = time.monotonic()
    info = {"state": "malformed_pdf", "pages": [], "page_count": 0, "pages_extracted": 0,
            "pdf_title": None, "pdf_created": None, "partial_reason": None, "unreadable_pages": 0}
    if b"%PDF-" not in data[:1024]:
        return info  # not a PDF at all (pymupdf would happily open HTML or images)
    try:
        import pymupdf  # imported here so the API still starts if the wheel is missing
    except Exception:
        info["state"] = "extractor_unavailable"
        return info
    try:
        document = pymupdf.open(stream=data, filetype="pdf")
    except Exception:
        info["seconds"] = round(time.monotonic() - started, 2)
        return info
    try:
        if document.needs_pass:
            info["state"] = "encrypted"
            return info
        info["page_count"] = document.page_count
        metadata = document.metadata or {}
        info["pdf_title"] = metadata.get("title") or None
        info["pdf_created"] = _pdf_date(metadata.get("creationDate"))
    except Exception:
        info["state"] = "malformed_pdf"
        return info

    limit = info["page_count"]
    if limit > max_pages:
        limit = max_pages
        info["partial_reason"] = "page_cap"
    pages = []
    with_text = 0
    for index in range(limit):
        if time.monotonic() - started > budget_s:
            info["partial_reason"] = "time_cap"
            break
        try:
            text = document[index].get_text("text")
        except Exception:
            info["unreadable_pages"] = info["unreadable_pages"] + 1
            continue
        pages.append({"page": index + 1, "text": text})
        if len(text.strip()) >= MIN_TEXT_CHARS:
            with_text = with_text + 1
    try:
        document.close()
    except Exception:
        pass
    info["pages"] = pages
    info["pages_extracted"] = len(pages)
    info["seconds"] = round(time.monotonic() - started, 2)

    if info["page_count"] == 0:
        info["state"] = "empty"
        return info
    if len(pages) == 0:
        info["state"] = "too_slow" if info["partial_reason"] == "time_cap" else "malformed_pdf"
        return info
    if with_text / float(len(pages)) < 0.2:
        info["state"] = "scanned_no_text"
        return info
    if info["partial_reason"] is None and info["unreadable_pages"] > 0:
        info["partial_reason"] = "unreadable_pages"
    info["state"] = "partial" if info["partial_reason"] else "ok"
    return info
