"""SQLite persistence for engagements.

Two layers:

* **Index DB** at ``<root>/_index.db`` — one row per engagement. Routes incoming Teams
  activity (``conversation_id``) to the owning engagement. This works uniformly
  across personal 1:1 DMs, group chats, and team channels — the conversation ID
  is unique per conversation in every case.
* **Per-engagement DB** at ``<root>/<engagement_id>/engagement.db`` — everything
  specific to a single audit (documents, transactions, findings, conversations, AJEs).

Phase 1 only wires the index plus ``engagement_meta`` and ``documents``. Later phases
add ``transactions``, ``findings``, etc., on the same per-engagement DB.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

DEFAULT_ROOT = Path(".tmp") / "engagements"
INDEX_FILENAME = "_index.db"
ENGAGEMENT_FILENAME = "engagement.db"

PHASE_INTAKE = "intake"
PHASE_TAX_AUDIT = "tax_audit"
PHASE_RECONCILIATION = "reconciliation"
PHASE_ROLLFORWARD = "rollforward"
PHASE_CPA_REVIEW = "cpa_review"
PHASE_DELIVERED = "delivered"

DOC_JOURNAL = "journal"
DOC_BALANCE_SHEET = "balance_sheet"
DOC_PNL = "pnl"
DOC_BANK_STATEMENT = "bank_statement"
DOC_VENDOR_BILL = "vendor_bill"

CONV_PERSONAL = "personal"
CONV_CHANNEL = "channel"
CONV_GROUP = "groupChat"

_INDEX_SCHEMA = """
CREATE TABLE IF NOT EXISTS engagement_index (
    engagement_id      TEXT PRIMARY KEY,
    client_id          TEXT,
    conversation_id    TEXT NOT NULL,
    conversation_type  TEXT NOT NULL,
    user_aad_id        TEXT,
    period_description TEXT,
    phase              TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    db_path            TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_engagement_conversation
    ON engagement_index (conversation_id, phase);
"""

_ENGAGEMENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS engagement_meta (
    engagement_id      TEXT PRIMARY KEY,
    client_id          TEXT,
    conversation_id    TEXT NOT NULL,
    conversation_type  TEXT NOT NULL,
    user_aad_id        TEXT,
    period_description TEXT,
    period_start       TEXT,
    period_end         TEXT,
    phase              TEXT NOT NULL,
    created_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_type          TEXT NOT NULL,
    file_path         TEXT NOT NULL,
    original_filename TEXT,
    parsed_json       TEXT,
    uploaded_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_documents_type ON documents (doc_type);
"""


@dataclass(frozen=True)
class Engagement:
    engagement_id: str
    client_id: str | None
    conversation_id: str
    conversation_type: str
    user_aad_id: str | None
    period_description: str | None
    phase: str
    created_at: str
    db_path: Path


@dataclass(frozen=True)
class Document:
    id: int
    doc_type: str
    file_path: str
    original_filename: str | None
    uploaded_at: str


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="microseconds")


@contextmanager
def _connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


