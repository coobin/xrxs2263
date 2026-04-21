from __future__ import annotations

import base64
import hmac
import json
import logging
import time
from hashlib import sha1
from urllib.parse import quote_plus

import requests

from app.config import Settings
from app.models import SourceDepartment, SourceUser

LOGGER = logging.getLogger(__name__)


class XrxsClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.session = requests.Session()
        self.token: str | None = None
        self.token_expires_at: float = 0.0

    def _api_base_url(self) -> str:
        return (self.settings.xrxs_base_url or "https://api.xinrenxinshi.com").rstrip("/")

    def _timestamp_ms(self) -> int:
        return int(time.time() * 1000)

    def _optional_str(self, value: object) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def _required_str(self, value: object, field_name: str) -> str:
        text = self._optional_str(value)
        if not text:
            raise RuntimeError(f"Missing required Xinrenxinshi field: {field_name}")
        return text

    def _username_from_email(self, email: str | None) -> str | None:
        if not email or "@" not in email:
            return None
        return email.split("@", 1)[0].strip() or None

    def _generate_signature(self, content: str) -> str:
        secret = self.settings.xrxs_client_secret
        if not secret:
            raise RuntimeError("XRXS_CLIENT_SECRET must be configured for Xinrenxinshi signing.")
        digest = hmac.new(secret.encode("utf-8"), content.encode("utf-8"), sha1).digest()
        return quote_plus(base64.b64encode(digest).decode("ascii"))

    def _get_access_token(self) -> str:
        now = time.time()
        if self.token and now < self.token_expires_at:
            return self.token

        if not self.settings.xrxs_client_id or not self.settings.xrxs_client_secret:
            raise RuntimeError(
                "XRXS_CLIENT_ID and XRXS_CLIENT_SECRET must be configured."
            )

        token_url = self.settings.xrxs_token_endpoint or f"{self._api_base_url()}/authorize/oauth/token"
        if not token_url.startswith("http://") and not token_url.startswith("https://"):
            token_url = f"{self._api_base_url()}/{token_url.lstrip('/')}"

        response = self.session.post(
            token_url,
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
            data={
                "grant_type": "client_credentials",
                "client_id": self.settings.xrxs_client_id,
                "client_secret": self.settings.xrxs_client_secret,
            },
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        token = payload.get("access_token")
        expires_in = int(payload.get("expires_in", 0))
        if not token:
            raise RuntimeError(f"Failed to acquire Xinrenxinshi access token: {payload}")

        self.token = str(token)
        self.token_expires_at = now + max(expires_in - 300, 60)
        return self.token

    def _post_json(self, path: str, payload: dict) -> dict | list:
        token = self._get_access_token()
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        sign = self._generate_signature(body)
        headers = {
            "access_token": token,
            "Content-Type": "application/json;charset=utf-8",
            "Accept": "application/json",
        }
        if self.settings.xrxs_company_id:
            headers["companyId"] = self.settings.xrxs_company_id

        response = self.session.post(
            f"{self._api_base_url()}{path}",
            params={"sign": sign},
            data=body.encode("utf-8"),
            headers=headers,
            timeout=30,
        )
        response.raise_for_status()
        response_payload = response.json()
        errcode = response_payload.get("errcode")
        if errcode != 0:
            raise RuntimeError(
                f"Xinrenxinshi API request failed for {path}: "
                f"errcode={errcode}, errmsg={response_payload.get('errmsg')}"
            )
        return response_payload.get("data") or {}

    def list_departments(self) -> list[SourceDepartment]:
        payload = {"timestamp": self._timestamp_ms()}
        response = self._post_json(self.settings.xrxs_departments_endpoint, payload)
        items = response if isinstance(response, list) else []
        departments: list[SourceDepartment] = []
        for item in items:
            departments.append(
                SourceDepartment(
                    source_id=self._required_str(item.get("departmentId"), "departmentId"),
                    name=self._required_str(item.get("name"), "department.name"),
                    parent_source_id=self._optional_str(item.get("parentId")),
                    raw=item,
                )
            )
        return departments

    def list_users(self, departments: list[SourceDepartment]) -> list[SourceUser]:
        users: list[SourceUser] = []
        seen_ids: set[str] = set()
        page_no = 0

        while True:
            payload = {
                "pageNo": page_no,
                "pageSize": self.settings.xrxs_page_size,
                "fetchChild": 1 if self.settings.xrxs_fetch_child_departments else 0,
                "status": 0,
                "timestamp": self._timestamp_ms(),
            }
            response = self._post_json(self.settings.xrxs_employees_by_department_endpoint, payload)
            page = response if isinstance(response, dict) else {}
            result = page.get("result") or []

            for item in result:
                employee_id = self._required_str(item.get("employeeId"), "employeeId")
                if employee_id in seen_ids:
                    continue
                seen_ids.add(employee_id)

                fields = item.get("fields") or {}
                email = self._optional_str(item.get("email"))
                if not email:
                    continue
                phone = self._optional_str(fields.get("联系手机")) or self._optional_str(item.get("mobile")) or ""

                title = None
                for key in ("岗位名称", "岗位", "职位", "职务", "职级"):
                    value = self._optional_str(fields.get(key))
                    if not value:
                        continue
                    if key == "岗位" and len(value) >= 24 and value.replace("-", "").isalnum():
                        continue
                    title = value
                    break

                department_id = self._optional_str(fields.get("部门"))
                users.append(
                    SourceUser(
                        source_id=employee_id,
                        email=email,
                        full_name=self._required_str(item.get("name"), "employee.name"),
                        department_source_ids=[department_id] if department_id else [],
                        title=title or "",
                        mobile=phone,
                        active=int(item.get("status", 0)) == 0,
                        raw=item,
                    )
                )

            if not page.get("hasMore"):
                break
            page_no += 1

        return users
