"""The ``chd-curator`` command line."""

from __future__ import annotations

from pathlib import Path

import typer

from chd_curator.atlas import AtlasError, default_atlas_cli, target

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.callback()
def main() -> None:
    """Literature curation tools for the CHD atlas."""


@app.command("target")
def target_command(
    symbol: str,
    root: Path = typer.Option(..., "--root", envvar="CHD_ATLAS_ROOT", help="Atlas checkout."),
    atlas_cli: Path | None = typer.Option(None, "--atlas-cli", envvar="CHD_ATLAS_CLI"),
) -> None:
    """Print the atlas's answer about a gene."""
    try:
        answer = target(root, symbol, atlas_cli or default_atlas_cli(root))
    except AtlasError as exc:
        typer.echo(f"error: {exc}")
        raise typer.Exit(2) from exc
    typer.echo(f"{answer.symbol} {answer.gene} published={answer.published}\n{answer.query}")
