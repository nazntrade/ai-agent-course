"""PDF layout helpers: lines, reading order, headers/footers, dehyphenation."""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field

_WS_RE = re.compile(r"[ \t\u00a0]+")
_HYPHEN_JOIN_RE = re.compile(r"(\w+)-\s*$")


@dataclass
class Word:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    size: float
    page: int


@dataclass
class Line:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    size: float
    page: int
    words: list[Word] = field(default_factory=list)


@dataclass
class Paragraph:
    text: str
    page: int
    size: float
    bbox: list[float]


def words_from_pdfplumber(page, page_number: int) -> list[Word]:
    words: list[Word] = []
    # Tightly-set PDFs (no explicit space glyphs) need a smaller, font-scaled
    # gap threshold; the pdfplumber default of 3pt merges such words.
    for raw in page.extract_words(
        extra_attrs=["size"],
        use_text_flow=False,
        x_tolerance=1.0,
        x_tolerance_ratio=0.15,
    ):
        text = (raw.get("text") or "").strip()
        if not text:
            continue
        words.append(
            Word(
                text=text,
                x0=float(raw.get("x0", 0.0)),
                x1=float(raw.get("x1", 0.0)),
                top=float(raw.get("top", 0.0)),
                bottom=float(raw.get("bottom", 0.0)),
                size=float(raw.get("size", raw.get("height", 0.0)) or 0.0),
                page=page_number,
            )
        )
    return words


def words_from_pypdfium(page, page_number: int) -> list[Word]:
    """Fallback extraction using pypdfium2's rectangular text boxes."""

    words: list[Word] = []
    text_page = page.get_textpage()
    height = page.get_height()
    count = text_page.count_rects()
    for index in range(count):
        left, bottom, right, top = text_page.get_rect(index)
        try:
            chunk = text_page.get_text_range(index)
        except Exception:  # noqa: BLE001 - version dependent API
            continue
        for token in _WS_RE.split(chunk):
            if not token:
                continue
            words.append(
                Word(
                    text=token,
                    x0=float(left),
                    x1=float(right),
                    top=float(height - top),
                    bottom=float(height - bottom),
                    size=0.0,
                    page=page_number,
                )
            )
    return words


def group_lines(words: list[Word], tolerance: float = 2.5) -> list[Line]:
    if not words:
        return []
    ordered = sorted(words, key=lambda word: (word.page, round(word.top, 1), word.x0))
    lines: list[Line] = []
    current: list[Word] = []
    for word in ordered:
        if current and not _same_line(current[-1], word, tolerance):
            lines.append(_make_line(current))
            current = []
        current.append(word)
    if current:
        lines.append(_make_line(current))
    return [line for line in lines if line.text]


# A word belongs to the same line as its neighbour when either their baselines
# (bottom edges) or their top edges align. Grouping by the top edge alone
# detaches a word whose ascender box is a little taller than the rest of its
# line (mixed fonts or an overlaid text-layer element) even though it shares
# the baseline. Aligning on the baseline keeps such a word on its real line
# without merging vertically overlapping but distinct rows.
def _same_line(previous: Word, word: Word, tolerance: float) -> bool:
    return (
        abs(word.bottom - previous.bottom) <= tolerance
        or abs(word.top - previous.top) <= tolerance
    )


def _make_line(words: list[Word]) -> Line:
    ordered = sorted(words, key=lambda word: word.x0)
    text = _WS_RE.sub(" ", " ".join(word.text for word in ordered)).strip()
    sizes = [word.size for word in ordered if word.size > 0]
    return Line(
        text=text,
        x0=min(word.x0 for word in ordered),
        x1=max(word.x1 for word in ordered),
        top=min(word.top for word in ordered),
        bottom=max(word.bottom for word in ordered),
        size=statistics.median(sizes) if sizes else 0.0,
        page=ordered[0].page,
        words=ordered,
    )


