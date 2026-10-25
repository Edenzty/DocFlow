import os
import subprocess
import sys


def test_chinese_cli_help_works_with_cp1252_redirected_output():
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "0"
    environment["PYTHONIOENCODING"] = "cp1252"
    result = subprocess.run(
        [sys.executable, "-m", "docflow_lab", "--help"],
        env=environment, capture_output=True, check=True, timeout=15,
    )
    assert "本地技术文档检索与人工审核工单" in result.stdout.decode("utf-8")
    assert b"build-index" in result.stdout
