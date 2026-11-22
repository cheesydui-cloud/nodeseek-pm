"""启动 NodeSeek 私信监控面板。"""

import faulthandler
import os
import sys
from pathlib import Path

from ns_panel.server import serve

def _enable_fault_log() -> None:
    candidates = [
        os.environ.get("NS_DATA_DIR") or "",
        os.environ.get("TEMP") or "",
        "/tmp",
        ".",
    ]
    for folder in candidates:
        if not folder:
            continue
        try:
            path = Path(folder) / "nodeseek-panel-fault.log"
            handle = open(path, "w", encoding="utf-8")
        except OSError:
            continue
        faulthandler.enable(handle, all_threads=True)
        return


_enable_fault_log()


def main() -> None:
    if sys.version_info < (3, 10):
        raise SystemExit("需要 Python 3.10 或更高版本。")
    serve()


if __name__ == "__main__":
    main()