def strip_headers_footers(
    pages: list[list[Line]],
    page_heights: list[float],
    top_band: float = 0.08,
    bottom_band: float = 0.08,
    min_frequency: float = 0.4,
) -> tuple[list[list[Line]], int]:
    """Remove repeated running heads/footers (SPEC 17.1).

    Journals often alternate two running heads (even/odd pages), so a key that
    repeats on about half of the pages must still be removed; the threshold is
    therefore 40% rather than 60%.
    """

    if len(pages) < 3:
        return pages, 0
    header_counts: dict[str, int] = {}
    footer_counts: dict[str, int] = {}
    for lines, height in zip(pages, page_heights):
        for line in lines:
            key = _normalize_key(line.text)
            if not key:
                continue
            if height and line.bottom <= top_band * height:
                header_counts[key] = header_counts.get(key, 0) + 1
            elif height and line.top >= (1.0 - bottom_band) * height:
                footer_counts[key] = footer_counts.get(key, 0) + 1
    threshold = max(2, int(min_frequency * len(pages)))
    repeated = {
        key
        for key, count in {**header_counts, **footer_counts}.items()
        if count >= threshold
    }
    removed = 0
    cleaned: list[list[Line]] = []
    for lines, height in zip(pages, page_heights):
        kept: list[Line] = []
        for line in lines:
            key = _normalize_key(line.text)
            is_band = height and (
                line.bottom <= top_band * height or line.top >= (1.0 - bottom_band) * height
            )
            if is_band and key in repeated:
                removed += 1
                continue
            kept.append(line)
        cleaned.append(kept)
    return cleaned, removed


def detect_column_valley(lines: list[Line], page_width: float) -> float | None:
    """Locate the two-column gutter as the x-position crossed by the fewest rows.

    Scans the middle band of the page for the vertical line straddled by the
    smallest number of word boxes. In a two-column layout only full-width
    elements (headings, captions, tables) cross the gutter, so few rows are
    crossed; in a single-column layout one word on almost every line crosses
    the middle, which is used to reject the false positive.
    """

    words = [word for line in lines for word in line.words]
    if page_width <= 0 or len(words) < 20 or len(lines) < 5:
        return None
    low, high = page_width * 0.3, page_width * 0.7
    if high <= low:
        return None
    steps = 60
    best: tuple[tuple[int, int, float], float] | None = None
    for step in range(steps + 1):
        position = low + (high - low) * step / steps
        left = right = 0
        crossing_words = 0
        crossing_lines = 0
        for line in lines:
            crosses = False
            for word in line.words:
                if word.x0 < position < word.x1:
                    crossing_words += 1
                    crosses = True
                elif word.x1 <= position:
                    left += 1
                else:
                    right += 1
            if crosses:
                crossing_lines += 1
        if not left or not right:
            continue
        balance = min(left, right) / (left + right)
        if balance < 0.2:
            continue
        score = (crossing_lines, crossing_words, -balance)
        if best is None or score < best[0]:
            best = (score, position)
    if best is None:
        return None
    crossing_lines = best[0][0]
    if crossing_lines > max(2, 0.15 * len(lines)):
        return None
    return best[1]


# A row crosses the gutter as a real two-column row only when its gap at the
# gutter is clearly wider than the row's normal word spacing. Full-width
# elements keep ordinary spacing across the gutter and stay intact.
_COLUMN_GAP_RATIO = 2.0
_COLUMN_GAP_MIN_PT = 6.0


