from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from hiera_gc import __version__
from hiera_gc.config import (
    DEFAULT_CODE_DIR,
    DEFAULT_GLOBAL_HIERA,
    RunConfig,
    discover_environments,
)

SECTIONS = [
    "unused",
    "possibly_used",
    "stale_params",
    "stale_files",
    "orphans",
    "redundant",
    "shadowed",
    "warnings",
]
FAIL_CHOICES = SECTIONS + ["none"]

EXIT_CLEAN = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="hiera-gc",
        description=(
            "Find unused, redundant and orphaned Hiera data in a deployed "
            "Puppet code tree. Reports key names, file paths and line "
            "numbers only; data values are never printed."
        ),
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--code-dir",
        type=Path,
        default=DEFAULT_CODE_DIR,
        help="Puppet code directory (default: %(default)s)",
    )
    parser.add_argument(
        "--global-hiera",
        type=Path,
        default=DEFAULT_GLOBAL_HIERA,
        help="Global hiera.yaml; ignored with a warning if absent "
        "(default: %(default)s)",
    )
    parser.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="NAME",
        help="Only analyse this environment (repeatable; default: all)",
    )
    parser.add_argument(
        "--env-glob",
        metavar="GLOB",
        help="Only analyse environments matching this glob",
    )
    parser.add_argument(
        "--extra-datadir",
        action="append",
        type=Path,
        default=[],
        metavar="PATH",
        help="Additional hiera data directory to scan (repeatable)",
    )
    parser.add_argument(
        "--allowlist",
        type=Path,
        metavar="PATH",
        help="File of key-name regexes (one per line, # comments) to "
        "suppress from findings",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Report format (default: %(default)s)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        metavar="PATH",
        help="Write the report here instead of stdout",
    )
    parser.add_argument(
        "--show",
        default=",".join(SECTIONS),
        metavar="LIST",
        help="Comma-separated report sections: %s (default: all)"
        % ",".join(SECTIONS),
    )
    parser.add_argument(
        "--fail-on",
        default="unused",
        metavar="LIST",
        help="Comma-separated finding kinds that give exit code 1: "
        "%s (default: %%(default)s)" % ",".join(FAIL_CHOICES),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat file parse errors as fatal instead of warnings",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Print scan statistics to stderr",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Increase diagnostic output on stderr (-v, -vv)",
    )
    args = parser.parse_args(argv)

    args.show = [s.strip() for s in args.show.split(",") if s.strip()]
    bad = [s for s in args.show if s not in SECTIONS]
    if bad:
        parser.error("unknown --show section(s): %s" % ", ".join(bad))

    args.fail_on = [s.strip() for s in args.fail_on.split(",") if s.strip()]
    bad = [s for s in args.fail_on if s not in FAIL_CHOICES]
    if bad:
        parser.error("unknown --fail-on value(s): %s" % ", ".join(bad))
    if "none" in args.fail_on:
        args.fail_on = []
    return args


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    config = RunConfig(
        code_dir=args.code_dir,
        global_hiera=args.global_hiera,
        envs=args.env,
        env_glob=args.env_glob,
        extra_datadirs=args.extra_datadir,
        allowlist=args.allowlist,
        strict=args.strict,
        verbosity=args.verbose,
    )

    if not config.code_dir.is_dir():
        print("hiera-gc: code dir not found: %s" % config.code_dir,
              file=sys.stderr)
        return EXIT_ERROR

    environments = discover_environments(config)
    if not environments:
        print("hiera-gc: no environments found under %s"
              % (config.code_dir / "environments"), file=sys.stderr)
        return EXIT_ERROR

    from hiera_gc.analysis import analyse
    from hiera_gc.report import render_json, render_text

    result = analyse(config, environments)

    if args.format == "json":
        text = render_json(result, show=args.show)
    else:
        text = render_text(result, show=args.show)

    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)

    if args.stats:
        print(result.stats_line(), file=sys.stderr)

    if config.strict and result.parse_errors:
        return EXIT_ERROR
    if result.fails(args.fail_on):
        return EXIT_FINDINGS
    return EXIT_CLEAN


def entry() -> None:
    """Entry point for the zipapp build; propagates the exit code."""
    sys.exit(main())


if __name__ == "__main__":
    sys.exit(main())
