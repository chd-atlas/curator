"""``python -m chd_curator`` runs the same command line as ``chd-curator``."""

from chd_curator.cli import app

app(prog_name="chd-curator")
