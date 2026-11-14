"""Route a plain-language PDF request to one PDF Studio skill (mirrors skills/pdf/SKILL.md).

``pdfcore route "highlight every mention of the contractor"`` → ``pdf-markup``.
Scores keyword groups; ties break toward the more specific skill (plans > standards > make > markup >
ocr-forms > organize).
"""

from __future__ import annotations

import re

RULES: dict[str, list[str]] = {
    "pdf-plans": [
        r"\bsheet index\b", r"\bdrawing (log|set|index)\b", r"\bdrawings?\b", r"\bplans?\b(?! to)", r"\bblueprints?\b",
        r"\bone[- ]?lines?\b", r"\bpanel schedules?\b", r"\btitle blocks?\b", r"\bslip ?sheet", r"\btakeoffs?\b",
        r"\bmeasure(ment)?s?\b", r"\bscale\b", r"\bcalibrat", r"\bcallouts? (to|across)|\bbatch link", r"\bdetail callouts?\b",
        r"\brevision clouds?\b", r"\bissued? for construction\b", r"\bseal\b", r"\barch [a-e]\b|\bansi [a-e]\b|\bd-size\b",
        r"\bdxf\b|\bdwg\b|\bhpgl\b|\bprogecad\b|\bautocad\b|\bcad\b", r"\bschematics?\b|\bladder\b|\bwiring diagram",
        r"\btile\b.*\bletter\b", r"\blayers?\b", r"\bequipment list\b", r"\bgeneral notes\b", r"\bbid set\b",
        r"\bsheets?\b.*\b(e|m|p|a)-\d{3}\b",
    ],
    "pdf-standards": [
        r"\bdigital(ly)? sign", r"\bsignatures? (valid|verif)", r"\bverify (the )?signature", r"\bcertify\b",
        r"\btimestamp\b", r"\bpades\b", r"\bpdf/?a\b", r"\bpdf/?ua\b", r"\barchiv(e|al) (format|standard)",
        r"\bveraPDF\b", r"\bpreflight\b", r"\baccessib", r"\bscreen reader", r"\balt text\b", r"\btagged pdf\b",
        r"\breading order\b", r"\bsign (this|the|it)\b", r"\bcertificate[- ]based\b", r"\bpdf/?x\b",
    ],
    "pdf-make": [
        r"\b(make|create|draft|write|build|generate|produce|typeset|design|prepare)\b.{0,40}\b(estimate|quote|invoice|report|proposal|letter|memo|certificate|run ?sheet|run of show|production sheet|booklet|manuscript|book|brochure|handbook|document|title block sheet)\b",
        r"\b(estimate|quote|invoice|proposal|letter|memo|certificate|run ?sheet|production sheet|booklet|manuscript)\b.{0,30}\b(for|to)\b.{0,30}\b(client|customer|crew|owner|team)",
        r"\bon (our|the) letterhead\b", r"\bbrand(ed)?\b.*\b(template|document)", r"\bnew pdf\b",
    ],
    "pdf-markup": [
        r"\bhighlight", r"\bunderline\b", r"\bstrike ?(through|out)\b", r"\bsticky note", r"\bcomments?\b", r"\bannotat",
        r"\bmark ?up\b", r"\bcallout\b", r"\bcloud\b", r"\bstamp .*\bannotation", r"\bcompare\b", r"\bdiff\b",
        r"\bwhat changed\b", r"\bchanges between\b", r"\bredact", r"\bblack out\b", r"\bremove (the )?(ssn|social security|phone|email|personal)",
        r"\bpii\b", r"\bink\b", r"\bfree ?text\b",
    ],
    "pdf-ocr-forms": [
        r"\bocr\b", r"\bsearchable\b", r"\bscann?(ed)?\b", r"\brecogni[sz]e text\b", r"\bdeskew\b", r"\bforms?\b",
        r"\bfill(able)?\b", r"\bfields?\b", r"\bcheckbox", r"\bmail ?merge\b", r"\bfrom (a |the )?csv\b", r"\bxfdf\b|\bfdf\b",
    ],
    "pdf-organize": [
        r"\bmerge\b|\bcombine\b|\bjoin\b", r"\bsplit\b", r"\binsert\b", r"\bdelete (page|the page)|\bremove pages?\b",
        r"\breorder\b|\brearrange\b", r"\brotate\b", r"\bcrop\b", r"\bextract\b", r"\bbookmarks?\b|\btable of contents\b|\btoc\b",
        r"\blinks?\b", r"\bmetadata\b", r"\battach", r"\battach\w*\b.{0,40}\b(to|into|in) (the|this|that) pdf\b", r"\bpage labels?\b", r"\bheaders?\b|\bfooters?\b", r"\bpage \w+ of\b",
        r"\bbates\b", r"\bwatermark", r"\bapproval stamp|\bstamp (it|them|each|every|all|the pages?)\b|\bapproved stamp",
        r"\bcompress\b|\bshrink\b|\boptimi[sz]e\b|\bsmaller\b|\bfile size\b", r"\bpassword\b|\bencrypt|\bdecrypt|\bpermissions?\b|\bprotect\b",
        r"\bbatch\b|\brecipe\b|\bevery pdf in\b|\bwhole folder\b", r"\bconvert\b|\bword (doc|file)|\bexcel\b|\bdocx\b|\bxlsx\b|\bpptx\b|\bjpe?g\b|\bpng\b",
        r"\btables?\b.*\b(csv|xlsx|excel|spreadsheet)\b", r"\bimages?\b", r"\btext\b",
    ],
}

PRIORITY = ["pdf-plans", "pdf-standards", "pdf-make", "pdf-markup", "pdf-ocr-forms", "pdf-organize"]


def scores(request: str) -> dict[str, int]:
    t = request.lower()
    return {skill: sum(1 for rx in rxs if re.search(rx, t, re.I)) for skill, rxs in RULES.items()}


def route(request: str) -> str:
    s = scores(request)
    best = max(s.values())
    if best == 0:
        return "pdf-organize"
    for skill in PRIORITY:
        if s[skill] == best:
            return skill
    return "pdf-organize"