def _split_line_at_valley(line: Line, valley: float) -> list[Line]:
    """Split one visual row into left/right parts when it mixes both columns.

    Rows that a single word straddles, or whose gap at the gutter is ordinary
    word spacing, are genuine full-width elements and are returned unchanged.
    """

    if not (line.x0 < valley < line.x1):
        return [line]
    if any(word.x0 < valley < word.x1 for word in line.words):
        return [line]
    left_words = [word for word in line.words if word.x1 <= valley]
    right_words = [word for word in line.words if word.x0 >= valley]
    if not left_words or not right_words:
        return [line]
    ordered = sorted(line.words, key=lambda word: word.x0)
    gaps = [b.x0 - a.x1 for a, b in zip(ordered, ordered[1:])]
    median_gap = statistics.median(gaps) if gaps else 0.0
    gutter = min(word.x0 for word in right_words) - max(word.x1 for word in left_words)
    if gutter < max(_COLUMN_GAP_MIN_PT, _COLUMN_GAP_RATIO * median_gap):
        return [line]
    return [_make_line(left_words), _make_line(right_words)]


def _blank_separator(reference: Line) -> Line:
    """Empty line that makes ``join_paragraphs`` flush between reading groups."""

    return Line(
        text="",
        x0=reference.x0,
        x1=reference.x0,
        top=reference.top,
        bottom=reference.top,
        size=0.0,
        page=reference.page,
    )


def order_lines(lines: list[Line], page_width: float) -> list[Line]:
    """Order lines for reading: single column top-down, two columns left→right."""

    if not lines:
        return []
    valley = detect_column_valley(lines, page_width)
    if valley is None:
        return sorted(lines, key=lambda line: (line.page, line.top, line.x0))

    segments: list[Line] = []
    for line in lines:
        segments.extend(_split_line_at_valley(line, valley))

    def kind(line: Line) -> str:
        if line.x0 < valley < line.x1:
            return "full"
        if line.x1 <= valley:
            return "left"
        return "right"

    pool = sorted(segments, key=lambda line: (line.top, line.x0))
    groups: list[list[Line]] = []
    index = 0
    while index < len(pool):
        if kind(pool[index]) == "full":
            run: list[Line] = []
            while index < len(pool) and kind(pool[index]) == "full":
                run.append(pool[index])
                index += 1
            groups.append(run)
            continue
        band: list[Line] = []
        while index < len(pool) and kind(pool[index]) != "full":
            band.append(pool[index])
            index += 1
        left = sorted(
            (line for line in band if kind(line) == "left"), key=lambda line: line.top
        )
        right = sorted(
            (line for line in band if kind(line) == "right"), key=lambda line: line.top
        )
        if left:
            groups.append(left)
        if right:
            groups.append(right)

    ordered: list[Line] = []
    for group in groups:
        if ordered:
            ordered.append(_blank_separator(group[0]))
        ordered.extend(group)
    return ordered


def join_paragraphs(lines: list[Line]) -> list[Paragraph]:
    """Merge wrapped lines into paragraphs and repair hyphen splits."""

    paragraphs: list[Paragraph] = []
    buffer: list[Line] = []

    def flush() -> None:
        if not buffer:
            return
        text = ""
        for line in buffer:
            if text and _HYPHEN_JOIN_RE.search(text) and line.text and line.text[0].islower():
                text = _HYPHEN_JOIN_RE.sub(r"\1", text) + line.text
            elif text:
                text = text + " " + line.text
            else:
                text = line.text
        sizes = [line.size for line in buffer if line.size > 0]
        paragraphs.append(
            Paragraph(
                text=_WS_RE.sub(" ", text).strip(),
                page=buffer[0].page,
                size=statistics.median(sizes) if sizes else 0.0,
                bbox=[buffer[0].x0, buffer[0].top, buffer[0].x1, buffer[-1].bottom],
            )
        )
        buffer.clear()

    previous: Line | None = None
    for line in lines:
        if line.text.strip() == "":
            flush()
            previous = None
            continue
        if previous is not None:
            gap = line.top - previous.bottom
            if gap > 1.6 * max(line.size or 1.0, 1.0) and not previous.text.endswith("-"):
                flush()
        buffer.append(line)
        previous = line
    flush()
    return paragraphs


def _normalize_key(text: str) -> str:
    key = _WS_RE.sub(" ", text).strip().lower()
    return re.sub(r"\d+", "#", key)
