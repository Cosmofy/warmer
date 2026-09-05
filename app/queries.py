from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from app.errors import Code, Error


@dataclass(frozen=True)
class Operation:
    name: str
    query: str
    root: str
    expected_date: str | None = None

    def validate(self, body: object) -> bool:
        if not isinstance(body, dict) or body.get("errors"):
            return False
        data = body.get("data")
        if not isinstance(data, dict) or self.root not in data:
            return False
        value = data[self.root]
        if self.root == "__typename":
            return isinstance(value, str) and bool(value)
        if self.root == "articles":
            return isinstance(value, list) and all(isinstance(item, dict) and isinstance(item.get("id"), str) for item in value)
        return (isinstance(value, dict) and value.get("date") == self.expected_date
                and isinstance(value.get("title"), str) and isinstance(value.get("url"), str))


ARTICLES = """query { articles {
  id title subtitle month year url source
  authors { name title image }
  banner { image designer }
} }"""
PICTURE = """query { apod {
  date title explanation mediaType url hdUrl credit copyright
} }"""
PROBE = Operation("discovery", "query { __typename }", "__typename")


def resolve_operation(name: str, now: datetime | None = None) -> Operation:
    if name == "articles":
        return Operation(name, ARTICLES, "articles")
    if name == "picture":
        today = (now or datetime.now(ZoneInfo("America/Denver"))).astimezone(ZoneInfo("America/Denver")).date()
        return Operation(name, PICTURE, "apod", today.isoformat())
    raise Error(Code.UNKNOWN_QUERY)
