"""Visual observation schema (plan section 4), validated with extra=forbid.

The model (or the mock) only reports observations. Coverage, quality and the
return decision are combined by deterministic code in services/rules.py.
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .schemas import DefectType, Grade, ShoeView


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PhotoQuality(Strict):
    photo_id: str
    sharpness: Literal["PASS", "FAIL"]
    exposure: Literal["PASS", "FAIL"]
    occlusion: Literal["LOW", "MEDIUM", "HIGH"]
    looks_like_footwear: bool
    duplicate_of: Optional[str] = None
    detected_view: Optional[ShoeView] = None


class VisionDefect(Strict):
    type: DefectType
    location: str = Field(min_length=1, max_length=80)
    severity: Grade
    description: str = Field(min_length=1, max_length=300)
    photo_ids: List[str] = Field(default_factory=list, max_length=8)
    uncertainty: Literal["low", "medium", "high"]


class VisionResult(Strict):
    photo_quality: List[PhotoQuality]
    covered_views: List[ShoeView]
    missing_views: List[ShoeView]
    defects: List[VisionDefect] = Field(max_length=12)
    possible_structural_or_safety_issue: bool
    text_photo_conflicts: List[str] = Field(default_factory=list, max_length=10)
    cannot_verify: List[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def _consistent(self):
        if set(self.covered_views) & set(self.missing_views):
            raise ValueError("a view can't be both covered and missing")
        return self

    def check_photo_ids(self, known: List[str]) -> List[str]:
        """Problems if the result mentions photos that weren't analysed."""
        allowed = set(known)
        problems = [f"unknown photo {q.photo_id}" for q in self.photo_quality if q.photo_id not in allowed]
        for defect in self.defects:
            problems += [f"defect cites unknown photo {p}" for p in defect.photo_ids if p not in allowed]
        return problems
