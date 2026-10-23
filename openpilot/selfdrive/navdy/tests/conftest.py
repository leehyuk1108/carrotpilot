import pytest

from openpilot.selfdrive.navdy import journal


@pytest.fixture(autouse=True)
def isolated_journal(monkeypatch, tmp_path):
  # Bridge tests must never write production logs, including when run as root.
  monkeypatch.setattr(journal, 'DIRECTORY', tmp_path.resolve() / 'diagnostics')
