"""Acceptance aggregator: runs the automated checks and prints final statuses.

UNIT/INT are always run offline. LIVE runs only when RUN_EMBED_LIVE=1.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
sys.path.insert(0, str(MODULE_DIR))
from harness.live_policy import live_policy

PDF_PATH = MODULE_DIR / "local-data" / "input" / "agents-survey.pdf"

# Pages used by the D21-08 acceptance check for two-column reading order.
INSPECT_PAGES = (3, 4, 10, 11, 12, 13, 32)
INSPECT_MAX_LINES = 120
INSPECT_PREVIEW = 74

# Local databases that may still hold indexes built before the reading-order
# fix; they are inspected read-only to show the "before" state (D21-08).
LEGACY_DB_PATHS = ("index.db", "live/index.db")
LEGACY_PREVIEW = 220
LEGACY_MAX_FRAGMENTS = 12

# D21-13: a hyphen split with an intermediate text-layer element slipped between
# the halves. These diagnostics show the raw PDF word boxes and the final
# extracted paragraph / chunk text without a temporary script (read-only).
FRAGMENT_PAGE = 3
FRAGMENT_ANCHOR = "capabil"
FRAGMENT_INTERMEDIATE = "different"
FRAGMENT_INTRO_PATH = "introduction"
# The corrected sentence marker and the before/after glue marker (D21-13).
FRAGMENT_JOINED_ANCHOR = "enhance the agent"
FRAGMENT_GLUED_MARKER = "capabildifferent"

CHUNK_DB_PATHS = ("live/index.db", "index.db")
CHUNK_NEEDLES = ("capabil", "different", "Introduction")
CHUNK_PREVIEW = 320
CHUNK_MAX_HITS = 6


def _column_label(line, valley: float | None) -> str:
    if not line.text.strip():
        return "-"
    if valley is None:
        return "single"
    if line.x0 < valley < line.x1:
        return "FULL"
    if line.x1 <= valley:
        return "L"
    if line.x0 >= valley:
        return "R"
    return "-"


def inspect_reading_order(source_ref) -> None:
    """Print per-page reading order for the target pages (offline, D21-08).

    Uses the same extraction code path as the adapter and labels every ordered
    line as a left/right column line or a full-width (spanning) element so the
    real pages 3,4,10-13,32 can be compared without temporary scripts.
    """

    from knowledge_agent.sources.pdf_layout import (
        detect_column_valley,
        order_lines,
        strip_headers_footers,
    )
    from knowledge_agent.sources.pdf_source import PdfSourceAdapter

    adapter = PdfSourceAdapter(useful_page_min_chars=500)
    data = PDF_PATH.read_bytes()
    try:
        pages_lines, page_heights, page_widths = adapter._extract_layout(  # noqa: SLF001
            data, source_ref
        )
    except Exception as exc:  # noqa: BLE001 - diagnostic must not hide stats
        print(f"PDF_ORDER: skipped ({exc})")
        return

    cleaned, _removed = strip_headers_footers(pages_lines, page_heights)
    two_column_pages = [
        page_number
        for page_number, (lines, width) in enumerate(
            zip(cleaned, page_widths), start=1
        )
        if detect_column_valley(lines, width) is not None
    ]
    print(
        f"PDF_ORDER valleys: two_column_pages={len(two_column_pages)}/{len(cleaned)} "
        f"pages={two_column_pages}"
    )
    print("PDF_ORDER: per-page reading order (L=left, R=right, FULL=spanning)")
    summary: list[str] = []
    for page_number in INSPECT_PAGES:
        if page_number > len(cleaned):
            continue
        lines = cleaned[page_number - 1]
        width = page_widths[page_number - 1]
        valley = detect_column_valley(lines, width)
        ordered = order_lines(lines, width)
        labels = [_column_label(line, valley) for line in ordered]
        counts = {
            label: labels.count(label) for label in ("L", "R", "FULL", "single", "-")
        }
        valley_text = "none" if valley is None else f"{valley:.0f}"
        print(
            f"PDF_ORDER page {page_number}: width={width:.0f} valley={valley_text} "
            f"lines={len(ordered)} L={counts['L']} R={counts['R']} FULL={counts['FULL']}"
        )
        shown = ordered[:INSPECT_MAX_LINES]
        for line, label in zip(shown, labels):
            preview = " ".join(line.text.split())[:INSPECT_PREVIEW]
            print(f"  [{label}] {preview}")
        if len(ordered) > len(shown):
            print(f"  ... ({len(ordered) - len(shown)} more lines)")
        summary.append(
            f"p{page_number}:L={counts['L']},R={counts['R']},FULL={counts['FULL']}"
        )
    print("PDF_ORDER summary: " + " ".join(summary))


def inspect_corpus() -> None:
    """Report the real PDF extraction stats without any network access (D21-01)."""

    if not PDF_PATH.is_file():
        print("PDF_INSPECT: skipped (local-data/input/agents-survey.pdf not found)")
        return
    from knowledge_agent.domain.models import SourceRef
    from knowledge_agent.sources.pdf_source import PdfSourceAdapter

    data = PDF_PATH.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    source_ref = SourceRef(
        source_id=digest,
        uri=str(PDF_PATH),
        public_uri=PDF_PATH.name,
        label=PDF_PATH.name,
        kind="pdf",
        content_sha256=digest,
        size_bytes=len(data),
    )
    document = PdfSourceAdapter(useful_page_min_chars=500).extract(source_ref)
    extraction = document.extraction
    print(
        "PDF_INSPECT: page_count=%s useful_pages=%s useful_chars=%s language=%s"
        % (
            extraction.page_count,
            extraction.useful_pages,
            extraction.useful_chars,
            extraction.language,
        )
    )
    print(f"PDF_INSPECT sections={len(document.sections)}")
    roles: dict[str, int] = {}
    for section in document.sections:
        roles[section.role] = roles.get(section.role, 0) + 1
    print(f"PDF_INSPECT roles={roles}")
    print("  TAIL: " + " | ".join(s.section_path for s in document.sections[-3:]))
    print("  ALL: " + " | ".join(s.section_path for s in document.sections))
    for section in document.sections[:6]:
        preview = " ".join(section.text.split())[:90]
        print(f"  section: {section.section_path!r} level={section.level} :: {preview}")

    inspect_reading_order(source_ref)
    inspect_intro_fragment()


def inspect_intro_fragment() -> None:
    """Dump raw word boxes and the joined '1 Introduction' paragraph (D21-13).

    Read-only: shows the raw ``(x0, x1, top, bottom, size, text)`` boxes on the
    page where a hyphen split has an intermediate element between the halves,
    then prints the final extracted paragraph so the defect and the fix are
    both visible from a trusted entry point.
    """

    if not PDF_PATH.is_file():
        print("PDF_FRAGMENT: skipped (input PDF not found)")
        return
    import io

    import pdfplumber

    from knowledge_agent.domain.models import SourceRef
    from knowledge_agent.sources.pdf_layout import words_from_pdfplumber
    from knowledge_agent.sources.pdf_source import PdfSourceAdapter

    data = PDF_PATH.read_bytes()
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        if FRAGMENT_PAGE > len(pdf.pages):
            print("PDF_FRAGMENT: skipped (page not present)")
            return
        page = pdf.pages[FRAGMENT_PAGE - 1]
        width = float(page.width)
        words = words_from_pdfplumber(page, FRAGMENT_PAGE)

    anchors = sorted(
        (
            word
            for word in words
            if FRAGMENT_ANCHOR in word.text or FRAGMENT_INTERMEDIATE in word.text
        ),
        key=lambda word: (word.top, word.x0),
    )
    print(
        f"PDF_FRAGMENT: page {FRAGMENT_PAGE} width={width:.0f}"
        f" anchors={len(anchors)}"
    )
    for word in anchors:
        print(
            f"  anchor {word.text!r} x0={word.x0:.1f} x1={word.x1:.1f}"
            f" top={word.top:.1f} bottom={word.bottom:.1f} size={word.size:.1f}"
        )
    if anchors:
        low = min(word.top for word in anchors) - 4
        high = max(word.bottom for word in anchors) + 46
        print("PDF_FRAGMENT: raw left-column words around the split")
        for word in sorted(words, key=lambda item: (round(item.top, 1), item.x0)):
            if word.top < low or word.top > high or word.x0 > width * 0.55:
                continue
            print(
                f"  word {word.text!r} x0={word.x0:.1f} x1={word.x1:.1f}"
                f" top={word.top:.1f} bottom={word.bottom:.1f} size={word.size:.1f}"
            )

    # Character-level view: the overlay word often uses a different font/bbox.
    chars = sorted(
        (
            char
            for char in page.chars
            if 250.0 <= float(char.get("top", 0.0)) <= 272.0
            and 40.0 <= float(char.get("x0", 0.0)) <= 365.0
        ),
        key=lambda char: (round(float(char.get("top", 0.0)), 1), char.get("x0", 0.0)),
    )
    print("PDF_FRAGMENT: chars in the overlay band")
    for char in chars:
        print(
            f"  char {char.get('text')!r} font={char.get('fontname')}"
            f" size={float(char.get('size', 0.0)):.1f}"
            f" x0={float(char.get('x0', 0.0)):.1f}"
            f" top={float(char.get('top', 0.0)):.1f}"
            f" bottom={float(char.get('bottom', 0.0)):.1f}"
        )

    digest = hashlib.sha256(data).hexdigest()
    source_ref = SourceRef(
        source_id=digest,
        uri=str(PDF_PATH),
        public_uri=PDF_PATH.name,
        label=PDF_PATH.name,
        kind="pdf",
        content_sha256=digest,
        size_bytes=len(data),
    )
    document = PdfSourceAdapter(useful_page_min_chars=500).extract(source_ref)
    for section in document.sections:
        if FRAGMENT_INTRO_PATH not in section.section_path.lower():
            continue
        normalized = " ".join(section.text.split())
        print(
            f"PDF_FRAGMENT: section {section.section_path!r}"
            f" page_start={section.page_start} chars={len(normalized)}"
        )
        glued = FRAGMENT_GLUED_MARKER
        print(f"  glued_marker_present={glued in normalized}")
        needle = FRAGMENT_JOINED_ANCHOR
        index = normalized.find(needle)
        if index >= 0:
            print(f"  before-after :: ...{normalized[index:index + 160]}...")
        else:
            print(f"  preview :: {normalized[:220]}")
        break


def _manifest_extractions(manifest_json) -> list[str]:
    try:
        manifest = json.loads(manifest_json) if manifest_json else {}
    except (TypeError, ValueError):
        return []
    pipeline = manifest.get("pipeline") or {}
    return list(pipeline.get("extraction_versions") or [])


def _dump_chunk_hits(conn, index_version_id: str) -> None:
    rows = conn.execute(
        "SELECT text, metadata_json FROM chunks WHERE index_version_id = ?"
        " ORDER BY ordinal",
        (index_version_id,),
    ).fetchall()
    needle_hits = 0
    anchor_hits = 0
    for text, metadata_json in rows:
        normalized = " ".join(text.split())
        lowered = normalized.lower()
        anchor = FRAGMENT_JOINED_ANCHOR in lowered
        general = any(needle.lower() in lowered for needle in CHUNK_NEEDLES)
        if not anchor and not general:
            continue
        # Always show the corrected sentence when a chunk carries it; only a
        # bounded number of other keyword hits are listed.
        if anchor and anchor_hits < 2:
            anchor_hits += 1
        elif general and needle_hits < CHUNK_MAX_HITS:
            needle_hits += 1
        else:
            continue
        try:
            metadata = json.loads(metadata_json)
        except (TypeError, ValueError):
            metadata = {}
        print(
            f"    chunk section_path={metadata.get('section_path')!r}"
            f" page_start={metadata.get('page_start')}"
            f" content_version={metadata.get('content_version')}"
        )
        print(f"      glued_marker_present={FRAGMENT_GLUED_MARKER in lowered}")
        if anchor:
            start = lowered.find(FRAGMENT_JOINED_ANCHOR)
            print(f"      joined :: ...{normalized[start:start + CHUNK_PREVIEW]}...")
        else:
            print(f"      :: {normalized[:CHUNK_PREVIEW]}")


def inspect_new_chunks() -> None:
    """Show the current-version fixed/structure chunk text (read-only, D21-13)."""

    from knowledge_agent.sources.pdf_source import EXTRACTION_VERSION

    for relative in CHUNK_DB_PATHS:
        db_path = MODULE_DIR / "local-data" / relative
        if not db_path.is_file():
            print(f"PDF_CHUNKS: skipped ({relative} not found)")
            continue
        print(f"PDF_CHUNKS: {relative}")
        try:
            conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            print(f"  skipped ({exc})")
            continue
        try:
            rows = conn.execute(
                "SELECT index_version_id, strategy, status, manifest_json, created_at"
                " FROM index_versions ORDER BY created_at"
            ).fetchall()
            newest: dict[str, tuple[str, list[str]]] = {}
            for index_version_id, strategy, status, manifest_json, _created in rows:
                if status != "ready":
                    continue
                extractions = _manifest_extractions(manifest_json)
                if EXTRACTION_VERSION not in extractions:
                    continue
                newest[strategy] = (index_version_id, extractions)
            if not newest:
                print(f"  no ready {EXTRACTION_VERSION} versions")
            for strategy, (index_version_id, extractions) in sorted(newest.items()):
                count = conn.execute(
                    "SELECT COUNT(*) FROM chunks WHERE index_version_id = ?",
                    (index_version_id,),
                ).fetchone()[0]
                print(
                    f"  {EXTRACTION_VERSION} {strategy}: id={index_version_id}"
                    f" chunks={count} extraction_versions={extractions}"
                )
                _dump_chunk_hits(conn, index_version_id)
        finally:
            conn.close()


def _legacy_extraction_versions(conn, index_version_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT d.extraction_version FROM documents d"
        " JOIN index_documents i ON d.document_id = i.document_id"
        " WHERE i.index_version_id = ?",
        (index_version_id,),
    ).fetchall()
    return sorted(row[0] for row in rows if row[0])


def _dump_legacy_chunks(conn, index_version_id: str) -> None:
    from knowledge_agent.sources.pdf_source import EXTRACTION_VERSION

    rows = conn.execute(
        "SELECT text, metadata_json FROM chunks WHERE index_version_id = ?"
        " ORDER BY ordinal",
        (index_version_id,),
    ).fetchall()
    page_hits = planning_hits = 0
    for text, metadata_json in rows:
        try:
            metadata = json.loads(metadata_json)
        except (TypeError, ValueError):
            metadata = {}
        if metadata.get("content_version") == EXTRACTION_VERSION:
            continue
        page = metadata.get("page_start")
        normalized = " ".join(text.split())
        is_page_hit = page in INSPECT_PAGES
        is_planning = "planning" in normalized.lower()
        if not (is_page_hit or is_planning):
            continue
        if is_page_hit and page_hits < LEGACY_MAX_FRAGMENTS:
            page_hits += 1
        elif is_planning and planning_hits < LEGACY_MAX_FRAGMENTS:
            planning_hits += 1
        else:
            continue
        print(
            f"    chunk page_start={page} section_path={metadata.get('section_path')!r}"
            f" content_version={metadata.get('content_version')}"
            f" :: {normalized[:LEGACY_PREVIEW]}"
        )


def _dump_legacy_db(conn) -> None:
    from knowledge_agent.sources.pdf_source import EXTRACTION_VERSION

    try:
        versions = conn.execute(
            "SELECT index_version_id, strategy, status, created_at FROM index_versions"
            " ORDER BY created_at"
        ).fetchall()
    except sqlite3.Error as exc:
        print(f"  skipped ({exc})")
        return
    if not versions:
        print("  no index versions")
        return
    # Every retained version is listed (proving old ones are not deleted); the
    # detailed text dump covers the oldest legacy version per strategy only.
    oldest_legacy: dict[str, str] = {}
    for index_version_id, strategy, status, created_at in versions:
        extractions = _legacy_extraction_versions(conn, index_version_id)
        count = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE index_version_id = ?",
            (index_version_id,),
        ).fetchone()[0]
        print(
            f"  version id={index_version_id} strategy={strategy} status={status}"
            f" extraction_versions={extractions} chunks={count} created_at={created_at}"
        )
        if extractions and EXTRACTION_VERSION not in extractions:
            oldest_legacy.setdefault(strategy, index_version_id)
    for strategy, index_version_id in oldest_legacy.items():
        print(f"  before[{strategy}] text from legacy version id={index_version_id}")
        _dump_legacy_chunks(conn, index_version_id)


def inspect_legacy_indexes() -> None:
    """Show chunks of indexes built before the reading-order fix (D21-08).

    Read-only: opens the existing databases with ``mode=ro`` and never writes.
    Stale ``pdf-v1`` versions still present in the databases are the "before"
    state whose chunk text mixes the two columns the fix now separates.
    """

    for relative in LEGACY_DB_PATHS:
        db_path = MODULE_DIR / "local-data" / relative
        if not db_path.is_file():
            print(f"PDF_LEGACY: skipped ({relative} not found)")
            continue
        print(f"PDF_LEGACY: {relative}")
        try:
            conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            print(f"  skipped ({exc})")
            continue
        try:
            _dump_legacy_db(conn)
        finally:
            conn.close()


def run(args: list[str]) -> int:
    print(f"$ {' '.join(args)}")
    env = dict(os.environ)
    env["KNOWLEDGE_SKIP_ENV_FILE"] = "1"
    if "pytest" in args or any(name.endswith(("restart_check.py", "interrupt_check.py")) for name in args):
        env = {key: value for key, value in env.items() if not key.startswith("AI_TEST_MODEL_") and key != "AI_TEST_LIVE_POLICY"}
    completed = subprocess.run(args, cwd=str(MODULE_DIR), env=env)
    return completed.returncode


def main() -> int:
    results: dict[str, bool] = {}
    denied = live_policy() in ('forbidden', 'invalid')
    live_requested = any(os.environ.get(key) == '1' for key in ('RUN_EMBED_LIVE', 'RUN_CHAT_LIVE'))
    if denied:
        print(f'LIVE_POLICY_STATUS: BLOCKED ({live_policy()} policy; real calls NOT_RUN)')

    print("ACCEPTANCE_SCOPE: automated checks; manual/UI evidence is assessed separately")
    if os.environ.get("RUN_KNOWLEDGE_READONLY_AUDIT") == "1":
        print("READONLY_AUDIT: approved corpus and retained indexes; not isolated test data")
        inspect_corpus()
        inspect_new_chunks()
        inspect_legacy_indexes()
    unit = run([PYTHON, "-m", "pytest", "tests/unit", "-q"])
    results["UNIT"] = unit == 0
    print(f"UNIT_STATUS: {'PASS' if unit == 0 else 'FAIL'}")

    integration = run([PYTHON, "-m", "pytest", "tests/integration", "-q"])
    results["INTEGRATION"] = integration == 0
    print(f"INTEGRATION_STATUS: {'PASS' if integration == 0 else 'FAIL'}")

    restart = run([PYTHON, "harness/restart_check.py"])
    results["RESTART"] = restart == 0

    interrupt = run([PYTHON, "harness/interrupt_check.py"])
    results["INTERRUPT"] = interrupt == 0

    if os.environ.get("RUN_EMBED_LIVE") == "1" and not denied:
        live = run([PYTHON, "harness/live_embed.py"])
        results["LIVE"] = live == 0
        print(f"EMBEDDING_LIVE_STATUS: {'PASS' if live == 0 else 'FAIL'}")
    else:
        print("EMBEDDING_LIVE_STATUS: BLOCKED (policy; NOT_RUN)" if denied else "EMBEDDING_LIVE_STATUS: not run (set RUN_EMBED_LIVE=1 to select LIVE)")

    if os.environ.get("RUN_CHAT_LIVE") == "1" and not denied:
        chat_live = run([PYTHON, "harness/live_chat.py"])
        results["CHAT_LIVE"] = chat_live == 0
        print(f"CHAT_LIVE_STATUS: {'PASS' if chat_live == 0 else 'FAIL'}")
    else:
        print("CHAT_LIVE_STATUS: BLOCKED (policy; NOT_RUN)" if denied else "CHAT_LIVE_STATUS: not run (set RUN_CHAT_LIVE=1 to select LIVE)")

    mandatory = ["UNIT", "INTEGRATION", "RESTART", "INTERRUPT"]
    if "CHAT_LIVE" in results:
        mandatory.append("CHAT_LIVE")
    if "LIVE" in results:
        mandatory.append("LIVE")
    overall = all(results.get(name, False) for name in mandatory)
    print(f"AUTOMATED_STATUS: {'PASS' if overall else 'FAIL'}")
    print("D21_ACCEPTANCE_STATUS: NOT_ASSESSED (requires LIVE and manual/UI evidence)")
    if denied and live_requested:
        print('TEST_STATUS: BLOCKED (mandatory LIVE forbidden; offline results shown separately)')
        return 3
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
