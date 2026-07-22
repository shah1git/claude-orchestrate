"""Temporary entry point for the run-lane smoke command."""
import json


def main(argv: list) -> int:
    print(json.dumps({"mode": "smoke", "status": "stub"}))
    return 0
