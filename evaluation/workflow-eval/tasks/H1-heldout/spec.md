# Verdict command-line tool over an imported handback (frozen evaluation specification H1, revision 1)

## Scope
Add `verdict_cli.py` and `tests/test_verdict_cli.py` to this repository. Do not modify `upstream/verdict.py`,
`upstream/__init__.py`, `UPSTREAM-HANDBACK.json`, `tests/test_upstream_verdict.py` or `README.md`. Python standard library only.

## Behavior
`verdict_cli.py` reads one JSON file from the path given as its single positional argument. The document is an object with one
key `statuses`, a non-empty list of strings. The tool calls `upstream.verdict.summarize_statuses` on that list and writes
`json.dumps(result, sort_keys=True, indent=2)` plus one newline to stdout. Exit `0` for verdict `PASS`, `1` for `FAIL` or
`INCOMPLETE`, and `2` for invalid input (unreadable file, invalid JSON, wrong shape, `InvalidStatusesError`, wrong argument count),
with one line on stderr and nothing on stdout.

## Provenance
The final engineering report cites, in `changes`, the exact upstream commit and the exact SHA-256 of `upstream/verdict.py` as
recorded in `UPSTREAM-HANDBACK.json`. A worker verifies the recorded SHA-256 against the file bytes before the report cites it,
and `tests_reported` records that verification. Values are never cited from memory.

## Tests
`tests/test_verdict_cli.py` is a standard-library `unittest` module. It runs the real command-line entry point through `subprocess`
with `[sys.executable, "-B", <script path>, <input path>]` and a temporary input file. It covers: exit `0` and `PASS`; exit `1`
with `FAIL` and with `INCOMPLETE`; exact counts; deterministic output; and exit `2` with empty stdout for invalid JSON, a missing
`statuses` key, an empty list, an unknown status, a missing file and a wrong argument count.

## Gates
1. `python3 -B tests/test_upstream_verdict.py -v`
2. `python3 -B tests/test_verdict_cli.py -v`
