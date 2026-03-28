from __future__ import annotations

import json
from pathlib import Path

from bub.onboarding.bundle import WorkspaceBundleService
from bub.onboarding.catalog import builtin_marketplace_manifests
from bub.onboarding.service import MarketplaceService
from bub.workspace import ensure_workspace_metadata, workspace_id_for_path, workspace_paths


def _marketplace(tmp_path: Path) -> MarketplaceService:
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir(parents=True)
    home.mkdir(parents=True)
    return MarketplaceService(workspace=workspace, home=home, manifests=builtin_marketplace_manifests())


def test_workspace_id_is_stable(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()

    first = ensure_workspace_metadata(workspace, home)
    second = ensure_workspace_metadata(workspace, home)

    assert first.workspace_id == second.workspace_id
    assert first.workspace_id == workspace_id_for_path(workspace, home)
    assert workspace_paths(workspace, home).metadata_path.exists()
    assert not (workspace / ".bub").exists()


def test_workspace_bundle_exports_and_imports_plaintext_secrets_and_tapes(tmp_path: Path) -> None:
    source = _marketplace(tmp_path / "source")
    source.install(
        "telegram",
        config_updates={"allow_users": ["alice"], "allow_chats": ["-1001"]},
        secret_values={"bot_token": "123:abc"},
    )
    tape_root = source.home / "tapes"
    tape_root.mkdir(parents=True, exist_ok=True)
    tape_name = f"{workspace_id_for_path(source.workspace, source.home)}__deadbeefdeadbeef"
    (tape_root / f"{tape_name}.jsonl").write_text(
        json.dumps({"id": 1, "kind": "message", "payload": {"role": "user", "content": "hello"}, "meta": {}, "date": "2026-03-23T00:00:00+00:00"})
        + "\n",
        encoding="utf-8",
    )
    bundle_path = tmp_path / "bundle.bub.zip"
    source_bundle = WorkspaceBundleService(workspace=source.workspace, home=source.home, marketplace=source)
    source_bundle.export_bundle(bundle_path, tape_mode="messages", secret_mode="plaintext")  # noqa: S106

    target_workspace = tmp_path / "target_workspace"
    target_home = tmp_path / "target_home"
    target_workspace.mkdir()
    target_home.mkdir()
    target = MarketplaceService(workspace=target_workspace, home=target_home, manifests=builtin_marketplace_manifests())
    target_bundle = WorkspaceBundleService(workspace=target_workspace, home=target_home, marketplace=target)
    manifest = target_bundle.import_bundle(bundle_path, force=True)
    target = MarketplaceService(workspace=target_workspace, home=target_home, manifests=builtin_marketplace_manifests())

    restored_state = target.state("telegram")
    restored_secret = target.resolve_secret("telegram", "bot_token")
    restored_tapes = sorted((target_home / "tapes").glob("*.jsonl"))

    assert manifest.secret_mode == "plaintext"  # noqa: S105
    assert manifest.tape_mode == "messages"
    assert ensure_workspace_metadata(target_workspace, target_home).workspace_id == ensure_workspace_metadata(source.workspace, source.home).workspace_id
    assert restored_state is not None
    assert restored_state.config["allow_users"] == ["alice"]
    assert restored_secret == "123:abc"  # noqa: S105
    assert len(restored_tapes) == 1
    assert restored_tapes[0].name.startswith(f"{manifest.workspace_id}__")


def test_workspace_bundle_encrypts_secret_records(tmp_path: Path) -> None:
    service = _marketplace(tmp_path)
    service.install("telegram", secret_values={"bot_token": "123:abc"})
    bundle_path = tmp_path / "bundle.enc.zip"

    bundle = WorkspaceBundleService(workspace=service.workspace, home=service.home, marketplace=service)
    bundle.export_bundle(bundle_path, secret_mode="encrypted", passphrase="passphrase")  # noqa: S106

    target_workspace = tmp_path / "target_workspace"
    target_home = tmp_path / "target_home"
    target_workspace.mkdir()
    target_home.mkdir()
    target = MarketplaceService(workspace=target_workspace, home=target_home, manifests=builtin_marketplace_manifests())
    target_bundle = WorkspaceBundleService(workspace=target_workspace, home=target_home, marketplace=target)
    target_bundle.import_bundle(bundle_path, passphrase="passphrase", force=True)  # noqa: S106
    target = MarketplaceService(workspace=target_workspace, home=target_home, manifests=builtin_marketplace_manifests())

    assert target.resolve_secret("telegram", "bot_token") == "123:abc"
