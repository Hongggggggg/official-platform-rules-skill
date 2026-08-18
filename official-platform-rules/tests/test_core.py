from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch

SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from core.audit import audit_skill
from core.clarify import clarify_question
from core.db import RuleDatabase
from core.fetch import fetch_url
from core.html_extract import extract_document
from core.locking import SyncLocked, platform_sync_lock
from core.models import ExtractedDocument, SourceDefinition
from core.sources import SourceRejected, validate_official_url


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = RuleDatabase(Path(self.temp.name) / "rules.sqlite3")
        self.db.initialize()
        self.policy = SourceDefinition(
            source_key="policy",
            canonical_rule_key="product.prohibited",
            url="https://seller-us.tiktok.com/policy",
            source_type="policy",
            topic="禁限售",
            risk="high",
            scope={"market": "US", "seller_type": "seller", "fulfillment": "any"},
        )
        self.db.upsert_source(self.policy)

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def doc(body: str, effective: str | None = None, published: str | None = "2026-01-01") -> ExtractedDocument:
        return ExtractedDocument(
            title="Official policy",
            language="en",
            text=body,
            sections=(("Restricted products", body),),
            published_at=published,
            effective_at=effective,
        )

    def ingest(self, source: SourceDefinition, doc: ExtractedDocument, stamp: str = "2026-07-25T00:00:00+00:00"):
        return self.db.ingest(source, doc, "snapshot.html", 200, stamp)

    def test_undated_change_needs_review_and_keeps_current(self) -> None:
        self.ingest(self.policy, self.doc("original official rule text " * 8))
        self.ingest(self.policy, self.doc("changed without a date " * 8, published=None))
        history = self.db.history("product.prohibited::restricted-products")
        self.assertEqual(history[0]["status"], "review_required")
        self.assertEqual(history[0]["review_reason"], "undated_change")
        self.assertEqual(len(self.db.search("original official rule")), 1)

    def test_lower_priority_news_cannot_replace_policy(self) -> None:
        self.ingest(self.policy, self.doc("formal policy requirement " * 8))
        news = SourceDefinition(
            source_key="news",
            canonical_rule_key="product.prohibited",
            url="https://seller-us.tiktok.com/news",
            source_type="news",
            topic="禁限售",
            risk="high",
            scope=self.policy.scope,
        )
        self.db.upsert_source(news)
        self.ingest(news, self.doc("news wording differs " * 8, published="2026-07-01"))
        review = self.db.review_required()
        self.assertEqual(review[0]["review_reason"], "lower_priority_conflict")
        self.assertIn("formal policy", self.db.search("formal policy")[0]["content"])

    def test_future_rule_activates_and_supersedes(self) -> None:
        self.ingest(self.policy, self.doc("current requirement " * 8))
        self.ingest(self.policy, self.doc("future requirement " * 8, effective="2999-01-01"))
        connection = sqlite3.connect(self.db.path)
        try:
            connection.execute(
                "UPDATE rule_versions SET effective_at='2000-01-01' WHERE status='pending'"
            )
            connection.commit()
        finally:
            connection.close()
        result = self.db.activate_due_pending()
        self.assertEqual(result["activated"], 1)
        rules = self.db.search("future requirement")
        self.assertEqual(rules[0]["status"], "current")
        self.assertEqual(self.db.history(rules[0]["rule_key"])[1]["status"], "superseded")

    def test_composite_fulfillment_scope_does_not_leak(self) -> None:
        source = SourceDefinition(
            source_key="partner",
            canonical_rule_key="fulfillment.partner",
            url="https://seller-us.tiktok.com/partner",
            source_type="specific_rule",
            topic="履约",
            risk="high",
            scope={
                "market": "RU_CIS",
                "seller_type": "cross_border",
                "fulfillment": "FBP_OR_REALFBS",
            },
        )
        self.db.upsert_source(source)
        self.ingest(source, self.doc("partner delivery official rule " * 8))
        found = self.db.search(
            "partner delivery", scope={"fulfillment": "FBP", "market": "RU"}
        )
        leaked = self.db.search(
            "partner delivery", scope={"fulfillment": "FBO", "market": "RU"}
        )
        self.assertEqual(len(found), 1)
        self.assertEqual(leaked, [])
    def test_missing_section_moves_to_review(self) -> None:
        document = ExtractedDocument(
            title="Policy",
            language="en",
            text="alpha body beta body",
            sections=(("Alpha", "alpha rule " * 20), ("Beta", "beta rule " * 20)),
            published_at="2026-01-01",
        )
        self.ingest(self.policy, document)
        changed = ExtractedDocument(
            title="Policy",
            language="en",
            text="alpha changed",
            sections=(("Alpha", "alpha changed rule " * 20),),
            published_at="2026-02-01",
        )
        self.ingest(self.policy, changed, "2026-07-26T00:00:00+00:00")
        reasons = {item["review_reason"] for item in self.db.review_required()}
        self.assertIn("section_missing", reasons)


    def test_bilingual_routing_returns_fulfillment_evidence(self) -> None:
        source = SourceDefinition(
            source_key="fulfillment-policy",
            canonical_rule_key="fulfillment.general",
            url="https://seller-us.tiktok.com/fulfillment",
            source_type="policy",
            topic="fulfillment",
            risk="high",
            scope={"market": "US", "seller_type": "seller", "fulfillment": "all"},
        )
        self.db.upsert_source(source)
        document = ExtractedDocument(
            title="Fulfillment",
            language="en",
            text="dispatch service level agreement",
            sections=((
                "Service Level Agreements (SLA)",
                "Regular orders must be scanned by a carrier within 2 business days. " * 8,
            ),),
            published_at="2026-07-01",
        )
        self.ingest(source, document)
        rules = self.db.search(
            "\u666e\u901a\u8ba2\u5355\u591a\u4e45\u5fc5\u987b\u627f\u8fd0\u5546\u626b\u63cf\uff1f",
            scope={"market": "US", "seller_type": "seller"},
        )
        self.assertEqual(rules[0]["source_key"], "fulfillment-policy")
        self.assertTrue(rules[0]["match_reasons"])

    def test_us_local_onboarding_does_not_leak_to_cross_border(self) -> None:
        source = SourceDefinition(
            source_key="seller-registration-corporation",
            canonical_rule_key="onboarding.us-business",
            url="https://seller-us.tiktok.com/onboarding",
            source_type="guide",
            topic="US local onboarding",
            risk="high",
            scope={"market": "US", "seller_type": "US_LOCAL", "fulfillment": "all"},
        )
        self.db.upsert_source(source)
        self.ingest(source, self.doc("business registration documents requirements " * 8))
        rules = self.db.search(
            "\u4e2d\u56fd\u8de8\u5883\u5356\u5bb6\u8425\u4e1a\u6267\u7167\u8981\u6c42",
            scope={"market": "US", "seller_type": "cross_border"},
        )
        self.assertEqual(rules, [])

    def test_v2_schema_is_idempotent_and_has_evidence_chain(self) -> None:
        self.ingest(self.policy, self.doc("traceable official evidence " * 8))
        first = self.db.status()
        second_migration = self.db.initialize()
        second = self.db.status()
        self.assertEqual(first["schema_version"], 2)
        self.assertTrue(second_migration["fts5_v2"])
        self.assertEqual(
            first["table_counts"]["evidence_links"],
            second["table_counts"]["evidence_links"],
        )
        result = self.db.search("traceable official evidence")[0]
        self.assertTrue(result["evidence"])
        self.assertEqual(result["evidence"][0]["parser_version"], "html-parser-v2")

    def test_as_of_date_returns_rule_effective_at_that_time(self) -> None:
        self.ingest(
            self.policy,
            self.doc(
                "legacy dispatch threshold seven days " * 8,
                effective="2026-01-01",
                published="2025-12-20",
            ),
            "2026-01-01T00:00:00+00:00",
        )
        self.ingest(
            self.policy,
            self.doc(
                "revised dispatch threshold two days " * 8,
                effective="2026-03-01",
                published="2026-02-15",
            ),
            "2026-03-01T00:00:00+00:00",
        )
        old = self.db.search(
            "dispatch threshold",
            as_of_date="2026-02-01",
        )
        current = self.db.search(
            "dispatch threshold",
            as_of_date="2026-04-01",
        )
        self.assertIn("seven days", old[0]["content"])
        self.assertIn("two days", current[0]["content"])
        self.assertEqual(old[0]["valid_to"], "2026-03-01")

    def test_extended_scope_filters_category_and_origin(self) -> None:
        source = SourceDefinition(
            source_key="medical-policy",
            canonical_rule_key="product.medical",
            url="https://seller-us.tiktok.com/medical",
            source_type="specific_rule",
            topic="medical",
            risk="high",
            scope={
                "market": "US",
                "seller_origin": "US",
                "actor_type": "seller",
                "seller_type": "US_LOCAL",
                "fulfillment": "all",
                "category": "medical_devices",
            },
        )
        self.db.upsert_source(source)
        self.ingest(source, self.doc("medical device certificate evidence " * 8))
        found = self.db.search(
            "medical device certificate",
            scope={"seller_origin": "US", "category": "medical"},
        )
        rejected = self.db.search(
            "medical device certificate",
            scope={"seller_origin": "CN", "category": "medical"},
        )
        self.assertEqual(found[0]["applicability"]["category"], "medical_devices")
        self.assertEqual(rejected, [])

    def test_review_decision_is_audited(self) -> None:
        self.ingest(self.policy, self.doc("original reviewed policy " * 8))
        self.ingest(
            self.policy,
            self.doc("undated candidate text " * 8, published=None),
        )
        candidate = self.db.review_required()[0]
        decision = self.db.record_review_decision(
            candidate["id"],
            "reject",
            "缺少发布日期",
            "unit-test",
        )
        self.assertEqual(decision["decision"], "reject")
        self.assertEqual(self.db.review_required(), [])
        self.assertEqual(self.db.history(candidate["rule_key"])[0]["status"], "withdrawn")

