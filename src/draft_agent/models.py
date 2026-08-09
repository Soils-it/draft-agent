from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Player:
    player_id: str
    name: str
    team: str
    position: str
    adp: float
    stats: dict[str, float] = field(default_factory=dict)
    upside: float = 0.5
    risk: float = 0.2
    status: str = "ACTIVE"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.player_id,
            "name": self.name,
            "team": self.team,
            "position": self.position,
            "adp": round(self.adp, 1),
            "upside": round(self.upside, 3),
            "risk": round(self.risk, 3),
            "status": self.status,
        }


@dataclass(frozen=True)
class Pick:
    overall: int
    team_slot: int
    player_id: str
    source: str
