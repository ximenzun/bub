from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bub.onboarding.service import MarketplaceService
from bub.onboarding.store import SecretStore
from bub.workspace import (
    SECRET_DIR_NAME,
    TAPE_DIR_NAME,
    SecretExportMode,
    TapeExportMode,
    WorkspaceBundleManifest,
    WorkspaceMetadata,
    ensure_workspace_metadata,
    workspace_paths,
    write_workspace_metadata,
)

_PBKDF2_ITERATIONS = 600_000


@dataclass(frozen=True, slots=True)
class WorkspaceDoctorReport:
    workspace_id: str
    installed_plugins: list[str]
    rebind_required: list[str]
    secret_plugins: list[str]
    tape_files: list[str]

    def render(self) -> str:
        lines = [
            f"workspace_id: {self.workspace_id}",
            f"installed_plugins: {', '.join(self.installed_plugins) if self.installed_plugins else '-'}",
            f"rebind_required: {', '.join(self.rebind_required) if self.rebind_required else '-'}",
            f"secret_plugins: {', '.join(self.secret_plugins) if self.secret_plugins else '-'}",
            f"tape_files: {len(self.tape_files)}",
        ]
        return "\n".join(lines)


class WorkspaceBundleService:
    def __init__(self, *, workspace: Path, home: Path, marketplace: MarketplaceService) -> None:
        self.workspace = workspace.resolve()
        self.home = home.expanduser().resolve()
        self.marketplace = marketplace
        self.metadata = ensure_workspace_metadata(self.workspace, self.home)

    def doctor(self) -> WorkspaceDoctorReport:
        installed_plugins: list[str] = []
        rebind_required: list[str] = []
        secret_plugins: list[str] = []
        for manifest in self.marketplace.manifests():
            state = self.marketplace.state(manifest.plugin_id)
            if state is None:
                continue
            installed_plugins.append(manifest.plugin_id)
            if state.secret_refs:
                secret_plugins.append(manifest.plugin_id)
            if manifest.portability.runtime_state == "rebind_required":
                rebind_required.append(manifest.plugin_id)
        tape_files = [path.name for path in self._workspace_tape_files(mode="full")]
        return WorkspaceDoctorReport(
            workspace_id=self.metadata.workspace_id,
            installed_plugins=sorted(installed_plugins),
            rebind_required=sorted(rebind_required),
            secret_plugins=sorted(secret_plugins),
            tape_files=sorted(tape_files),
        )

    def export_bundle(
        self,
        destination: Path,
        *,
        tape_mode: TapeExportMode = "messages",
        secret_mode: SecretExportMode = "none",  # noqa: S107
        passphrase: str | None = None,
    ) -> Path:
        destination = destination.expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        bundle_manifest = WorkspaceBundleManifest(
            workspace_id=self.metadata.workspace_id,
            tape_mode=tape_mode,
            secret_mode=secret_mode,
            source_bub_version=_source_bub_version(),
            portability_notes=self._portability_notes(secret_mode=secret_mode),
        )
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            self._write_json(bundle, "manifest.json", bundle_manifest.model_dump(mode="json"))
            self._write_json(bundle, "workspace.json", self.metadata.model_dump(mode="json"))
            paths = workspace_paths(self.workspace, self.home)
            self._write_file_if_exists(bundle, paths.control_dir / "marketplace.json", "control/marketplace.json")
            self._write_file_if_exists(bundle, paths.control_dir / "events.jsonl", "control/events.jsonl")
            self._write_tapes(bundle, tape_mode=tape_mode)
            self._write_secrets(bundle, secret_mode=secret_mode, passphrase=passphrase)
        return destination

    def import_bundle(self, bundle_path: Path, *, passphrase: str | None = None, force: bool = False) -> WorkspaceBundleManifest:
        bundle_path = bundle_path.expanduser().resolve()
        with zipfile.ZipFile(bundle_path) as bundle:
            manifest = WorkspaceBundleManifest.model_validate(json.loads(bundle.read("manifest.json").decode("utf-8")))
            metadata = WorkspaceMetadata.model_validate(json.loads(bundle.read("workspace.json").decode("utf-8")))
            current = ensure_workspace_metadata(self.workspace, self.home)
            if current.workspace_id != metadata.workspace_id and not force:
                raise ValueError(
                    f"Workspace id mismatch: current={current.workspace_id} bundle={metadata.workspace_id}. Use force to replace."
                )
            write_workspace_metadata(self.workspace, self.home, metadata)
            paths = workspace_paths(self.workspace, self.home)
            self._extract_if_present(bundle, "control/marketplace.json", paths.control_dir / "marketplace.json")
            self._extract_if_present(bundle, "control/events.jsonl", paths.control_dir / "events.jsonl")
            self._extract_tapes(bundle)
            self._extract_secrets(bundle, mode=manifest.secret_mode, passphrase=passphrase)
            return manifest

    def _write_tapes(self, bundle: zipfile.ZipFile, *, tape_mode: TapeExportMode) -> None:
        if tape_mode == "none":
            return
        for tape_path in self._workspace_tape_files(mode=tape_mode):
            relative = f"{TAPE_DIR_NAME}/{tape_path.name}"
            if tape_mode == "full":
                bundle.write(tape_path, relative)
                continue
            lines = [json.loads(line) for line in tape_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            filtered = [payload for payload in lines if _keep_tape_payload(payload, mode=tape_mode)]
            if not filtered:
                continue
            bundle.writestr(relative, "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in filtered))

    def _write_secrets(self, bundle: zipfile.ZipFile, *, secret_mode: SecretExportMode, passphrase: str | None) -> None:
        records = self.marketplace.secrets.export_records()
        if secret_mode == "none":  # noqa: S105
            return
        if secret_mode == "refs-only":  # noqa: S105
            refs = [{"ref": record["meta"]["ref"], "plugin_id": record["meta"]["plugin_id"], "key": record["meta"]["key"]} for record in records]
            self._write_json(bundle, f"{SECRET_DIR_NAME}/refs.json", {"records": refs})
            return
        if secret_mode == "plaintext":  # noqa: S105
            self._write_json(bundle, f"{SECRET_DIR_NAME}/records.json", {"records": records})
            return
        if passphrase is None:
            raise ValueError("Encrypted secret export requires a passphrase.")
        envelope = _encrypt_payload({"records": records}, passphrase)
        self._write_json(bundle, f"{SECRET_DIR_NAME}/records.enc.json", envelope)

    def _extract_tapes(self, bundle: zipfile.ZipFile) -> None:
        tape_root = self.home / TAPE_DIR_NAME
        tape_root.mkdir(parents=True, exist_ok=True)
        for name in bundle.namelist():
            if not name.startswith(f"{TAPE_DIR_NAME}/") or name.endswith("/"):
                continue
            target = tape_root / Path(name).name
            target.write_bytes(bundle.read(name))

    def _extract_secrets(self, bundle: zipfile.ZipFile, *, mode: SecretExportMode, passphrase: str | None) -> None:
        if mode == "none":
            return
        if mode == "refs-only":
            return
        if mode == "plaintext":
            payload = json.loads(bundle.read(f"{SECRET_DIR_NAME}/records.json").decode("utf-8"))
        else:
            if passphrase is None:
                raise ValueError("Encrypted secret import requires a passphrase.")
            envelope = json.loads(bundle.read(f"{SECRET_DIR_NAME}/records.enc.json").decode("utf-8"))
            payload = _decrypt_payload(envelope, passphrase)
        records = payload.get("records", [])
        if isinstance(records, list):
            SecretStore(self.home, self.workspace).import_records(records)

    def _workspace_tape_files(self, *, mode: TapeExportMode) -> list[Path]:
        del mode
        tape_root = self.home / TAPE_DIR_NAME
        if not tape_root.exists():
            return []
        prefix = f"{self.metadata.workspace_id}__"
        return sorted(path for path in tape_root.glob(f"{prefix}*.jsonl"))

    def _portability_notes(self, *, secret_mode: SecretExportMode) -> list[str]:
        notes: list[str] = []
        for manifest in self.marketplace.manifests():
            state = self.marketplace.state(manifest.plugin_id)
            if state is None:
                continue
            if manifest.portability.runtime_state == "rebind_required":
                notes.append(f"{manifest.plugin_id}: runtime state must be rebound after import")
            if manifest.portability.secrets == "rebind_required" and secret_mode != "none":  # noqa: S105
                notes.append(f"{manifest.plugin_id}: secrets should be re-entered after import")
        return notes

    @staticmethod
    def _write_json(bundle: zipfile.ZipFile, arcname: str, payload: dict[str, Any]) -> None:
        bundle.writestr(arcname, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

    @staticmethod
    def _write_file_if_exists(bundle: zipfile.ZipFile, path: Path, arcname: str) -> None:
        if path.exists():
            bundle.write(path, arcname)

    @staticmethod
    def _extract_if_present(bundle: zipfile.ZipFile, arcname: str, target: Path) -> None:
        if arcname not in bundle.namelist():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(bundle.read(arcname))


def _keep_tape_payload(payload: dict[str, Any], *, mode: TapeExportMode) -> bool:
    kind = payload.get("kind")
    if mode == "messages":
        return kind in {"message", "tool_call", "tool_result", "anchor", "event", "error", "system"}
    if mode == "metadata":
        return kind in {"anchor", "event", "error", "system"}
    return True


def _source_bub_version() -> str:
    try:
        from bub import __version__
    except Exception:  # pragma: no cover
        return "-"
    return __version__


def _encrypt_payload(payload: dict[str, Any], passphrase: str) -> dict[str, Any]:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    salt = os.urandom(16)
    key = hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, _PBKDF2_ITERATIONS, dklen=32)
    nonce = os.urandom(16)
    ciphertext = _xor_keystream(raw, key=key, nonce=nonce)
    mac = hmac.new(key, b"mac" + nonce + ciphertext, hashlib.sha256).digest()
    return {
        "scheme": "pbkdf2_hmac_sha256_xor_v1",
        "iterations": _PBKDF2_ITERATIONS,
        "salt": base64.b64encode(salt).decode("ascii"),
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        "mac": base64.b64encode(mac).decode("ascii"),
    }


def _decrypt_payload(envelope: dict[str, Any], passphrase: str) -> dict[str, Any]:
    salt = base64.b64decode(str(envelope["salt"]))
    nonce = base64.b64decode(str(envelope["nonce"]))
    ciphertext = base64.b64decode(str(envelope["ciphertext"]))
    expected_mac = base64.b64decode(str(envelope["mac"]))
    iterations = int(envelope["iterations"])
    key = hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, iterations, dklen=32)
    actual_mac = hmac.new(key, b"mac" + nonce + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(expected_mac, actual_mac):
        raise ValueError("Bundle secret payload integrity check failed.")
    plaintext = _xor_keystream(ciphertext, key=key, nonce=nonce)
    data = json.loads(plaintext.decode("utf-8"))
    if not isinstance(data, dict):
        raise TypeError("Bundle secret payload is not a mapping.")
    return data


def _xor_keystream(data: bytes, *, key: bytes, nonce: bytes) -> bytes:
    output = bytearray()
    counter = 0
    while len(output) < len(data):
        block = hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        output.extend(block)
        counter += 1
    return bytes(a ^ b for a, b in zip(data, output[: len(data)], strict=False))
