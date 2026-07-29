"""Compatibility projection adapter for the RP card file format.

The Agent Framework does not write chat logs or browser projections. This
adapter is the remaining first-party RP integration and lazily loads the
legacy card handler until that projection implementation is migrated too.
"""

from __future__ import annotations

import importlib
from pathlib import Path


def _handler():
    return importlib.import_module("handler")


class LegacyProjectionAdapter:
    def __init__(self, card_folder, projection_root):
        self.card_folder = Path(card_folder)
        self.projection_root = Path(projection_root)

    def apply(self, text, draft, tokens=None):
        backups = self._backup()
        try:
            full_text = draft.content
            if draft.mvu_commands:
                full_text = full_text + "\n" + draft.mvu_commands
            _handler().append_turn(
                self.card_folder,
                polished_input=text,
                content=draft.content,
                summary=draft.summary,
                options=draft.options,
                tokens=tokens,
                full_text=full_text,
                projection_root=self.projection_root,
            )
        except Exception:
            self._restore(backups)
            raise

    def rewrite_content(self):
        _handler().write_content_js(self.card_folder, projection_root=self.projection_root)

    def _backup(self):
        paths = [
            self.card_folder / "chat_log.json",
            self.card_folder / "content.js",
            self.card_folder / "state.js",
            self.card_folder / ".var_diff.json",
            self.projection_root / "content.js",
            self.projection_root / "state.js",
        ]
        return {path: path.read_bytes() if path.exists() else None for path in paths}

    @staticmethod
    def _restore(backups):
        for path, contents in backups.items():
            if contents is None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(contents)
