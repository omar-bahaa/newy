from __future__ import annotations

import json
from pathlib import Path

from .models import Source


def load_source_seed(path: Path) -> list[Source]:
    payload = json.loads(path.read_text())
    return [Source(**item) for item in payload]
