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
from core.profiles import ProfileError, ProfileStore
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


def _add_profile(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile")


def _resolve_profile(store: ProfileStore, value: str | None) -> str:
    profile_id = value or store.active()
    if profile_id:
        return profile_id
    raise ProfileError("尚无平台档案；必须先运行 onboard 并选择平台")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="动态官方平台规则知识库")
    sub = parser.add_subparsers(dest="command", required=True)

    onboard = sub.add_parser("onboard")
    onboard.add_argument("--platform-name", required=True)
    onboard.add_argument("--market", required=True)
    onboard.add_argument("--seller-origin", default="unspecified")
    onboard.add_argument("--actor-type", default="seller")
    onboard.add_argument("--seller-type", default="unspecified")
    onboard.add_argument("--fulfillment", default="unspecified")
    onboard.add_argument("--official-url", action="append", default=[])
    onboard.add_argument("--timezone", default="Asia/Shanghai")
    onboard.add_argument("--daily-update-time", default="03:00")

    profiles = sub.add_parser("profiles")
    profiles.add_argument("--profile")
    profiles.add_argument("--activate")
    profiles.add_argument("--archive")
    profiles.add_argument("--add-official-url", action="append", default=[])

    discover_parser = sub.add_parser("discover")
    _add_profile(discover_parser)
    discover_parser.add_argument("--timeout", type=int, default=30)
    discover_parser.add_argument("--max-pages", type=int, default=1000)

    build = sub.add_parser("build")
    _add_profile(build)
    build.add_argument("--timeout", type=int, default=30)
    build.add_argument("--max-pages", type=int, default=1000)

    update = sub.add_parser("update")
    _add_profile(update)
    update.add_argument("--all-due", action="store_true")
    update.add_argument("--rediscover", action="store_true")
    update.add_argument("--timeout", type=int, default=30)

    for name in ("coverage", "status", "review"):
        item = sub.add_parser(name)
        _add_profile(item)

    import_file = sub.add_parser("import-official")
    _add_profile(import_file)
    import_file.add_argument("--source", required=True)
    import_file.add_argument("--file", required=True)
    import_file.add_argument("--content-type", choices=("text/html", "text/plain"))

    clarify = sub.add_parser("clarify")
    clarify.add_argument("--question", required=True)

    query = sub.add_parser("query")
    _add_profile(query)
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
    _add_profile(digest)
    digest.add_argument("--since-days", type=int, default=1)

    history = sub.add_parser("history")
    _add_profile(history)
    history.add_argument("--rule-key", required=True)

    review_decide = sub.add_parser("review-decide")
    _add_profile(review_decide)
    review_decide.add_argument("--rule-version-id", required=True, type=int)
    review_decide.add_argument(
        "--decision", required=True,
        choices=("approve", "reject", "withdraw", "keep_current"),
    )
    review_decide.add_argument("--reason", required=True)
    review_decide.add_argument("--reviewer", required=True)
    review_decide.add_argument("--notes")

    sub.add_parser("audit")
    return parser


def _update_all_due(store: ProfileStore, timeout: int) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for profile in store.list():
        profile_id = profile["profile_id"]
        if profile.get("status") == "archived" or not store.is_update_due(profile_id):
            continue
        try:
            results.append(RuleService(SKILL_ROOT, profile_id).update(timeout=timeout))
        except Exception as exc:
            results.append(
                {"profile_id": profile_id, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
            )
    return {"ok": all(item.get("ok", False) for item in results), "updated": results}


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    store = ProfileStore(SKILL_ROOT)
    try:
        if args.command == "onboard":
            profile = store.create(
                platform_name=args.platform_name,
                market=args.market,
                seller_origin=args.seller_origin,
                actor_type=args.actor_type,
                seller_type=args.seller_type,
                fulfillment=args.fulfillment,
                official_urls=args.official_url,
                timezone_name=args.timezone,
                daily_update_time=args.daily_update_time,
            )
            _print({"ok": True, "profile": profile, "next": "discover" if profile["official_seed_urls"] else "verify_official_source"})
            return 0
        if args.command == "profiles":
            action = None
            if args.activate:
                store.activate(args.activate)
                action = {"activated": args.activate}
            if args.archive:
                action = store.delete(args.archive)
            if args.add_official_url:
                target = _resolve_profile(store, args.profile)
                action = {
                    "updated_profile": store.add_official_urls(
                        target, args.add_official_url
                    )
                }
            _print({"ok": True, "active": store.active(), "action": action, "profiles": store.list()})
            return 0
        if args.command == "clarify":
            _print(clarify_question(args.question, profiles=store.list()))
            return 0
        if args.command == "audit":
            result = audit_skill(SKILL_ROOT)
            _print(result)
            return 0 if result["ok"] else 2
        if args.command == "update" and args.all_due:
            result = _update_all_due(store, args.timeout)
            _print(result)
            return 0 if result["ok"] else 3

        profile_id = _resolve_profile(store, getattr(args, "profile", None))
        service = RuleService(SKILL_ROOT, profile_id)
        if args.command == "discover":
            result = service.discover(args.timeout, args.max_pages)
        elif args.command == "build":
            result = service.build(args.timeout, args.max_pages)
        elif args.command == "update":
            result = service.update(args.timeout, rediscover=True if args.rediscover else None)
        elif args.command == "coverage":
            result = service.coverage()
        elif args.command == "import-official":
            result = service.import_official_file(args.source, Path(args.file), args.content_type)
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
                args.rule_version_id, args.decision, args.reason,
                args.reviewer, args.notes,
            )
        else:
            raise AssertionError(args.command)
        _print(result)
        if args.command in {"build", "update"} and not result.get("ok", False):
            return 3
        return 0
    except (PlatformError, ProfileError, ValueError, OSError) as exc:
        _print({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
