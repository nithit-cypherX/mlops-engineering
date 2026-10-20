"""Check the audit boundary with deliberate provider leaks in temporary files."""
import ast
import re
from pathlib import Path

import pytest

from scripts import portability_audit as audit

ROOT = Path(__file__).resolve().parents[2]


def test_repository_audit_keeps_all_instructor_scan_directories(monkeypatch):
    assert audit.SCANNED == ["src", "service", "monitoring", "tests"]
    monkeypatch.setattr(audit, "ROOT", ROOT)
    assert audit.main() == 0


@pytest.mark.parametrize("folder", ["src", "service", "monitoring", "tests"])
def test_provider_leak_in_each_scanned_directory_fails(tmp_path, monkeypatch, capsys, folder):
    target = tmp_path / folder / "leak.py"
    target.parent.mkdir()
    target.write_text("from azure.identity import DefaultAzureCredential\n")
    monkeypatch.setattr(audit, "ROOT", tmp_path)
    assert audit.main() == 1
    output = capsys.readouterr().out
    assert f"{folder}/leak.py:1" in output
    assert "Azure SDK" in output


def test_provider_details_belong_in_adapter_and_adapter_tests(tmp_path, monkeypatch):
    for folder in ("cloudlayer", "cloudlayer/tests"):
        target = tmp_path / folder / "adapter.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("from azure.identity import DefaultAzureCredential\n")
    monkeypatch.setattr(audit, "ROOT", tmp_path)
    assert audit.main() == 0


def test_core_has_no_provider_configuration_or_adapter_imports():
    # The supplied regex does not detect all configuration names or SDK imports.
    # Check the stronger src/ boundary as well, without changing the audit rules.
    for path in (ROOT / "src").rglob("*.py"):
        source = path.read_text()
        assert not re.search(r"azure|amazonaws|googleapis|s3://|gs://|abfss://", source, re.I), path
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                continue
            assert not {name.split(".")[0] for name in modules} & {
                "cloudlayer", "azure", "boto3", "botocore", "google",
            }, path
