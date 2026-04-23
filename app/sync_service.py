from __future__ import annotations

import logging
from dataclasses import asdict

from app.config import Settings
from app.mail263_client import Mail263Client
from app.models import Mail263Department, Mail263User, SourceDepartment, SourceUser, SyncStats
from app.state import StateStore
from app.xrxs_client import XrxsClient

LOGGER = logging.getLogger(__name__)


class SyncService:
    def __init__(
        self,
        settings: Settings,
        source_client: XrxsClient,
        target_client: Mail263Client,
        state_store: StateStore,
    ) -> None:
        self.settings = settings
        self.source_client = source_client
        self.target_client = target_client
        self.state_store = state_store

    def run(self) -> dict[str, object]:
        LOGGER.info("开始同步：dry_run=%s", self.settings.dry_run)
        summary: dict[str, list[str]] = {
            "departments_created": [],
            "departments_updated": [],
            "users_created": [],
            "users_updated": [],
            "users_disabled": [],
            "users_skipped_unmatched": [],
            "users_skipped_disabled_departed": [],
        }
        source_departments = self.source_client.list_departments()
        source_users = self.source_client.list_users(source_departments)
        target_departments = self.target_client.list_departments()
        target_users = self.target_client.list_users()

        stats = SyncStats()
        department_mapping = self._sync_departments(source_departments, target_departments, stats, summary)
        self._sync_users(source_users, target_users, target_departments, department_mapping, stats, summary)

        result = {
            "dry_run": self.settings.dry_run,
            "stats": stats.as_dict(),
            "source_departments": len(source_departments),
            "source_users": len(source_users),
            "target_departments": len(target_departments),
            "target_users": len(target_users),
        }
        LOGGER.info("同步完成：%s", result)
        self._log_summary(result, summary)
        return result

    def _sync_departments(
        self,
        source_departments: list[SourceDepartment],
        target_departments: list[Mail263Department],
        stats: SyncStats,
        summary: dict[str, list[str]],
    ) -> dict[str, str]:
        existing_mapping = self.state_store.get_department_mapping()
        by_target_id = {dept.department_id: dept for dept in target_departments}
        created_mapping: dict[str, str] = dict(existing_mapping)

        remaining = {dept.source_id: dept for dept in source_departments}
        while remaining:
            progressed = False
            for source_id, source_dept in list(remaining.items()):
                if source_dept.parent_source_id:
                    parent_target_id = created_mapping.get(source_dept.parent_source_id)
                    if parent_target_id is None:
                        continue
                else:
                    parent_target_id = "0"

                target_id = created_mapping.get(source_id)
                description = f"synced_from_xrxs:{source_id}"

                if not target_id:
                    matched = self._find_department_by_name(target_departments, source_dept.name, parent_target_id)
                    if matched:
                        target_id = matched.department_id
                        LOGGER.info(
                            "department matched: name=%s source_id=%s target_id=%s parent_target_id=%s",
                            source_dept.name,
                            source_id,
                            target_id,
                            parent_target_id,
                        )
                    else:
                        if self.settings.dry_run:
                            target_id = f"dry-run-{source_id}"
                        else:
                            target_id = str(
                                self.target_client.create_department(
                                    name=source_dept.name,
                                    parent_id=int(parent_target_id),
                                    description=description,
                                )
                            )
                        LOGGER.info(
                            "department created: name=%s source_id=%s target_id=%s parent_target_id=%s dry_run=%s",
                            source_dept.name,
                            source_id,
                            target_id,
                            parent_target_id,
                            self.settings.dry_run,
                        )
                        summary["departments_created"].append(source_dept.name)
                        stats.departments_created += 1
                    created_mapping[source_id] = target_id
                    if not self.settings.dry_run:
                        self.state_store.upsert_department_mapping(source_id, target_id)
                else:
                    current = by_target_id.get(target_id)
                    if current and (current.name != source_dept.name or (current.parent_id or "0") != parent_target_id):
                        if not self.settings.dry_run:
                            self.target_client.update_department(
                                department_id=int(target_id),
                                name=source_dept.name,
                                parent_id=int(parent_target_id),
                                description=description,
                            )
                        LOGGER.info(
                            "department updated: name=%s source_id=%s target_id=%s old_parent=%s new_parent=%s dry_run=%s",
                            source_dept.name,
                            source_id,
                            target_id,
                            current.parent_id or "0",
                            parent_target_id,
                            self.settings.dry_run,
                        )
                        summary["departments_updated"].append(source_dept.name)
                        stats.departments_updated += 1

                del remaining[source_id]
                progressed = True

            if not progressed:
                unresolved = ", ".join(sorted(remaining.keys()))
                raise RuntimeError(f"Unresolved department parent mapping for: {unresolved}")

        return created_mapping

    @staticmethod
    def _find_department_by_name(
        target_departments: list[Mail263Department], name: str, parent_id: str
    ) -> Mail263Department | None:
        for item in target_departments:
            if item.name == name and (item.parent_id or "0") == parent_id:
                return item
        return None

    def _sync_users(
        self,
        source_users: list[SourceUser],
        target_users: list[Mail263User],
        target_departments: list[Mail263Department],
        department_mapping: dict[str, str],
        stats: SyncStats,
        summary: dict[str, list[str]],
    ) -> None:
        target_by_userid = {user.userid: user for user in target_users}
        target_department_names = {dept.department_id: dept.name for dept in target_departments}
        source_userids: set[str] = set()

        for source_user in source_users:
            userid = self._to_mail263_userid(source_user.email)
            source_userids.add(userid)
            department_id = self._pick_department_id(source_user, department_mapping)
            current = target_by_userid.get(userid)

            if current is None:
                if not source_user.active:
                    LOGGER.info(
                        "skip inactive source user without 263 match: userid=%s email=%s",
                        userid,
                        source_user.email,
                    )
                    continue
                if not self.settings.dry_run:
                    self.target_client.create_user(
                        userid=userid,
                        password=source_user.password or self.settings.sync_default_password,
                        department_id=int(department_id),
                        full_name=source_user.full_name,
                    )
                    self.state_store.upsert_user_mapping(source_user.source_id, userid)
                LOGGER.info(
                    "user created: userid=%s email=%s department_id=%s dry_run=%s",
                    userid,
                    source_user.email,
                    department_id,
                    self.settings.dry_run,
                )
                summary["users_created"].append(f"{source_user.full_name}<{userid}>")
                stats.users_created += 1
                continue

            if self._is_disabled_departed_backup_user(current, target_department_names):
                LOGGER.info(
                    "skip disabled 263 user in departed backup department: userid=%s email=%s departments=%s",
                    userid,
                    source_user.email,
                    current.department_ids,
                )
                summary["users_skipped_disabled_departed"].append(
                    f"{current.full_name or source_user.full_name}<{userid}>"
                )
                continue

            desired_full_name = self._desired_full_name(userid, source_user.full_name, current.full_name)
            needs_update = (
                current.full_name != desired_full_name
                or department_id not in current.department_ids
            )
            if needs_update:
                if not self.settings.dry_run:
                    self.target_client.update_user(
                        userid=userid,
                        department_ids=[int(department_id)],
                        full_name=desired_full_name,
                    )
                LOGGER.info(
                    "user updated: userid=%s email=%s old_name=%s new_name=%s old_departments=%s new_department=%s dry_run=%s",
                    userid,
                    source_user.email,
                    current.full_name,
                    desired_full_name,
                    current.department_ids,
                    department_id,
                    self.settings.dry_run,
                )
                summary["users_updated"].append(f"{desired_full_name}<{userid}>:{current.department_ids}->{department_id}")
                stats.users_updated += 1

            desired_enabled = source_user.active
            current_enabled = current.status != 0 if current.status is not None else True
            if desired_enabled != current_enabled:
                if not self.settings.dry_run:
                    self.target_client.set_user_status(user=current, enabled=desired_enabled)
                LOGGER.info(
                    "user status changed: userid=%s email=%s from=%s to=%s dry_run=%s",
                    userid,
                    source_user.email,
                    "enabled" if current_enabled else "disabled",
                    "enabled" if desired_enabled else "disabled",
                    self.settings.dry_run,
                )
                if not desired_enabled:
                    summary["users_disabled"].append(f"{desired_full_name}<{userid}>")
                stats.users_disabled += 1 if not desired_enabled else 0

            if self.settings.sync_update_password and source_user.password:
                if not self.settings.dry_run:
                    self.target_client.update_password(userid, source_user.password)
                LOGGER.info(
                    "user password updated: userid=%s email=%s dry_run=%s",
                    userid,
                    source_user.email,
                    self.settings.dry_run,
                )

        for stale_userid, target_user in target_by_userid.items():
            if stale_userid in source_userids:
                continue
            LOGGER.info(
                "skip unmatched 263 user: userid=%s, full_email=%s",
                stale_userid,
                target_user.full_email,
            )
            summary["users_skipped_unmatched"].append(target_user.full_email)

    def _to_mail263_userid(self, email: str) -> str:
        if self.settings.sync_userid_mode == "email_full":
            return email
        if "@" not in email:
            return email
        return email.split("@", 1)[0]

    def _desired_full_name(self, userid: str, source_full_name: str, current_full_name: str) -> str:
        if userid in self.settings.sync_name_preserve_userids and current_full_name.strip():
            return current_full_name
        return source_full_name

    def _log_summary(self, result: dict[str, object], summary: dict[str, list[str]]) -> None:
        stats = result["stats"]
        LOGGER.info(
            "本轮同步摘要：部门新增 %s，部门更新 %s；用户新增 %s，用户更新 %s，用户禁用 %s；源端部门 %s、用户 %s；目标端部门 %s、用户 %s",
            stats["departments_created"],
            stats["departments_updated"],
            stats["users_created"],
            stats["users_updated"],
            stats["users_disabled"],
            result["source_departments"],
            result["source_users"],
            result["target_departments"],
            result["target_users"],
        )
        self._log_summary_examples("本轮新建部门", summary["departments_created"])
        self._log_summary_examples("本轮更新部门", summary["departments_updated"])
        self._log_summary_examples("本轮新建用户", summary["users_created"])
        self._log_summary_examples("本轮更新用户", summary["users_updated"])
        self._log_summary_examples("本轮禁用用户", summary["users_disabled"])
        self._log_summary_examples("本轮跳过的未匹配 263 账号", summary["users_skipped_unmatched"])
        self._log_summary_examples("本轮跳过的离职待备份禁用账号", summary["users_skipped_disabled_departed"])

    @staticmethod
    def _log_summary_examples(label: str, items: list[str], limit: int = 8) -> None:
        if not items:
            return
        preview = "; ".join(items[:limit])
        suffix = "" if len(items) <= limit else f"；其余 {len(items) - limit} 条未展开"
        LOGGER.info("%s：%s%s", label, preview, suffix)

    @staticmethod
    def _pick_department_id(source_user: SourceUser, department_mapping: dict[str, str]) -> str:
        for source_department_id in source_user.department_source_ids:
            target_id = department_mapping.get(source_department_id)
            if target_id:
                return target_id
        return "-1"

    @staticmethod
    def _is_disabled_departed_backup_user(user: Mail263User, department_names: dict[str, str]) -> bool:
        if user.status != 0:
            return False
        return any(department_names.get(department_id) == "离职待备份" for department_id in user.department_ids)
