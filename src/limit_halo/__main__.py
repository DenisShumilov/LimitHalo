from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--check-update"] or (len(args) == 3 and args[:2] == ["--check-update", "--from-version"]):
        # Read-only public release probe: no Tk, providers, settings or install.
        # 0 = current; 10 = newer; 20 = network/validation failure.
        from limit_halo import RELEASE_TAG
        from limit_halo.updates import discover
        try:
            release = discover(RELEASE_TAG if len(args) == 1 else args[2])
            if sys.stdout is not None:
                import json
                print(json.dumps({"status": "available" if release else "current", "tag": release.tag if release else None}))
            return 10 if release else 0
        except Exception:
            return 20
    health_args = ["--health-check", "--offline", "--no-provider-start"]
    if args == health_args:
        from limit_halo.health import offline_health_check

        return 0 if offline_health_check() else 1
    if any(argument in health_args for argument in args):
        raise SystemExit("Health check requires exact offline no-provider arguments")
    if args == ["--configure"]:
        # This intentionally imports only the provider-free setup route.  It
        # works with zero installed clients and starts no collector or auth.
        from limit_halo.configure import main as configure_main

        return configure_main()
    if args == ["--grant-claude-quota-access"]:
        # The installer reaches this only after its explicit disclosure
        # checkbox.  This route changes preferences and starts no provider.
        from limit_halo.configure import grant_claude_quota_access

        return grant_claude_quota_access()
    if "--configure" in args or "--grant-claude-quota-access" in args:
        raise SystemExit("Configuration routes require one exact argument")
    if args == ["--startup"]:
        from limit_halo.app import main as production_main

        return production_main(startup_launch=True)
    if "--startup" in args:
        raise SystemExit("Startup requires one exact argument")
    if "--demo-fixture" in args:
        if args.count("--demo-fixture") != 1:
            raise SystemExit("--demo-fixture may be supplied only once")
        args.remove("--demo-fixture")
        from limit_halo.demo import main as demo_main

        return demo_main(args)
    if args:
        raise SystemExit("Unsupported command-line arguments")
    from limit_halo.app import main as production_main

    return production_main(startup_launch=False)


if __name__ == "__main__":
    raise SystemExit(main())
