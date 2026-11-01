"""Score supplied ASR transcripts privately; no audio, network or task execution."""

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys
import uuid
from asr_evaluation import CORPUS, evaluate, read_json, make_template
from private_output import PrivateOutput, private_directory

ROOT = Path(__file__).resolve().parents[2]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--input', type=Path, help='Owner-private transcript JSON')
    mode.add_argument(
        '--template',
        type=Path,
        help='Create a private blank matrix; never pretends rows are measured',
    )
    parser.add_argument('--baseline-input', type=Path, help='Optional paired owner-private JSON')
    parser.add_argument('--output', type=Path, help='New JSON in an owner-private directory')
    args = parser.parse_args(argv)
    try:
        if args.template:
            if args.output or args.baseline_input:
                raise ValueError('Template cannot score or compare')
            with PrivateOutput(args.template) as output:
                output.write(make_template(read_json(CORPUS)))
            print(args.template)
            return 0
        result = evaluate(
            read_json(CORPUS),
            read_json(args.input, private=True),
            read_json(args.baseline_input, private=True) if args.baseline_input else None,
        )
        destination = args.output
        if destination is None:
            folder = private_directory(
                private_directory(ROOT / 'device-access/private') / 'asr-scores'
            )
            destination = folder / (
                'score-'
                + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-')
                + uuid.uuid4().hex[:8]
                + '.json'
            )
        with PrivateOutput(destination) as output:
            output.write(result)
        print(destination)
        return 0
    except (OSError, ValueError, TypeError, RecursionError, UnicodeError):
        print(
            'ASR scoring failed; check schema, paired coverage and private file permissions. No transcript values shown.',
            file=sys.stderr,
        )
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
