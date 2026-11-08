import logging

import pytest

from datapulse.config import Settings
from datapulse.db import Database
from datapulse.demo_data import build_demo_db
from datapulse.embeddings import HashEmbedding
from datapulse.mcp_server import DataService, build_server
from datapulse.schema_index import SchemaIndex

logging.getLogger("mcp").setLevel(logging.CRITICAL)


@pytest.fixture(scope="session")
def db_path(tmp_path_factory):
    return build_demo_db(tmp_path_factory.mktemp("data") / "tms_demo.db")


@pytest.fixture(scope="session")
def settings(db_path):
    return Settings(_env_file=None, db_path=str(db_path), llm_api_key="")


@pytest.fixture(scope="session")
def db(db_path):
    return Database(db_path)


@pytest.fixture(scope="session")
def index(db):
    return SchemaIndex(db, HashEmbedding())


@pytest.fixture(scope="session")
def service(settings, index):
    return DataService(settings, index=index)


@pytest.fixture
def server(settings, service):
    return build_server(settings, service=service)
