"""Allow `python -m brain` as an alternative CLI entry when console scripts fail."""
from apps.cli.main import cli

if __name__ == "__main__":
    cli()
