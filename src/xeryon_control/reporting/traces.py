"""Application result paths and the on-disk motion trace format."""
from datetime import datetime
import json
import os
from pathlib import Path
import time


def results_folder() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "XeryonControl" / "results"


def new_trace_path() -> Path:
    folder = results_folder()
    folder.mkdir(parents=True, exist_ok=True)
    return folder / (datetime.now().strftime("motion_%Y%m%d_%H%M%S_%f") + ".jsonl")


def append_event(path: Path, event: str, values: dict) -> None:
    record = {"time": datetime.now().astimezone().isoformat(), "monotonic_s": time.monotonic(),
              "event": event, **values}
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
