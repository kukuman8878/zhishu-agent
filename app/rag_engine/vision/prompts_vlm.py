"""视觉读取 VLM 提示词（P3+P6，docx 五/六/八）。

原则（docx 七）：描述只用于定位；原图是最终事实来源；低置信度自动进入复核。
输出统一为结构化 JSON：answer / evidence / confidence / region / needs_verification / observations。
"""

from __future__ import annotations

REGION_LOCATOR_SYSTEM = """You locate the region(s) of an image that contain the information needed to answer a question.
The image may be an engineering drawing, chart, diagram, table or photo.
Output ONLY a JSON object (no markdown fences):
{"regions": [[x0, y0, x1, y1], ...], "reason": "brief"}
- Coordinates are relative to the FULL image, each an integer in [0, 1000]:
  (x0, y0) = top-left corner, (x1, y1) = bottom-right corner of the region.
- If the answer location is unique, give exactly 1 region.
- If the image contains MULTIPLE sub-panels (e.g. stacked or grid of small charts),
  return ONE REGION PER PANEL (at most 4), ordered top-to-bottom / left-to-right.
- If unsure between a few candidate locations, give up to 3 regions ordered by likelihood.
- NEVER repeat the same region twice. Output the JSON once and stop.
- Each region must FULLY contain the needed text labels, numbers or symbols (include some margin)."""

RETRY_LOCATOR_SYSTEM = """Locate the region of the image containing the answer to the question.
Output ONLY JSON: {"regions": [[x0, y0, x1, y1]]}
Coordinates relative to the image, integers in [0, 1000]. One region only."""


def region_locator_user(question: str) -> str:
    return (
        'Question: """' + question + '"""\n\n'
        "Find the region(s) in the image that contain the answer to this question."
    )


CROP_READ_SYSTEM = """You read a ZOOMED-IN crop of a document image and answer from the IMAGE itself.
The image is the source of truth. Any description/caption text given is only a hint for orientation.

Rules:
1. Read every character, digit, symbol EXACTLY as printed (e.g. "Block C", "50 mm", "B8", "DN80").
2. Never guess: if a character is ambiguous, lower confidence and set needs_verification=true.
3. Report numbers verbatim (sign, decimals, unit). Do not compute or round.
4. If the question asks about ONE specific item/factor among several shown, report ONLY that
   item's values (labeled per model/class/panel). Never enumerate all items.
5. Never repeat the same value over and over. Answer once, completely, then stop.
6. MINIMALISM: answer only what the question asks. Do NOT add extra numbers, dates, m/z values
   or details that the question does not request.
7. ENUMERATION questions ("which machinery/vehicles/objects appear", "what is depicted",
   "list the ..."): answer as "Machine 1: <name> (<role>, <attachments>). Machine 2: ..."
   listing EVERY distinct machine/vehicle/object visible. Describe attachments explicitly
   (e.g. a crane with a fixed jib, a machine with a bucket attachment).
   Do not merge two machines into one, do not omit any, and do not stop after two.
8. MULTI-LABEL COMPLETENESS: if the relevant region shows several applicable labels/items
   ("A and B", multiple bullets/annotations), explicitly check whether more than one must
   be included. Never stop after the first matching item, never drop a second label.
9. HIGH-RISK READINGS (numbers, units, similar-looking characters — 50 vs 90, nm vs μm,
   I vs l, O vs 0): read character by character. If not fully certain, re-read the local
   region and keep the visually supported reading — never substitute a statistically
   common value.
10. If the crop truly lacks the information, answer "NOT_FOUND" with confidence 0.

Output ONLY a JSON object (no markdown fences):
{
  "answer": "complete self-contained sentence with the exact value(s)",
  "evidence": "quote what is visible and where in the crop",
  "confidence": 0.0,
  "region": "short location description inside the crop",
  "needs_verification": true or false,
  "observations": [{"content": "one observed fact/value", "confidence": 0.0}]
}"""


FULL_PLUS_CROP_SYSTEM = """You answer from TWO views of the same document content:
1) a FULL page image (use it for overall structure and context), and
2) ZOOMED-IN crops of the relevant region (use them as the AUTHORITATIVE source for exact
   characters, numbers, dimensions, model numbers, block letters).

Rules:
1. Full page: understand layout, which section/figure the question targets.
2. Crops: read every character/digit EXACTLY as printed.
3. When the full page and a crop conflict, TRUST THE CROP.
4. Any description text given is only a hint for orientation, never the source of facts.
5. Never guess: ambiguous characters -> lower confidence, needs_verification=true.
6. Report numbers verbatim. If truly absent everywhere, answer "NOT_FOUND" with confidence 0.
7. If the question asks about ONE specific item/factor among several shown, report ONLY that
   item's values (labeled per model/class/panel). Never enumerate all items.
8. Never repeat the same value over and over. Answer once, completely, then stop.
9. MINIMALISM: answer only what the question asks. Do NOT add extra numbers, dates, m/z values,
   units or details that the question does not request.
10. ENUMERATION questions ("which machinery/vehicles/objects appear", "what is depicted",
    "list the ..."): answer as "Machine 1: <name> (<role>, <attachments>). Machine 2: ..."
    listing EVERY distinct machine/vehicle/object visible. Describe attachments explicitly
    (a truck with a mounted plow blade, a machine with an auger, a chute that throws snow).
    Do not merge two machines into one, do not omit any, and do not stop after two.
11. MULTI-LABEL COMPLETENESS: if the relevant region shows several applicable labels/items
    ("A and B", multiple bullets/annotations), explicitly check whether more than one must
    be included. Never stop after the first matching item, never drop a second label.
12. HIGH-RISK READINGS (numbers, units, similar-looking characters — 50 vs 90, nm vs μm,
    I vs l, O vs 0): read character by character. If not fully certain, re-read the local
    region and keep the visually supported reading — never substitute a statistically
    common value.

Output ONLY a JSON object (no markdown fences):
{
  "answer": "complete self-contained sentence with the exact value(s)",
  "evidence": "quote what is visible and where (page or crop)",
  "confidence": 0.0,
  "region": "short location description",
  "needs_verification": true or false,
  "observations": [{"content": "one observed fact/value", "confidence": 0.0}]
}"""


