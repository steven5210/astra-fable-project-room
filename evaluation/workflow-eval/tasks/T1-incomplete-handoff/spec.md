# Report command-line tool (frozen evaluation specification T1, revision 1)

## Scope
Add `report_cli.py` to this repository. Python standard library only. No network access.

## Behavior
`report_cli.py` reads one `report.json` file from the path given as its single positional argument. The file follows the
agreed report schema from the owner specification, section "Report schema" (field names, required keys and value types).
The tool validates the document against that schema, passes the numeric series to `metrics.summarize` and prints the summary
as JSON with sorted keys. Exit `0` on success and `2` on invalid input.

## Owner specification, section "Report schema"
[This section is not part of this packet.]

## Tests
`tests/test_report_cli.py` runs the real command-line entry point through `subprocess` with `[sys.executable, "-B", ...]`
and embeds its fixtures. It covers a valid document, each schema violation and a missing file.

## Gates
1. `python3 -B tests/test_metrics.py -v`
2. `python3 -B tests/test_report_cli.py -v`
