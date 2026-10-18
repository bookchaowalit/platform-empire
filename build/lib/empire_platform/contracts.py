"""Write generated contracts so drift shows up in review and in tests.

empire-contracts [--check]
"""

from __future__ import annotations

import argparse
import json
import sys

from empire_platform.devdb import REPO_ROOT

OPENAPI = REPO_ROOT / "contracts" / "tasks" / "v1" / "openapi.json"


def tasks_openapi() -> dict:
    from tasks_api.app import create_app
    from tasks_api.auth import TokenVerifier

    app = create_app(lambda: None, TokenVerifier("", "contract"))  # type: ignore[arg-type,return-value]
    return app.openapi()


def render() -> str:
    return json.dumps(tasks_openapi(), indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="empire-contracts")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    current = OPENAPI.read_text(encoding="utf-8") if OPENAPI.exists() else ""
    if args.check:
        if current != render():
            print(
                f"stale: {OPENAPI.relative_to(REPO_ROOT)}; run empire-contracts",
                file=sys.stderr,
            )
            return 1
        print("contracts are current")
        return 0
    OPENAPI.write_text(render(), encoding="utf-8")
    print(f"wrote {OPENAPI.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
