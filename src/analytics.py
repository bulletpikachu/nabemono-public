import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.config import RESPONSE_ANALYTICS_PATH


@dataclass
class ResponseAnalytics:
    message_id: int              # Discord message ID of the nabemono's response
    request_message_id: int      # User message that triggered it
    channel_id: int

    model: str
    timestamp: datetime

    latency_ms: int              # Difference in time from user message to nabemono response

    input_tokens: int
    output_tokens: int
    thought_tokens: int
    tool_tokens: int
    total_tokens: int

    tool_steps: int              # Amount of steps that used a tool call
    thought_signatures: list[str] = field(default_factory=list)
    tool_results: list[dict] = field(default_factory=list)
    is_interim: bool = False     # True when this Discord message was sent mid-turn via interim_message


class ResponseAnalyticsStore:
    def __init__(self, path: Optional[str | Path] = None):
        self.path = Path(path or RESPONSE_ANALYTICS_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, analytics: ResponseAnalytics) -> None:
        records = self.load()
        records.append(analytics)
        records.sort(key=lambda record: record.message_id)
        self._write_all(records)

    def load(self, limit: Optional[int] = None) -> list[ResponseAnalytics]:
        if not self.path.exists():
            return []

        records = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                records.append(self._from_dict(json.loads(line)))

        records.sort(key=lambda record: record.message_id)
        if limit is not None:
            return records[:limit]
        return records

    def get_recent(self, limit: int = 10) -> list[ResponseAnalytics]:
        records = self.load()
        return list(reversed(records[-limit:])) if limit is not None else list(reversed(records))

    def find_by_message_id(self, message_id: int) -> Optional[ResponseAnalytics]:
        records = self.load()
        left = 0
        right = len(records) - 1

        while left <= right:
            middle = (left + right) // 2
            candidate = records[middle]
            if candidate.message_id == message_id:
                return candidate
            if candidate.message_id < message_id:
                left = middle + 1
            else:
                right = middle - 1

        return None

    def _write_all(self, records: list[ResponseAnalytics]) -> None:
        with self.path.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(self._to_dict(record), default=self._json_default) + "\n")

    @staticmethod
    def _to_dict(analytics: ResponseAnalytics) -> dict:
        return {
            "message_id": analytics.message_id,
            "request_message_id": analytics.request_message_id,
            "channel_id": analytics.channel_id,
            "model": analytics.model,
            "timestamp": analytics.timestamp.isoformat(),
            "latency_ms": analytics.latency_ms,
            "input_tokens": analytics.input_tokens,
            "output_tokens": analytics.output_tokens,
            "thought_tokens": analytics.thought_tokens,
            "tool_tokens": analytics.tool_tokens,
            "total_tokens": analytics.total_tokens,
            "tool_steps": analytics.tool_steps,
            "thought_signatures": analytics.thought_signatures,
            "tool_results": analytics.tool_results,
            "is_interim": analytics.is_interim,
        }

    @staticmethod
    def _from_dict(data: dict) -> ResponseAnalytics:
        timestamp = data.get("timestamp")
        if isinstance(timestamp, str):
            timestamp = timestamp.replace("Z", "+00:00")
            timestamp = datetime.fromisoformat(timestamp)
        elif timestamp is None:
            timestamp = datetime.now(timezone.utc)

        return ResponseAnalytics(
            message_id=int(data["message_id"]),
            request_message_id=int(data["request_message_id"]),
            channel_id=int(data["channel_id"]),
            model=str(data["model"]),
            timestamp=timestamp,
            latency_ms=int(data.get("latency_ms", 0)),
            input_tokens=int(data.get("input_tokens", 0)),
            output_tokens=int(data.get("output_tokens", 0)),
            thought_tokens=int(data.get("thought_tokens", 0)),
            tool_tokens=int(data.get("tool_tokens", 0)),
            total_tokens=int(data.get("total_tokens", 0)),
            tool_steps=int(data.get("tool_steps", 0)),
            thought_signatures=list(data.get("thought_signatures") or []),
            tool_results=list(data.get("tool_results") or []),
            is_interim=bool(data.get("is_interim", False)),
        )

    @staticmethod
    def _json_default(value):
        if isinstance(value, datetime):
            return value.isoformat()
        raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")