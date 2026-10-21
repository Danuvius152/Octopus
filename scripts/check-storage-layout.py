#!/usr/bin/env python3
"""Check managed storage after publication; explicitly prune only owned empty shells."""

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prune", action="store_true", help="Remove owned empty legacy trees before checking")
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from server.config import settings
    from server.service_installation import (
        InstallationError,
        legacy_shells,
        prune_legacy_shells,
        read_service_profile,
    )

    try:
        if read_service_profile(settings) is None:
            raise InstallationError("Storage release checks require the managed service configuration")
        removed = prune_legacy_shells(settings) if args.prune else []
        remaining = [str(path) for path in legacy_shells(settings) if path.exists()]
        print(json.dumps({"passed": not remaining, "unused_legacy_directories": remaining,
                          "removed": [str(path) for path in removed]}))
        return int(bool(remaining))
    except (InstallationError, OSError) as error:
        print(json.dumps({"passed": False, "error": str(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
