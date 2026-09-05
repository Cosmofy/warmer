from datetime import UTC, datetime

import pytest

from app.errors import Error
from app.queries import resolve_operation


def test_picture_uses_modern_apod_and_denver_date():
    operation = resolve_operation("picture", datetime(2026, 9, 5, 4, tzinfo=UTC))
    assert operation.root == "apod" and operation.expected_date == "2026-09-04"
    assert "query { apod" in operation.query
    assert "picture(" not in operation.query


def test_yesterdays_apod_cannot_count_as_warmed_today():
    operation = resolve_operation("picture", datetime(2026, 9, 5, 12, tzinfo=UTC))
    assert not operation.validate({"data": {"apod": {"date": "2026-09-04", "title": "Old", "url": "https://example.org"}}})


@pytest.mark.parametrize("name", ["unknown", "query { apiKey }", "https://localhost", "../picture"])
def test_cannot_supply_arbitrary_queries_or_urls(name):
    with pytest.raises(Error):
        resolve_operation(name)