class EngagementStore:
    """Thin facade over the two-DB layout. One instance per process is fine."""

    def __init__(self, root: Path | str = DEFAULT_ROOT) -> None:
        self.root = Path(root)
        self.index_path = self.root / INDEX_FILENAME
        self._init_index()

    # ---- index ------------------------------------------------------------

    def _init_index(self) -> None:
        with _connect(self.index_path) as conn:
            conn.executescript(_INDEX_SCHEMA)

    def create_engagement(
        self,
        conversation_id: str,
        conversation_type: str,
        user_aad_id: str | None = None,
        client_id: str | None = None,
        period_description: str | None = None,
    ) -> Engagement:
        engagement_id = uuid.uuid4().hex[:12]
        engagement_dir = self.root / engagement_id
        engagement_dir.mkdir(parents=True, exist_ok=True)
        db_path = engagement_dir / ENGAGEMENT_FILENAME
        created_at = _now_iso()
        phase = PHASE_INTAKE

        with _connect(db_path) as conn:
            conn.executescript(_ENGAGEMENT_SCHEMA)
            conn.execute(
                """
                INSERT INTO engagement_meta
                    (engagement_id, client_id, conversation_id, conversation_type,
                     user_aad_id, period_description, phase, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (engagement_id, client_id, conversation_id, conversation_type,
                 user_aad_id, period_description, phase, created_at),
            )

        with _connect(self.index_path) as conn:
            conn.execute(
                """
                INSERT INTO engagement_index
                    (engagement_id, client_id, conversation_id, conversation_type,
                     user_aad_id, period_description, phase, created_at, db_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (engagement_id, client_id, conversation_id, conversation_type,
                 user_aad_id, period_description, phase, created_at, str(db_path)),
            )

        return Engagement(
            engagement_id=engagement_id,
            client_id=client_id,
            conversation_id=conversation_id,
            conversation_type=conversation_type,
            user_aad_id=user_aad_id,
            period_description=period_description,
            phase=phase,
            created_at=created_at,
            db_path=db_path,
        )

    def get_active_engagement(self, conversation_id: str) -> Engagement | None:
        """Return the most recent non-delivered engagement for this conversation, if any."""
        with _connect(self.index_path) as conn:
            row = conn.execute(
                """
                SELECT engagement_id, client_id, conversation_id, conversation_type,
                       user_aad_id, period_description, phase, created_at, db_path
                FROM engagement_index
                WHERE conversation_id = ? AND phase != ?
                ORDER BY created_at DESC, rowid DESC
                LIMIT 1
                """,
                (conversation_id, PHASE_DELIVERED),
            ).fetchone()
        if row is None:
            return None
        return Engagement(
            engagement_id=row["engagement_id"],
            client_id=row["client_id"],
            conversation_id=row["conversation_id"],
            conversation_type=row["conversation_type"],
            user_aad_id=row["user_aad_id"],
            period_description=row["period_description"],
            phase=row["phase"],
            created_at=row["created_at"],
            db_path=Path(row["db_path"]),
        )

    def update_phase(self, engagement_id: str, phase: str) -> None:
        with _connect(self.index_path) as conn:
            conn.execute(
                "UPDATE engagement_index SET phase = ? WHERE engagement_id = ?",
                (phase, engagement_id),
            )
        db_path = self._require_db_path(engagement_id)
        with _connect(db_path) as conn:
            conn.execute(
                "UPDATE engagement_meta SET phase = ? WHERE engagement_id = ?",
                (phase, engagement_id),
            )

    # ---- documents --------------------------------------------------------

    def attach_document(
        self,
        engagement_id: str,
        doc_type: str,
        file_path: Path | str,
        original_filename: str | None = None,
    ) -> int:
        db_path = self._require_db_path(engagement_id)
        with _connect(db_path) as conn:
            cur = conn.execute(
                """
                INSERT INTO documents
                    (doc_type, file_path, original_filename, uploaded_at)
                VALUES (?, ?, ?, ?)
                """,
                (doc_type, str(file_path), original_filename, _now_iso()),
            )
            return int(cur.lastrowid)

    def list_documents(self, engagement_id: str) -> list[Document]:
        db_path = self._require_db_path(engagement_id)
        with _connect(db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, doc_type, file_path, original_filename, uploaded_at
                FROM documents
                ORDER BY uploaded_at ASC, id ASC
                """
            ).fetchall()
        return [
            Document(
                id=r["id"],
                doc_type=r["doc_type"],
                file_path=r["file_path"],
                original_filename=r["original_filename"],
                uploaded_at=r["uploaded_at"],
            )
            for r in rows
        ]

    # ---- helpers ----------------------------------------------------------

    def _require_db_path(self, engagement_id: str) -> Path:
        with _connect(self.index_path) as conn:
            row = conn.execute(
                "SELECT db_path FROM engagement_index WHERE engagement_id = ?",
                (engagement_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown engagement_id: {engagement_id}")
        return Path(row["db_path"])

    def engagement_dir(self, engagement_id: str) -> Path:
        return self.root / engagement_id
