"""Retrieval for earnings-call transcripts and statement pages.

Why this exists: window search over pages ranked the page with management's actual
outlook statement 8th of 20 on the KPIT Q1 FY27 transcript, because analyst questions
repeat the same words. This module splits a transcript into speaker TURNS, tells
management from analysts, and scores management turns higher.
"""

import re

GUIDANCE_TERMS = {
    "guidance": 3, "guided": 3, "outlook": 3, "expect": 1.5, "next quarter": 2,
    "second half": 2, "h2": 2, "h1": 1, "by q4": 2, "full year": 2, "medium term": 2,
    "medium-term": 2, "going forward": 2, "return to growth": 3, "growth": 1,
    "margin": 1.5, "aspiration": 2, "target": 1.5, "we believe": 1, "will improve": 2,
    "will be better": 2,
}
# The earlier, generic term list (kept for the ablation in tests/eval_retrieval.py)
OLD_TERMS = {"guidance": 3, "outlook": 3, "going forward": 2, "full year": 2, "fy27": 2,
             "expect": 1, "target": 1, "growth": 1, "margin": 1, "next quarter": 2}

SPEAKER_RE = re.compile(r"^([A-Z][A-Za-z\.\'\-]*(?: [A-Z][A-Za-z\.\'\-]*){0,3}):\s*(.*)$")
PAGE_LINE_RE = re.compile(r"^Page \d+ of \d+\s*$")
DATE_LINE_RE = re.compile(r"^[A-Z][a-z]+ \d{1,2}, \d{4}\s*$")


def _strip_headers(pages):
    """Remove lines that repeat on most pages (company name, date, 'Page N of M')."""
    counts = {}
    for page in pages:
        seen = set()
        for line in page["text"].splitlines():
            line = line.strip()
            if line != "" and line not in seen:
                seen.add(line)
                counts[line] = counts.get(line, 0) + 1
    limit = 0.6 * len(pages)
    repeated = set()
    for line in counts:
        if counts[line] >= limit and len(pages) >= 5:
            # A speaker label ("Kishor Patil:") can repeat on most pages because that person
            # talks a lot. It is NOT a page header; deleting it merges turns and misattributes
            # quotes (found in the Render host-check review).
            if SPEAKER_RE.match(line) and len(line.split()) <= 5:
                continue
            repeated.add(line)
    cleaned = []
    for page in pages:
        lines = []
        for line in page["text"].splitlines():
            stripped = line.strip()
            if stripped == "" or stripped in repeated:
                continue
            if PAGE_LINE_RE.match(stripped) or DATE_LINE_RE.match(stripped):
                continue
            lines.append(stripped)
        cleaned.append({"page": page["page"], "lines": lines})
    return cleaned


TITLE_MGMT_RE = re.compile(r"chief|\bceo\b|\bcfo\b|\bcoo\b|\bcto\b|managing director|\bmd\b|chairman|president|director|"
                           r"\bhead\b|company secretary|\bfinance\b|vp\b", re.I)
TITLE_OUTSIDE_RE = re.compile(r"securities|research|analyst|capital|broking|equities|moderator|bank|asset management", re.I)
LIST_HEADER_RE = re.compile(r"speakers|management|panelists|participants|company representatives|from the company", re.I)
LIST_ENTRY_RE = re.compile(r"(?:MR|MS|MRS|DR|SHRI|SMT)\.?\s+([A-Z][A-Za-z\.\- ]{2,40}?)\s+[–\-—:]\s+([^\n]{3,160})", re.I)


def speaker_roles(pages):
    """Return {surname_lower: 'management' | 'host'} read from the speaker list near the top of the call.

    Two layouts are understood: 'MANAGEMENT: MR. X - TITLE ... MODERATOR: ...' (older KPIT/Pidilite style) and a
    'Speakers:' list of 'Mr./Ms. First Last - Title' lines (HUL style). Investor-relations staff who read out
    questions are 'host', not management. Anything not listed stays an analyst."""
    roles = {}
    for page in pages[:4]:
        text = page["text"]
        position = LIST_HEADER_RE.search(text)
        if position is None:
            continue
        block = text[position.start():position.start() + 3000]
        stop = re.search(r"MODERATOR\s*:", block)
        if stop is not None:
            block = block[:stop.start()]
        for match in LIST_ENTRY_RE.finditer(block):
            words = match.group(1).split()
            title = match.group(2)
            if len(words) == 0:
                continue
            surname = words[-1].lower().strip(".")
            if TITLE_OUTSIDE_RE.search(title):
                continue
            if re.search(r"investor relations", title, re.I):
                roles[surname] = "host"
            elif TITLE_MGMT_RE.search(title):
                roles[surname] = "management"
        if roles:
            break
    return roles


