"""What every command here shares as a command line: click, with the help
laid out as the module docstring has it, and the exit statuses."""

from __future__ import annotations

import click

EXIT_ERROR = 1
EXIT_USAGE = 2


class Command(click.Command):
    """A command whose help is its module docstring as written -- the usage
    lines and file excerpts there are laid out by hand -- with the option
    summary after it. -h works like --help."""

    context_settings = {"help_option_names": ["-h", "--help"]}

    def __init__(self, *args, **kw):
        settings = dict(self.context_settings, **(kw.pop("context_settings", None) or {}))
        super().__init__(*args, context_settings=settings, **kw)

    def format_help(self, ctx, formatter):
        formatter.write((self.help or "").rstrip() + "\n")
        self.format_options(ctx, formatter)


def run(command: click.Command, argv: list[str] | None, prog: str) -> int:
    """command as a main(): its return value as the exit status, a usage
    error printed and 2, --help printed and 0."""
    try:
        rv = command.main(args=argv, prog_name=prog, standalone_mode=False)
    except click.UsageError as e:
        e.show()
        return EXIT_USAGE
    except click.ClickException as e:
        e.show()
        return e.exit_code
    return rv if isinstance(rv, int) else 0
