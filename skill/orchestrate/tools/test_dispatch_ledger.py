import hashlib
import multiprocessing

import pytest

import dispatch_ledger as ledger


def _claim_in_process(ledger_path, run_id, candidates, queue):
    queue.put(ledger.claim_dispatch(ledger_path, run_id, candidates))


def test_claim_rotates_round_robin(tmp_path):
    path = tmp_path / "nested" / "ledger.jsonl"

    assert ledger.claim_dispatch(path, "run", ["a", "b"]) == "a"
    assert ledger.claim_dispatch(path, "run", ["a", "b"]) == "b"
    assert ledger.claim_dispatch(path, "run", ["a", "b"]) == "a"
    assert path.is_file()


def test_tie_breaker_is_among_order(tmp_path):
    path = tmp_path / "ledger.jsonl"

    assert ledger.select_candidate(["second", "first"], {}) == "second"
    ledger.record_dispatch(path, "run", "a")
    ledger.record_dispatch(path, "run", "b")
    assert ledger.select_candidate(["b", "a"], ledger.load_counts(path, "run")) == "b"


def test_peek_is_read_only_and_does_not_affect_claim(tmp_path, capsys):
    path = tmp_path / "ledger.jsonl"
    ledger.record_dispatch(path, "run", "a")
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    assert ledger.main(["peek", "--run-id", "run", "--among", "a,b", "--ledger", str(path)]) == 0
    assert capsys.readouterr().out == "b\n"
    after = hashlib.sha256(path.read_bytes()).hexdigest()

    assert after == before
    assert ledger.claim_dispatch(path, "run", ["a", "b"]) == "b"


def test_run_ids_are_isolated(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger.record_dispatch(path, "A", "a")

    assert ledger.claim_dispatch(path, "B", ["a", "b"]) == "a"
    assert ledger.claim_dispatch(path, "A", ["a", "b"]) == "b"


def test_recorded_forced_candidate_is_counted(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger.record_dispatch(path, "run", "a", role="builder")

    assert path.is_file()
    assert ledger.claim_dispatch(path, "run", ["a", "b"]) == "b"


def test_parallel_claims_choose_distinct_candidates(tmp_path):
    path = tmp_path / "ledger.jsonl"
    candidates = [f"candidate-{number}" for number in range(8)]
    queue = multiprocessing.Queue()
    processes = [
        multiprocessing.Process(
            target=_claim_in_process,
            args=(str(path), "run", candidates, queue),
        )
        for _ in candidates
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    selected = [queue.get(timeout=2) for _ in candidates]
    assert sorted(selected) == candidates
    assert sum(ledger.load_counts(path, "run").values()) == len(candidates)


def test_corrupt_ledger_fails_loudly_for_claim_and_peek(tmp_path):
    path = tmp_path / "ledger.jsonl"
    path.write_text("not-json\n", encoding="utf-8")

    with pytest.raises(ledger.LedgerError, match="not valid JSON"):
        ledger.claim_dispatch(path, "run", ["a"])
    with pytest.raises(ledger.LedgerError, match="not valid JSON"):
        ledger.main(["peek", "--run-id", "run", "--among", "a", "--ledger", str(path)])
