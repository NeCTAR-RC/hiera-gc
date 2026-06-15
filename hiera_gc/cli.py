from __future__ import annotations

import argparse
from pathlib import Path
import sys

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

FIX_KINDS = ["unused", "stale_params", "redundant", "orphans", "stale_files"]
DEFAULT_FIX_KINDS = "unused,redundant,orphans,stale_files"

EXIT_CLEAN = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
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
        "--env-dir",
        action="append",
        type=Path,
        default=[],
        metavar="PATH",
        help="Additional environments-root directory to search "
        "(repeatable). Like an extra entry on Puppet's environmentpath: "
        "each holds environment subdirectories. The default "
        "<code-dir>/environments is always searched first; on a name "
        "clash the first root wins",
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
        help="Comma-separated report sections: {} (default: all)".format(
            ",".join(SECTIONS)
        ),
    )
    parser.add_argument(
        "--fail-on",
        default="unused",
        metavar="LIST",
        help="Comma-separated finding kinds that give exit code 1: "
        "{} (default: %(default)s)".format(",".join(FAIL_CHOICES)),
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Remove fixable findings from the --env environment's own "
        "data files. Requires exactly one --env (so a run never fixes "
        "every environment at once); findings in shared, global or "
        "module data are reported as out of scope",
    )
    parser.add_argument(
        "--fix-kinds",
        default=None,
        metavar="LIST",
        help="Comma-separated finding kinds --fix may touch: {} "
        "(default: {})".format(",".join(FIX_KINDS), DEFAULT_FIX_KINDS),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="With --fix: report what would change without writing",
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
        parser.error("unknown --show section(s): {}".format(", ".join(bad)))

    args.fail_on = [s.strip() for s in args.fail_on.split(",") if s.strip()]
    bad = [s for s in args.fail_on if s not in FAIL_CHOICES]
    if bad:
        parser.error("unknown --fail-on value(s): {}".format(", ".join(bad)))
    if "none" in args.fail_on:
        args.fail_on = []

    if args.fix:
        if not args.env:
            parser.error(
                "--fix requires --env NAME: it removes a single "
                "environment's own findings and will not fix "
                "every environment at once"
            )
        if len(args.env) > 1:
            parser.error(
                "--fix acts on exactly one environment per run; "
                "give a single --env NAME"
            )
        if args.env_glob:
            parser.error(
                "--fix cannot be combined with --env-glob; name "
                "the one environment with --env"
            )
    if args.dry_run and not args.fix:
        parser.error("--dry-run requires --fix")
    if args.fix_kinds is not None and not args.fix:
        parser.error("--fix-kinds requires --fix")
    if args.fix_kinds is None:
        args.fix_kinds = DEFAULT_FIX_KINDS
    args.fix_kinds = [
        s.strip() for s in args.fix_kinds.split(",") if s.strip()
    ]
    bad = [s for s in args.fix_kinds if s not in FIX_KINDS]
    if bad:
        parser.error("unknown --fix-kinds value(s): {}".format(", ".join(bad)))
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = RunConfig(
        code_dir=args.code_dir,
        global_hiera=args.global_hiera,
        envs=args.env,
        env_glob=args.env_glob,
        env_dirs=args.env_dir,
        extra_datadirs=args.extra_datadir,
        allowlist=args.allowlist,
        strict=args.strict,
        verbosity=args.verbose,
    )

    if not config.code_dir.is_dir():
        print(
            f"hiera-gc: code dir not found: {config.code_dir}", file=sys.stderr
        )
        return EXIT_ERROR

    env_problems: list[str] = []
    environments = discover_environments(config, env_problems)
    if not environments:
        roots = [config.code_dir / "environments"] + config.env_dirs
        print(
            "hiera-gc: no environments found under {}".format(
                ", ".join(str(r) for r in roots)
            ),
            file=sys.stderr,
        )
        return EXIT_ERROR

    from hiera_gc.analysis import Warn, analyse
    from hiera_gc.report import render_json, render_text

    result = analyse(config, environments)
    for message in env_problems:
        result.warnings.append(Warn("environment", message))

    plan = None
    if args.fix:
        if result.parse_errors:
            print(
                f"hiera-gc: refusing to fix: "
                f"{len(result.parse_errors)} parse error(s) leave "
                "the analysis blind to some consumers; resolve them "
                "first (rerun without --fix to see the warnings)",
                file=sys.stderr,
            )
            return EXIT_ERROR
        from hiera_gc.fix import apply_fixes, plan_fixes

        plan = plan_fixes(result, args.env[0], args.fix_kinds)
        apply_fixes(plan, dry_run=args.dry_run)

    if args.format == "json":
        text = render_json(result, show=args.show, fixes=plan)
    else:
        text = render_text(result, show=args.show, fixes=plan)

    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)

    if args.stats:
        print(result.stats_line(), file=sys.stderr)

    if plan is not None and plan.errors:
        for error in plan.errors:
            print(f"hiera-gc: fix failed: {error}", file=sys.stderr)
        return EXIT_ERROR
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
