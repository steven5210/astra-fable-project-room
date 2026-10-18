"""Download the timing-summary artifact of recent successful main runs into
per-run baseline files for the timing summary's drift comparison. CI-only,
stdlib only; always exits 0 -- every run that fails at any stage is skipped
with a note, and a failure listing runs leaves DIR/notes.txt with the reason.

    python3 tests/ci_timing_baseline.py --repo OWNER/NAME --workflow tests.yml \
        --out DIR [--max-runs 10] [--scan-runs 25] [--skip-run-id ID]

Reads go through `gh api` (argument array, GH_TOKEN from the step env). Each
artifact zip is opened in memory and only the exact member
`timing-combined.json` (<= 8 MiB uncompressed) is read; member names are never
used as paths and nothing else in the zip is written.
"""
import argparse
import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

MEMBER = "timing-combined.json"
MAX_MEMBER_BYTES = 8 * 1024 * 1024


def gh_api(path, binary=False):
    proc = subprocess.run(["gh", "api", path], check=True, capture_output=True)
    return proc.stdout if binary else proc.stdout.decode("utf-8", "replace")


def _note(notes, text):
    notes.append(text)
    print(f"note: {text}")


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="ci_timing_baseline.py",
        description="fetch timing-summary artifacts of recent successful "
                    "main runs as per-run baseline files")
    parser.add_argument("--repo", required=True, metavar="OWNER/NAME")
    parser.add_argument("--workflow", required=True, metavar="FILE",
                        help="workflow file name, e.g. tests.yml")
    parser.add_argument("--out", required=True, metavar="DIR",
                        help="write baseline-<run_id>.json files here")
    parser.add_argument("--max-runs", type=int, default=10, metavar="N",
                        help="stop after N baseline files (default: 10)")
    parser.add_argument("--scan-runs", type=int, default=25, metavar="N",
                        help="list the N newest successful main runs "
                             "(default: 25)")
    parser.add_argument("--skip-run-id", metavar="ID",
                        help="skip this run id (the in-flight run)")
    return parser.parse_args(argv)


def main(argv=None, api=None):
    args = parse_args(argv)
    api = api or gh_api
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    notes = []
    listing = (f"repos/{args.repo}/actions/workflows/{args.workflow}/runs"
               f"?branch=main&event=push&status=success"
               f"&per_page={args.scan_runs}")
    try:
        data = json.loads(api(listing))
        runs = data.get("workflow_runs")
        if not isinstance(runs, list):
            raise ValueError("workflow_runs is not a list")
    except Exception as exc:
        (out / "notes.txt").write_text(
            f"cannot list runs: {type(exc).__name__}: {exc}\n")
        print(f"ci_timing_baseline: cannot list runs: {exc}; wrote notes.txt")
        return 0
    fetched = 0
    for run in runs:
        if fetched >= args.max_runs:
            break
        if not isinstance(run, dict):
            continue
        try:
            run_id = int(run["id"])
        except (KeyError, TypeError, ValueError):
            continue
        if str(run_id) == str(args.skip_run_id):
            continue
        try:
            payload = json.loads(
                api(f"repos/{args.repo}/actions/runs/{run_id}/artifacts"))
            artifacts = payload.get("artifacts")
            artifact = next(
                (entry for entry in artifacts if isinstance(entry, dict)
                 and entry.get("name") == "timing-summary"
                 and not entry.get("expired")), None) \
                if isinstance(artifacts, list) else None
            if artifact is None:
                _note(notes, f"run {run_id}: no timing-summary artifact")
                continue
            size = artifact.get("size_in_bytes")
            if not isinstance(size, int) or size > 16 * 1024 * 1024:
                _note(notes, f"run {run_id}: artifact size unknown"
                             " or over 16 MiB")
                continue
            blob = api(
                f"repos/{args.repo}/actions/artifacts/{artifact['id']}/zip",
                binary=True)
            archive = zipfile.ZipFile(io.BytesIO(blob))
            member = next((info for info in archive.infolist()
                           if info.filename == MEMBER), None)
            if member is None:
                _note(notes, f"run {run_id}: no {MEMBER} member")
                continue
            if member.file_size > MAX_MEMBER_BYTES:
                _note(notes, f"run {run_id}: {MEMBER} over 8 MiB")
                continue
            payload = archive.read(member)
            combined = json.loads(payload)
            if not isinstance(combined, dict):
                raise ValueError("combined payload is not an object")
            target = out / f"baseline-{run_id}.json"
            target.write_bytes(payload)
            fetched += 1
            print(f"run {run_id}: revision {combined.get('revision')},"
                  f" generated_at {combined.get('generated_at')}"
                  f" -> {target.name}")
        except Exception as exc:
            _note(notes, f"run {run_id}: skipped"
                         f" ({type(exc).__name__}: {exc})")
    if notes:
        (out / "notes.txt").write_text("".join(note + "\n" for note in notes))
    print(f"ci_timing_baseline: {fetched} baseline(s) -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
