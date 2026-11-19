"""Application result paths and the on-disk motion trace format."""
from datetime import datetime
import json
import os
from pathlib import Path
import time

from xeryon_control.reporting.logging_service import current_logging, start_logging


def results_folder() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "XeryonControl" / "results"


def new_trace_path() -> Path:
    service = current_logging() or start_logging(install_hooks=False)
    folder = service.path.parent / "motion"
    return folder / (datetime.now().strftime("motion_%Y%m%d_%H%M%S_%f") + ".jsonl")


def append_event(path: Path, event: str, values: dict) -> None:
    record = {"time": datetime.now().astimezone().isoformat(), "monotonic_s": time.monotonic(),
              "event": event, **values}
    service = current_logging() or start_logging(install_hooks=False)
    record["session_id"] = service.session_id
    # Freeze values before the caller can mutate them. All filesystem I/O is queued.
    service.trace(path, json.dumps(record, ensure_ascii=False) + "\n")
