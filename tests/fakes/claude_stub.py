"""Logs argv, cwd and stdin to the file STUB_LOG names, then replies as scripted.

STUB_REPLY is the stdout text, STUB_EXIT the exit code, STUB_STDERR the stderr text and
STUB_SLEEP seconds to wait first. The working directory's listing is logged too.
"""

import json
import os
import sys
import time

if sys.argv[1:] == ["--version"]:
    with open(os.environ["STUB_LOG"] + ".version", "a", encoding="utf-8") as vf:
        vf.write("x")
    sys.stdout.write(os.environ.get("STUB_VERSION", "9.9.9 (Claude Code)") + "\n")
    sys.exit(0)
argv = sys.argv[1:]
system = ""
system_path = ""
system_mode = ""
if "--system-prompt-file" in argv:
    system_path = argv[argv.index("--system-prompt-file") + 1]
    system_mode = oct(os.stat(system_path).st_mode & 0o777)
    with open(system_path, encoding="utf-8") as sf:
        system = sf.read()
stdin = sys.stdin.read()
cwd = os.getcwd()
with open(os.environ["STUB_LOG"], "w", encoding="utf-8") as f:
    json.dump(
        {
            "argv": sys.argv[1:],
            "cwd": cwd,
            "listing": sorted(os.listdir(cwd)),
            "stdin": stdin,
            "system": system,
            "system_path": system_path,
            "system_mode": system_mode,
        },
        f,
    )
time.sleep(float(os.environ.get("STUB_SLEEP", "0")))
sys.stderr.write(os.environ.get("STUB_STDERR", ""))
sys.stdout.write(os.environ.get("STUB_REPLY", ""))
sys.exit(int(os.environ.get("STUB_EXIT", "0")))
