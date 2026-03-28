from __future__ import annotations

from pathlib import Path

import typer

import bub.__main__ as main


def test_bootstrap_framework_from_argv_updates_workspace_and_home(tmp_path: Path) -> None:
    framework = main.BubFramework()
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"

    main._bootstrap_framework_from_argv(
        framework,
        ["--workspace", str(workspace), "--home", str(home), "hooks"],
    )

    assert framework.workspace == workspace.resolve()
    assert framework.home == home.resolve()


def test_create_cli_app_bootstraps_paths_before_loading_hooks(monkeypatch, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    observed: dict[str, Path] = {}

    class FakeFramework:
        def __init__(self) -> None:
            self.workspace = Path("/default-workspace")
            self.home = Path("/default-home")

        def load_hooks(self) -> None:
            observed["workspace"] = self.workspace
            observed["home"] = self.home

        def create_cli_app(self) -> typer.Typer:
            return typer.Typer()

    monkeypatch.setattr(main, "BubFramework", FakeFramework)

    app = main.create_cli_app(["--workspace", str(workspace), "--home", str(home), "hooks"])

    assert isinstance(app, typer.Typer)
    assert observed["workspace"] == workspace.resolve()
    assert observed["home"] == home.resolve()
