from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _parse_env_file(path: str = ".env") -> dict[str, str]:
    env: dict[str, str] = {}
    env_path = Path(path)
    if not env_path.exists():
        return env

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


class Settings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    app_name: str = "xrxs-263-sync"
    log_level: str = "INFO"
    timezone: str = "Asia/Shanghai"
    sync_interval_minutes: int = 30
    dry_run: bool = False

    state_db_path: Path = Path("/data/sync-state.db")

    sync_disable_absent_users: bool = True
    sync_delete_absent_users: bool = False
    sync_update_password: bool = False
    sync_default_password: str = "ChangeMe123!"
    sync_force_change_password: bool = True
    sync_userid_mode: str = "email_localpart"
    sync_name_preserve_userids: list[str] = Field(default_factory=list)

    xrxs_adapter: str = "twohaohr"
    xrxs_base_url: str = ""
    xrxs_access_token: str = ""
    xrxs_token_endpoint: str = "/authorize/oauth/token"
    xrxs_client_id: str = ""
    xrxs_client_secret: str = ""
    xrxs_departments_endpoint: str = "/v5/department/list"
    xrxs_employees_by_department_endpoint: str = "/v5/employee/list"
    xrxs_employee_base_info_endpoint: str = "/api/employees/base_info/"
    xrxs_page_size: int = 100
    xrxs_fetch_child_departments: bool = True
    xrxs_company_id: str = ""

    mail263_wsdl_url: str = ""
    mail263_endpoint_url: str = "https://macom.263.net/api/mail/v2"
    mail263_domain: str = ""
    mail263_account: str = ""
    mail263_key: str = ""
    mail263_admin_userid: str = "admin"
    mail263_gid: int = 33
    mail263_role_id: int = 0
    mail263_request_timeout_seconds: int = 30
    mail263_retry_max_attempts: int = 5
    mail263_retry_initial_delay_seconds: float = 3.0
    mail263_retry_backoff_multiplier: float = 2.0
    mail263_request_interval_seconds: float = 0.3

    mail263_sso_enabled: bool = False
    mail263_partner_id: str = ""
    mail263_auth_corp_id: str = ""
    mail263_sso_key: str = ""
    mail263_sso_base_url: str = "https://weixin.263.net/partner/web/third/mail/loginMail.do"

    @property
    def sync_requires_delete(self) -> bool:
        return self.sync_delete_absent_users

    @field_validator("*", mode="before")
    @classmethod
    def strip_string_values(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value


def _convert_value(value: str, annotation: Any) -> Any:
    origin = get_origin(annotation)
    if origin is None:
        target = annotation
    elif origin is list:
        return [item.strip() for item in value.split(",") if item.strip()]
    else:
        args = [arg for arg in get_args(annotation) if arg is not type(None)]
        target = args[0] if args else str

    if target is bool:
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if target is int:
        return int(value)
    if target is float:
        return float(value)
    if target is Path:
        return Path(value)
    return value


@lru_cache
def get_settings() -> Settings:
    env_data = _parse_env_file()
    merged: dict[str, Any] = {}

    for field_name, field_info in Settings.model_fields.items():
        env_key = field_name.upper()
        raw_value = os.environ.get(env_key, env_data.get(env_key))
        if raw_value is None:
            continue
        merged[field_name] = _convert_value(raw_value, field_info.annotation)

    return Settings(**merged)
