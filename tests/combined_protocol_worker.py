"""Independent process for mixed protocol schedules; exit without cleanup."""
import json
import os
from pathlib import Path
import sys

from models.combined_conformance import Harness


def main():
    directory = Path(sys.argv[1])
    scenario = json.loads((directory/'worker.json').read_text(encoding='utf-8'))
    target = scenario.get('fault')

    def fault(point):
        if point == target:
            os._exit(77)

    h = Harness(directory, fault=fault)
    for action in scenario['actions']:
        h.step(action)
    if target is not None:
        raise AssertionError('requested fault point was not reached')
    os._exit(77)


if __name__ == '__main__':
    main()