class WorkflowTests(unittest.TestCase):
    def test_conditional_fetch_sends_cache_validators(self) -> None:
        captured: dict[str, str] = {}
        headers = Message()
        headers["Content-Type"] = "text/html"
        headers["ETag"] = '"next"'

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit: int) -> bytes:
                return b"<html><body>official content</body></html>"

            def geturl(self) -> str:
                return "https://seller-us.tiktok.com/policy"

            @property
            def headers(self):
                return headers

        def fake_open(request, _timeout):
            captured["etag"] = request.get_header("If-none-match")
            captured["modified"] = request.get_header("If-modified-since")
            return Response()

        with patch("core.fetch._open", side_effect=fake_open):
            result = fetch_url(
                "https://seller-us.tiktok.com/policy",
                etag='"old"',
                last_modified="Sat, 25 Jul 2026 00:00:00 GMT",
            )
        self.assertEqual(captured["etag"], '"old"')
        self.assertEqual(
            captured["modified"],
            "Sat, 25 Jul 2026 00:00:00 GMT",
        )
        self.assertEqual(result.etag, '"next"')

    def test_platform_sync_lock_rejects_second_writer(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with platform_sync_lock(root):
                with self.assertRaises(SyncLocked):
                    with platform_sync_lock(root):
                        pass

    def test_table_of_contents_is_not_extracted(self) -> None:
        body = (
            "<html><title>Policy</title><body><h1>Policy</h1>"
            "<h2>Requirements</h2><p>" + ("substantive requirement " * 10) + "</p>"
            "<h2>Table of contents</h2><p>" + ("Requirements Overview " * 10) + "</p>"
            "</body></html>"
        ).encode()
        document = extract_document(body)
        self.assertEqual([title for title, _ in document.sections], ["Requirements"])

    def test_validation_fixture_has_broad_query_set(self) -> None:
        import json
        payload = json.loads(
            (SKILL_ROOT / "validation" / "query_cases.json").read_text(encoding="utf-8")
        )
        query_count = sum(len(case["queries"]) for case in payload["cases"])
        negative_count = sum(not case["expected"]["confirmed"] for case in payload["cases"])
        self.assertGreaterEqual(query_count, 80)
        self.assertGreaterEqual(negative_count, 8)
    def test_leading_bare_date_is_publication_date(self) -> None:
        body = (
            "<html><title>Official Policy</title><body>"
            "<h1>Official Policy</h1><p>07/24/2026</p>"
            "<h2>Requirements</h2><p>" + ("official requirement text " * 10) + "</p>"
            "</body></html>"
        ).encode()
        document = extract_document(body)
        self.assertEqual(document.published_at, "2026-07-24")
    def test_rendered_404_is_rejected(self) -> None:
        body = (
            "<html><title>Article viewer</title><body><h1>Article viewer</h1>"
            "<p>Ошибка 404. Похоже, мы не можем найти нужную вам страницу.</p>"
            "<p>Back to the official help center.</p></body></html>"
        ).encode("utf-8")
        with self.assertRaisesRegex(ValueError, "404"):
            extract_document(body)

    def test_hotkey_modal_is_not_a_rule_section(self) -> None:
        body = (
            "<html><title>Requirements</title><body><h1>Requirements</h1>"
            "<p>Hotkeys General Select all Ctrl A Copy Selection Ctrl C "
            "Browser-based page search Ctrl F Scroll down PgDn</p>"
            "</body></html>"
        ).encode()
        with self.assertRaisesRegex(ValueError, "实质规则章节"):
            extract_document(body)
    def test_dynamic_shell_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "正文不足"):
            extract_document(
                b"<html><title>Article viewer</title><body>Article viewer</body></html>"
            )
    def test_official_domain_validation_rejects_lookalike(self) -> None:
        with self.assertRaises(SourceRejected):
            validate_official_url(
                "https://seller-us.tiktok.com.evil.example/policy",
                ["seller-us.tiktok.com"],
            )

    def test_clarification_requests_fulfillment(self) -> None:
        result = clarify_question("Ozon cross-border seller shipping rules")
        self.assertEqual(result["confirmed"]["platform"], "ozon")
        self.assertIn("fulfillment", result["missing"])
        self.assertLessEqual(len(result["questions"]), 3)

    def test_skill_audit_passes_and_platforms_are_isolated(self) -> None:
        result = audit_skill(SKILL_ROOT)
        self.assertTrue(result["ok"], result)
        self.assertNotEqual(
            result["isolated_namespaces"]["tiktok"],
            result["isolated_namespaces"]["ozon"],
        )


if __name__ == "__main__":
    unittest.main()





