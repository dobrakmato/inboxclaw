"""Generic text diff calculator for paragraph-level change detection.

Compares two text strings by splitting them into paragraph blocks and
producing a structured diff with changed sections, character counts,
and optional truncation.  Used by any source that needs to report
textual changes (Google Drive, filesystem watcher, etc.).
"""

import difflib
import hashlib
import re
from typing import Any, Optional


class TextDiffCalculator:
    """Paragraph-level text diff calculator.

    Splits texts on blank-line boundaries, then uses
    ``difflib.SequenceMatcher`` to identify changed, added, and removed
    sections.  Results are returned as a dict suitable for inclusion in
    event payloads.
    """

    def __init__(self, max_section_chars: int = 300, max_changed_sections: int = 5):
        self.max_section_chars = max_section_chars
        self.max_changed_sections = max_changed_sections

    def normalize(self, text: str) -> list[str]:
        if not text:
            return []
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = [line.rstrip() for line in text.split("\n")]

        content = "\n".join(lines)
        paragraphs = re.split(r"\n\s*\n", content)

        return [p.strip() for p in paragraphs if p.strip()]

    def compute_diff(self, old_text: Optional[str], new_text: Optional[str]) -> dict[str, Any]:
        old_blocks = self.normalize(old_text or "")
        new_blocks = self.normalize(new_text or "")

        matcher = difflib.SequenceMatcher(None, old_blocks, new_blocks)

        changed_sections_count = 0
        added_char_count = 0
        removed_char_count = 0

        changes: list[dict[str, Optional[str]]] = []

        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue

            changed_sections_count += max(i2 - i1, j2 - j1)

            old_part = "\n\n".join(old_blocks[i1:i2])
            new_part = "\n\n".join(new_blocks[j1:j2])

            removed_char_count += len(old_part)
            added_char_count += len(new_part)

            if len(changes) < self.max_changed_sections:
                max_to_add = self.max_changed_sections - len(changes)
                num_blocks = max(i2 - i1, j2 - j1)
                for idx in range(min(num_blocks, max_to_add)):
                    before = old_blocks[i1 + idx] if (i1 + idx) < i2 else None
                    after = new_blocks[j1 + idx] if (j1 + idx) < j2 else None

                    if before and len(before) > self.max_section_chars:
                        before = before[:self.max_section_chars] + " (truncated)"
                    if after and len(after) > self.max_section_chars:
                        after = after[:self.max_section_chars] + " (truncated)"

                    changes.append({
                        "before": before,
                        "after": after
                    })

        return {
            "totalChangedSections": changed_sections_count,
            "changes": changes,
            "addedCharCount": added_char_count,
            "removedCharCount": removed_char_count,
        }

    @staticmethod
    def get_hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
