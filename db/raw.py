"""File grezzi: ogni download si salva così come arriva, per poterlo rileggere senza riscaricarlo.

Struttura: raw/<fonte>/<AAAA-MM-GG>/<HHMMSS>_<nome>. Un file identico a uno già salvato della
stessa fonte e con lo stesso nome non viene duplicato. In cloud la cartella può diventare uno storage
a oggetti: chi la usa passa sempre da save() e path_of().
"""
from __future__ import annotations

import datetime as dt
import hashlib
from pathlib import Path

from .config import raw_dir


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save(source: str, name: str, data: bytes, when: dt.datetime | None = None) -> Path:
    """Salva data e ritorna il percorso (quello già esistente se il contenuto è identico)."""
    root = raw_dir() / source
    digest = sha256(data)
    for old in sorted(root.glob(f"*/*_{name}"), reverse=True):
        if old.stat().st_size == len(data) and sha256(old.read_bytes()) == digest:
            return old
    when = when or dt.datetime.now()
    path = root / f"{when:%Y-%m-%d}" / f"{when:%H%M%S}_{name}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def register(cur, source: str, path: Path) -> None:
    """Annota il file nella tabella raw_files (percorso relativo a raw/)."""
    rel = str(Path(path).resolve().relative_to(raw_dir().resolve()))
    cur.execute("INSERT INTO raw_files (source, path, sha256) VALUES (%s, %s, %s) ON CONFLICT (path) DO NOTHING",
                (source, rel, sha256(Path(path).read_bytes())))
