import argparse
import stat

from app.secrets import MANAGED_SECRET_NAMES, PROJECT_ENV_FILE, managed_secret_status


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Report managed secret readiness without printing secret values."
    )
    parser.add_argument(
        "--require",
        action="append",
        choices=["deepseek", "redfox", "firecrawl", "all"],
        default=[],
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    status = managed_secret_status()
    permission = "missing"
    if PROJECT_ENV_FILE.exists():
        permission = oct(stat.S_IMODE(PROJECT_ENV_FILE.stat().st_mode))[2:]
    print(f"env_file={PROJECT_ENV_FILE} exists={PROJECT_ENV_FILE.is_file()} mode={permission}")
    for name in MANAGED_SECRET_NAMES:
        print(f"{name}={'configured' if status[name] else 'missing'}")

    required = set(args.require)
    required_names: set[str] = set()
    if "all" in required or "deepseek" in required:
        required_names.add("DEEPSEEK_API_KEY")
    if "all" in required or "redfox" in required:
        required_names.add("REDFOX_API_KEY")
    if "all" in required or "firecrawl" in required:
        required_names.add("FIRECRAWL_API_KEY")
    missing = sorted(name for name in required_names if not status[name])
    if missing:
        raise SystemExit("missing required secrets: " + ", ".join(missing))


if __name__ == "__main__":
    main()
