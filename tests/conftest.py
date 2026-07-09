import pytest

from agenticiam import db as db_module
from agenticiam.directory import Directory


@pytest.fixture
def conn():
    connection = db_module.connect(":memory:")
    db_module.init_schema(connection)
    yield connection
    connection.close()


@pytest.fixture
def directory(conn):
    return Directory(conn)
