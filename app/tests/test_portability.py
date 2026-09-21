"""app/ だけを別の場所へ置いても動く（隣の検証用フォルダ・作業フォルダに依存しない）。"""

import pathlib
import shutil
import subprocess
import sys

import pytest

from app import agent, cards

APP = pathlib.Path(agent.__file__).resolve().parent
LEGACY = APP.parents[1] / "your_folder" / "specs.py"


def test_the_bundled_specs_are_the_ones_used_by_default(monkeypatch):
    monkeypatch.delenv("MIRUCON_SPECS_PATH", raising=False)
    assert agent._find_specs_path() == APP / "specs.py"


@pytest.mark.skipif(not LEGACY.is_file(), reason="検証用フォルダがない環境")
def test_the_bundled_copy_is_identical_to_the_verification_specs():
    """写しがずれると、検証（run_verify）とアプリで、ツールの定義が食い違う。"""
    assert (APP / "specs.py").read_bytes() == LEGACY.read_bytes()


def test_the_environment_variable_wins(monkeypatch, tmp_path):
    f = tmp_path / "s.py"
    f.write_text("x = 1", encoding="utf-8")
    monkeypatch.setenv("MIRUCON_SPECS_PATH", str(f))
    assert agent._find_specs_path() == f


def test_a_missing_environment_path_falls_back(monkeypatch, tmp_path):
    monkeypatch.setenv("MIRUCON_SPECS_PATH", str(tmp_path / "none.py"))
    assert agent._find_specs_path() == APP / "specs.py"


def test_the_app_imports_without_the_sibling_verification_folder(tmp_path):
    """app/ だけを空のフォルダへコピーして、作業フォルダも別の場所にして、読み込めること。"""
    dest = tmp_path / "elsewhere" / "app"
    shutil.copytree(APP, dest, ignore=shutil.ignore_patterns("tests", "data", "__pycache__", "*.db"))
    other_cwd = tmp_path / "cwd"
    other_cwd.mkdir()
    r = subprocess.run([sys.executable, "-c", "import app.agent, app.web, app.vision; print(app.agent._SPEC_PATH)"],
                       cwd=other_cwd, capture_output=True, text=True, encoding="utf-8",
                       env={"PYTHONPATH": str(dest.parent), "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", ""), "PATH": __import__("os").environ.get("PATH", "")})
    assert r.returncode == 0, r.stderr[-500:]
    assert str(dest) in r.stdout


def test_a_moved_image_folder_is_still_found(monkeypatch, tmp_path):
    """DB に古い絶対パスが残っていても、今の DATA_DIR に同名ファイルがあれば読める。"""
    monkeypatch.setattr(cards, "DATA_DIR", tmp_path)
    (tmp_path / "img_abc.jpg").write_bytes(b"x")
    assert cards.locate_image(r"C:\old\place\img_abc.jpg") == tmp_path / "img_abc.jpg"


def test_an_existing_absolute_path_is_used_as_before(tmp_path):
    f = tmp_path / "img_1.jpg"
    f.write_bytes(b"x")
    assert cards.locate_image(str(f)) == f


def test_a_missing_file_returns_the_original_path(tmp_path):
    p = str(tmp_path / "none.jpg")
    assert str(cards.locate_image(p)) == p
