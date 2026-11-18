"""Python text is the existing authorized source evidence, never an extra read."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys

from olympus.execution.source_metadata import CHECK_DRIVER, build_source_metadata


SOURCE = '"""Search interface.\u641c\u7d22"""\r\nclass ISearchAPI:\r\n    def search(self, keyword: str) -> list:\r\n        ...\r\n'


def test_python_text_preserves_authorized_utf8_source_and_the_same_hash():
    facts = build_source_metadata({"interfaces/ISearchAPI.py": SOURCE})["interfaces/ISearchAPI.py"]
    assert facts["text"] == SOURCE
    assert facts["text"].encode("utf-8") == SOURCE.encode("utf-8")
    assert facts["sha256"] == hashlib.sha256(facts["text"].encode("utf-8")).hexdigest()
    assert "..." in facts["text"]
    assert "ISearchAPI.search" in facts["signatures"]


def test_python_text_uses_only_the_supplied_file_map_without_file_reads(monkeypatch):
    def forbidden_read(*args, **kwargs):
        raise AssertionError("metadata generation must not read another file")

    with monkeypatch.context() as patch:
        patch.setattr("builtins.open", forbidden_read)
        patch.setattr(Path, "open", forbidden_read)
        patch.setattr(Path, "read_text", forbidden_read)
        patch.setattr(Path, "read_bytes", forbidden_read)
        facts = build_source_metadata({"allowed.py": SOURCE, "not_executed.py": "raise RuntimeError('never run')\n"})
    assert set(facts) == {"allowed.py", "not_executed.py"}
    assert facts["allowed.py"]["text"] == SOURCE
    assert facts["not_executed.py"]["text"] == "raise RuntimeError('never run')\n"


def test_actual_driver_injects_python_text_as_read_only_scoped_evidence(tmp_path):
    driver, check, metadata = (tmp_path / name for name in ("driver.py", "check.py", "source_metadata.json"))
    driver.write_text(CHECK_DRIVER, encoding="utf-8")
    metadata.write_text(json.dumps(build_source_metadata({"interfaces/ISearchAPI.py": SOURCE})), encoding="utf-8")
    check.write_text(
        "facts = source_metadata['interfaces/ISearchAPI.py']\n"
        f"assert facts['text'] == {SOURCE!r}\n"
        "assert '...' in facts['text']\n"
        "assert set(source_metadata) == {'interfaces/ISearchAPI.py'}\n"
        "try:\n    facts['text'] = 'forged source'\n"
        "except TypeError:\n    pass\n"
        "else:\n    raise AssertionError('source text was writable')\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", str(driver), str(tmp_path), str(check), str(metadata)],
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0, result.stdout + result.stderr
