from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from bub.onboarding.models import OnboardingSessionRecord, PluginInstallState, SecretRef, ValidationReport
from bub.workspace import workspace_paths


class MarketplaceStore:
    def __init__(self, workspace: Path, home: Path) -> None:
        self.workspace = workspace.resolve()
        self.home = home.expanduser().resolve()
        self._paths = workspace_paths(self.workspace, self.home)
        self.workspace_id = self._paths.workspace_id
        self._base_dir = self._paths.control_dir
        self._state_path = self._base_dir / "marketplace.json"
        self._events_path = self._base_dir / "events.jsonl"
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def load_states(self) -> dict[str, PluginInstallState]:
        payload = self._read_state_payload()
        plugins = payload.get("plugins", {})
        result: dict[str, PluginInstallState] = {}
        if not isinstance(plugins, dict):
            return result
        for plugin_id, raw in plugins.items():
            if isinstance(plugin_id, str) and isinstance(raw, dict):
                result[plugin_id] = PluginInstallState.model_validate(raw)
        return result

    def load_sessions(self) -> dict[str, OnboardingSessionRecord]:
        payload = self._read_state_payload()
        sessions = payload.get("sessions", {})
        result: dict[str, OnboardingSessionRecord] = {}
        if not isinstance(sessions, dict):
            return result
        for session_id, raw in sessions.items():
            if isinstance(session_id, str) and isinstance(raw, dict):
                result[session_id] = OnboardingSessionRecord.model_validate(raw)
        return result

    def save_state(self, state: PluginInstallState) -> None:
        payload = self._read_state_payload()
        plugins = payload.setdefault("plugins", {})
        plugins[state.plugin_id] = state.model_dump(mode="json")
        self._write_state_payload(payload)

    def delete_state(self, plugin_id: str) -> None:
        payload = self._read_state_payload()
        plugins = payload.setdefault("plugins", {})
        if isinstance(plugins, dict):
            plugins.pop(plugin_id, None)
        self._write_state_payload(payload)

    def save_session(self, session: OnboardingSessionRecord) -> None:
        payload = self._read_state_payload()
        sessions = payload.setdefault("sessions", {})
        sessions[session.session_id] = session.model_dump(mode="json")
        self._write_state_payload(payload)

    def append_event(self, name: str, payload: dict[str, Any]) -> None:
        event = {
            "name": name,
            "date": datetime.now(UTC).isoformat(),
            "payload": payload,
        }
        with self._events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _read_state_payload(self) -> dict[str, Any]:
        if not self._state_path.exists():
            return {"plugins": {}, "sessions": {}}
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"plugins": {}, "sessions": {}}
        return cast(dict[str, Any], payload if isinstance(payload, dict) else {"plugins": {}, "sessions": {}})

    def _write_state_payload(self, payload: dict[str, Any]) -> None:
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class SecretStore:
    def __init__(self, home: Path, workspace: Path) -> None:
        self.home = home.expanduser().resolve()
        self.workspace = workspace.resolve()
        self._paths = workspace_paths(self.workspace, self.home)
        self.workspace_id = self._paths.workspace_id
        self._base_dir = self._paths.secrets_dir
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def put(self, *, plugin_id: str, key: str, value: str) -> SecretRef:
        ref_id = f"{plugin_id}_{key}_{os.urandom(8).hex()}"
        now = datetime.now(UTC)
        secret_ref = SecretRef(
            ref=f"secret://{self.workspace_id}/{ref_id}",
            plugin_id=plugin_id,
            key=key,
            created_at=now,
            updated_at=now,
        )
        payload = {"value": value, "meta": secret_ref.model_dump(mode="json")}
        path = self._secret_path(ref_id)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.chmod(path, 0o600)
        return secret_ref

    def get(self, ref: SecretRef) -> str:
        ref_id = ref.ref.rsplit("/", 1)[-1]
        payload = json.loads(self._secret_path(ref_id).read_text(encoding="utf-8"))
        value = payload.get("value")
        if not isinstance(value, str):
            raise KeyError(f"Secret value missing for {ref.ref}")
        return value

    def delete(self, ref: SecretRef) -> None:
        ref_id = ref.ref.rsplit("/", 1)[-1]
        path = self._secret_path(ref_id)
        if path.exists():
            path.unlink()

    def export_records(self) -> list[dict[str, Any]]:
        if not self._base_dir.exists():
            return []
        records: list[dict[str, Any]] = []
        for file in sorted(self._base_dir.glob("*.json")):
            payload = json.loads(file.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                continue
            value = payload.get("value")
            meta = payload.get("meta")
            if not isinstance(value, str) or not isinstance(meta, dict):
                continue
            records.append({"ref_id": file.stem, "value": value, "meta": meta})
        return records

    def import_records(self, records: list[dict[str, Any]]) -> None:
        self._base_dir.mkdir(parents=True, exist_ok=True)
        for record in records:
            ref_id = record.get("ref_id")
            value = record.get("value")
            meta = record.get("meta")
            if not isinstance(ref_id, str) or not isinstance(value, str) or not isinstance(meta, dict):
                continue
            path = self._secret_path(ref_id)
            path.write_text(json.dumps({"value": value, "meta": meta}, ensure_ascii=False), encoding="utf-8")
            os.chmod(path, 0o600)

    def _secret_path(self, ref_id: str) -> Path:
        return self._base_dir / f"{ref_id}.json"


class ValidationCache:
    def __init__(self, store: MarketplaceStore) -> None:
        self._store = store

    def save(self, plugin_id: str, report: ValidationReport) -> None:
        self._store.append_event(
            "plugin.validation",
            {
                "plugin_id": plugin_id,
                "ok": report.ok,
                "summary": report.summary,
                "issues": [issue.model_dump(mode="json") for issue in report.issues],
            },
        )
