from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


class Pop(BaseModel):
    code: str = Field(pattern=r"^[A-Z0-9]{3}$")
    city: str


class Mapping(BaseModel):
    pop: str
    ip: str
    verified_at: datetime
    server: str


class Inventory(BaseModel):
    fetched_at: datetime
    source: str
    pops: list[Pop]
    fastly_ranges: list[str]
    mappings: list[Mapping] = Field(default_factory=list)
    discovery_errors: list[str] = Field(default_factory=list)


class EdgeResult(BaseModel):
    target_pop: str | None = None
    ip: str | None = None
    actual_pop: str | None = None
    server: str | None = None
    status_code: int | None = None
    cache_status: str | None = None
    duration_ms: float = 0
    success: bool = False
    error: str | None = None


class Run(BaseModel):
    model_config = ConfigDict(validate_assignment=True)
    id: str
    operation: Literal["extract_ip", "warm"]
    query_name: str | None = None
    status: Literal["running", "complete", "incomplete", "failed", "interrupted"] = "running"
    created_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime | None = None
    target_pops: list[str] = Field(default_factory=list)
    covered_pops: list[str] = Field(default_factory=list)
    missing_pops: list[str] = Field(default_factory=list)
    results: list[EdgeResult] = Field(default_factory=list)
    error: str | None = None

    def finish_coverage(self, covered: set[str]) -> None:
        self.covered_pops = sorted(set(self.target_pops) & covered)
        self.missing_pops = sorted(set(self.target_pops) - covered)
        self.status = "complete" if self.target_pops and not self.missing_pops else "incomplete"
        self.finished_at = utc_now()
