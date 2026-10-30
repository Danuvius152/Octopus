"""A private CLI settings input owned by one subprocess invocation."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class PrivateSettings:
    argument_index: int
    values: dict[str, Any] = field(repr=False)
    _directory: Path | None = field(default=None, init=False, repr=False)

    def materialize(self, argv: list[str]) -> list[str]:
        """Called only after spawn authorization; rendering remains pure."""
        directory = self._directory = Path(tempfile.mkdtemp(prefix="octopus-cli-settings-"))
        path = directory / "settings.json"
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(self.values, stream)
            result = list(argv)
            result[self.argument_index] = str(path)
            return result
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._directory is not None:
            shutil.rmtree(self._directory)
            self._directory = None
