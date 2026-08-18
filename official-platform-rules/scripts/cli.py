from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from core.audit import audit_skill
from core.clarify import clarify_question
from core.service import PlatformError, RuleService


def _print(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _scope(args: argparse.Namespace) -> dict[str, str]:
    return {
        key: value
        for key, value in {
            "market": getattr(args, "market", None),
            "seller_origin": getattr(args, "seller_origin", None),
            "actor_type": getattr(args, "actor_type", None),
            "seller_type": getattr(args, "seller_type", None),
            "shop_type": getattr(args, "shop_type", None),
            "fulfillment": getattr(args, "fulfillment", None),
            "category": getattr(args, "category", None),
            "program": getattr(args, "program", None),
            "order_state": getattr(args, "order_state", None),
        }.items()
        if value
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="官方平台规则知识库")
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("init", "status", "review"):
        item = sub.add_parser(name)
        item.add_argument("--platform", required=True, choices=("tiktok", "ozon"))

    sync = sub.add_parser("sync")
    sync.add_argument("--platform", required=True, choices=("tiktok", "ozon"))
    sync.add_argument("--source", action="append", dest="sources")
    sync.add_argument("--timeout", type=int, default=30)
    sync.add_argument("--full", action="store_true")

    import_file = sub.add_parser("import-official")
    import_file.add_argument("--platform", required=True, choices=("tiktok", "ozon"))
    import_file.add_argument("--source", required=True)
    import_file.add_argument("--file", required=True)
    import_file.add_argument("--content-type", choices=("text/html", "text/plain"))

    clarify = sub.add_parser("clarify")
    clarify.add_argument("--question", required=True)

    query = sub.add_parser("query")
    query.add_argument("--platform", required=True, choices=("tiktok", "ozon"))
    query.add_argument("--question", required=True)
    query.add_argument("--limit", type=int, default=8)
    query.add_argument("--market")
    query.add_argument("--seller-origin")
    query.add_argument("--actor-type")
    query.add_argument("--seller-type")
    query.add_argument("--shop-type")
    query.add_argument("--fulfillment")
    query.add_argument("--category")
    query.add_argument("--program")
    query.add_argument("--order-state")
    query.add_argument("--as-of", dest="as_of_date")
    query.add_argument("--no-refresh", action="store_true")
    query.add_argument("--timeout", type=int, default=30)

    digest = sub.add_parser("digest")
    digest.add_argument("--platform", required=True, choices=("tiktok", "ozon"))
    digest.add_argument("--since-days", type=int, default=1)

    history = sub.add_parser("history")
    history.add_argument("--platform", required=True, choices=("tiktok", "ozon"))
    history.add_argument("--rule-key", required=True)

    review_decide = sub.add_parser("review-decide")
    review_decide.add_argument(
        "--platform", required=True, choices=("tiktok", "ozon")
    )
    review_decide.add_argument("--rule-version-id", required=True, type=int)
    review_decide.add_argument(
        "--decision",
        required=True,
        choices=("approve", "reject", "withdraw", "keep_current"),
    )
    review_decide.add_argument("--reason", required=True)
    review_decide.add_argument("--reviewer", required=True)
    review_decide.add_argument("--notes")

    sub.add_parser("audit")
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        if args.command == "clarify":
            _print(clarify_question(args.question))
            return 0
        if args.command == "audit":
            result = audit_skill(SKILL_ROOT)
            _print(result)
            return 0 if result["ok"] else 2
        service = RuleService(SKILL_ROOT, args.platform)
        if args.command == "init":
            result = service.initialize()
        elif args.command == "sync":
            result = service.sync(
                set(args.sources) if args.sources else None,
                timeout=args.timeout,
                mode="full" if args.full else "incremental",
            )
        elif args.command == "import-official":
            result = service.import_official_file(
                args.source, Path(args.file), args.content_type
            )
        elif args.command == "query":
            result = service.query(
                args.question,
                limit=max(1, min(args.limit, 50)),
                scope=_scope(args),
                refresh_stale=not args.no_refresh,
                timeout=args.timeout,
                as_of_date=args.as_of_date,
            )
        elif args.command == "digest":
            result = service.digest(max(0, args.since_days))
        elif args.command == "history":
            result = service.history(args.rule_key)
        elif args.command == "status":
            result = service.status()
        elif args.command == "review":
            result = service.review()
        elif args.command == "review-decide":
            result = service.decide_review(
                args.rule_version_id,
                args.decision,
                args.reason,
                args.reviewer,
                args.notes,
            )
        else:
            raise AssertionError(args.command)
        _print(result)
        if args.command == "sync" and not result.get("ok", False):
            return 3
        return 0
    except (PlatformError, ValueError, OSError) as exc:
        _print({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

