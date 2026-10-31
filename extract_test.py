# Step 2: text extraction test on one real annual report and one real concall transcript.
# Downloads two PDFs from BSE, extracts text page by page, flags weak pages.
# Output: results/02_extraction.json and data/text/<id>.pages.jsonl

import json
import os
import string
import time
from collections import Counter

import pymupdf
import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "*/*",
}

# Both URLs came from screener.in's KPITTECH Documents section and returned
# HTTP 200 with %PDF magic bytes in the browser test. Content type of each
# (transcript vs slides) is NOT yet confirmed, so the first page text is recorded.
DOCS = [
    {
        "id": "KPIT_CONCALL_AUG2026",
        "kind": "concall",
        "url": "https://www.bseindia.com/stockinfo/AnnPdfOpen.aspx?Pname=d07f9c52-5c5a-443b-b239-6acd5e94b97a.pdf",
    },
    {
        "id": "KPIT_AR_2026",
        "kind": "annual_report",
        "url": "https://www.bseindia.com/xml-data/corpfiling/AttachHis/4d2467e3-e28a-472b-8492-7fd9e46e63c4.pdf",
    },
]

PDF_DIR = "data/pdfs"
TEXT_DIR = "data/text"
RESULTS_DIR = "results"

ALLOWED_SYMBOLS = "₹–—‘’“”•·…°×%±²³½¼¾©®™€£¥§"


def download(doc):
    path = os.path.join(PDF_DIR, doc["id"] + ".pdf")
    info = {"id": doc["id"], "url": doc["url"], "path": path}

    if os.path.exists(path) and os.path.getsize(path) > 1000:
        info["download"] = "already_present"
        info["bytes"] = os.path.getsize(path)
        return info

    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        session.get("https://www.bseindia.com/", timeout=30)
        response = session.get(
            doc["url"],
            timeout=180,
            headers={"Referer": "https://www.bseindia.com/"},
        )
    except Exception as error:
        info["download"] = "error"
        info["error"] = str(error)
        return info

    info["status"] = response.status_code
    info["content_type"] = response.headers.get("content-type")
    info["bytes"] = len(response.content)
    first_bytes = response.content[:5]

    if response.status_code == 200 and first_bytes == b"%PDF-":
        file_handle = open(path, "wb")
        file_handle.write(response.content)
        file_handle.close()
        info["download"] = "ok"
    else:
        info["download"] = "failed"
        info["first_bytes"] = first_bytes.decode("latin-1")
    return info


def count_weird_chars(text):
    weird = 0
    for ch in text:
        if ch.isalnum():
            continue
        if ch.isspace():
            continue
        if ch in string.printable:
            continue
        if ch in ALLOWED_SYMBOLS:
            continue
        weird = weird + 1
    return weird


def single_letter_ratio(text):
    words = text.split()
    if len(words) == 0:
        return 0.0
    singles = 0
    for word in words:
        if len(word) == 1 and word.isalpha() and word.lower() not in ("a", "i"):
            singles = singles + 1
    return singles / len(words)


def image_fraction(page):
    page_area = page.rect.width * page.rect.height
    if page_area <= 0:
        return 0.0
    total = 0.0
    for image in page.get_images(full=True):
        xref = image[0]
        try:
            rects = page.get_image_rects(xref)
        except Exception:
            rects = []
        for rect in rects:
            total = total + rect.width * rect.height
    if total / page_area > 1.0:
        return 1.0
    return total / page_area


def printed_page_guess(text):
    lines = []
    for line in text.strip().splitlines():
        line = line.strip()
        if line != "":
            lines.append(line)
    if len(lines) == 0:
        return None
    candidates = lines[:2] + lines[-2:]
    for line in candidates:
        if line.isdigit() and len(line) <= 4:
            return int(line)
    return None


