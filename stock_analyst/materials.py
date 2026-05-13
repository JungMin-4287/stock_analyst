from __future__ import annotations

import json
from pathlib import Path

from .models import SupplementalMaterial

TEXT_SUFFIXES = {".txt", ".md", ".csv", ".tsv", ".json", ".yaml", ".yml"}


def read_material(path: Path) -> SupplementalMaterial:
    suffix = path.suffix.lower()
    if suffix in TEXT_SUFFIXES:
        text = path.read_text(encoding="utf-8", errors="ignore")
    elif suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("PDF ingestion requires `pip install 'stock-analyst[docs]'`.") from exc
        reader = PdfReader(str(path))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    else:
        raise ValueError(f"Unsupported material type: {path}")
    return SupplementalMaterial.from_path(path, text)


def ingest_directory(input_dir: Path, output_jsonl: Path) -> list[SupplementalMaterial]:
    materials: list[SupplementalMaterial] = []
    for path in sorted(input_dir.rglob("*")):
        if path.is_file() and (path.suffix.lower() in TEXT_SUFFIXES or path.suffix.lower() == ".pdf"):
            materials.append(read_material(path))
    output_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with output_jsonl.open("w", encoding="utf-8") as fh:
        for material in materials:
            fh.write(json.dumps(material.to_jsonable(), ensure_ascii=False) + "\n")
    return materials


def load_materials(jsonl_path: Path) -> list[SupplementalMaterial]:
    if not jsonl_path.exists():
        return []
    rows = []
    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        data = json.loads(line)
        data["tags"] = tuple(data.get("tags", []))
        rows.append(SupplementalMaterial(**data))
    return rows
