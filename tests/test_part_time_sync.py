from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from app.config import Settings
from app.mail263_client import Mail263Client
from app.models import Mail263Department, Mail263User, SourceUser
from app.state import StateStore
from app.sync_service import SyncService
from app.xrxs_client import XrxsClient


GROUPS = [{"groupId": "tenant-part-time", "groupName": "兼职信息", "isFix": 1}]


def employee(employee_id="employee", department="main"):
    return {
        "employeeId": employee_id, "email": f"{employee_id}@example.com",
        "name": "测试员工", "status": 0, "fields": {"部门": department},
    }


def records(employee_id, *departments):
    return {
        "employeeId": employee_id,
        "recordDataList": [
            {"fields": [{"fieldIdName": "兼职部门", "fieldValue": department,
                         "fieldValueName": "不能作为部门ID的名称"}]}
            for department in departments
        ],
    }


class SourceTests(unittest.TestCase):
    def client(self, *responses, **settings):
        client = XrxsClient(Settings(**settings))
        client._post_json = Mock(side_effect=responses)
        return client

    def test_main_and_multiple_part_time_departments_are_deduplicated(self):
        client = self.client(
            {"result": [employee()], "hasMore": False}, GROUPS,
            [records("employee", "part-a", "main", "part-a", "part-b", "", None)],
        )
        users = client.list_users([])
        self.assertEqual(users[0].department_source_ids, ["main", "part-a", "part-b"])
        payload = client._post_json.call_args.args[1]
        self.assertEqual(payload["groupId"], "tenant-part-time")
        self.assertEqual(payload["hasDel"], 0)
        self.assertEqual(payload["status"], 0)

    def test_pagination_and_group_requests_are_batched_at_100(self):
        client = self.client(
            {"result": [employee(str(i)) for i in range(100)], "hasMore": True},
            {"result": [employee("100")], "hasMore": False}, GROUPS,
            [], [records("100", "part")],
        )
        users = client.list_users([])
        calls = client._post_json.call_args_list
        self.assertEqual([call.args[1]["pageNo"] for call in calls[:2]], [0, 1])
        self.assertEqual([len(call.args[1]["employeeIds"]) for call in calls[3:]], [100, 1])
        self.assertEqual(users[-1].department_source_ids, ["main", "part"])

    def test_empty_records_and_no_records_keep_main_department(self):
        for response in ([], [records("employee")]):
            with self.subTest(response=response):
                client = self.client({"result": [employee()]}, GROUPS, response)
                self.assertEqual(client.list_users([])[0].department_source_ids, ["main"])

    def test_opt_out_does_not_call_group_endpoints(self):
        client = self.client({"result": [employee()]}, xrxs_sync_part_time_departments=False)
        self.assertEqual(client.list_users([])[0].department_source_ids, ["main"])
        self.assertEqual(client._post_json.call_count, 1)

    def test_api_and_schema_errors_stop_sync_before_target_calls(self):
        for response in (
            RuntimeError("errcode=910002"), {},
            [{"employeeId": "employee", "recordDataList": None}],
            [records("other", "part")],
            [{"employeeId": "employee", "recordDataList": [{"fields": None}]}],
        ):
            with self.subTest(response=response):
                client = self.client({"result": [employee()]}, GROUPS, response)
                client.list_departments = Mock(return_value=[])
                target = Mock()
                service = SyncService(Settings(), client, target, Mock())
                with self.assertRaises(RuntimeError):
                    service.run()
                self.assertEqual(target.mock_calls, [])

    def test_missing_group_stops_sync(self):
        client = self.client({"result": [employee()]}, [])
        with self.assertRaisesRegex(RuntimeError, "兼职信息 group"):
            client.list_users([])


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.target = Mock()
        self.service = SyncService(
            Settings(dry_run=False, sync_disable_absent_users=False), Mock(), self.target,
            StateStore(Path(self.temp.name) / "state.db"),
        )
        self.source = SourceUser("employee", "employee@example.com", "测试员工", ["main", "part"])
        self.current = Mail263User("employee", "employee@example.com", "测试员工", ["10"],
                                   title="保留职位", mobile="123", status=1)
        self.departments = [Mail263Department("99", "离职待备份")]

    def sync(self, current=True, dry_run=False, mapping=None):
        self.service.settings.dry_run = dry_run
        self.service.source_client.list_departments.return_value = []
        self.service.source_client.list_users.return_value = [self.source]
        self.target.list_departments.return_value = self.departments
        self.target.list_users.return_value = [self.current] if current else []
        self.service._sync_departments = Mock(return_value=mapping or {"main": "10", "part": "20"})
        return self.service.run()

    def test_add_part_time_updates_all_memberships_and_preserves_contact(self):
        result = self.sync()
        payload = self.target.update_user.call_args.kwargs
        self.assertEqual(payload["department_ids"], [10, 20])
        self.assertEqual(payload["title"], "保留职位")
        self.assertEqual(payload["mobile"], "123")
        self.assertEqual(result["stats"]["users_updated"], 1)

    def test_create_user_receives_all_departments(self):
        self.sync(current=False)
        self.assertEqual(self.target.create_user.call_args.kwargs["department_ids"], [10, 20])

    def test_cancelled_part_time_is_removed(self):
        self.source.department_source_ids = ["main"]
        self.current.department_ids = ["10", "20"]
        self.sync()
        self.assertEqual(self.target.update_user.call_args.kwargs["department_ids"], [10])

    def test_order_and_duplicates_do_not_cause_updates(self):
        self.source.department_source_ids = ["main", "part", "part"]
        self.current.department_ids = ["20", "10"]
        self.sync()
        self.target.update_user.assert_not_called()

    def test_partial_mapping_skips_employee_without_overwriting(self):
        self.sync(mapping={"main": "10"})
        self.target.update_user.assert_not_called()
        self.target.create_user.assert_not_called()

    def test_dry_run_reports_change_without_target_mutations(self):
        result = self.sync(dry_run=True)
        self.assertEqual(result["stats"]["users_updated"], 1)
        self.target.update_user.assert_not_called()
        self.target.create_user.assert_not_called()

    def test_departed_employee_only_moves_to_backup(self):
        self.source.active = False
        self.sync()
        payload = self.target.update_user.call_args.kwargs
        self.assertEqual(payload["department_ids"], [99])
        self.assertFalse(payload["enabled"])

    def test_already_disabled_backup_employee_stays_untouched(self):
        self.current.department_ids = ["99"]
        self.current.status = 0
        self.sync()
        self.target.update_user.assert_not_called()


