"""Recompute the entire summary before authorizing a performance claim."""
import argparse, json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / 'timing-release'))
from timing_validate import summarize

def verify(report, summary):
    recomputed = summarize(report)
    if summary != recomputed:
        raise ValueError('Summary differs from recomputed structure and whole-run quality')
    if not (recomputed['structurally_valid'] is True and recomputed['passed'] is True
            and recomputed['performance_claim_allowed'] is True
            and recomputed['status'] == 'valid' and recomputed['quality']['passed'] is True):
        raise ValueError('inconclusive_invalid_quality: no performance claim permitted')
    return True

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('report', type=Path)
    parser.add_argument('summary', type=Path)
    args = parser.parse_args()
    verify(json.loads(args.report.read_bytes()), json.loads(args.summary.read_bytes()))
    print('SOURCE_ON_RECOMPUTED_QUALITY_PASSED', flush=True)

if __name__ == '__main__':
    main()
