from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from datetime import datetime
from email.utils import quote
from typing import Any

import requests

from app.config import Settings
from app.models import Mail263Department, Mail263User

LOGGER = logging.getLogger(__name__)


def _md5(raw: str) -> str:
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _gbk_base64(raw: str) -> str:
    if not raw:
        return ""
    return base64.b64encode(raw.encode("gbk", errors="ignore")).decode("ascii")


class Mail263Client:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.session = requests.Session()
        self._last_request_monotonic: float = 0.0

    def _timestamp_ms(self) -> int:
        return int(time.time() * 1000)

    def _api_base_url(self) -> str:
        return self.settings.mail263_endpoint_url.rstrip("/")

    def _normalize_for_sign(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in payload.items() if key != "sign" and value is not None}

    def _sign_payload(self, payload: dict[str, Any]) -> str:
        normalized = self._normalize_for_sign(payload)
        canonical = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        return _md5(f"{canonical}{self.settings.mail263_key}")

    def _throttle(self) -> None:
        interval = max(self.settings.mail263_request_interval_seconds, 0.0)
        if interval <= 0:
            return
        now = time.monotonic()
        elapsed = now - self._last_request_monotonic
        if elapsed < interval:
            time.sleep(interval - elapsed)

    def _post_json(self, path: str, payload: dict[str, Any]) -> Any:
        delay_seconds = max(self.settings.mail263_retry_initial_delay_seconds, 0.0)
        for attempt in range(1, self.settings.mail263_retry_max_attempts + 1):
            request_payload = {
                "tag": str(self._timestamp_ms()),
                "account": self.settings.mail263_account,
                "ts": self._timestamp_ms(),
                **payload,
            }
            request_payload["sign"] = self._sign_payload(request_payload)

            self._throttle()
            response = self.session.post(
                f"{self._api_base_url()}/{path.lstrip('/')}",
                json=request_payload,
                headers={
                    "Content-Type": "application/json;charset=utf-8",
                    "Accept": "application/json",
                },
                timeout=self.settings.mail263_request_timeout_seconds,
            )
            self._last_request_monotonic = time.monotonic()
            response.raise_for_status()
            result = response.json()
            errcode = int(result.get("errcode", -1))
            if errcode == 0:
                return result.get("data")

            errmsg = result.get("errmsg", "unknown error")
            is_rate_limited = errcode == -1042
            if is_rate_limited and attempt < self.settings.mail263_retry_max_attempts:
                LOGGER.warning(
                    "263 API rate limited for %s on attempt %s/%s, sleeping %.1fs: errcode=%s errmsg=%s",
                    path,
                    attempt,
                    self.settings.mail263_retry_max_attempts,
                    delay_seconds,
                    errcode,
                    errmsg,
                )
                time.sleep(delay_seconds)
                delay_seconds *= max(self.settings.mail263_retry_backoff_multiplier, 1.0)
                continue

            raise RuntimeError(f"263 API request failed for {path}: errcode={errcode}, errmsg={errmsg}")

        raise RuntimeError(f"263 API request failed for {path}: exhausted retry attempts")

    def list_departments(self, department_id: int | None = None) -> list[Mail263Department]:
        payload: dict[str, Any] = {"domain": self.settings.mail263_domain}
        if department_id is not None:
            payload["departmentid"] = department_id
        result = self._post_json("/depts/get", payload) or []
        departments: list[Mail263Department] = []
        for item in result:
            departments.append(
                Mail263Department(
                    department_id=str(item.get("departmentid") or ""),
                    name=str(item.get("name") or "").strip(),
                    parent_id=str(item.get("parent")) if item.get("parent") is not None else None,
                    description=str(item.get("description") or "").strip(),
                )
            )
        return departments

    def create_department(self, name: str, parent_id: int, description: str = "") -> int:
        result = self._post_json(
            "/depts/create",
            {
                "domain": self.settings.mail263_domain,
                "name": name,
                "parent": parent_id,
                "description": description or None,
            },
        )
        return int(result)

    def update_department(self, department_id: int, name: str, parent_id: int, description: str = "") -> int:
        self._post_json(
            "/depts/update",
            {
                "domain": self.settings.mail263_domain,
                "departmentid": department_id,
                "name": name,
                "parent": parent_id,
                "description": description or None,
            },
        )
        return 0

    def delete_department(self, department_id: str) -> int:
        self._post_json(
            "/depts/delete",
            {
                "domain": self.settings.mail263_domain,
                "departmentid": int(department_id),
            },
        )
        return 0

    def list_users(self) -> list[Mail263User]:
        result = self._post_json("/user/list", {"domain": self.settings.mail263_domain}) or []
        users: list[Mail263User] = []
        for item in result:
            full_email = str(item.get("xmuserid") or "").strip()
            localpart = full_email.split("@", 1)[0] if "@" in full_email else full_email
            dept_values = item.get("deptids") or item.get("deptId") or []
            if isinstance(dept_values, list):
                department_ids = [str(dep) for dep in dept_values if dep is not None]
            else:
                department_ids = [str(dept_values)] if dept_values not in (None, "") else []
            users.append(
                Mail263User(
                    userid=localpart,
                    full_email=full_email,
                    full_name=str(item.get("xmname") or "").strip(),
                    department_ids=department_ids,
                    title=str(item.get("xmposition") or "").strip(),
                    mobile=str(item.get("xmcell") or "").strip(),
                    phone=str(item.get("xmtel") or "").strip(),
                    fax=str(item.get("xmfax") or "").strip(),
                    status=int(item.get("mailstatus")) if item.get("mailstatus") is not None else None,
                )
            )
        return users

    def create_user(
        self,
        userid: str,
        password: str,
        department_id: int,
        full_name: str,
        title: str = "",
        mobile: str = "",
        phone: str = "",
    ) -> int:
        self._post_json(
            "/user/create",
            {
                "xmuserid": userid,
                "domain": self.settings.mail263_domain,
                "passwd": _md5(password),
                "gid": self.settings.mail263_gid,
                "roleid": self.settings.mail263_role_id,
                "deptids": [department_id],
                "xmname": _gbk_base64(full_name),
                "changepwd": 1 if self.settings.sync_force_change_password else 0,
                "mailstatus": 1,
                "emstatus": 1,
                "tbpstatus": 0,
                "secret": 1,
                "xmposition": _gbk_base64(title) if title else None,
                "xmtel": phone or None,
                "xmcell": mobile or None,
            },
        )
        return 0

    def update_user(
        self,
        userid: str,
        department_ids: list[int],
        full_name: str,
        title: str = "",
        mobile: str = "",
        phone: str = "",
        enabled: bool | None = None,
    ) -> int:
        payload: dict[str, Any] = {
            "xmuserid": userid,
            "domain": self.settings.mail263_domain,
            "deptids": department_ids,
            "xmname": _gbk_base64(full_name) if full_name else None,
            "xmposition": _gbk_base64(title) if title else None,
            "xmtel": phone or None,
            "xmcell": mobile or None,
        }
        if enabled is not None:
            payload["mailstatus"] = 1 if enabled else 0
            payload["emstatus"] = 1 if enabled else 0

        self._post_json("/user/update", payload)
        return 0

    def set_user_status(self, user: Mail263User, enabled: bool) -> int:
        department_ids = [int(dep) for dep in user.department_ids if str(dep).strip()]
        if not department_ids:
            raise RuntimeError(f"Cannot change 263 user status without department ids: {user.userid}")
        return self.update_user(
            userid=user.userid,
            department_ids=department_ids,
            full_name=user.full_name,
            title=user.title,
            mobile=user.mobile,
            phone=user.phone,
            enabled=enabled,
        )

    def update_password(self, userid: str, password: str) -> int:
        self._post_json(
            "/user/modpwd",
            {
                "xmuserid": userid,
                "domain": self.settings.mail263_domain,
                "passwd": _md5(password),
            },
        )
        return 0

    def delete_user(self, userid: str) -> int:
        self._post_json(
            "/user/delete",
            {
                "xmuserid": userid,
                "domain": self.settings.mail263_domain,
            },
        )
        return 0

    def build_sso_url(self, full_email: str, login_platform: str = "windows") -> str:
        timestamp = str(int(datetime.now().timestamp() * 1000))
        sign = hashlib.md5(
            (
                f"{self.settings.mail263_sso_key}"
                f"{login_platform}"
                f"READMAIL"
                f"{self.settings.mail263_partner_id}"
                f"{self.settings.mail263_auth_corp_id}"
                f"{full_email}"
                f"{timestamp}"
            ).encode("utf-8")
        ).hexdigest()
        return (
            f"{self.settings.mail263_sso_base_url}?"
            f"loginPlatform={quote(login_platform)}&"
            f"type=READMAIL&"
            f"partnerid={quote(self.settings.mail263_partner_id)}&"
            f"authcorpid={quote(self.settings.mail263_auth_corp_id)}&"
            f"userid={quote(full_email)}&"
            f"timestamp={quote(timestamp)}&"
            f"sign={quote(sign)}"
        )
