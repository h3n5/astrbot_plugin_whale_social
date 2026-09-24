"""Atomic JSON persistence for per-group social state."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping, Optional

from core.models import SCHEMA_VERSION


class StateStore:
    """Load/save ``{umo: state}`` maps with a schema version guard."""

    def __init__(self, path: str | os.PathLike[str], *, schema_version: int = SCHEMA_VERSION) -> None:
        self.path = Path(path)
        self.schema_version = schema_version

    def load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        if raw.get("schema_version") != self.schema_version:
            return {}
        states = raw.get("states")
        if not isinstance(states, dict):
            return {}
        return {
            str(umo): dict(payload)
            for umo, payload in states.items()
            if isinstance(payload, dict)
        }

    def load_global(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        if raw.get("schema_version") != self.schema_version:
            return {}
        payload = raw.get("global")
        return dict(payload) if isinstance(payload, dict) else {}

    def save(
        self,
        states: Mapping[str, Mapping[str, Any]],
        global_state: Optional[Mapping[str, Any]] = None,
    ) -> None:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "states": {str(umo): dict(state) for umo, state in states.items()},
        }
        if global_state is not None:
            payload["global"] = dict(global_state)
        text = json.dumps(payload, ensure_ascii=False, indent=2)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent),
            prefix=self.path.name + ".",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        finally:
            if os.path.exists(tmp_name):
                try:
                    os.remove(tmp_name)
                except OSError:
                    pass
