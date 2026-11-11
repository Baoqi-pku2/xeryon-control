"""Compare packaged application bytecode with source, without executing the EXE."""
import argparse
import hashlib
import json
import marshal
from pathlib import Path

from PyInstaller.archive.readers import CArchiveReader


def verify(executable, source_dir):
    archive = CArchiveReader(str(executable))
    pyz = archive.open_embedded_archive("PYZ.pyz")
    modules = {}
    package = source_dir / "xeryon_control"
    if not (package / "__main__.py").is_file():
        raise ValueError(f"Missing application source under {source_dir}")
    for source in sorted(package.rglob("*.py")):
        parts = source.relative_to(source_dir).with_suffix("").parts
        module = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
        if module == "xeryon_control.__main__":
            code = marshal.loads(archive.extract("__main__"))
        else:
            code = pyz.extract(module)
        compiled = compile(source.read_bytes(), code.co_filename, "exec", optimize=0)
        modules[module] = {"matches": code == compiled,
                           "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
    return {"exe": str(executable.resolve()),
            "exe_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
            "all_modules_match": all(entry["matches"] for entry in modules.values()),
            "modules": modules}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("exe", type=Path)
    parser.add_argument("--source-dir", type=Path, default=Path(__file__).resolve().parents[1] / "src")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = verify(args.exe, args.source_dir)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["all_modules_match"] else 1)
