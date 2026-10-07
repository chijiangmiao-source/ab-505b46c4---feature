"""授权族轮换核心逻辑的单元测试（标准库 unittest，无第三方依赖）。"""
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import service  # noqa: E402
from storage import Storage  # noqa: E402


class RotationTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        self.storage = Storage(self.db_path)

    def tearDown(self):
        self.storage.close()
        self.tmp.cleanup()

    def _family(self, terminal="term-1"):
        return service.create_family(self.storage, terminal)

    # ---- 创建 ----

    def test_create_family_issues_generation_one_credential(self):
        fam = self._family()
        self.assertEqual(fam["generation"], 1)
        self.assertTrue(fam["credential"].startswith("rft_"))
        self.assertEqual(fam["family_status"], "active")

    def test_terminal_can_bind_only_one_family(self):
        self._family("term-dup")
        with self.assertRaises(service.TerminalAlreadyBoundError):
            self._family("term-dup")

    def test_create_family_requires_terminal_id(self):
        with self.assertRaises(ValueError):
            self._family("  ")

    # ---- 首次轮换 ----

    def test_first_rotation_accepted_and_generation_increments(self):
        fam = self._family()
        res = service.rotate(self.storage, "term-1", fam["credential"], "rot-1")
        self.assertEqual(res["outcome"], "accepted")
        self.assertEqual(res["generation"], 2)
        self.assertTrue(res["credential"].startswith("rft_"))
        self.assertNotEqual(res["credential"], fam["credential"])
        self.assertEqual(res["family_status"], "active")

    # ---- 幂等重放 ----

    def test_replay_returns_identical_successor_without_advancing(self):
        fam = self._family()
        first = service.rotate(self.storage, "term-1", fam["credential"], "rot-1")
        for _ in range(3):
            again = service.rotate(self.storage, "term-1", fam["credential"], "rot-1")
            self.assertEqual(again["outcome"], "replayed")
            self.assertEqual(again["credential"], first["credential"])
            self.assertEqual(again["generation"], first["generation"])
        view = service.get_family_by_terminal(self.storage, "term-1")
        self.assertEqual(view["generation"], 2)
        self.assertEqual(self.storage.count_rotations(fam["family_id"]), 1)

    def test_replay_survives_service_restart(self):
        fam = self._family()
        first = service.rotate(self.storage, "term-1", fam["credential"], "rot-1")
        # 模拟服务重启：关闭并重新打开同一数据库文件。
        self.storage.close()
        self.storage = Storage(self.db_path)
        again = service.rotate(self.storage, "term-1", fam["credential"], "rot-1")
        self.assertEqual(again["outcome"], "replayed")
        self.assertEqual(again["credential"], first["credential"])
        self.assertEqual(again["generation"], first["generation"])

    def test_replay_returns_historical_successor_after_later_rotations(self):
        fam = self._family()
        first = service.rotate(self.storage, "term-1", fam["credential"], "rot-1")
        second = service.rotate(self.storage, "term-1", first["credential"], "rot-2")
        self.assertEqual(second["generation"], 3)
        again = service.rotate(self.storage, "term-1", fam["credential"], "rot-1")
        self.assertEqual(again["outcome"], "replayed")
        self.assertEqual(again["credential"], first["credential"])
        self.assertEqual(again["generation"], 2)

    # ---- 并发同标识 ----

    def test_concurrent_identical_requests_observe_same_result(self):
        fam = self._family()
        barrier = threading.Barrier(2)

        def call():
            barrier.wait()
            return service.rotate(self.storage, "term-1", fam["credential"], "rot-1")

        results = []
        threads = [threading.Thread(target=lambda: results.append(call())) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(results), 2)
        self.assertEqual(sorted(r["outcome"] for r in results), ["accepted", "replayed"])
        self.assertEqual(results[0]["credential"], results[1]["credential"])
        self.assertEqual(results[0]["generation"], results[1]["generation"])
        self.assertEqual(results[0]["generation"], 2)
        view = service.get_family_by_terminal(self.storage, "term-1")
        self.assertEqual(view["generation"], 2)

    # ---- 异标识重放 => 撤销 ----

    def test_reuse_with_different_rotation_id_revokes_family(self):
        fam = self._family()
        first = service.rotate(self.storage, "term-1", fam["credential"], "rot-1")
        res = service.rotate(self.storage, "term-1", fam["credential"], "rot-OTHER")
        self.assertEqual(res["outcome"], "revoked")
        self.assertEqual(res["family_status"], "revoked")
        self.assertIn("reuse", res["revocation_reason"])

        # 此前签发的后继凭证随后同样被拒绝。
        successor = service.rotate(self.storage, "term-1", first["credential"], "rot-2")
        self.assertEqual(successor["outcome"], "revoked")
        self.assertIn("reuse", successor["revocation_reason"])

        # 原（旧凭证, 原轮换标识）重放也被拒绝。
        replay = service.rotate(self.storage, "term-1", fam["credential"], "rot-1")
        self.assertEqual(replay["outcome"], "revoked")

        # 状态查询可见撤销原因。
        view = service.get_family_by_terminal(self.storage, "term-1")
        self.assertEqual(view["family_status"], "revoked")
        self.assertIn("reuse", view["revocation_reason"])

    # ---- 错误路径 ----

    def test_unknown_terminal_rejected(self):
        with self.assertRaises(service.UnknownTerminalError):
            service.rotate(self.storage, "no-such-terminal", "rft_x", "rot-1")

    def test_unknown_credential_rejected(self):
        self._family()
        with self.assertRaises(service.InvalidCredentialError):
            service.rotate(self.storage, "term-1", "rft_not_issued", "rot-1")

    def test_rotate_requires_all_fields(self):
        fam = self._family()
        with self.assertRaises(ValueError):
            service.rotate(self.storage, "term-1", fam["credential"], "")
        with self.assertRaises(ValueError):
            service.rotate(self.storage, "term-1", "", "rot-1")


if __name__ == "__main__":
    unittest.main()
