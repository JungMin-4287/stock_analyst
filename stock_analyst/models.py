from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Company:
    corp_code: str
    corp_name: str
    stock_code: str
    modify_date: str | None = None


@dataclass(frozen=True)
class Filing:
    corp_code: str
    corp_name: str
    stock_code: str
    corp_cls: str
    report_nm: str
    rcept_no: str
    flr_nm: str
    rcept_dt: str
    rm: str | None = None


@dataclass(frozen=True)
class SupplementalMaterial:
    source_path: str
    kind: str
    title: str
    published_at: str | None
    author: str | None
    text: str
    tags: tuple[str, ...] = ()

    def to_jsonable(self) -> dict[str, Any]:
        row = asdict(self)
        row["tags"] = list(self.tags)
        return row

    @classmethod
    def from_path(cls, path: Path, text: str) -> "SupplementalMaterial":
        stem = path.stem.replace("_", " ").replace("-", " ").strip()
        return cls(
            source_path=str(path),
            kind=path.suffix.lower().lstrip(".") or "text",
            title=stem,
            published_at=None,
            author=None,
            text=text,
        )
