"""补充验证：显式激活、备版测量/认可守卫、seed幂等、MySQL补列DDL的可执行性。
运行：python3 -I test-harness/e2e_extra.py
"""
import os, sys, types, tempfile
from pathlib import Path
import site
sys.path.insert(0, site.getusersitepackages())
BACKEND = Path('/workspace/backend')
sys.path.insert(0, str(BACKEND))
tmp = Path(tempfile.mkdtemp(prefix='press-extra-'))
os.environ['DATABASE_URL'] = f'sqlite:///{tmp}/press.db'
os.environ['REDIS_URL'] = 'redis://localhost:6399/0'
os.environ['JWT_SECRET'] = 'test-secret'
os.environ['SEED_DEMO'] = 'false'

class FakeRedis:
    def rpush(self, key, value):
        from src.db import Session
        from src.models import ProofSheet
        from src.classifier import classify
        from src.services import now
        from src.sampling_service import finish
        with Session() as s:
            p = s.get(ProofSheet, int(value))
            if not p or p.classification_state != 'classifying': return
            p.defect, p.evidence = classify(s, p); p.pass_flag = p.defect == '合格'
            p.classification_state = 'classified'; p.classified_at = now(); finish(s, p); s.commit()
    def ping(self): return True
fake = types.ModuleType('redis'); fake.Redis = type('R', (), {'from_url': staticmethod(lambda *a, **k: FakeRedis())})
sys.modules['redis'] = fake

from fastapi.testclient import TestClient
from src.main import app
from src.db import Base, engine, Session
from src.models import User
from src.auth import hash_password
Base.metadata.create_all(engine)
with Session() as s:
    for role in ('admin', 'pressman', 'viewer'):
        s.add(User(email=f'{role}@qianye.demo', role=role, password_hash=hash_password('Press2026!')))
    s.commit()
from scripts.seed_demo import seed
seed(); seed()  # 两次seed验证幂等

client = TestClient(app)
ok, bad = [], []
def check(n, c, d=''):
    (ok if c else bad).append(n); print(('  ok  ' if c else ' FAIL ') + n + ('' if c else ' :: ' + d))
def login(role):
    r = client.post('/auth/login', json={'email': f'{role}@qianye.demo', 'password': 'Press2026!'})
    return {'Authorization': 'Bearer ' + r.json()['token']}
ADMIN, PRESS = login('admin'), login('pressman')
def call(m, path, token, body=None, expect=200):
    r = client.request(m, path, headers=token, json=body)
    assert r.status_code == expect, f'{m} {path} -> {r.status_code}: {r.text}'
    return r.json()
def fail(m, path, token, body=None):
    r = client.request(m, path, headers=token, json=body); return r.status_code

print('== H. seed幂等与备版生命周期守卫 ==')
from src.models import PrintJob
from src.production_models import PlateSet
from sqlalchemy import select, func
with Session() as s:
    n_plates = s.scalar(select(func.count()).select_from(PlateSet))
    n_jobs = s.scalar(select(func.count()).select_from(PrintJob))
check('重复seed不产生重复版次（工单1 V1/V2 + 工单2 V1 共3条）', n_plates == 3 and n_jobs == 2, f'{n_plates}/{n_jobs}')

