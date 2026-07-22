"""Temporary entry point for the run-lane detect command."""
import json


def main(argv: list) -> int:
    print(json.dumps({"mode": "detect", "status": "stub"}))
    return 0
