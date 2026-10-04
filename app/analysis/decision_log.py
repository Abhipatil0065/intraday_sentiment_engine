"""
DECISION LOG (improvement item 11).

Every classification and every published bias is appended to a JSONL file so the
weights can be audited and tuned LATER against what actually happened in the
market. Without this the transmission map and the decay table can only be
guessed at; with it, ``tools/hitrate_report.py`` can answer "when the engine
said BULLISH at 0.8 confidence, how often was the asset actually higher?".

JSONL rather than extra tables: it is append-only, survives schema changes,
and can be tailed or shipped elsewhere without a database.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.config import BASE_DIR, settings
from app.logging_conf import get_logger

log = get_logger(__name__)


@dataclass
class DecisionRecord:
    """One emitted decision, one line of JSON."""

    ts: str                       # UTC ISO-8601
    kind: str                     # "article" | "bias"
    asset: str = ""
    title: str = ""
    source: str = ""
    cluster_id: str = ""
    category: str = ""
    direction: str = ""
    score: float = 0.0
    confidence: float = 0.0
    bias: str = ""
    mode: str = "heuristic"
    drivers: list = field(default_factory=list)
    event_risk: bool = False
    extra: dict = field(default_factory=dict)


class DecisionLog:
    """
    Append-only JSONL writer.

    Failures are logged and swallowed: losing an audit line must never take
    the live pipeline down.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        raw = path or getattr(settings, "decision_log_path",
                              "logs/decisions.jsonl")
        self.path = Path(raw)
        if not self.path.is_absolute():
            self.path = BASE_DIR / self.path

    def _ensure_parent(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: DecisionRecord) -> None:
        try:
            self._ensure_parent()
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(record), default=str) + "\n")
        except Exception as exc:  # noqa: BLE001 - auditing is best-effort
            log.warning("Decision log append failed: %s", exc)

    # -- convenience writers ------------------------------------------------
    def record_article(self, *, title: str, source: str, cluster_id: str,
                       category: str, mode: str, assets: Any = None,
                       extra: Optional[dict] = None) -> None:
        for entry in (assets or []):
            self.append(DecisionRecord(
                ts=_now_iso(), kind="article", title=title, source=source,
                cluster_id=cluster_id, category=category, mode=mode,
                asset=getattr(getattr(entry, "asset", None), "value", ""),
                direction=getattr(getattr(entry, "direction", None), "value", ""),
                relevance=getattr(entry, "relevance", 0.0),
                extra=extra or {},
            ))

    def record_bias(self, result: Any) -> None:
        """Persist one asset's published verdict."""
        self.append(DecisionRecord(
            ts=_now_iso(),
            kind="bias",
            asset=getattr(getattr(result, "asset", None), "value", ""),
            bias=getattr(getattr(result, "bias", None), "value", ""),
            score=getattr(result, "score", 0.0),
            confidence=getattr(result, "confidence", 0.0),
            mode=getattr(result, "mode", "heuristic"),
            event_risk=bool(getattr(result, "event_risk", False)),
            drivers=[
                {"title": d.title, "contribution": d.contribution,
                 "direction": d.direction}
                for d in (getattr(result, "drivers", None) or [])
            ],
        ))

    # -- reading ------------------------------------------------------------
    def read(self, limit: Optional[int] = None) -> list[DecisionRecord]:
        if not self.path.exists():
            return []
        out: list[DecisionRecord] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(DecisionRecord(**json.loads(line)))
                except (json.JSONDecodeError, TypeError):
                    continue
        return out[-limit:] if limit else out


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_LOG: Optional[DecisionLog] = None


def get_decision_log() -> DecisionLog:
    """Module-level singleton so every writer shares one file handle path."""
    global _LOG
    if _LOG is None:
        _LOG = DecisionLog()
    return _LOG