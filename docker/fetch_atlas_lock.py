"""Download the atlas's pyproject.toml and uv.lock at one commit (its dependencies, never its code).

    python fetch_atlas_lock.py <owner/repo> <commit>

Exits 2 on a bad argument and 1 when a file cannot be fetched or is empty.
"""

import re
import sys
import urllib.request

FILES = ("pyproject.toml", "uv.lock")


def main(argv: list[str]) -> int:
    if len(argv) != 3 or not re.fullmatch(r"[0-9a-f]{40}", argv[2]):
        print("usage: fetch_atlas_lock.py <owner/repo> <40-hex commit>", file=sys.stderr)
        return 2
    repo, commit = argv[1], argv[2]
    for name in FILES:
        url = f"https://raw.githubusercontent.com/{repo}/{commit}/{name}"
        with urllib.request.urlopen(url, timeout=60) as response:
            data = response.read()
        if not data:
            print(f"{url} is empty", file=sys.stderr)
            return 1
        with open(name, "wb") as handle:
            handle.write(data)
        print(f"fetched {name} ({len(data)} bytes) at {commit}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
