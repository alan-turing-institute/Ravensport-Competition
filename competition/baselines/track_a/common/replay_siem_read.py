"""The query interface as the offline replay serves it (replay.py).

    replay_siem_read.py --logs DIR --state DIR --now-file FILE -- ARGUMENTS

runs the query interface's own program, competition/siem_read/siem-read, with
ARGUMENTS, against the replay's copy of the Wazuh manager's logs in DIR and
the program's state in the state directory, at the time on the clock of
siem-01 that FILE holds. The replay writes that time before each decision
request, and appends to the copy of the logs the records that the Wazuh
manager had written by then.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

SIEM_READ = Path(__file__).resolve().parents[3] / "siem_read" / "siem-read"


def load_siem_read():
    """The query interface's program, loaded as a module."""
    loader = importlib.machinery.SourceFileLoader("siem_read_program", str(SIEM_READ))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--" not in argv:
        print("replay_siem_read: no -- before the program's arguments", file=sys.stderr)
        return 2
    split = argv.index("--")
    options, arguments = argv[:split], argv[split + 1 :]
    settings = dict(zip(options[::2], options[1::2]))
    try:
        logs = Path(settings["--logs"])
        state = Path(settings["--state"])
        now = float(Path(settings["--now-file"]).read_text())
    except (KeyError, OSError, ValueError) as error:
        print(f"replay_siem_read: {error}", file=sys.stderr)
        return 2
    return load_siem_read().main(arguments, logs=logs, state=state, now=lambda: now)


if __name__ == "__main__":
    sys.exit(main())
