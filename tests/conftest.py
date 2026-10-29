import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))


@pytest.fixture(scope="session")
def snapshot():
    return json.loads((ROOT / "tests" / "schema_snapshot.json").read_text())


@pytest.fixture(scope="session")
def bench():
    import yaml
    return yaml.safe_load((ROOT / "bench" / "questions.yaml").read_text())


@pytest.fixture(scope="session")
def db():
    from sql_agent.db import PG, DBError
    try:
        d = PG()
        d.value("SELECT 1 FROM edge.orders LIMIT 1")
        return d
    except DBError as e:
        pytest.skip(f"retail database not reachable ({e}); run db/init.sh or docker compose up")


@pytest.fixture(scope="session")
def ctx(db):
    from sql_agent.runner import build_context
    return build_context(timeout_ms=60000)
