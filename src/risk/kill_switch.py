"""
Kill switch — a file-backed hard stop.

When tripped, the RiskEngine rejects every order with HALTED. It is file-backed
(not just in-memory) so an operator can stop the bot from outside the process by
creating the file, and so a halt survives a crash/restart until explicitly
cleared. Trips on daily-loss breach, repeated adapter errors, or manually.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional


class KillSwitch:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._reason: str = ""
        self._tripped_at: float = 0.0
        # Adopt an existing on-disk trip (survives restarts).
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                self._reason = data.get("reason", "pre-existing kill file")
                self._tripped_at = data.get("tripped_at", time.time())
            except Exception:
                self._reason = "pre-existing kill file"
                self._tripped_at = time.time()

    def is_tripped(self) -> bool:
        # The on-disk file is authoritative — an operator may create it manually.
        return self.path.exists() or self._tripped_at > 0

    @property
    def reason(self) -> str:
        return self._reason

    def trip(self, reason: str) -> None:
        if self._tripped_at > 0:
            return  # already tripped; keep the original reason/time
        self._reason = reason
        self._tripped_at = time.time()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(
                {"reason": reason, "tripped_at": self._tripped_at}, indent=2
            ))
        except Exception:
            # Even if we can't write the file, the in-memory flag still halts us.
            pass

    def reset(self) -> None:
        """Clear the halt. Operator action only — never call this automatically."""
        self._reason = ""
        self._tripped_at = 0.0
        try:
            if self.path.exists():
                self.path.unlink()
        except Exception:
            pass

    def status(self) -> dict:
        return {
            "tripped": self.is_tripped(),
            "reason": self._reason,
            "tripped_at": self._tripped_at,
            "path": str(self.path),
        }
