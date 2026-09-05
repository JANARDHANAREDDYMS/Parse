"""Expose the worker process CLI and produce an exit status without doing PDF work."""

import asyncio

import typer

from maximor.worker import run_worker


def main(
    once: bool = typer.Option(False, "--once", help="Claim at most one queued job."),
) -> None:
    """Start normal polling or claim at most one registered job with --once."""

    raise typer.Exit(code=asyncio.run(run_worker(once=once)))


if __name__ == "__main__":
    typer.run(main)
