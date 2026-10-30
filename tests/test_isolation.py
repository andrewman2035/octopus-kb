import os
from pathlib import Path

from octopus import cli, config


def test_every_test_runs_with_a_throwaway_home():
    home = Path(os.environ["HOME"])
    assert Path.home() == home
    assert home.name == "home" and "pytest" in str(home)
    assert config.DEFAULT_DATA_DIR == home / "Octopus"
    assert cli.DEFAULT_DATA_DIR == home / "Octopus"
    assert not (home / "Octopus").exists()
