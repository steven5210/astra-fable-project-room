# Summarize command-line tool (frozen evaluation specification T2, revision 1)

## Scope
Add `summarize_cli.py` and `tests/test_summarize_cli.py` to this repository. Do not modify `helper.py`, `tests/test_helper.py`
or `README.md`. Python standard library only. No network access.

## Behavior
`summarize_cli.py` reads one text file from the path given as its single positional argument. Each non-empty line holds one
decimal number. The tool calls `helper.summarize` on the parsed list and writes `json.dumps(result, sort_keys=True)` plus one
newline to stdout. Exit `0` on success. On a missing file, a line that is not a number, an empty list or a wrong argument
count, write one line to stderr, write nothing to stdout and exit `2`.

## Tests
`tests/test_summarize_cli.py` is a standard-library `unittest` module. It runs the real command-line entry point through
`subprocess` with `[sys.executable, "-B", <script path>, <input path>]` and a temporary input file, so no bytecode cache is
written into the repository. Tests of `helper.summarize` alone are not proof of the command-line behavior. It covers: a valid
file with mixed integers and decimals (exact JSON output and exit `0`), deterministic output (two runs byte-identical), and exit
`2` with empty stdout for a missing file, a non-numeric line, an empty file and a wrong argument count.

## Gates
1. `python3 -B tests/test_helper.py -v`
2. `python3 -B tests/test_summarize_cli.py -v`
