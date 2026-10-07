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

    # ---- 轮换链审计 ----

    def _chain(self, terminal="term-1"):
        return service.get_rotation_chain(self.storage, terminal)

    def test_chain_fresh_family_has_only_created_entry(self):
        fam = self._family()
        chain = self._chain()
        self.assertEqual(chain["family_status"], "active")
        self.assertFalse(chain["chain_invalidated"])
        self.assertEqual(chain["length"], 1)
        entry = chain["entries"][0]
        self.assertEqual(entry["type"], "created")
        self.assertEqual(entry["result_generation"], 1)
        self.assertTrue(entry["result_credential_fp"].startswith("sha256:"))
        self.assertIsNone(entry["previous_credential_fp"])

    def test_chain_records_each_rotation_in_order_with_fingerprints(self):
        fam = self._family()
        r1 = service.rotate(self.storage, "term-1", fam["credential"], "rot-1")
        r2 = service.rotate(self.storage, "term-1", r1["credential"], "rot-2")
        chain = self._chain()
        self.assertEqual(chain["length"], 3)
        types = [e["type"] for e in chain["entries"]]
        self.assertEqual(types, ["created", "rotated", "rotated"])
        gens = [e["result_generation"] for e in chain["entries"]]
        self.assertEqual(gens, [1, 2, 3])
        # 稳定轮换标识随记录可见；前后凭证均为不可逆指纹。
        self.assertEqual(chain["entries"][1]["rotation_id"], "rot-1")
        self.assertEqual(chain["entries"][2]["rotation_id"], "rot-2")
        for e in chain["entries"][1:]:
            self.assertTrue(e["previous_credential_fp"].startswith("sha256:"))
            self.assertTrue(e["result_credential_fp"].startswith("sha256:"))
            self.assertEqual(e["status"], "active")
        # 相邻条目：上一条的结果指纹 == 下一条的前凭证指纹。
        self.assertEqual(chain["entries"][1]["result_credential_fp"],
                         chain["entries"][2]["previous_credential_fp"])

    def test_chain_never_exposes_usable_credentials(self):
        fam = self._family()
        r1 = service.rotate(self.storage, "term-1", fam["credential"], "rot-1")
        blob = repr(self._chain())
        self.assertNotIn(fam["credential"], blob)
        self.assertNotIn(r1["credential"], blob)

    def test_chain_replay_does_not_add_entries(self):
        fam = self._family()
        service.rotate(self.storage, "term-1", fam["credential"], "rot-1")
        before = self._chain()
        for _ in range(3):
            service.rotate(self.storage, "term-1", fam["credential"], "rot-1")
        after = self._chain()
        self.assertEqual(after["length"], before["length"])
        self.assertEqual(after["entries"], before["entries"])

    def test_chain_replay_after_restart_adds_no_entries_and_keeps_order(self):
        fam = self._family()
        first = service.rotate(self.storage, "term-1", fam["credential"], "rot-1")
        self.storage.close()
        self.storage = Storage(self.db_path)  # 模拟服务重启
        again = service.rotate(self.storage, "term-1", fam["credential"], "rot-1")
        self.assertEqual(again["outcome"], "replayed")
        chain = self._chain()
        self.assertEqual(chain["length"], 2)  # 仅创建 + 首轮换，重放不产生记录
        self.assertEqual([e["type"] for e in chain["entries"]], ["created", "rotated"])
        self.assertEqual(chain["entries"][1]["rotation_id"], "rot-1")
        self.assertEqual(chain["entries"][1]["result_generation"], first["generation"])

    def test_chain_marks_reuse_revocation_and_full_chain_invalidated(self):
        fam = self._family()
        service.rotate(self.storage, "term-1", fam["credential"], "rot-1")
        service.rotate(self.storage, "term-1", fam["credential"], "rot-DIFFERENT")
        chain = self._chain()
        self.assertTrue(chain["chain_invalidated"])
        self.assertTrue(chain["reuse_triggered"])
        self.assertEqual(chain["family_status"], "revoked")
        # 末尾为撤销记录，明确标记异标识重用触发。
        rev = chain["entries"][-1]
        self.assertEqual(rev["type"], "revoked")
        self.assertTrue(rev["reuse_trigger"])
        self.assertTrue(rev["chain_invalidated"])
        self.assertEqual(rev["original_rotation_id"], "rot-1")
        self.assertEqual(rev["attempted_rotation_id"], "rot-DIFFERENT")
        self.assertIsNone(rev["result_credential_fp"])
        self.assertIn("reuse", rev["reason"])
        # 历史轮换条目状态随全链失效。
        self.assertEqual(chain["entries"][1]["status"], "revoked")
        # 撤销链路依旧不含任何可用凭证明文。
        self.assertNotIn(fam["credential"], repr(chain))

    def test_chain_unknown_terminal_is_clear_failure(self):
        with self.assertRaises(service.UnknownTerminalError):
            service.get_rotation_chain(self.storage, "no-such-terminal")


if __name__ == "__main__":
    unittest.main()