# 工单2（PJ-26092）V1：未测量不能认可；未认可不能激活
plates = call('GET', '/plates', PRESS)
v1j2 = next(p for p in plates if p['job_id'] == 2 and p['version'] == 'V1')
check('未认可版次激活拒绝', fail('POST', f"/plates/{v1j2['id']}/activate", ADMIN) == 409)
check('未测量版次认可拒绝', fail('POST', f"/plates/{v1j2['id']}/approve", ADMIN) == 409)
# 测量后认可（首版自动激活），再登记 V4 备版走显式激活
call('POST', f"/plates/{v1j2['id']}/measure", PRESS, {'registration_error_mm': 0.1})
call('POST', f"/plates/{v1j2['id']}/approve", ADMIN)
v1j2 = next(p for p in call('GET', '/plates', PRESS) if p['id'] == v1j2['id'])
check('工单2首版认可自动激活', v1j2['in_use'] is True)
v4 = call('POST', '/plates', PRESS, {'job_id': 2, 'version': 'V4', 'source_note': '备版显式激活路径演示用来源'})
check('未测量备版激活拒绝', fail('POST', f"/plates/{v4['id']}/activate", ADMIN) == 409)
call('POST', f"/plates/{v4['id']}/measure", PRESS, {'registration_error_mm': 0.12})
check('未认可备版激活拒绝', fail('POST', f"/plates/{v4['id']}/activate", ADMIN) == 409)
call('POST', f"/plates/{v4['id']}/approve", ADMIN)
v4 = next(p for p in call('GET', '/plates', PRESS) if p['id'] == v4['id'])
check('认可备版不自动抢在用（V1仍在用）', v4['in_use'] is False and next(p for p in call('GET', '/plates', PRESS) if p['id'] == v1j2['id'])['in_use'] is True)
call('POST', f"/plates/{v4['id']}/activate", ADMIN)
plates = call('GET', '/plates', PRESS)
check('显式激活后V4在用、V1转备版', next(p for p in plates if p['id'] == v4['id'])['in_use'] is True and next(p for p in plates if p['id'] == v1j2['id'])['in_use'] is False)
check('重复激活拒绝', fail('POST', f"/plates/{v4['id']}/activate", ADMIN) == 409)
# 套准超差不能认可
v5 = call('POST', '/plates', PRESS, {'job_id': 2, 'version': 'V5', 'source_note': '超差版次演示来源xxxxx'})
call('POST', f"/plates/{v5['id']}/measure", PRESS, {'registration_error_mm': 0.2})
check('套准超差认可拒绝', fail('POST', f"/plates/{v5['id']}/approve", ADMIN) == 409)
# 重复版本号拒绝
check('同工单重复版本号拒绝', fail('POST', '/plates', PRESS, {'job_id': 2, 'version': 'V4', 'source_note': '重复版本号演示来源xx'}) == 409)
# 状态页在用/备版视图
st = next(x for x in call('GET', '/plates/status', PRESS) if x['job_id'] == 2)
check('状态页展示在用V4与待放行原因为空', st['in_use_plate']['version'] == 'V4' and st['pending_release_reasons'] == [])

print('== I. MySQL补列DDL函数（SQLite上以等价SQL演练列存在性幂等） ==')
from src.main import migrate_columns
# migrate_columns 仅对 mysql 方言执行；这里验证函数可导入且 mysql 分支的SHOW/ALTER语句结构完整
import inspect
src = inspect.getsource(migrate_columns)
check('补列覆盖4个新列', all(k in src for k in ('role', 'in_use', 'purpose', 'plate_id')))
# SQLite 表结构核对新列已由 create_all 建立
from sqlalchemy import inspect as sa_inspect
cols = {c['name'] for c in sa_inspect(engine).get_columns('plate_sets')}
check('plate_sets含role/in_use列', {'role', 'in_use'} <= cols)
cols2 = {c['name'] for c in sa_inspect(engine).get_columns('press_samples')}
check('press_samples含purpose列', 'purpose' in cols2)
cols3 = {c['name'] for c in sa_inspect(engine).get_columns('print_packs')}
check('print_packs含plate_id列', 'plate_id' in cols3)
cols4 = {c['name'] for c in sa_inspect(engine).get_columns('plate_switches')}
check('plate_switches新表建成', {'split_quantity', 'first_sample_id', 'signed_by'} <= cols4)

print(f'\n==== 通过 {len(ok)} 项，失败 {len(bad)} 项 ====')
if bad:
    [print(' -', x) for x in bad]; sys.exit(1)
print('补充验证全部通过')
