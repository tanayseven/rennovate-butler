from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

from renovate_butler import config

_spec = importlib.util.spec_from_file_location("add_repo", Path(__file__).parent.parent / "scripts" / "add_repo.py")
add_repo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(add_repo)

CONFIG = """\
# comment at the top
repos:
  - o/one
  # a comment inside the list
  - o/two
main_branch: main

merge:
  method: squash   # keep me
"""


def test_appends_after_last_item_keeping_comments():
    text, added = add_repo.add_repos(CONFIG, ["o/three"])
    assert added == ["o/three"]
    assert text == CONFIG.replace("  - o/two\n", "  - o/two\n  - o/three\n")
    assert yaml.safe_load(text)["repos"] == ["o/one", "o/two", "o/three"]


def test_skips_existing_repos_case_insensitively_and_dedupes_input():
    text, added = add_repo.add_repos(CONFIG, ["O/One", "o/new", "o/new"])
    assert added == ["o/new"]
    assert yaml.safe_load(text)["repos"] == ["o/one", "o/two", "o/new"]


def test_empty_list():
    text, added = add_repo.add_repos("repos:\nmain_branch: main\n", ["o/r"])
    assert added == ["o/r"]
    assert yaml.safe_load(text) == {"repos": ["o/r"], "main_branch": "main"}


def test_missing_repos_key():
    with pytest.raises(ValueError):
        add_repo.add_repos("main_branch: main\n", ["o/r"])


def test_real_config_stays_loadable(tmp_path):
    real = (Path(__file__).parent.parent / "butler.yaml").read_text()
    text, _ = add_repo.add_repos(real, ["someone/new-repo"])
    (tmp_path / "butler.yaml").write_text(text)
    assert config.load(tmp_path / "butler.yaml").repos[-1] == "someone/new-repo"
