"""RuleMiddleware 管辖、例外、承诺窗口和离线边界测试。"""

import unittest

from rule_middleware import RuleMiddleware
from terminal import StoreTerminal, OfflineCentralClient


def authority(at, issuer, role, site="mine", category="H", rev=1, active=True):
    return {
        "at": at,
        "kind": "authority",
        "record": {
            "issuer": issuer,
            "role": role,
            "site": site,
            "category": category,
            "revision": rev,
            "active": active,
        },
    }


def clause(at, issuer, cid, rev, kind, **extra):
    record = {
        "issuer": issuer,
        "clause": cid,
        "rev": rev,
        "kind": kind,
        "site": "mine",
        "category": "H",
        "purpose": "fix",
    }
    record.update(extra)
    return {"at": at, "kind": "clause", "record": record}


def complete(at, store="s1", value=True):
    return {
        "at": at,
        "kind": "completeness",
        "record": {"store": store, "site": "mine", "category": "H", "complete": value},
    }


def request_event(at, rid="r1", store="s1", **extra):
    record = {
        "store": store,
        "id": rid,
        "site": "mine",
        "category": "H",
        "purpose": "fix",
        "battery": "pack",
        "credentials": [],
    }
    record.update(extra)
    return {"at": at, "kind": "request", "record": record}


def commit_event(at, rid="r1", pid="p1", store="s1", expires=30):
    return {
        "at": at,
        "kind": "commit",
        "record": {"store": store, "request": rid, "promise": pid, "expires": expires},
    }


def redeem_event(at, pid="p1", store="s1", site="mine"):
    return {
        "at": at,
        "kind": "redeem",
        "record": {"store": store, "promise": pid, "site": site},
    }


