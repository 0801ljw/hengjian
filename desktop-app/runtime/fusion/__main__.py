"""Run the trusted local controller as python -m fusion."""
import sys


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--version"]:
        from .packaging import version_label
        print(version_label())
        return 0
    if args and args[0] == "doctor":
        from .packaging import doctor_main
        return doctor_main(args[1:])
    if args and args[0] == "serve":
        from .server import main as serve_main
        return serve_main(args[1:])
    if args and args[0] in ("agents", "bind"):
        from .bind_cli import main as bind_main
        return bind_main(args)
    if args and args[0] == "recover":
        from .recovery_cli import main as recover_main
        return recover_main(args[1:])
    if args and args[0] == "stuck":
        from .stuck import main as stuck_main
        return stuck_main(args[1:])
    if args and args[0] == "run":
        from .ai_run import main as run_main
        return run_main(args[1:])
    if args and args[0] == "approve":
        from .approve import main as approve_main
        return approve_main(args[1:])
    if args and args[0] == "confirm":
        from .approve import confirm_main
        return confirm_main(args[1:])
    if args and args[0] == "sync-project":
        from .sync_projection import main as sync_project_main
        return sync_project_main(args[1:])
    from .control import main as control_main
    # Preserve the historical controller's argument parsing and exit semantics.
    original = sys.argv
    try:
        sys.argv = [original[0], *args]
        return control_main()
    finally:
        sys.argv = original


if __name__ == "__main__":
    raise SystemExit(main())