VERIFY_TOKEN_SYSTEM = """Second-look verification on a zoomed crop. Read the target region CHARACTER BY CHARACTER.
Output ONLY a JSON object (no markdown fences):
{"value": "your exact reading", "confidence": 0.0, "uncertain": false}
- If you cannot determine the characters/numbers with certainty, set uncertain=true and value="uncertain".
- Do not explain, do not hedge, do not output anything but JSON."""


def verify_token_user(first_reading: str, candidates: str = "") -> str:
    extra = ""
    if candidates:
        extra = (
            "\nCandidate readings (from a previous pass):\n"
            + candidates
            + '\nYour first pass reported: "'
            + first_reading
            + '"\n'
        )
    return (
        "Focus ONLY on the marked/cropped region. Read every character of the label, number, "
        "dimension or code in it, character by character."
        + extra
        + "Output your exact reading as JSON."
    )


DESC_HINT_SYSTEM = """You answer a question about figures/tables/charts in documents.

For each relevant figure/table you get:
1. A DESCRIPTION generated earlier (use it ONLY to locate which figure/table and roughly where;
   it may contain errors, never trust it as fact).
2. The figure/table IMAGE itself — THE SOURCE OF TRUTH.
3. Surrounding page text.

Rules:
1. Answer from the IMAGE. When the image and the description conflict, TRUST THE IMAGE.
2. Read every character, digit, symbol EXACTLY as printed.
3. Report numbers verbatim (sign, decimals, unit). Do not compute or guess.
4. Ambiguous characters -> lower confidence, needs_verification=true.
5. ENUMERATION questions ("which machinery/vehicles/objects appear", "what is depicted",
   "list the ..."): be EXHAUSTIVE — name EVERY distinct machine/vehicle/object visible,
   describe its role and attachments explicitly (e.g. a crane with a fixed jib,
   a machine with a bucket attachment). Do not merge two machines into one and do not omit any.
6. MULTI-LABEL COMPLETENESS: if the relevant region shows several applicable labels/items
   ("A and B", multiple bullets/annotations), explicitly check whether more than one must
   be included. Never stop after the first matching item, never drop a second label.
7. HIGH-RISK READINGS (numbers, units, similar-looking characters — 50 vs 90, nm vs μm,
   I vs l, O vs 0): read character by character. If not fully certain, re-read the local
   region and keep the visually supported reading — never substitute a statistically
   common value.
8. Only answer "NOT_FOUND" if image AND page text all lack the information.

Output ONLY a JSON object (no markdown fences):
{
  "answer": "complete self-contained sentence with the exact value(s)",
  "evidence": "quote what is visible and where",
  "confidence": 0.0,
  "region": "short location description",
  "needs_verification": true or false,
  "observations": [{"content": "one observed fact/value", "confidence": 0.0}]
}"""


TABLE_STRUCT_SYSTEM = """You answer a question about tabular data. The evidence contains STRUCTURED TABLE CELLS
extracted deterministically from the table markup — they are EXACT and authoritative.
A description may also be provided — use it ONLY to understand row/column labels
(its numbers may be wrong; never take numbers from it).

Rules:
1. Answer from the structured cells. Report numbers verbatim with their units/brackets.
2. Confusion matrix lines are already phrased as '<reference category> is misclassified as
   <predicted category>: count' — quote them exactly, do not swap the two categories.
3. If the question asks for a specific metric/parameter row (e.g. eta-squared η2), report ALL
   values in that row. If a row label looks garbled (e.g. 'm2' for η2, '2' for γ2), use the
   description to identify the intended row, but keep the structured cells' numbers.
   If the column-to-parameter mapping is ambiguous, report ALL values of that row in column
   order, each labeled with its position (column 1..N) and its confidence interval.
4. Never substitute values from adjacent rows/columns. Never compute or guess.
5. SHIFT/CHANGE over a period: if the question asks for a shift/change/movement of a
   metric between two dates, identify the row matching the START date (the question's
   stated start, e.g. "mid-2015" = the 1 July 2015 balance, NOT 31 December 2015) and
   the row matching the END date. Report the start value, the end value, and the CHANGE
   (= end minus start) stated explicitly as "a decline of X" or "an increase of X".
6. Be concise. If the structured cells do NOT contain the requested row/column, fall back to
   the description and page text; only answer NOT_FOUND if ALL evidence lacks the information.

Output ONLY a JSON object (no markdown fences):
{"analysis": "which cells were used", "answer": "complete sentence with exact values and labels", "cited_pages": [1,2], "confidence": 0.0}
"""
