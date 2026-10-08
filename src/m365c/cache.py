"""In-memory snapshot cache keyed by ``(tenant_id, collector)``.

A successful collector run *replaces* the snapshot for its key, so series for departed
users disappear. A failed run leaves the previous snapshot alone. Optionally each
snapshot is persisted as JSON so restarts don't start with empty metrics.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

import structlog

from m365c.collectors.base import MetricSample

log = structlog.get_logger(__name__)

_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


@dataclass(frozen=True)
class Snapshot:
    tenant_id: str
    collector: str
    collected_at: float
    samples: tuple[MetricSample, ...]


class SnapshotCache:
    def __init__(self, persist_dir: Path | None = None) -> None:
        self._data: dict[tuple[str, str], Snapshot] = {}
        self._dir = persist_dir
        if self._dir is not None:
            self._dir.mkdir(parents=True, exist_ok=True)

    # -- reads (used by /metrics) -------------------------------------------------

    def get(self, tenant_id: str, collector: str) -> Snapshot | None:
        return self._data.get((tenant_id, collector))

    def snapshots(self) -> list[Snapshot]:
        return list(self._data.values())

    # -- writes -------------------------------------------------------------------

    def replace(self, snapshot: Snapshot) -> None:
        self._data[(snapshot.tenant_id, snapshot.collector)] = snapshot
        if self._dir is not None:
            self._persist(snapshot)

    def drop(self, tenant_id: str, collector: str) -> None:
        self._data.pop((tenant_id, collector), None)
        if self._dir is not None:
            self._path(tenant_id, collector).unlink(missing_ok=True)

    def drop_tenant(self, tenant_id: str) -> None:
        for key in [k for k in self._data if k[0] == tenant_id]:
            self.drop(*key)
        if self._dir is not None:
            for path in self._dir.glob(f"{_SAFE.sub('_', tenant_id)}__*.json"):
                path.unlink(missing_ok=True)

    # -- persistence --------------------------------------------------------------

    def _path(self, tenant_id: str, collector: str) -> Path:
        assert self._dir is not None
        return self._dir / f"{_SAFE.sub('_', tenant_id)}__{_SAFE.sub('_', collector)}.json"

    def _persist(self, snap: Snapshot) -> None:
        path = self._path(snap.tenant_id, snap.collector)
        payload = {
            "tenant_id": snap.tenant_id,
            "collector": snap.collector,
            "collected_at": snap.collected_at,
            "samples": [s.to_json() for s in snap.samples],
        }
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp, path)
        except OSError:
            Path(tmp).unlink(missing_ok=True)
            log.exception("cache_persist_failed", path=str(path))

    def load(self, valid_keys: set[tuple[str, str]] | None = None) -> int:
        """Load persisted snapshots; skip keys not in ``valid_keys`` when given."""
        if self._dir is None:
            return 0
        loaded = 0
        for path in sorted(self._dir.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                snap = Snapshot(
                    tenant_id=str(raw["tenant_id"]),
                    collector=str(raw["collector"]),
                    collected_at=float(raw["collected_at"]),
                    samples=tuple(MetricSample.from_json(s) for s in raw["samples"]),
                )
            except (OSError, ValueError, KeyError, TypeError):
                log.warning("cache_entry_unreadable", path=str(path))
                continue
            if valid_keys is not None and (snap.tenant_id, snap.collector) not in valid_keys:
                continue
            self._data[(snap.tenant_id, snap.collector)] = snap
            loaded += 1
        return loaded
