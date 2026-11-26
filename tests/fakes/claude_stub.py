"""Logs argv, cwd and stdin to the file STUB_LOG names, then replies as scripted.

STUB_REPLY is the stdout text, STUB_EXIT the exit code, STUB_STDERR the stderr text and
STUB_SLEEP seconds to wait first. The working directory's listing is logged too.
"""

import json
import os
import sys
import time

stdin = sys.stdin.read()
cwd = os.getcwd()
with open(os.environ["STUB_LOG"], "w", encoding="utf-8") as f:
    json.dump(
        {"argv": sys.argv[1:], "cwd": cwd, "listing": sorted(os.listdir(cwd)), "stdin": stdin}, f
    )
time.sleep(float(os.environ.get("STUB_SLEEP", "0")))
sys.stderr.write(os.environ.get("STUB_STDERR", ""))
sys.stdout.write(os.environ.get("STUB_REPLY", ""))
sys.exit(int(os.environ.get("STUB_EXIT", "0")))
