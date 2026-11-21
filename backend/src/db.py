import os
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, DeclarativeBase
_url=os.environ['DATABASE_URL']
if _url.startswith('sqlite'):
    engine = create_engine(_url, pool_pre_ping=True, connect_args={'check_same_thread': False})
    # 测试环境对齐 MySQL READ COMMITTED：连接不持有读快照，能读到其他连接已提交数据
    @event.listens_for(engine, 'connect')
    def _sqlite_pragmas(dbapi_conn, _):
        cur = dbapi_conn.cursor(); cur.execute('PRAGMA read_uncommitted=ON'); cur.close()
else:
    engine = create_engine(_url, pool_pre_ping=True, isolation_level='READ COMMITTED')
Session = sessionmaker(engine, expire_on_commit=False)
class Base(DeclarativeBase): pass
def db_session():
    with Session() as s:
        yield s
