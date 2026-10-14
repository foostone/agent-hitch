"""Detached worker entry points; an RPC client never owns engine lifetime."""
import argparse
from .engine import supervisor, engine


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("_supervisor", "_engine", "_pool"))
    parser.add_argument("--directory", required=True)
    args = parser.parse_args()
    if args.action == "_pool":
        from .pool import pool_worker
        pool_worker(args.directory)
    else:
        (supervisor if args.action == "_supervisor" else engine)(args.directory)


if __name__ == "__main__":
    main()