class MailClientTests(unittest.TestCase):
    def test_repeated_account_rows_merge_all_departments(self):
        client = Mail263Client(Settings())
        client._post_json = Mock(return_value=[
            {"xmuserid": "employee@example.com", "xmname": "测试员工", "deptids": [10], "mailstatus": 1},
            {"xmuserid": "other@example.com", "xmname": "另一个员工", "deptId": 30},
            {"xmuserid": "employee@example.com", "xmname": "测试员工", "deptids": [20, 10], "mailstatus": 1},
        ])
        users = client.list_users()
        self.assertEqual(len(users), 2)
        self.assertEqual(users[0].userid, "employee")
        self.assertEqual(users[0].department_ids, ["10", "20"])
        self.assertEqual(users[0].status, 1)
        self.assertEqual(users[1].department_ids, ["30"])

    def test_create_and_update_send_full_department_list(self):
        client = Mail263Client(Settings())
        client._post_json = Mock()
        client.create_user("employee", "test-password", [10, 20], "测试员工")
        self.assertEqual(client._post_json.call_args.args[1]["deptids"], [10, 20])
        client.update_user("employee", [10, 20], "测试员工")
        self.assertEqual(client._post_json.call_args.args[1]["deptids"], [10, 20])


if __name__ == "__main__":
    unittest.main()
