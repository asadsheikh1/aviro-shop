"""Switch AVIRO runtime faults on and off through the gateway.

  uv run python scripts/fault.py set payments latency ms=8000 --for 300
  uv run python scripts/fault.py set inventory memory_leak mb_per_s=20
  uv run python scripts/fault.py list orders
  uv run python scripts/fault.py stats inventory
  uv run python scripts/fault.py clear payments            # all faults on payments
  uv run python scripts/fault.py clear payments latency    # just one

Env: AVIRO_URL (default http://127.0.0.1:8080), AVIRO_ADMIN_TOKEN (default: dev token).
"""

import argparse
import json
import os
import sys
from typing import Any

import httpx

URL = os.environ.get("AVIRO_URL", "http://127.0.0.1:8080")
TOKEN = os.environ.get("AVIRO_ADMIN_TOKEN", "aviro-admin-dev-only")
SERVICES = ["gateway", "inventory", "payments", "orders"]


def _value(raw: str) -> Any:
    for cast in (int, float):
        try:
            return cast(raw)
        except ValueError:
            pass
    return {"true": True, "false": False}.get(raw.lower(), raw)


def _path(service: str, suffix: str) -> str:
    return f"/admin/{suffix}" if service == "gateway" else f"/admin/{service}/{suffix}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("set")
    s.add_argument("service", choices=SERVICES)
    s.add_argument("type")
    s.add_argument("params", nargs="*", help="key=value")
    s.add_argument("--for", dest="duration", type=float, default=300, help="seconds (default 300)")
    for name in ("list", "stats"):
        sub.add_parser(name).add_argument("service", choices=SERVICES)
    c = sub.add_parser("clear")
    c.add_argument("service", choices=SERVICES)
    c.add_argument("type", nargs="?")
    args = p.parse_args(argv)

    headers = {"X-Aviro-Admin-Token": TOKEN}
    with httpx.Client(base_url=URL, headers=headers, timeout=10) as client:
        if args.cmd == "set":
            params = dict(kv.split("=", 1) for kv in args.params)
            body = {
                "type": args.type,
                "params": {k: _value(v) for k, v in params.items()},
                "duration_s": args.duration,
            }
            r = client.post(_path(args.service, "faults"), json=body)
        elif args.cmd == "list":
            r = client.get(_path(args.service, "faults"))
        elif args.cmd == "stats":
            r = client.get(_path(args.service, "stats"))
        else:
            suffix = f"faults/{args.type}" if args.type else "faults"
            r = client.delete(_path(args.service, suffix))
    print(json.dumps(r.json(), indent=2))
    return 0 if r.is_success else 1


if __name__ == "__main__":
    sys.exit(main())
