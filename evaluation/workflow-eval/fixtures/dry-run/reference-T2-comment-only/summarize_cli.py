import json
import sys

import helper


def main(argv):
    if len(argv) != 2:
        sys.stderr.write("usage: summarize_cli.py INPUT\n")
        return 2
    try:
        with open(argv[1], "r", encoding="utf-8") as handle:
            lines = [line.strip() for line in handle if line.strip()]
        values = [float(line) for line in lines]
        result = helper.summarize(values)
    except (OSError, ValueError) as exc:
        sys.stderr.write(str(exc).splitlines()[0] + "\n")
        return 2
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
