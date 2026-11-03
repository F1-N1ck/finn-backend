"""PDF text extraction with page numbers and explicit failure states.

Page numbers are PDF page indexes (1 = first page of the file), NOT printed page numbers.
No OCR: a scanned PDF is reported as scanned_no_text instead of guessed at.
"""

import re
import time

MIN_TEXT_CHARS = 30


def rss_mb():
    """Resident memory of this process in MB (Linux only; None elsewhere)."""
    try:
        with open("/proc/self/status") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024.0, 1)
    except Exception:
        pass
    return None


LIGATURES = {"\ufb01": "fi", "\ufb02": "fl", "\ufb00": "ff", "\ufb03": "ffi", "\ufb04": "ffl", "\ufb05": "st", "\ufb06": "st"}


def fix_ligatures(text):
    """PDF fonts often store 'fi', 'fl' as one ligature character, so 'Profit' is read as 'Proﬁt' and no search for
    'profit' finds it. Spell them out."""
    for ligature, plain in LIGATURES.items():
        if ligature in text:
            text = text.replace(ligature, plain)
    return text


def page_text(page):
    return fix_ligatures(_page_text_raw(page))


def _page_text_raw(page):
    """Page text. Annual reports often draw the rupee sign with a special font whose character code is
    'H' or a backtick; on pages that use such a font, that character is replaced with the real rupee sign."""
    try:
        names = " ".join(str(font[3]) for font in page.get_fonts()).lower()
    except Exception:
        names = ""
    if "rupee" not in names:
        return page.get_text("text")
    lines = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            parts = []
            for span in line["spans"]:
                text = span["text"]
                if "rupee" in str(span.get("font", "")).lower() and text.strip() in ("H", "`"):
                    text = text.replace(text.strip(), "₹")
                parts.append(text)
            lines.append("".join(parts))
    return "\n".join(lines) + "\n"


def _pdf_date(raw):
    if not raw:
        return None
    match = re.match(r"D:(\d{4})(\d{2})(\d{2})", raw)
    if match:
        return match.group(1) + "-" + match.group(2) + "-" + match.group(3)
    return None


def extract_pdf(data, max_pages=600, budget_s=90.0, max_rss_mb=None):
    """Return {"state", "pages", "page_count", ...}. Never raises.

    state: ok | partial | scanned_no_text | encrypted | malformed_pdf | empty | too_slow
    partial_reason (when partial): time_cap | page_cap | memory_cap | unreadable_pages
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
        if max_rss_mb and index % 25 == 0:
            current = rss_mb()
            if current is not None and current > max_rss_mb:
                info["partial_reason"] = "memory_cap"
                break
        try:
            text = page_text(document[index])
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
    info["rss_mb_after"] = rss_mb()
    info["pages_extracted"] = len(pages)
    info["seconds"] = round(time.monotonic() - started, 2)

    if info["page_count"] == 0:
        info["state"] = "empty"
        return info
    if len(pages) == 0:
        info["state"] = "too_slow" if info["partial_reason"] in ("time_cap", "memory_cap") else "malformed_pdf"
        return info
    if with_text / float(len(pages)) < 0.2:
        info["state"] = "scanned_no_text"
        return info
    if info["partial_reason"] is None and info["unreadable_pages"] > 0:
        info["partial_reason"] = "unreadable_pages"
    info["state"] = "partial" if info["partial_reason"] else "ok"
    return info