class RuleMiddlewareTest(unittest.TestCase):
    def setUp(self):
        self.mw = RuleMiddleware()
        self.mw.process_events(
            [
                authority(0, "reg", "regulator"),
                authority(0, "tech", "equipment"),
                authority(0, "sales", "commercial"),
                complete(1),
            ]
        )

    def test_exception_only_waves_named_credential_and_cannot_beat_ban(self):
        self.mw.handle(
            clause(
                2,
                "tech",
                "fit",
                1,
                "offer",
                required_credentials=["agreement", "safety"],
                until=40,
            )
        )
        self.mw.handle(
            clause(
                3,
                "sales",
                "exc",
                1,
                "exception",
                related="fit#1",
                waive=["agreement"],
                until=20,
            )
        )
        result = self.mw.handle(request_event(4))["evaluation"]
        self.assertEqual(result["decision"], "needs_info")
        self.assertEqual([x["id"] for x in result["missing_credentials"]], ["safety"])
        self.assertEqual(result["missing_credentials"][0]["clause"]["clause"], "fit")

        self.mw.handle(clause(5, "reg", "ban-pack", 1, "ban", reason="recall"))
        banned = self.mw.handle(request_event(6, rid="r2"))["evaluation"]
        self.assertEqual(banned["decision"], "deny")
        self.assertEqual(banned["status"], "banned")
        self.assertTrue(banned["bans"][0]["clause"]["clause"] == "ban-pack")

    def test_unrelated_or_unknown_exception_does_not_fill_gap(self):
        self.mw.handle(
            clause(2, "tech", "fit", 1, "offer", required_credentials=["agreement"])
        )
        self.mw.handle(
            clause(
                3,
                "sales",
                "exc",
                1,
                "exception",
                related="does-not-exist#9",
                waive=["agreement"],
            )
        )
        result = self.mw.handle(request_event(4))["evaluation"]
        self.assertEqual(result["decision"], "needs_info")
        self.assertEqual(result["missing_credentials"][0]["id"], "agreement")
        self.assertFalse(result["exceptions"][0]["references_valid_offer"])

    def test_partial_completeness_allows_evaluation_but_not_commit(self):
        mw = RuleMiddleware()
        mw.process_events(
            [
                authority(0, "reg", "regulator"),
                authority(0, "tech", "equipment"),
                clause(1, "tech", "fit", 1, "offer", until=40),
                complete(2, value=False),
                request_event(3),
            ]
        )
        req = mw.results[-1]["evaluation"]
        self.assertEqual(req["rule_decision"], "allow")
        self.assertEqual(req["decision"], "hold")
        self.assertFalse(req["can_commit"])
        self.assertEqual(req["missing"][0]["type"], "completeness")
        commit = mw.handle(commit_event(4))
        self.assertFalse(commit["ok"])
        self.assertEqual(commit["status"], "promise_not_issued")
        self.assertNotIn("p1", mw.promises)

    def test_general_revision_honors_pinned_promise(self):
        self.mw.handle(clause(2, "tech", "fit", 1, "offer", until=40))
        self.mw.handle(request_event(3))
        issued = self.mw.handle(commit_event(4, expires=20))
        self.assertEqual(issued["status"], "promise_issued")

        self.mw.handle(
            clause(
                5,
                "tech",
                "fit",
                2,
                "offer",
                required_credentials=["new-cert"],
                until=50,
            )
        )
        redeemed = self.mw.handle(redeem_event(15))
        self.assertEqual(redeemed["status"], "redeemed")
        view = self.mw.inspect_promise("p1", at=16)
        revision_changes = [
            c for c in view["breaking_changes"] if c["change_type"] == "general_revision"
        ]
        self.assertEqual(len(revision_changes), 1)
        self.assertFalse(revision_changes[0]["breaking"])

    def test_revision_with_explicit_stop_breaks_existing_promise(self):
        self.mw.handle(clause(2, "tech", "fit", 1, "offer", until=40))
        self.mw.handle(request_event(3))
        self.mw.handle(commit_event(4, expires=30))
        self.mw.handle(
            clause(
                5,
                "tech",
                "fit",
                2,
                "offer",
                until=50,
                stop_fulfillment=True,
                stop_revs=[1],
                stop_reason="new revision recall",
            )
        )
        redeem = self.mw.handle(redeem_event(6))
        self.assertEqual(redeem["status"], "stopped")
        self.assertFalse(redeem["consumed"])
        self.assertEqual(self.mw.promises["p1"]["status"], "stopped")

    def test_temporary_exception_caps_window_and_expires(self):
        self.mw.handle(
            clause(
                2,
                "tech",
                "fit",
                1,
                "offer",
                required_credentials=["agreement"],
                until=45,
            )
        )
        self.mw.handle(
            clause(
                3,
                "sales",
                "temp",
                1,
                "exception",
                related="fit",
                waive=["agreement"],
                until=10,
            )
        )
        self.mw.handle(request_event(4))
        issued = self.mw.handle(commit_event(5, expires=30))
        self.assertEqual(issued["valid_until"], 10)

        # A second promise is rejected after the inclusive window boundary.
        self.mw.handle(request_event(6, rid="r2"))
        self.mw.handle(commit_event(7, rid="r2", pid="p2", expires=30))
        self.assertEqual(self.mw.handle(redeem_event(10))["status"], "redeemed")
        expired = self.mw.handle(redeem_event(11, pid="p2"))
        self.assertEqual(expired["status"], "expired")
        self.assertFalse(expired["consumed"])
        self.assertTrue(expired["reapply_conditions"])

    def test_regulator_cross_issuer_stop_and_cross_store_duplicate(self):
        self.mw.handle(clause(2, "tech", "fit", 1, "offer", until=40))
        self.mw.handle(request_event(3))
        self.mw.handle(commit_event(4))
        stop = {
            "at": 5,
            "kind": "notify_stop",
            "record": {"issuer": "reg", "clause": "fit", "rev": 1, "reason": "hold"},
        }
        self.assertEqual(self.mw.handle(stop)["status"], "stop_registered")
        self.mw.handle(complete(6, store="s2", value=True))
        rejected = self.mw.handle(redeem_event(7, store="s2"))
        self.assertEqual(rejected["status"], "stopped")
        self.assertFalse(rejected["consumed"])

        # If it had been delivered first, stop creates review, not unissue.
        mw = RuleMiddleware()
        mw.process_events(
            [
                authority(0, "reg", "regulator"),
                authority(0, "tech", "equipment"),
                complete(1),
                clause(2, "tech", "fit", 1, "offer"),
                request_event(3),
                commit_event(4),
                redeem_event(5),
                {
                    "at": 6,
                    "kind": "notify_stop",
                    "record": {"issuer": "reg", "clause": "fit", "rev": 1},
                },
            ]
        )
        view = mw.inspect_promise("p1")
        self.assertEqual(view["status"], "redeemed")
        self.assertTrue(view["review_required"])
        duplicate = mw.handle(redeem_event(7, store="s2"))
        self.assertEqual(duplicate["status"], "already_redeemed")
        self.assertFalse(duplicate["consumed"])
        self.assertEqual(duplicate["prior_redemption"]["store"], "s1")

    def test_authority_transfer_rejects_late_old_issuer_clause(self):
        self.mw.handle(authority(5, "tech1", "equipment", rev=2, active=False))
        self.mw.handle(authority(5, "tech2", "equipment", rev=3, active=True))
        late = self.mw.handle(clause(6, "tech1", "fit", 2, "offer"))
        self.assertEqual(late["status"], "clause_rejected")
        self.assertEqual(late["code"], "publisher_without_active_jurisdiction")

        new = self.mw.handle(clause(7, "tech2", "fit2", 1, "offer"))
        self.assertEqual(new["status"], "clause_published")

    def test_incomplete_redemption_does_not_consume_then_completion_redeems(self):
        self.mw.handle(clause(2, "tech", "fit", 1, "offer"))
        self.mw.handle(request_event(3))
        self.mw.handle(commit_event(4))
        self.mw.handle(complete(5, store="s2", value=False))
        held = self.mw.handle(redeem_event(6, store="s2"))
        self.assertEqual(held["status"], "held")
        self.assertFalse(held["consumed"])
        self.assertIsNone(self.mw.promises["p1"]["redemption"])
        self.mw.handle(complete(7, store="s2", value=True))
        redeemed = self.mw.handle(redeem_event(8, store="s2"))
        self.assertEqual(redeemed["status"], "redeemed")
        self.assertEqual(redeemed["redemption"]["store"], "s2")

    def test_ban_lift_credentials_are_explicit(self):
        self.mw.handle(clause(2, "tech", "fit", 1, "offer", until=40))
        self.mw.handle(
            clause(
                3,
                "reg",
                "ban",
                1,
                "ban",
                reason="incident",
                lift_credentials=["inspection-cert"],
            )
        )
        denied = self.mw.handle(request_event(4, rid="r1"))["evaluation"]
        self.assertEqual(denied["decision"], "deny")
        self.assertEqual(
            denied["bans"][0]["lift"]["missing_credentials"][0]["id"],
            "inspection-cert",
        )
        allowed = self.mw.handle(
            request_event(
                5, rid="r2", credentials=[{"id": "inspection-cert", "line": "regulator"}]
            )
        )["evaluation"]
        self.assertEqual(allowed["decision"], "allow")

    def test_unauthorized_stop_and_wrong_site_do_not_change_facts(self):
        self.mw.handle(clause(2, "tech", "fit", 1, "offer", until=40))
        self.mw.handle(request_event(3))
        self.mw.handle(commit_event(4))
        invalid_stop = self.mw.handle(
            {
                "at": 5,
                "kind": "notify_stop",
                "record": {"issuer": "sales", "clause": "fit", "rev": 1},
            }
        )
        self.assertEqual(invalid_stop["status"], "notice_rejected")
        wrong_site = self.mw.handle(redeem_event(6, site="quarry"))
        self.assertEqual(wrong_site["status"], "wrong_site")
        self.assertFalse(wrong_site["consumed"])
        redeemed = self.mw.handle(redeem_event(7))
        self.assertEqual(redeemed["status"], "redeemed")

    def test_offline_terminal_is_explicit_and_does_not_consume(self):
        terminal = StoreTerminal(client=OfflineCentralClient(), online=False)
        result = terminal.redeem({"store": "s1", "promise": "p1", "site": "mine"})
        self.assertEqual(result["status"], "offline")
        self.assertFalse(result["can_redeem"])
        self.assertFalse(result["consumed"])

    def test_scenario_files_end_to_end(self):
        for path in (
            "examples/exception-cannot-rescue-stopped-promise.jsonl",
            "examples/authority-transfer-after-redemption.jsonl",
        ):
            mw = RuleMiddleware()
            results = mw.run_jsonl(path)
            self.assertTrue(results)
            self.assertFalse(any(r.get("status") == "parse_error" for r in results))
            self.assertTrue(any(r.get("status") == "promise_issued" for r in results))


if __name__ == "__main__":
    unittest.main()
