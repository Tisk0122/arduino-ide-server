import importlib.util
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMP_HOME = tempfile.TemporaryDirectory()
os.environ["HELPER_HOME"] = TEMP_HOME.name
SPEC = importlib.util.spec_from_file_location("arduino_helper_accounts", ROOT / "arduino-helper.py")
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)


class AccountApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        server.HOME = Path(self.temp.name)
        server.WORK_DIR = server.HOME / "work"
        server.WORK_DIR.mkdir()
        server.TOKEN = "user-token-for-tests"
        server.ADMIN_TOKEN = "admin-token-for-tests"
        server.SINGLE_TOKEN = False
        server.AUTH_FAILS.clear()
        server.AUTH_BLOCK.clear()
        self.httpd = server.Server(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def _stop_server(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def request(self, path, method="GET", body=None, token=None, auth_scheme="Session"):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = auth_scheme + " " + token
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            response = urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as exc:
            response = exc
        return response.code, json.loads(response.read().decode("utf-8"))

    def create_teacher(self):
        server._create_teacher_account("teacher01", "TeacherPass-01", "先生")
        status, data = self.request("/account/login", "POST", {
            "username": "teacher01", "password": "TeacherPass-01",
        })
        self.assertEqual(status, 200)
        return data["token"]

    def test_admin_can_create_teacher_with_one_time_password(self):
        status, created = self.request("/admin/api/teachers", "POST", {
            "username": "newteacher", "name": "新しい先生",
        }, server.ADMIN_TOKEN, auth_scheme="Bearer")
        self.assertEqual(status, 201)
        self.assertEqual(created["username"], "newteacher")
        self.assertEqual(created["name"], "新しい先生")
        self.assertGreaterEqual(len(created["initialPassword"]), 10)

        status, login = self.request("/account/login", "POST", {
            "username": created["username"], "password": created["initialPassword"],
        })
        self.assertEqual(status, 200)
        self.assertEqual(login["user"]["role"], "teacher")
        self.assertTrue(login["user"]["mustChangePassword"])

        status, error = self.request("/admin/api/teachers", "POST", {
            "username": "newteacher", "name": "重複",
        }, server.ADMIN_TOKEN, auth_scheme="Bearer")
        self.assertEqual(status, 409)

        status, _ = self.request("/admin/api/teachers", "POST", {
            "username": "otherteacher", "name": "権限なし",
        }, server.TOKEN, auth_scheme="Bearer")
        self.assertEqual(status, 403)

    def test_import_login_student_data_review_and_teacher_edit(self):
        teacher_token = self.create_teacher()
        status, classroom = self.request("/teacher/api/classes", "POST",
                                         {"name": "1年A組", "schoolYear": "2026"}, teacher_token)
        self.assertEqual(status, 201)
        class_id = classroom["classId"]
        status, imported = self.request("/teacher/api/students/import", "POST",
                                        {"classId": class_id, "csv": "grade,class,number\n1,A,1\n"},
                                        teacher_token)
        self.assertEqual(status, 201)
        credentials = imported["credentials"][0]
        self.assertEqual(credentials["studentId"], "010101")
        status, login = self.request("/account/login", "POST",
                                     {"username": credentials["studentId"],
                                      "password": credentials["initialPassword"]})
        self.assertEqual(status, 200)
        student_token = login["token"]
        self.assertTrue(login["user"]["mustChangePassword"])
        status, forbidden = self.request("/account/sketches", token=student_token)
        self.assertEqual(status, 403)
        status, _ = self.request("/account/password", "POST", {
            "currentPassword": credentials["initialPassword"], "newPassword": "NewStudentPass-01",
        }, student_token)
        self.assertEqual(status, 200)

        sketch = {"id": "sketch-one", "name": "LED", "board": "arduino:avr:uno",
                  "files": [{"name": "LED.ino", "content": "void setup() {}"}]}
        status, saved = self.request("/account/sketches/sketch-one", "PUT",
                                     {"sketch": sketch, "revision": 0}, student_token)
        self.assertEqual(status, 200)
        self.assertEqual(saved["revision"], 1)

        status, students = self.request("/teacher/api/classes/%s/students" % class_id,
                                        token=teacher_token)
        self.assertEqual(status, 200)
        student_id = students["students"][0]["id"]
        status, works = self.request("/teacher/api/students/%s/sketches" % student_id,
                                     token=teacher_token)
        self.assertEqual(status, 200)
        self.assertEqual(works["sketches"][0]["name"], "LED")
        status, _ = self.request("/teacher/api/students/%s/sketches/sketch-one/review" % student_id,
                                 "PUT", {"checked": True, "comment": "配線を確認してください"}, teacher_token)
        self.assertEqual(status, 200)
        status, student_works = self.request("/account/sketches", token=student_token)
        self.assertTrue(student_works["sketches"][0]["checked"])
        self.assertEqual(student_works["sketches"][0]["teacherComment"], "配線を確認してください")

        updated = dict(works["sketches"][0])
        updated["files"] = [{"name": "LED.ino", "content": "void setup() { pinMode(LED_BUILTIN, OUTPUT); }"}]
        status, edited = self.request("/teacher/api/students/%s/sketches/sketch-one" % student_id,
                                      "PUT", {"sketch": updated, "revision": 1}, teacher_token)
        self.assertEqual(status, 200)
        self.assertEqual(edited["revision"], 2)
        status, versions = self.request(
            "/teacher/api/students/%s/sketches/sketch-one/versions" % student_id, token=teacher_token)
        self.assertEqual(status, 200)
        self.assertEqual(versions["versions"][0]["revision"], 1)
        status, restored = self.request(
            "/teacher/api/students/%s/sketches/sketch-one/versions/1/restore" % student_id,
            "POST", {"revision": 2}, teacher_token)
        self.assertEqual(status, 200)
        self.assertEqual(restored["revision"], 3)
        _, final_works = self.request("/teacher/api/students/%s/sketches" % student_id,
                                      token=teacher_token)
        self.assertEqual(final_works["sketches"][0]["files"][0]["content"], "void setup() {}")

    def test_revision_conflict_and_student_cannot_access_teacher_apis(self):
        teacher_token = self.create_teacher()
        status, classroom = self.request("/teacher/api/classes", "POST",
                                         {"name": "1年B組", "schoolYear": "2026"}, teacher_token)
        status, imported = self.request("/teacher/api/students/import", "POST",
                                        {"classId": classroom["classId"], "csv": "grade,class,number\n1,B,2\n"},
                                        teacher_token)
        student_data = imported["credentials"][0]
        _, student_login = self.request("/account/login", "POST", {
            "username": student_data["studentId"], "password": student_data["initialPassword"],
        })
        student_token = student_login["token"]
        self.request("/account/password", "POST", {
            "currentPassword": student_data["initialPassword"], "newPassword": "NewStudentPass-02",
        }, student_token)
        sketch = {"id": "conflict", "name": "Conflict", "files": [{"name": "A.ino", "content": "a"}]}
        self.request("/account/sketches/conflict", "PUT", {"sketch": sketch, "revision": 0}, student_token)
        status, data = self.request("/account/sketches/conflict", "PUT",
                                    {"sketch": sketch, "revision": 0}, student_token)
        self.assertEqual(status, 409)
        status, _ = self.request("/teacher/api/classes", token=student_token)
        self.assertEqual(status, 403)

    def test_class_clear_requires_exact_confirmation_and_keeps_accounts(self):
        teacher_token = self.create_teacher()
        _, classroom = self.request("/teacher/api/classes", "POST",
                                    {"name": "2年A組", "schoolYear": "2026"}, teacher_token)
        class_id = classroom["classId"]
        _, imported = self.request("/teacher/api/students/import", "POST",
                                   {"classId": class_id, "csv": "grade,class,number\n2,A,1\n"},
                                   teacher_token)
        student = imported["credentials"][0]
        status, _ = self.request("/teacher/api/classes/%s/clear" % class_id, "POST",
                                  {"confirm": "CLEAR 2年A組 2026"}, teacher_token)
        self.assertEqual(status, 409)
        status, _ = self.request("/teacher/api/classes/%s/archive" % class_id, "POST", {}, teacher_token)
        self.assertEqual(status, 200)
        status, _ = self.request("/account/login", "POST", {
            "username": student["studentId"], "password": student["initialPassword"],
        })
        self.assertEqual(status, 401)
        status, _ = self.request("/teacher/api/classes/%s/clear" % class_id, "POST",
                                  {"confirm": "CLEAR 2年A組"}, teacher_token)
        self.assertEqual(status, 400)
        status, _ = self.request("/teacher/api/classes/%s/clear" % class_id, "POST",
                                  {"confirm": "CLEAR 2年A組 2026"}, teacher_token)
        self.assertEqual(status, 200)
        status, students = self.request("/teacher/api/classes/%s/students" % class_id,
                                        token=teacher_token)
        self.assertEqual(status, 200)
        self.assertEqual(len(students["students"]), 1)
        status, next_class = self.request("/teacher/api/classes", "POST",
                                          {"name": "3年A組", "schoolYear": "2027"}, teacher_token)
        self.assertEqual(status, 201)
        status, moved = self.request("/teacher/api/students/import", "POST", {
            "classId": next_class["classId"],
            "csv": "grade,class,number,previous_id\n3,A,1,%s\n" % student["studentId"],
        }, teacher_token)
        self.assertEqual(status, 201)
        self.assertEqual(moved["credentials"], [])
        self.assertEqual(moved["reassigned"][0]["studentId"], "030101")
        self.assertEqual(moved["reassigned"][0]["previousId"], student["studentId"])
        status, login = self.request("/account/login", "POST", {
            "username": "030101", "password": student["initialPassword"],
        })
        self.assertEqual(status, 200)
        self.assertEqual(login["user"]["classId"], next_class["classId"])

    def test_roster_uses_structured_student_ids_without_names(self):
        teacher_token = self.create_teacher()
        _, classroom = self.request("/teacher/api/classes", "POST",
                                    {"name": "1年D組", "schoolYear": "2026"}, teacher_token)
        status, _ = self.request("/teacher/api/students/import", "POST", {
            "classId": classroom["classId"], "csv": "username,name\nstudent1,氏名\n",
        }, teacher_token)
        self.assertEqual(status, 400)
        status, imported = self.request("/teacher/api/students/import", "POST", {
            "classId": classroom["classId"], "csv": "grade,class,number\n1,D,24\n",
        }, teacher_token)
        self.assertEqual(status, 201)
        self.assertEqual(imported["credentials"][0]["studentId"], "010424")
        status, _ = self.request("/teacher/api/students/import", "POST", {
            "classId": classroom["classId"], "csv": "grade,class,number\n100,D,24\n",
        }, teacher_token)
        self.assertEqual(status, 400)
        status, _ = self.request("/teacher/api/classes/%s/archive" % classroom["classId"],
                                 "POST", {}, teacher_token)
        self.assertEqual(status, 200)
        status, _ = self.request("/teacher/api/classes/%s/clear" % classroom["classId"], "POST",
                                 {"confirm": "CLEAR 1年D組 2026"}, teacher_token)
        self.assertEqual(status, 200)
        _, next_class = self.request("/teacher/api/classes", "POST",
                                     {"name": "1年D組", "schoolYear": "2027"}, teacher_token)
        status, recycled = self.request("/teacher/api/students/import", "POST", {
            "classId": next_class["classId"], "csv": "grade,class,number\n1,D,24\n",
        }, teacher_token)
        self.assertEqual(status, 201)
        self.assertNotEqual(recycled["credentials"][0]["initialPassword"],
                            imported["credentials"][0]["initialPassword"])
        status, _ = self.request("/account/login", "POST", {
            "username": "010424", "password": imported["credentials"][0]["initialPassword"],
        })
        self.assertEqual(status, 401)
        status, _ = self.request("/account/login", "POST", {
            "username": "010424", "password": recycled["credentials"][0]["initialPassword"],
        })
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
