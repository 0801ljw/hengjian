"""Local trusted-controller CLI. All task writes use apply_mutation."""
import argparse
import json
from pathlib import Path

from .approvals import import_approval, prepare_request
from .budget import import_policy, ledger
from .core import Conflict, PolicyError, Store
from .mutate import apply_mutation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path)
    parser.add_argument("--company", default="default")
    parser.add_argument("--board-key-file", type=Path,
                        default=Path("/home/ljw/.config/fusion/board_ed25519.pub.pem"),
                        help="Ed25519 public PEM only (never a signing key)")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("snapshot")
    sub.add_parser("budget-status")
    sub.add_parser("budget-ledger")
    sub.add_parser("lease-status")
    export = sub.add_parser("export")
    export.add_argument("--out", type=Path, required=True)
    restore = sub.add_parser("import")
    restore.add_argument("--bundle", type=Path, required=True)
    budget = sub.add_parser("import-budget-policy")
    budget.add_argument("envelope", type=Path)
    approve = sub.add_parser("import-approval")
    approve.add_argument("envelope", type=Path)
    for name in ("mutate", "prepare"):
        command = sub.add_parser(name)
        command.add_argument("request", type=Path, help="JSON keyword arguments; never a shell command")
    # Accept both historic global flags and `fusion export --home ...` syntax.
    for command in sub.choices.values():
        command.add_argument("--home", type=Path, default=argparse.SUPPRESS)
        command.add_argument("--company", default=argparse.SUPPRESS)
        command.add_argument("--board-key-file", type=Path, default=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.home is None:
        parser.error("--home is required")
    try:
        if args.action in {"export", "import"}:
            from .transfer import export_bundle, import_bundle
            result = (export_bundle(args.home, args.out, company=args.company,
                                    board_key_file=args.board_key_file) if args.action == "export"
                      else import_bundle(args.bundle, args.home, company=args.company,
                                         board_key_file=args.board_key_file))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        store = (Store(args.home, args.board_key_file) if args.company == "default" else
                 Store(args.home, args.board_key_file, company=args.company))
        if args.action == "snapshot":
            result = store.snapshot()
        elif args.action == "budget-status":
            result = store.budget_status()
        elif args.action == "budget-ledger":
            result = ledger(store)
        elif args.action == "lease-status":
            from .leases import read
            with store.connect() as conn:
                result = {"leases": [read(conn, task_id) for (task_id,) in
                                     conn.execute("SELECT task_id FROM leases ORDER BY task_id")],
                          "execution_commits": [dict(task_id=task, run_id=run,
                                                     fencing_token=token, artifact=json.loads(body))
                                                for task, run, token, body in conn.execute(
                                                    "SELECT * FROM execution_commits ORDER BY task_id")]}
        elif args.action == "import-budget-policy":
            result = import_policy(store, json.loads(args.envelope.read_text()))
        elif args.action == "import-approval":
            result = import_approval(store, json.loads(args.envelope.read_text()))
        else:
            request = json.loads(args.request.read_text())
            if "conn" in request:
                raise PolicyError("internal connection is not a CLI argument")
            result = (apply_mutation if args.action == "mutate" else prepare_request)(store, **request)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (PolicyError, ValueError, TypeError, OSError, KeyError) as exc:
        print(json.dumps({"error": str(exc), "code": getattr(exc, "code", None),
                          "status": 409 if isinstance(exc, Conflict) else 403}))
        return 3 if isinstance(exc, Conflict) else 2


if __name__ == "__main__":
    raise SystemExit(main())
