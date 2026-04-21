from __future__ import annotations

import sqlite3
from pathlib import Path


class StateStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS department_mapping (
                    source_department_id TEXT PRIMARY KEY,
                    target_department_id TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_mapping (
                    source_user_id TEXT PRIMARY KEY,
                    target_user_id TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def get_department_mapping(self) -> dict[str, str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT source_department_id, target_department_id FROM department_mapping"
            ).fetchall()
        return {row["source_department_id"]: row["target_department_id"] for row in rows}

    def upsert_department_mapping(self, source_department_id: str, target_department_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO department_mapping (source_department_id, target_department_id)
                VALUES (?, ?)
                ON CONFLICT(source_department_id)
                DO UPDATE SET target_department_id = excluded.target_department_id
                """,
                (source_department_id, target_department_id),
            )
            conn.commit()

    def get_user_mapping(self) -> dict[str, str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT source_user_id, target_user_id FROM user_mapping"
            ).fetchall()
        return {row["source_user_id"]: row["target_user_id"] for row in rows}

    def upsert_user_mapping(self, source_user_id: str, target_user_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO user_mapping (source_user_id, target_user_id)
                VALUES (?, ?)
                ON CONFLICT(source_user_id)
                DO UPDATE SET target_user_id = excluded.target_user_id
                """,
                (source_user_id, target_user_id),
            )
            conn.commit()
