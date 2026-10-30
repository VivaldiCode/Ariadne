from sqlalchemy import Engine, create_engine


def make_engine(database_url: str) -> Engine:
    # connect_timeout curto: o /health deve responder 503 depressa se a base cair.
    return create_engine(database_url, pool_pre_ping=True, connect_args={"connect_timeout": 5})
