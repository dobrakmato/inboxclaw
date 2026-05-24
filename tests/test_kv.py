from unittest.mock import MagicMock

from src.database import Source, init_db
from src.pipeline.kv import SourceKVService


def test_list_keys_with_prefix_escapes_sql_like_wildcards(tmp_path):
    services = MagicMock()
    services.db_session_maker = init_db(str(tmp_path / "kv.db"))
    kv = SourceKVService(services)

    with services.db_session_maker() as session:
        session.add(Source(id=1, name="test", type="test"))
        session.commit()

    kv.set(1, "snap:sales_2024@example.com:event-1", {"id": "event-1"})
    kv.set(1, "snap:salesX2024@example.com:event-2", {"id": "event-2"})
    kv.set(1, "snap:budget%team@example.com:event-3", {"id": "event-3"})
    kv.set(1, "snap:budget-team@example.com:event-4", {"id": "event-4"})

    assert kv.list_keys_with_prefix(1, "snap:sales_2024@example.com:") == [
        "snap:sales_2024@example.com:event-1"
    ]
    assert kv.list_keys_with_prefix(1, "snap:budget%team@example.com:") == [
        "snap:budget%team@example.com:event-3"
    ]
