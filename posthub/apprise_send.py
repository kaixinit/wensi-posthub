"""Isolate Apprise transports and their bounded execution from the web process.
Credentials enter over stdin, never command-line arguments. Plugin logs are muted.
"""
import json
import logging
import sys

import apprise


def main():
    logging.disable(logging.CRITICAL)
    data = json.loads(sys.stdin.read())
    service = apprise.Apprise(asset=apprise.AppriseAsset(async_mode=False))
    if not service.add(data["url"]) or len(service) != 1:
        return 2
    ok = service.notify(title=data["title"], body=data["body"], body_format=apprise.NotifyFormat.TEXT)
    print("accepted" if ok else "unknown")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(2)