def management_surnames(pages):
    """Kept for the KPIT demo and tests: the set of management surnames."""
    roles = speaker_roles(pages)
    return set(name for name in roles if roles[name] == "management")


def split_turns(pages):
    """Return a list of turns: speaker, role, page (where the turn starts), text,
    and page_marks = [(char_offset, page)] so a window can be mapped back to a page."""
    roles_by_surname = speaker_roles(pages)
    cleaned = _strip_headers(pages)
    turns = []
    current = None
    for page in cleaned:
        for line in page["lines"]:
            match = SPEAKER_RE.match(line)
            if match and len(match.group(1).split()) <= 4:
                name = match.group(1)
                if name.upper() == name and len(name) > 12:
                    match = None
            if match:
                if current is not None:
                    turns.append(current)
                name = match.group(1)
                last = name.split()[-1].lower()
                if name.lower() == "moderator":
                    role = "moderator"
                elif roles_by_surname.get(last) == "host":
                    role = "host"
                elif roles_by_surname.get(last) == "management":
                    role = "management"
                else:
                    role = "analyst"
                current = {"speaker": name, "role": role, "page": page["page"],
                           "text": "", "page_marks": [(0, page["page"])]}
                rest = match.group(2)
                if rest != "":
                    current["text"] = rest + "\n"
                continue
            if current is None:
                continue
            if current["page_marks"][-1][1] != page["page"]:
                current["page_marks"].append((len(current["text"]), page["page"]))
            current["text"] = current["text"] + line + "\n"
    if current is not None:
        turns.append(current)
    return turns


def _page_at(turn, offset):
    page = turn["page_marks"][0][1]
    for mark_offset, mark_page in turn["page_marks"]:
        if mark_offset <= offset:
            page = mark_page
    return page


def _window_score(text, terms):
    lowered = text.lower()
    score = 0.0
    for term in terms:
        count = lowered.count(term)
        if count > 2:
            count = 2
        score = score + terms[term] * count
    return score


ROLE_MULTIPLIER = {"management": 1.5, "analyst": 0.5, "moderator": 0.0, "host": 0.0}


def retrieve_turns(pages, terms, top_k=4, window=1400, step=400):
    """Top-k windows from speaker turns. Returns passages with source IDs, page, speaker, role."""
    turns = split_turns(pages)
    candidates = []
    for turn_index, turn in enumerate(turns):
        text = turn["text"]
        best = (0.0, 0)
        start = 0
        while True:
            chunk = text[start:start + window]
            score = _window_score(chunk, terms)
            if score > best[0]:
                best = (score, start)
            if start + window >= len(text):
                break
            start = start + step
        weighted = best[0] * ROLE_MULTIPLIER[turn["role"]]
        if weighted > 0:
            candidates.append((weighted, turn_index, best[1]))
    candidates.sort(reverse=True)
    passages = []
    for rank, item in enumerate(candidates[:top_k]):
        turn = turns[item[1]]
        start = item[2]
        raw = turn["text"][start:start + window]
        lead = len(raw) - len(raw.lstrip())
        shown = raw.strip()
        marks = [(0, _page_at(turn, start + lead))]
        for mark_offset, mark_page in turn["page_marks"]:
            relative = mark_offset - start - lead
            if 0 < relative < len(shown):
                marks.append((relative, mark_page))
        passages.append({
            "source_id": "S" + str(rank + 1),
            "page": _page_at(turn, start),
            "speaker": turn["speaker"],
            "role": turn["role"],
            "char_start": start,
            "score": round(item[0], 2),
            "text": shown,
            "page_marks": marks,
        })
    return passages
