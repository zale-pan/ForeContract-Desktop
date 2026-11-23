"""Stdlib SQLite single-database persistence for APP-T03 durable sessions, history, and events."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import Any

from forecontract_app.chat_models import (
    MAX_CHAT_SESSION_TITLE_BYTES,
    ChatExecutionOfferView,
    ChatIntentMode,
    ChatReferenceItem,
    ChatToolReadRecord,
    ChatTurnsPage,
    ChatTurnView,
    CommandReference,
    DiffHunkReference,
    ResolvedChatEvidence,
    SessionTitleSource,
    SessionTitleView,
)
from forecontract_app.models import (
    AppError,
    AppEvent,
    ModelSelection,
    ProjectView,
    RunView,
    SessionView,
)
from forecontract_app.security import check_no_reparse_ancestors, is_reparse_or_symlink
from forecontract_app.session_models import (
    MAX_PERSISTED_EVENTS_BYTES_PER_RUN,
    MAX_PERSISTED_EVENTS_PER_RUN,
    SESSION_DB_FILENAME,
    SESSION_DB_MAX_PAGES,
    SESSION_DB_PAGE_SIZE,
    SESSION_DB_SCHEMA_VERSION,
    HistoryItem,
    QuestionView,
    UsageView,
    canonical_json_dumps,
    compute_history_item_sha256,
    truncate_utf8_safe,
    utc_now,
)

_CHAT_TURNS_DDL = """CREATE TABLE IF NOT EXISTS chat_turns (
    turn_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    seq INTEGER NOT NULL,
    client_request_id TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    intent_mode TEXT NOT NULL DEFAULT 'readonly',
    user_message TEXT NOT NULL,
    references_json TEXT NOT NULL DEFAULT '[]',
    resolved_evidence_json TEXT NOT NULL DEFAULT '[]',
    tool_reads_json TEXT NOT NULL,
    assistant_message TEXT,
    error_code TEXT,
    error_message TEXT,
    provider_mode TEXT NOT NULL DEFAULT 'offline',
    context_truncated INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (session_id, seq),
    UNIQUE (session_id, client_request_id)
);"""

_CHAT_TURNS_IDX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_chat_turns_session_seq ON chat_turns(session_id, seq);"
)

_CHAT_EXECUTION_OFFERS_DDL = """CREATE TABLE IF NOT EXISTS chat_execution_offers (
    offer_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    turn_id TEXT NOT NULL UNIQUE REFERENCES chat_turns(turn_id),
    client_request_id TEXT NOT NULL,
    goal TEXT NOT NULL,
    summary TEXT NOT NULL,
    status TEXT NOT NULL,
    run_id TEXT,
    run_client_request_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);"""

_CHAT_EXECUTION_OFFERS_IDX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_chat_offers_session_turn ON chat_execution_offers(session_id, turn_id);"
)


def _derive_bounded_title(raw_text: str | None, fallback_session_id: str) -> str:
    if isinstance(raw_text, str):
        for line in raw_text.splitlines():
            stripped = line.strip()
            if stripped:
                bounded, cut = truncate_utf8_safe(stripped, MAX_CHAT_SESSION_TITLE_BYTES)
                return f"{bounded}…" if cut and len(bounded) >= 1 else bounded
    return f"会话 {fallback_session_id[:8]}"

_REQUIRED_TABLES = frozenset(
    {
        "store_meta",
        "projects",
        "sessions",
        "runs",
        "run_idempotency",
        "constraint_idempotency",
        "history_items",
        "questions",
        "run_events",
    }
)

_SQLITE_HEADER = b"SQLite format 3\x00"


class SessionStore:
    """Bounded, synchronous SQLite session store anchored strictly inside ``data_root``."""

    def __init__(self, *, data_root: Path, max_page_count: int = SESSION_DB_MAX_PAGES) -> None:
        if not isinstance(data_root, Path):
            raise AppError("INVALID_HOST_ROOT", "data_root must be a Path", status_code=400)
        if isinstance(max_page_count, bool) or not isinstance(max_page_count, int) or max_page_count < 1:
            raise AppError("INVALID_INPUT", "max_page_count must be positive int", status_code=422)
        check_no_reparse_ancestors(data_root, label="data_root")
        self._data_root = data_root
        self._db_path = data_root / SESSION_DB_FILENAME
        self._max_page_count = max_page_count
        self._tx_depth = 0
        self._closed = False

        self._check_db_paths_safe()
        existed = os.path.lexists(self._db_path)
        if existed:
            self._preflight_existing_file()

        try:
            self._conn = sqlite3.connect(
                str(self._db_path),
                isolation_level=None,
                timeout=5.0,
            )
            self._conn.row_factory = sqlite3.Row
        except sqlite3.DatabaseError as exc:
            raise AppError("STORE_UNAVAILABLE", "failed to open session store", status_code=503) from exc

        try:
            self._conn.execute("PRAGMA journal_mode=DELETE;")
            self._conn.execute("PRAGMA synchronous=FULL;")
            self._conn.execute("PRAGMA foreign_keys=ON;")
            if not existed:
                self._conn.execute(f"PRAGMA page_size={SESSION_DB_PAGE_SIZE};")
                self._conn.execute(f"PRAGMA max_page_count={self._max_page_count};")
                self._init_schema()
            else:
                self._verify_existing_schema()
                self._conn.execute(f"PRAGMA max_page_count={self._max_page_count};")
                self._ensure_chat_turns_table()
        except AppError:
            self._safe_close_conn()
            raise
        except sqlite3.DatabaseError as exc:
            self._safe_close_conn()
            raise AppError("STORE_CORRUPT", "session store verification failed", status_code=500) from exc

    @property
    def db_path(self) -> Path:
        return self._db_path

    def _safe_close_conn(self) -> None:
        try:
            self._conn.close()
        except sqlite3.DatabaseError:
            pass
        self._closed = True

    def _require_open(self) -> None:
        if self._closed:
            raise AppError("STORE_UNAVAILABLE", "session store is closed", status_code=503)

    def _check_db_paths_safe(self) -> None:
        check_no_reparse_ancestors(self._data_root, label="data_root")
        for suffix in ("", "-journal", "-wal", "-shm"):
            candidate = Path(str(self._db_path) + suffix)
            if os.path.lexists(candidate) and is_reparse_or_symlink(candidate):
                raise AppError(
                    "STORE_CORRUPT",
                    "session store path cannot be a symlink or reparse point",
                    status_code=500,
                )

    def _preflight_existing_file(self) -> None:
        if self._db_path.is_dir():
            raise AppError("STORE_CORRUPT", "session store path is a directory", status_code=500)
        try:
            with open(self._db_path, "rb") as fh:
                header = fh.read(100)
        except OSError as exc:
            raise AppError("STORE_CORRUPT", "session store file is unreadable", status_code=500) from exc
        if len(header) < 16 or header[:16] != _SQLITE_HEADER:
            raise AppError("STORE_CORRUPT", "session store header is corrupt", status_code=500)
        if len(header) < 100:
            raise AppError("STORE_CORRUPT", "session store header is truncated", status_code=500)

        header_version = int.from_bytes(header[60:64], "big", signed=True)
        if header_version > SESSION_DB_SCHEMA_VERSION:
            raise AppError(
                "STORE_INCOMPATIBLE",
                f"session store schema version {header_version} is newer than supported {SESSION_DB_SCHEMA_VERSION}",
                status_code=500,
            )

        wal_path = Path(f"{self._db_path}-wal")
        shm_path = Path(f"{self._db_path}-shm")
        journal_path = Path(f"{self._db_path}-journal")
        for sidecar_path in (wal_path, shm_path, journal_path):
            if os.path.lexists(sidecar_path) and sidecar_path.is_dir():
                raise AppError("STORE_CORRUPT", "session store sidecar path is a directory", status_code=500)

        if os.path.lexists(wal_path):
            wal_version = self._inspect_wal_page1_user_version(wal_path)
            if wal_version is not None and wal_version > SESSION_DB_SCHEMA_VERSION:
                raise AppError(
                    "STORE_INCOMPATIBLE",
                    f"session store schema version {wal_version} is newer than supported {SESSION_DB_SCHEMA_VERSION}",
                    status_code=500,
                )

        if not (self._has_nonempty_file(wal_path) or self._has_nonempty_file(journal_path)):
            ro_conn: sqlite3.Connection | None = None
            try:
                ro_uri = f"{self._db_path.absolute().as_uri()}?mode=ro&immutable=1"
                ro_conn = sqlite3.connect(
                    ro_uri,
                    uri=True,
                    isolation_level=None,
                    timeout=5.0,
                )
                ro_conn.row_factory = sqlite3.Row
                self._verify_existing_schema(conn=ro_conn)
            except AppError:
                raise
            except sqlite3.DatabaseError as exc:
                raise AppError("STORE_CORRUPT", "session store verification failed", status_code=500) from exc
            finally:
                if ro_conn is not None:
                    with suppress(sqlite3.Error):
                        ro_conn.close()

    @staticmethod
    def _has_nonempty_file(path: Path) -> bool:
        if not os.path.lexists(path):
            return False
        try:
            return os.lstat(path).st_size > 0
        except OSError as exc:
            raise AppError("STORE_CORRUPT", "session store sidecar file is unreadable", status_code=500) from exc

    @staticmethod
    def _inspect_wal_page1_user_version(wal_path: Path) -> int | None:
        """Read uncheckpointed page-1 user_version from WAL frames without opening SQLite RW."""
        try:
            data = wal_path.read_bytes()
        except OSError:
            return None
        if len(data) < 32 or data[:4] not in {b"\x37\x7f\x06\x82", b"\x37\x7f\x06\x83"}:
            return None
        fmt_ver = int.from_bytes(data[4:8], "big")
        page_size = int.from_bytes(data[8:12], "big")
        if fmt_ver != 3007000 or page_size < 512 or page_size > 65536 or (page_size & (page_size - 1)) != 0:
            return None
        frame_size = 24 + page_size
        offset = 32
        latest_version: int | None = None
        while offset + frame_size <= len(data):
            page_no = int.from_bytes(data[offset : offset + 4], "big")
            if page_no == 1:
                page_bytes = data[offset + 24 : offset + frame_size]
                if page_bytes[:16] == _SQLITE_HEADER:
                    latest_version = int.from_bytes(page_bytes[60:64], "big", signed=True)
            offset += frame_size
        return latest_version

    def _init_schema(self) -> None:
        ddl = (
            "CREATE TABLE store_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);",
            """CREATE TABLE projects (
                project_id TEXT PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                kind TEXT NOT NULL,
                workspace_root TEXT NOT NULL,
                canonical_root TEXT NOT NULL,
                read_grant_id TEXT,
                spec_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );""",
            """CREATE TABLE sessions (
                session_id TEXT PRIMARY KEY,
                seq_order INTEGER NOT NULL,
                project_id TEXT NOT NULL REFERENCES projects(project_id),
                model_json TEXT NOT NULL,
                active_run_id TEXT,
                constraints TEXT NOT NULL DEFAULT '',
                constraints_revision INTEGER NOT NULL DEFAULT 0,
                history_cursor INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );""",
            """CREATE TABLE runs (
                run_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(session_id),
                project_id TEXT NOT NULL REFERENCES projects(project_id),
                seq_order INTEGER NOT NULL,
                client_request_id TEXT NOT NULL,
                goal TEXT NOT NULL,
                state TEXT NOT NULL,
                verification TEXT NOT NULL,
                reason_code TEXT,
                last_seq INTEGER NOT NULL DEFAULT 0,
                truncated_before INTEGER NOT NULL DEFAULT 0,
                result_json TEXT,
                usage_json TEXT,
                cleanup_confirmed INTEGER NOT NULL DEFAULT 1,
                poisoned_workspace INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );""",
            """CREATE TABLE run_idempotency (
                session_id TEXT NOT NULL REFERENCES sessions(session_id),
                client_request_id TEXT NOT NULL,
                payload_digest TEXT NOT NULL,
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                PRIMARY KEY (session_id, client_request_id)
            );""",
            """CREATE TABLE constraint_idempotency (
                session_id TEXT NOT NULL REFERENCES sessions(session_id),
                client_request_id TEXT NOT NULL,
                payload_digest TEXT NOT NULL,
                revision INTEGER NOT NULL,
                PRIMARY KEY (session_id, client_request_id)
            );""",
            """CREATE TABLE history_items (
                session_id TEXT NOT NULL REFERENCES sessions(session_id),
                seq INTEGER NOT NULL,
                item_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                text TEXT NOT NULL,
                source_ids_json TEXT NOT NULL,
                truncated INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                PRIMARY KEY (session_id, seq)
            );""",
            """CREATE TABLE questions (
                question_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                session_id TEXT NOT NULL REFERENCES sessions(session_id),
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                action_id TEXT NOT NULL,
                seq_in_run INTEGER NOT NULL,
                question TEXT NOT NULL,
                options_json TEXT NOT NULL,
                digest TEXT NOT NULL,
                state TEXT NOT NULL,
                answer TEXT,
                reason_code TEXT,
                answer_client_request_id TEXT,
                answer_payload_digest TEXT,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                UNIQUE (run_id, action_id)
            );""",
            """CREATE TABLE run_events (
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                seq INTEGER NOT NULL,
                kind TEXT NOT NULL,
                project_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                byte_size INTEGER NOT NULL,
                PRIMARY KEY (run_id, seq)
            );""",
            _CHAT_TURNS_DDL,
            _CHAT_TURNS_IDX_DDL,
            _CHAT_EXECUTION_OFFERS_DDL,
            _CHAT_EXECUTION_OFFERS_IDX_DDL,
        )
        with self.transaction() as conn:
            for stmt in ddl:
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO store_meta(key, value) VALUES ('schema_version', ?)",
                (str(SESSION_DB_SCHEMA_VERSION),),
            )
            conn.execute(f"PRAGMA user_version={SESSION_DB_SCHEMA_VERSION};")

    def _verify_existing_schema(self, conn: sqlite3.Connection | None = None) -> None:
        target = conn if conn is not None else self._conn
        uv_row = target.execute("PRAGMA user_version;").fetchone()
        version = int(uv_row[0]) if uv_row is not None else 0
        if version > SESSION_DB_SCHEMA_VERSION:
            raise AppError(
                "STORE_INCOMPATIBLE",
                f"session store schema version {version} is newer than supported {SESSION_DB_SCHEMA_VERSION}",
                status_code=500,
            )
        if version != SESSION_DB_SCHEMA_VERSION:
            raise AppError(
                "STORE_CORRUPT",
                "session store schema version is invalid",
                status_code=500,
            )
        check_row = target.execute("PRAGMA integrity_check(1);").fetchone()
        if check_row is None or check_row[0] != "ok":
            raise AppError("STORE_CORRUPT", "session store failed integrity_check", status_code=500)

        rows = target.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        found_tables = {str(r["name"]) for r in rows}
        if not _REQUIRED_TABLES.issubset(found_tables):
            raise AppError("STORE_CORRUPT", "session store missing required tables", status_code=500)

        meta = target.execute(
            "SELECT value FROM store_meta WHERE key='schema_version'"
        ).fetchone()
        if meta is None:
            raise AppError("STORE_CORRUPT", "session store missing schema_version metadata", status_code=500)
        if str(meta["value"]) != str(SESSION_DB_SCHEMA_VERSION):
            raise AppError("STORE_INCOMPATIBLE", "store_meta schema_version mismatch", status_code=500)

    def _ensure_chat_turns_table(self) -> None:
        """Create the additive chat_turns / chat_execution_offers tables or missing columns."""
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_turns'"
        ).fetchone()
        if row is None:
            with self.transaction() as conn:
                conn.execute(_CHAT_TURNS_DDL)
                conn.execute(_CHAT_TURNS_IDX_DDL)
                conn.execute(_CHAT_EXECUTION_OFFERS_DDL)
                conn.execute(_CHAT_EXECUTION_OFFERS_IDX_DDL)
            return
        cols = {
            str(c["name"])
            for c in self._conn.execute("PRAGMA table_info(chat_turns)").fetchall()
        }
        missing: list[str] = []
        if "references_json" not in cols:
            missing.append(
                "ALTER TABLE chat_turns ADD COLUMN references_json TEXT NOT NULL DEFAULT '[]'"
            )
        if "resolved_evidence_json" not in cols:
            missing.append(
                "ALTER TABLE chat_turns ADD COLUMN resolved_evidence_json TEXT NOT NULL DEFAULT '[]'"
            )
        if "intent_mode" not in cols:
            missing.append(
                "ALTER TABLE chat_turns ADD COLUMN intent_mode TEXT NOT NULL DEFAULT 'readonly'"
            )
        offer_row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_execution_offers'"
        ).fetchone()
        if missing or offer_row is None:
            with self.transaction() as conn:
                for stmt in missing:
                    conn.execute(stmt)
                if offer_row is None:
                    conn.execute(_CHAT_EXECUTION_OFFERS_DDL)
                    conn.execute(_CHAT_EXECUTION_OFFERS_IDX_DDL)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Execute synchronous atomic write transaction with rollback and sanitized 503 on failure."""
        self._require_open()
        depth = self._tx_depth
        sp_name = f"sp_{depth}"
        try:
            if depth == 0:
                self._conn.execute("BEGIN IMMEDIATE;")
            else:
                self._conn.execute(f"SAVEPOINT {sp_name};")
            self._tx_depth += 1
        except sqlite3.DatabaseError as exc:
            raise AppError("STORE_UNAVAILABLE", "unable to start store transaction", status_code=503) from exc

        try:
            yield self._conn
            if depth == 0:
                self._conn.execute("COMMIT;")
            else:
                self._conn.execute(f"RELEASE SAVEPOINT {sp_name};")
        except AppError:
            self._rollback(depth, sp_name)
            raise
        except sqlite3.DatabaseError as exc:
            self._rollback(depth, sp_name)
            raise AppError("STORE_UNAVAILABLE", "session store write failed", status_code=503) from exc
        except Exception:
            self._rollback(depth, sp_name)
            raise
        finally:
            self._tx_depth = depth

    def _rollback(self, depth: int, sp_name: str) -> None:
        try:
            if depth == 0:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK;")
            else:
                self._conn.execute(f"ROLLBACK TO SAVEPOINT {sp_name};")
                self._conn.execute(f"RELEASE SAVEPOINT {sp_name};")
        except sqlite3.DatabaseError:
            pass

    def storage_stats(self) -> tuple[int, int]:
        """Return ``(used_bytes, limit_bytes)`` for the SQLite session store."""
        self._require_open()
        pc_row = self._conn.execute("PRAGMA page_count;").fetchone()
        ps_row = self._conn.execute("PRAGMA page_size;").fetchone()
        page_count = int(pc_row[0]) if pc_row is not None else 0
        page_size = int(ps_row[0]) if ps_row is not None else SESSION_DB_PAGE_SIZE
        used = max(0, page_count * page_size)
        limit = self._max_page_count * page_size
        return used, limit

    def load(self) -> dict[str, object]:
        """Return detached, schema-validated snapshot of persisted session data without live handles."""
        self._require_open()
        try:
            projects_rows = self._conn.execute(
                "SELECT * FROM projects ORDER BY rowid ASC"
            ).fetchall()
            sessions_rows = self._conn.execute(
                "SELECT * FROM sessions ORDER BY seq_order ASC"
            ).fetchall()
            runs_rows = self._conn.execute(
                "SELECT * FROM runs ORDER BY seq_order ASC"
            ).fetchall()
            idem_rows = self._conn.execute(
                "SELECT * FROM run_idempotency ORDER BY rowid ASC"
            ).fetchall()
            cidem_rows = self._conn.execute(
                "SELECT * FROM constraint_idempotency ORDER BY rowid ASC"
            ).fetchall()
            hist_rows = self._conn.execute(
                "SELECT * FROM history_items ORDER BY session_id ASC, seq ASC"
            ).fetchall()
            q_rows = self._conn.execute(
                "SELECT * FROM questions ORDER BY run_id ASC, seq_in_run ASC"
            ).fetchall()
            ev_rows = self._conn.execute(
                "SELECT * FROM run_events ORDER BY run_id ASC, seq ASC"
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise AppError("STORE_CORRUPT", "failed reading session store", status_code=500) from exc

        try:
            projects: list[dict[str, Any]] = []
            for r in projects_rows:
                spec_obj = json.loads(r["spec_json"])
                if not isinstance(spec_obj, dict) or "view" not in spec_obj:
                    raise ValueError("invalid project spec")
                view = ProjectView.model_validate(spec_obj["view"])
                projects.append(
                    {
                        "view": view.model_dump(mode="json"),
                        "kind": str(r["kind"]),
                        "workspace_root": str(r["workspace_root"]),
                        "canonical_root": str(r["canonical_root"]),
                        "read_grant_id": r["read_grant_id"],
                        "spec": dict(spec_obj),
                    }
                )

            sessions: list[dict[str, Any]] = []
            for r in sessions_rows:
                model_sel = ModelSelection.model_validate(json.loads(r["model_json"]))
                s_view = SessionView(
                    session_id=str(r["session_id"]),
                    project_id=str(r["project_id"]),
                    model=model_sel,
                    active_run_id=r["active_run_id"],
                    constraints_revision=int(r["constraints_revision"]),
                    constraints=str(r["constraints"]),
                    history_cursor=int(r["history_cursor"]),
                )
                sessions.append(
                    {
                        "view": s_view.model_dump(mode="json"),
                        "seq_order": int(r["seq_order"]),
                    }
                )

            runs: list[dict[str, Any]] = []
            for r in runs_rows:
                res_obj = json.loads(r["result_json"]) if r["result_json"] is not None else None
                usage_obj = (
                    UsageView.model_validate(json.loads(r["usage_json"])).model_dump(mode="json")
                    if r["usage_json"] is not None
                    else None
                )
                r_view = RunView(
                    run_id=str(r["run_id"]),
                    session_id=str(r["session_id"]),
                    project_id=str(r["project_id"]),
                    client_request_id=str(r["client_request_id"]),
                    goal=str(r["goal"]),
                    state=r["state"],
                    verification=r["verification"],
                    reason_code=r["reason_code"],
                    last_seq=int(r["last_seq"]),
                    result=res_obj,
                    truncated_before=int(r["truncated_before"]),
                    usage=usage_obj,
                )
                runs.append(
                    {
                        "view": r_view.model_dump(mode="json"),
                        "seq_order": int(r["seq_order"]),
                        "cleanup_confirmed": bool(r["cleanup_confirmed"]),
                        "poisoned_workspace": bool(r["poisoned_workspace"]),
                    }
                )

            history_by_session: dict[str, list[dict[str, Any]]] = {}
            for r in hist_rows:
                src_ids = json.loads(r["source_ids_json"])
                item = HistoryItem(
                    seq=int(r["seq"]),
                    item_id=str(r["item_id"]),
                    project_id=str(r["project_id"]),
                    session_id=str(r["session_id"]),
                    run_id=str(r["run_id"]),
                    kind=r["kind"],
                    text=str(r["text"]),
                    source_ids=src_ids,
                    truncated=bool(r["truncated"]),
                    sha256=str(r["sha256"]),
                )
                history_by_session.setdefault(item.session_id, []).append(item.model_dump(mode="json"))

            questions_by_run: dict[str, list[dict[str, Any]]] = {}
            q_idempotency: dict[str, dict[str, tuple[str, str]]] = {}
            for r in q_rows:
                q_view = QuestionView(
                    id=str(r["question_id"]),
                    project_id=str(r["project_id"]),
                    session_id=str(r["session_id"]),
                    run_id=str(r["run_id"]),
                    action_id=str(r["action_id"]),
                    question=str(r["question"]),
                    options=json.loads(r["options_json"]),
                    digest=str(r["digest"]),
                    state=r["state"],
                    answer=r["answer"],
                    created_at=datetime.fromisoformat(str(r["created_at"])),
                    expires_at=datetime.fromisoformat(str(r["expires_at"])),
                    reason_code=r["reason_code"],
                )
                questions_by_run.setdefault(q_view.run_id, []).append(q_view.model_dump(mode="json"))
                if r["answer_client_request_id"] and r["answer_payload_digest"]:
                    q_idempotency.setdefault(q_view.run_id, {})[str(r["answer_client_request_id"])] = (
                        q_view.id,
                        str(r["answer_payload_digest"]),
                    )

            events_by_run: dict[str, list[dict[str, Any]]] = {}
            for r in ev_rows:
                ev = AppEvent(
                    schema_version="1",
                    project_id=str(r["project_id"]),
                    session_id=str(r["session_id"]),
                    run_id=str(r["run_id"]),
                    seq=int(r["seq"]),
                    kind=r["kind"],
                    payload=json.loads(r["payload_json"]),
                )
                events_by_run.setdefault(ev.run_id, []).append(ev.model_dump(mode="json"))

            run_idem = [
                {
                    "session_id": str(r["session_id"]),
                    "client_request_id": str(r["client_request_id"]),
                    "payload_digest": str(r["payload_digest"]),
                    "run_id": str(r["run_id"]),
                }
                for r in idem_rows
            ]
            constraint_idem = [
                {
                    "session_id": str(r["session_id"]),
                    "client_request_id": str(r["client_request_id"]),
                    "payload_digest": str(r["payload_digest"]),
                    "revision": int(r["revision"]),
                }
                for r in cidem_rows
            ]
        except Exception as exc:
            raise AppError("STORE_CORRUPT", "stored JSON or DTO validation failed", status_code=500) from exc

        return {
            "schema_version": SESSION_DB_SCHEMA_VERSION,
            "projects": projects,
            "sessions": sessions,
            "runs": runs,
            "run_idempotency": run_idem,
            "constraint_idempotency": constraint_idem,
            "history_items": history_by_session,
            "questions": questions_by_run,
            "answer_idempotency": q_idempotency,
            "run_events": events_by_run,
        }

    def reconcile_after_restart(self) -> dict[str, object]:
        """Convert unfinished runs and pending questions to HOST_RESTARTED in one transaction."""
        now_iso = utc_now().isoformat()
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE runs
                   SET state = 'unknown',
                       verification = 'unknown',
                       reason_code = 'HOST_RESTARTED',
                       cleanup_confirmed = 0,
                       poisoned_workspace = 1,
                       updated_at = ?
                 WHERE state IN ('queued', 'running', 'cancelling')
                """,
                (now_iso,),
            )
            conn.execute(
                """
                UPDATE questions
                   SET state = 'cancelled',
                       reason_code = 'HOST_RESTARTED'
                 WHERE state = 'pending'
                """,
            )
            conn.execute(
                """
                UPDATE sessions
                   SET active_run_id = NULL,
                       updated_at = ?
                 WHERE active_run_id IS NOT NULL
                """,
                (now_iso,),
            )
            conn.execute(
                """
                UPDATE chat_turns
                   SET status = 'interrupted',
                       error_code = 'HOST_RESTARTED',
                       error_message = 'host restarted before read-only chat turn completed',
                       updated_at = ?
                 WHERE status = 'running'
                """,
                (now_iso,),
            )
        return self.load()

    def insert_project(
        self,
        *,
        view: ProjectView,
        kind: str,
        workspace_root: str,
        canonical_root: str,
        read_grant_id: str | None,
        spec: dict[str, Any],
    ) -> None:
        now_iso = utc_now().isoformat()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO projects (
                    project_id, name, kind, workspace_root, canonical_root,
                    read_grant_id, spec_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    view.project_id,
                    view.name,
                    kind,
                    workspace_root,
                    canonical_root,
                    read_grant_id,
                    canonical_json_dumps(spec),
                    now_iso,
                ),
            )

    def insert_session(self, *, view: SessionView, seq_order: int) -> None:
        now_iso = utc_now().isoformat()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO sessions (
                    session_id, seq_order, project_id, model_json, active_run_id,
                    constraints, constraints_revision, history_cursor, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    view.session_id,
                    seq_order,
                    view.project_id,
                    view.model.model_dump_json(),
                    view.active_run_id,
                    view.constraints,
                    view.constraints_revision,
                    view.history_cursor,
                    now_iso,
                    now_iso,
                ),
            )

    def update_session_constraints(
        self,
        *,
        session_id: str,
        constraints: str,
        revision: int,
        client_request_id: str,
        payload_digest: str,
    ) -> None:
        now_iso = utc_now().isoformat()
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE sessions
                   SET constraints = ?,
                       constraints_revision = ?,
                       updated_at = ?
                 WHERE session_id = ?
                """,
                (constraints, revision, now_iso, session_id),
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO constraint_idempotency (
                    session_id, client_request_id, payload_digest, revision
                ) VALUES (?, ?, ?, ?)
                """,
                (session_id, client_request_id, payload_digest, revision),
            )

    def create_run_initial(
        self,
        *,
        run_view: RunView,
        run_seq_order: int,
        payload_digest: str,
        goal_history_item: HistoryItem,
        queued_event: AppEvent,
        usage_view: UsageView,
    ) -> None:
        """Persist run, idempotency, session active pointer, user_goal item, and queued event atomically."""
        now_iso = utc_now().isoformat()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, session_id, project_id, seq_order, client_request_id, goal,
                    state, verification, reason_code, last_seq, truncated_before,
                    result_json, usage_json, cleanup_confirmed, poisoned_workspace,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_view.run_id,
                    run_view.session_id,
                    run_view.project_id,
                    run_seq_order,
                    run_view.client_request_id,
                    run_view.goal,
                    run_view.state,
                    run_view.verification,
                    run_view.reason_code,
                    queued_event.seq,
                    0,
                    None,
                    usage_view.model_dump_json(),
                    1,
                    0,
                    now_iso,
                    now_iso,
                ),
            )
            conn.execute(
                """
                INSERT INTO run_idempotency (
                    session_id, client_request_id, payload_digest, run_id
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    run_view.session_id,
                    run_view.client_request_id,
                    payload_digest,
                    run_view.run_id,
                ),
            )
            conn.execute(
                """
                UPDATE sessions
                   SET active_run_id = ?,
                       history_cursor = ?,
                       updated_at = ?
                 WHERE session_id = ?
                """,
                (run_view.run_id, goal_history_item.seq, now_iso, run_view.session_id),
            )
            self._insert_history_row(conn, goal_history_item)
            self._insert_and_trim_event(conn, queued_event)

    def _insert_history_row(self, conn: sqlite3.Connection, item: HistoryItem) -> None:
        expected_sha = compute_history_item_sha256(
            seq=item.seq,
            item_id=item.item_id,
            project_id=item.project_id,
            session_id=item.session_id,
            run_id=item.run_id,
            kind=item.kind,
            text=item.text,
            source_ids=list(item.source_ids),
            truncated=item.truncated,
        )
        if expected_sha != item.sha256:
            raise AppError("INVALID_INPUT", "history item sha256 mismatch", status_code=422)
        conn.execute(
            """
            INSERT INTO history_items (
                session_id, seq, item_id, project_id, run_id,
                kind, text, source_ids_json, truncated, sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.session_id,
                item.seq,
                item.item_id,
                item.project_id,
                item.run_id,
                item.kind,
                item.text,
                canonical_json_dumps(list(item.source_ids)),
                1 if item.truncated else 0,
                item.sha256,
            ),
        )

    def _insert_and_trim_event(self, conn: sqlite3.Connection, ev: AppEvent) -> int:
        ev_bytes = len(ev.model_dump_json().encode("utf-8"))
        payload_text = canonical_json_dumps(ev.payload)
        conn.execute(
            """
            INSERT OR REPLACE INTO run_events (
                run_id, seq, kind, project_id, session_id, payload_json, byte_size
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ev.run_id,
                ev.seq,
                ev.kind,
                ev.project_id,
                ev.session_id,
                payload_text,
                ev_bytes,
            ),
        )
        rows = conn.execute(
            "SELECT seq, byte_size FROM run_events WHERE run_id = ? ORDER BY seq ASC",
            (ev.run_id,),
        ).fetchall()
        cur_row = conn.execute(
            "SELECT truncated_before FROM runs WHERE run_id = ?",
            (ev.run_id,),
        ).fetchone()
        trunc_before = int(cur_row["truncated_before"]) if cur_row is not None else 0
        total_bytes = sum(int(r["byte_size"]) for r in rows)
        count = len(rows)
        idx = 0
        while idx < len(rows) and (
            count > MAX_PERSISTED_EVENTS_PER_RUN or total_bytes > MAX_PERSISTED_EVENTS_BYTES_PER_RUN
        ):
            evicted_seq = int(rows[idx]["seq"])
            evicted_size = int(rows[idx]["byte_size"])
            conn.execute(
                "DELETE FROM run_events WHERE run_id = ? AND seq = ?",
                (ev.run_id, evicted_seq),
            )
            trunc_before = max(trunc_before, evicted_seq)
            count -= 1
            total_bytes -= evicted_size
            idx += 1

        conn.execute(
            """
            UPDATE runs
               SET last_seq = CASE WHEN last_seq < ? THEN ? ELSE last_seq END,
                   truncated_before = ?,
                   updated_at = ?
             WHERE run_id = ?
            """,
            (ev.seq, ev.seq, trunc_before, utc_now().isoformat(), ev.run_id),
        )
        return trunc_before

    def append_run_event(self, *, event: AppEvent) -> int:
        """Persist one event inside the bounded per-run event tail and return ``truncated_before``."""
        with self.transaction() as conn:
            return self._insert_and_trim_event(conn, event)

    def update_run_progress(
        self,
        *,
        run_id: str,
        state: str,
        usage_view: UsageView | None = None,
    ) -> None:
        now_iso = utc_now().isoformat()
        with self.transaction() as conn:
            if usage_view is not None:
                conn.execute(
                    "UPDATE runs SET state = ?, usage_json = ?, updated_at = ? WHERE run_id = ?",
                    (state, usage_view.model_dump_json(), now_iso, run_id),
                )
            else:
                conn.execute(
                    "UPDATE runs SET state = ?, updated_at = ? WHERE run_id = ?",
                    (state, now_iso, run_id),
                )

    def upsert_question(
        self,
        *,
        question: QuestionView,
        seq_in_run: int,
        history_item: HistoryItem | None = None,
        answer_client_request_id: str | None = None,
        answer_payload_digest: str | None = None,
    ) -> None:
        """Persist question state transition and optional history item atomically."""
        now_iso = utc_now().isoformat()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO questions (
                    question_id, project_id, session_id, run_id, action_id, seq_in_run,
                    question, options_json, digest, state, answer, reason_code,
                    answer_client_request_id, answer_payload_digest, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(question_id) DO UPDATE SET
                    state = excluded.state,
                    answer = COALESCE(excluded.answer, questions.answer),
                    reason_code = COALESCE(excluded.reason_code, questions.reason_code),
                    answer_client_request_id = COALESCE(excluded.answer_client_request_id, questions.answer_client_request_id),
                    answer_payload_digest = COALESCE(excluded.answer_payload_digest, questions.answer_payload_digest)
                """,
                (
                    question.id,
                    question.project_id,
                    question.session_id,
                    question.run_id,
                    question.action_id,
                    seq_in_run,
                    question.question,
                    canonical_json_dumps(list(question.options)),
                    question.digest,
                    question.state,
                    question.answer,
                    question.reason_code,
                    answer_client_request_id,
                    answer_payload_digest,
                    question.created_at.isoformat(),
                    question.expires_at.isoformat(),
                ),
            )
            if history_item is not None:
                self._insert_history_row(conn, history_item)
                conn.execute(
                    "UPDATE sessions SET history_cursor = ?, updated_at = ? WHERE session_id = ?",
                    (history_item.seq, now_iso, question.session_id),
                )

    def finalize_run(
        self,
        *,
        run_view: RunView,
        usage_view: UsageView | None,
        result_history_item: HistoryItem,
        finished_event: AppEvent,
        cleanup_confirmed: bool,
        poisoned_workspace: bool,
    ) -> int:
        """Atomically commit terminal run state, result history item, usage, and run_finished event."""
        now_iso = utc_now().isoformat()
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE runs
                   SET state = ?,
                       verification = ?,
                       reason_code = ?,
                       last_seq = ?,
                       truncated_before = MAX(truncated_before, ?),
                       result_json = ?,
                       usage_json = ?,
                       cleanup_confirmed = ?,
                       poisoned_workspace = ?,
                       updated_at = ?
                 WHERE run_id = ?
                """,
                (
                    run_view.state,
                    run_view.verification,
                    run_view.reason_code,
                    finished_event.seq,
                    run_view.truncated_before,
                    canonical_json_dumps(run_view.result) if run_view.result is not None else None,
                    usage_view.model_dump_json() if usage_view is not None else None,
                    1 if cleanup_confirmed else 0,
                    1 if poisoned_workspace else 0,
                    now_iso,
                    run_view.run_id,
                ),
            )
            self._insert_history_row(conn, result_history_item)
            conn.execute(
                """
                UPDATE sessions
                   SET active_run_id = CASE WHEN active_run_id = ? THEN NULL ELSE active_run_id END,
                       history_cursor = ?,
                       updated_at = ?
                 WHERE session_id = ?
                """,
                (run_view.run_id, result_history_item.seq, now_iso, run_view.session_id),
            )
            return self._insert_and_trim_event(conn, finished_event)

    @staticmethod
    def _row_to_offer(row: sqlite3.Row | None) -> ChatExecutionOfferView | None:
        if row is None:
            return None
        return ChatExecutionOfferView(
            offer_id=str(row["offer_id"]),
            session_id=str(row["session_id"]),
            turn_id=str(row["turn_id"]),
            client_request_id=str(row["client_request_id"]),
            goal=str(row["goal"]),
            summary=str(row["summary"]),
            status=str(row["status"]),  # type: ignore[arg-type]
            run_id=str(row["run_id"]) if row["run_id"] is not None else None,
            run_client_request_id=str(row["run_client_request_id"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _row_to_chat_turn(
        row: sqlite3.Row, offer: ChatExecutionOfferView | None = None
    ) -> ChatTurnView:
        raw_reads = json.loads(str(row["tool_reads_json"]))
        tool_reads = [ChatToolReadRecord.model_validate(item) for item in raw_reads]
        row_cols = set(row.keys())
        raw_refs = json.loads(str(row["references_json"])) if "references_json" in row_cols else []
        references: list[ChatReferenceItem] = [
            DiffHunkReference.model_validate(r)
            if isinstance(r, dict) and r.get("kind") == "diff_hunk"
            else CommandReference.model_validate(r)
            for r in raw_refs
        ]
        raw_ev = (
            json.loads(str(row["resolved_evidence_json"]))
            if "resolved_evidence_json" in row_cols
            else []
        )
        resolved_evidence = [ResolvedChatEvidence.model_validate(ev) for ev in raw_ev]
        intent_mode: ChatIntentMode = (
            str(row["intent_mode"]) if "intent_mode" in row_cols and row["intent_mode"] else "readonly"
        )  # type: ignore[assignment]
        return ChatTurnView(
            turn_id=str(row["turn_id"]),
            project_id=str(row["project_id"]),
            session_id=str(row["session_id"]),
            client_request_id=str(row["client_request_id"]),
            seq=int(row["seq"]),
            status=str(row["status"]),  # type: ignore[arg-type]
            intent_mode=intent_mode,
            user_message=str(row["user_message"]),
            references=references,
            resolved_evidence=resolved_evidence,
            tool_reads=tool_reads,
            assistant_message=str(row["assistant_message"]) if row["assistant_message"] is not None else None,
            execution_offer=offer,
            error_code=str(row["error_code"]) if row["error_code"] is not None else None,
            error_message=str(row["error_message"]) if row["error_message"] is not None else None,
            provider_mode=str(row["provider_mode"]),
            context_truncated=bool(int(row["context_truncated"])),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def _load_offers_for_turns(
        self, session_id: str, turn_ids: list[str]
    ) -> dict[str, ChatExecutionOfferView]:
        if not turn_ids:
            return {}
        placeholders = ",".join("?" for _ in turn_ids)
        rows = self._conn.execute(
            f"SELECT * FROM chat_execution_offers WHERE session_id = ? AND turn_id IN ({placeholders})",
            (session_id, *turn_ids),
        ).fetchall()
        out: dict[str, ChatExecutionOfferView] = {}
        for r in rows:
            ov = self._row_to_offer(r)
            if ov is not None:
                out[ov.turn_id] = ov
        return out

    def get_chat_turn_by_client_request_id(
        self, *, session_id: str, client_request_id: str
    ) -> tuple[str, ChatTurnView] | None:
        self._require_open()
        row = self._conn.execute(
            """
            SELECT * FROM chat_turns
             WHERE session_id = ? AND client_request_id = ?
            """,
            (session_id, client_request_id),
        ).fetchone()
        if row is None:
            return None
        offer_row = self._conn.execute(
            "SELECT * FROM chat_execution_offers WHERE session_id = ? AND turn_id = ?",
            (session_id, str(row["turn_id"])),
        ).fetchone()
        return str(row["payload_digest"]), self._row_to_chat_turn(
            row, self._row_to_offer(offer_row)
        )

    def insert_chat_turn_running(
        self,
        *,
        turn_id: str,
        project_id: str,
        session_id: str,
        client_request_id: str,
        payload_digest: str,
        user_message: str,
        provider_mode: str,
        created_at: str,
        references: list[ChatReferenceItem] | None = None,
        resolved_evidence: list[ResolvedChatEvidence] | None = None,
        intent_mode: ChatIntentMode = "readonly",
    ) -> ChatTurnView:
        ref_list = list(references or [])
        ev_list = list(resolved_evidence or [])
        refs_json = canonical_json_dumps([r.model_dump(mode="json") for r in ref_list])
        ev_json = canonical_json_dumps([e.model_dump(mode="json") for e in ev_list])
        with self.transaction() as conn:
            seq_row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM chat_turns WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            next_seq = int(seq_row["next_seq"]) if seq_row is not None else 1
            conn.execute(
                """
                INSERT INTO chat_turns(
                    turn_id, project_id, session_id, seq, client_request_id,
                    payload_digest, status, intent_mode, user_message, references_json,
                    resolved_evidence_json, tool_reads_json,
                    assistant_message, error_code, error_message,
                    provider_mode, context_truncated, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, '[]', NULL, NULL, NULL, ?, 0, ?, ?)
                """,
                (
                    turn_id,
                    project_id,
                    session_id,
                    next_seq,
                    client_request_id,
                    payload_digest,
                    intent_mode,
                    user_message,
                    refs_json,
                    ev_json,
                    provider_mode,
                    created_at,
                    created_at,
                ),
            )
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                (created_at, session_id),
            )
        return ChatTurnView(
            turn_id=turn_id,
            project_id=project_id,
            session_id=session_id,
            client_request_id=client_request_id,
            seq=next_seq,
            status="running",
            intent_mode=intent_mode,
            user_message=user_message,
            references=ref_list,
            resolved_evidence=ev_list,
            tool_reads=[],
            assistant_message=None,
            error_code=None,
            error_message=None,
            provider_mode=provider_mode,
            context_truncated=False,
            created_at=created_at,
            updated_at=created_at,
        )

    def finalize_chat_turn(self, *, turn: ChatTurnView) -> ChatTurnView:
        tool_reads_json = canonical_json_dumps(
            [item.model_dump(mode="json") for item in turn.tool_reads]
        )
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE chat_turns
                   SET status = ?,
                       tool_reads_json = ?,
                       assistant_message = ?,
                       error_code = ?,
                       error_message = ?,
                       context_truncated = ?,
                       updated_at = ?
                 WHERE turn_id = ? AND session_id = ?
                """,
                (
                    turn.status,
                    tool_reads_json,
                    turn.assistant_message,
                    turn.error_code,
                    turn.error_message,
                    1 if turn.context_truncated else 0,
                    turn.updated_at,
                    turn.turn_id,
                    turn.session_id,
                ),
            )
            if turn.execution_offer is not None:
                ov = turn.execution_offer
                conn.execute(
                    """
                    INSERT INTO chat_execution_offers(
                        offer_id, session_id, turn_id, client_request_id,
                        goal, summary, status, run_id, run_client_request_id,
                        created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(turn_id) DO NOTHING
                    """,
                    (
                        ov.offer_id,
                        ov.session_id,
                        ov.turn_id,
                        ov.client_request_id,
                        ov.goal,
                        ov.summary,
                        ov.status,
                        ov.run_id,
                        ov.run_client_request_id,
                        ov.created_at,
                        ov.updated_at,
                    ),
                )
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                (turn.updated_at, turn.session_id),
            )
        return turn

    def get_chat_execution_offer(
        self, *, session_id: str, turn_id: str
    ) -> ChatExecutionOfferView | None:
        self._require_open()
        row = self._conn.execute(
            "SELECT * FROM chat_execution_offers WHERE session_id = ? AND turn_id = ?",
            (session_id, turn_id),
        ).fetchone()
        return self._row_to_offer(row)

    def mark_chat_execution_offer_started(
        self,
        *,
        session_id: str,
        turn_id: str,
        offer_id: str,
        run_id: str,
        updated_at: str,
    ) -> ChatExecutionOfferView | None:
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE chat_execution_offers
                   SET status = 'started',
                       run_id = ?,
                       updated_at = ?
                 WHERE session_id = ? AND turn_id = ? AND offer_id = ?
                """,
                (run_id, updated_at, session_id, turn_id, offer_id),
            )
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                (updated_at, session_id),
            )
        return self.get_chat_execution_offer(session_id=session_id, turn_id=turn_id)

    def list_chat_turns(
        self, *, session_id: str, after_seq: int = 0, limit: int = 50
    ) -> ChatTurnsPage:
        self._require_open()
        rows = self._conn.execute(
            """
            SELECT * FROM chat_turns
             WHERE session_id = ? AND seq > ?
             ORDER BY seq ASC
             LIMIT ?
            """,
            (session_id, after_seq, limit + 1),
        ).fetchall()
        has_more = len(rows) > limit
        sliced = rows[:limit]
        offers = self._load_offers_for_turns(
            session_id, [str(r["turn_id"]) for r in sliced]
        )
        items = [self._row_to_chat_turn(r, offers.get(str(r["turn_id"]))) for r in sliced]
        next_after = items[-1].seq if items else after_seq
        return ChatTurnsPage(
            session_id=session_id,
            items=items,
            next_after_seq=next_after,
            has_more=has_more,
        )

    def list_started_offer_run_ids_desc(self, *, session_id: str) -> list[str]:
        """Return started execution-offer run_ids for a session ordered newest offer first."""
        self._require_open()
        rows = self._conn.execute(
            """
            SELECT o.run_id
              FROM chat_execution_offers o
              JOIN chat_turns t ON t.turn_id = o.turn_id
             WHERE o.session_id = ? AND o.status = 'started' AND o.run_id IS NOT NULL
             ORDER BY t.seq DESC, o.rowid DESC
            """,
            (session_id,),
        ).fetchall()
        return [str(r["run_id"]) for r in rows if r["run_id"]]

    def list_recent_completed_chat_turns(
        self, *, session_id: str, limit: int = 6
    ) -> list[ChatTurnView]:
        self._require_open()
        rows = self._conn.execute(
            """
            SELECT * FROM chat_turns
             WHERE session_id = ? AND status = 'completed'
             ORDER BY seq DESC
             LIMIT ?
            """,
            (session_id, limit),
        ).fetchall()
        return [self._row_to_chat_turn(r) for r in reversed(rows)]

    def list_project_session_titles(self, *, project_id: str) -> list[SessionTitleView]:
        self._require_open()
        rows = self._conn.execute(
            """
            SELECT s.session_id,
                   s.project_id,
                   s.updated_at,
                   (SELECT ct.user_message
                      FROM chat_turns ct
                     WHERE ct.session_id = s.session_id
                     ORDER BY ct.seq ASC
                     LIMIT 1) AS first_chat_msg,
                   (SELECT hi.text
                      FROM history_items hi
                     WHERE hi.session_id = s.session_id AND hi.kind = 'user_goal'
                     ORDER BY hi.seq ASC
                     LIMIT 1) AS first_goal_msg
              FROM sessions s
             WHERE s.project_id = ?
             ORDER BY s.seq_order ASC
            """,
            (project_id,),
        ).fetchall()
        titles: list[SessionTitleView] = []
        for r in rows:
            sid = str(r["session_id"])
            first_chat = r["first_chat_msg"]
            first_goal = r["first_goal_msg"]
            if isinstance(first_chat, str) and first_chat.strip():
                src: SessionTitleSource = "chat_turn"
                title = _derive_bounded_title(first_chat, sid)
            elif isinstance(first_goal, str) and first_goal.strip():
                src = "history_goal"
                title = _derive_bounded_title(first_goal, sid)
            else:
                src = "session_id"
                title = _derive_bounded_title(None, sid)
            titles.append(
                SessionTitleView(
                    session_id=sid,
                    project_id=str(r["project_id"]),
                    title=title,
                    source=src,
                    updated_at=str(r["updated_at"]),
                )
            )
        return titles

    def close(self) -> None:
        """Close the SQLite connection cleanly."""
        if self._closed:
            return
        self._safe_close_conn()