def analyse_page(page, page_number):
    text = page.get_text("text")
    stripped = text.strip()
    chars = len(stripped)
    words = len(stripped.split())
    n_images = len(page.get_images(full=True))
    img_frac = image_fraction(page)
    weird = count_weird_chars(stripped)
    weird_ratio = 0.0
    if chars > 0:
        weird_ratio = weird / chars
    singles = single_letter_ratio(stripped)

    flags = []
    if chars < 30 and n_images > 0:
        flags.append("no_text_has_image_likely_scanned")
    elif chars < 30:
        flags.append("no_text_no_image_blank_or_vector")
    elif chars < 200:
        flags.append("low_text")
    if weird_ratio > 0.03:
        flags.append("garbled_chars")
    if words >= 50 and singles > 0.25:
        flags.append("letter_spaced")
    if img_frac > 0.7 and chars >= 30:
        flags.append("full_page_image_with_text_layer_check_quality")

    return {
        "page": page_number,
        "chars": chars,
        "words": words,
        "n_images": n_images,
        "image_fraction": round(img_frac, 2),
        "weird_ratio": round(weird_ratio, 4),
        "single_letter_ratio": round(singles, 3),
        "printed_page_guess": printed_page_guess(text),
        "flags": flags,
        "text": text,
    }


def process(doc, download_info):
    result = {"id": doc["id"], "kind": doc["kind"], "download": download_info}
    if download_info.get("download") not in ("ok", "already_present"):
        result["extraction"] = "skipped_download_failed"
        return result

    start = time.time()
    pdf = pymupdf.open(download_info["path"])
    n_pages = pdf.page_count
    pages = []
    for index in range(n_pages):
        page = pdf[index]
        pages.append(analyse_page(page, index + 1))
    elapsed = time.time() - start

    text_path = os.path.join(TEXT_DIR, doc["id"] + ".pages.jsonl")
    file_handle = open(text_path, "w")
    for page_info in pages:
        file_handle.write(json.dumps({
            "doc": doc["id"],
            "page": page_info["page"],
            "flags": page_info["flags"],
            "text": page_info["text"],
        }) + "\n")
    file_handle.close()

    flag_counts = Counter()
    flagged_pages = {}
    for page_info in pages:
        for flag in page_info["flags"]:
            flag_counts[flag] = flag_counts[flag] + 1
            if flag not in flagged_pages:
                flagged_pages[flag] = []
            flagged_pages[flag].append(page_info["page"])

    offsets = Counter()
    for page_info in pages:
        printed = page_info["printed_page_guess"]
        if printed is not None:
            offsets[page_info["page"] - printed] += 1

    samples = []
    for sample_page in (1, max(1, n_pages // 2), n_pages):
        page_info = pages[sample_page - 1]
        samples.append({
            "page": sample_page,
            "first_200_chars": page_info["text"].strip()[:200],
        })

    total_chars = 0
    for page_info in pages:
        total_chars = total_chars + page_info["chars"]

    result["extraction"] = "ok"
    result["n_pages"] = n_pages
    result["total_chars"] = total_chars
    result["seconds"] = round(elapsed, 2)
    result["flag_counts"] = dict(flag_counts)
    result["flagged_pages"] = {key: value[:80] for key, value in flagged_pages.items()}
    result["pages_with_good_text"] = n_pages - len(set(
        p for plist in flagged_pages.values() for p in plist))
    result["printed_page_offset_most_common"] = offsets.most_common(3)
    result["text_file"] = text_path
    result["samples"] = samples
    return result


def main():
    os.makedirs(PDF_DIR, exist_ok=True)
    os.makedirs(TEXT_DIR, exist_ok=True)
    os.makedirs(RESULTS_DIR, exist_ok=True)

    all_results = []
    for doc in DOCS:
        print("== " + doc["id"])
        download_info = download(doc)
        print("   download:", download_info.get("download"), "bytes:", download_info.get("bytes"))
        result = process(doc, download_info)
        print("   extraction:", result.get("extraction"), "pages:", result.get("n_pages"),
              "flags:", result.get("flag_counts"))
        all_results.append(result)

    out_path = os.path.join(RESULTS_DIR, "02_extraction.json")
    file_handle = open(out_path, "w")
    json.dump(all_results, file_handle, indent=2)
    file_handle.close()
    print("wrote " + out_path)


if __name__ == "__main__":
    main()
