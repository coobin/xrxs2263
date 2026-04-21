from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class SourceDepartment:
    source_id: str
    name: str
    parent_source_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SourceUser:
    source_id: str
    email: str
    full_name: str
    department_source_ids: list[str]
    title: str = ""
    mobile: str = ""
    phone: str = ""
    active: bool = True
    password: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Mail263Department:
    department_id: str
    name: str
    parent_id: str | None = None
    description: str = ""


@dataclass(slots=True)
class Mail263User:
    userid: str
    full_email: str
    full_name: str
    department_ids: list[str]
    title: str = ""
    mobile: str = ""
    phone: str = ""
    fax: str = ""
    status: int | None = None


@dataclass(slots=True)
class SyncStats:
    departments_created: int = 0
    departments_updated: int = 0
    users_created: int = 0
    users_updated: int = 0
    users_disabled: int = 0
    users_deleted: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "departments_created": self.departments_created,
            "departments_updated": self.departments_updated,
            "users_created": self.users_created,
            "users_updated": self.users_updated,
            "users_disabled": self.users_disabled,
            "users_deleted": self.users_deleted,
        }
