"""本地端到端验证：备用/在用印版分离、换版状态机、按版次封箱额度。
运行：python3 -I test-harness/e2e_switch.py
用 SQLite + 进程内假 Redis + 同步 worker，覆盖用户描述的全部裁决规则。
"""
import os, sys, types, json, tempfile, site
from pathlib import Path

# -I 隔离模式下显式加入受信任的用户依赖目录（脚本目录与 cwd 不自动入路径）
sys.path.insert(0, site.getusersitepackages())
BACKEND = Path('/workspace/backend')
sys.path.insert(0, str(BACKEND))
tmp = Path(tempfile.mkdtemp(prefix='press-e2e-'))
os.environ['DATABASE_URL'] = f'sqlite:///{tmp}/press.db'
os.environ['REDIS_URL'] = 'redis://localhost:6399/0'
os.environ['JWT_SECRET'] = 'test-secret'
os.environ['SEED_DEMO'] = 'false'

# ---- 假 redis：rpush 立即在本进程执行 worker 的判读逻辑 ----
class FakeRedis:
    def __init__(self): self.lists = {}
    def rpush(self, key, value):
        from src.db import Session
        from src.models import ProofSheet
        from src.classifier import classify
        from src.services import now
        from src.sampling_service import finish
        with Session() as s:
            p = s.get(ProofSheet, int(value))
            if not p or p.classification_state != 'classifying':
                return
            try:
                p.defect, p.evidence = classify(s, p)
                p.pass_flag = p.defect == '合格'
                p.classification_state = 'classified'
                p.classified_at = now()
                finish(s, p)
                s.commit()
            except Exception:
                s.rollback()
                p = s.get(ProofSheet, int(value))
                p.classification_state = 'failed'
                p.evidence = {'error': 'fake-worker failure'}
                finish(s, p)
                s.commit()
        self.lists.setdefault(key, []).append(str(value))
    def ping(self): return True

fake_redis = types.ModuleType('redis')
fake_redis.Redis = type('R', (), {'from_url': staticmethod(lambda *a, **k: FakeRedis())})
sys.modules['redis'] = fake_redis

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
seed()

client = TestClient(app)
passed, failed = [], []
def check(name, cond, detail=''):
    (passed if cond else failed).append(name)
    print(('  ok  ' if cond else ' FAIL ') + name + ((' :: ' + detail) if detail and not cond else ''))

def login(role):
    r = client.post('/auth/login', json={'email': f'{role}@qianye.demo', 'password': 'Press2026!'})
    assert r.status_code == 200, r.text
    return {'Authorization': 'Bearer ' + r.json()['token']}

ADMIN, PRESS, VIEWER = login('admin'), login('pressman'), login('viewer')

def call(method, path, token, body=None, expect=200):
    r = client.request(method, path, headers=token, json=body)
    if r.status_code != expect:
        raise AssertionError(f'{method} {path} -> {r.status_code} (want {expect}): {r.text}')
    return r.json()

def may_fail(method, path, token, body=None):
    r = client.request(method, path, headers=token, json=body)
    return r.status_code, r.json().get('detail', '')

GOOD = {'density_c': 1.4, 'density_m': 1.39, 'density_y': 1.41, 'density_k': 1.4, 'dot_gain': 14}
BAD_EMU = {'density_c': 1.02, 'density_m': 1.04, 'density_y': 1.0, 'density_k': 1.05, 'dot_gain': 28}
BAD_COLOR = {'density_c': 1.7, 'density_m': 1.1, 'density_y': 1.55, 'density_k': 1.4, 'dot_gain': 15}

# ================= 场景A：把 V1 做成在用版并进入量产 =================
print('\n== A. 首版认可自动激活、量产闭环 ==')
plates = call('GET', '/plates', PRESS)
v1 = next(p for p in plates if p['version'] == 'V1' and p['job_id'] == 1)
v2 = next(p for p in plates if p['version'] == 'V2' and p['job_id'] == 1)
check('种子V2以备版登记(in_use=False)', v2['in_use'] is False and v2['role'] == 'backup')
call('POST', f"/plates/{v1['id']}/measure", PRESS, {'registration_error_mm': 0.08})
call('POST', f"/plates/{v1['id']}/approve", ADMIN)
v1 = next(p for p in call('GET', '/plates', PRESS) if p['id'] == v1['id'])
check('V1认可后自动激活为在用', v1['in_use'] is True and v1['role'] == 'in_use')

# 工单1调机闭环：首张缺陷 -> 调机 -> 执行 -> 复验合格 -> 签印量产
call('POST', '/proofs', PRESS, {'job_id': 1, 'code': 'PS-T1-FIRST', **BAD_EMU})
proofs = call('GET', '/proofs', PRESS)
first_id = next(p['id'] for p in proofs if p['code'] == 'PS-T1-FIRST')
call('POST', f'/proofs/{first_id}/classify', PRESS)
adj = call('POST', '/adjustments', PRESS, {'proof_id': first_id})
# 今日点检
call('POST', '/paper-issues', PRESS, {'job_id': 1, 'lot_id': 1, 'sheets': 10000})
import datetime
today = datetime.date.today().isoformat()
call('POST', '/press-inspections', PRESS, {'job_id': 1, 'machine_id': 1, 'inspection_date': today, 'lubricated': True, 'guards_checked': True})
insp = call('GET', '/press-inspections', PRESS)[0]
call('POST', f"/press-inspections/{insp['id']}/approve", ADMIN)
call('POST', f"/adjustments/{adj['id']}/execute", PRESS)
call('POST', '/proofs', PRESS, {'job_id': 1, 'code': 'PS-T1-RECHECK', **GOOD})
recheck_id = next(p['id'] for p in call('GET', '/proofs', PRESS) if p['code'] == 'PS-T1-RECHECK')
call('POST', f'/proofs/{recheck_id}/classify', PRESS)
call('POST', f"/adjustments/{adj['id']}/sign", ADMIN, {'reproof_id': recheck_id})
job1 = next(j for j in call('GET', '/jobs', PRESS) if j['id'] == 1)
check('工单1进入production', job1['status'] == 'production')

# V1 常规抽检 2 张（累计 1000 / 2000）
call('POST', '/press-samples', PRESS, {'job_id': 1, 'code': 'PS-RUN-V1-1', 'sequence': 1, 'quantity': 1000, **GOOD})
call('POST', '/press-samples', PRESS, {'job_id': 1, 'code': 'PS-RUN-V1-2', 'sequence': 2, 'quantity': 2000, **GOOD})
samples = call('GET', '/press-samples', PRESS)
check('常规抽检purpose=routine且绑定V1', all(x['purpose'] == 'routine' and x['plate_id'] == v1['id'] for x in samples))

# 封 1200 张（V1 额度=min(合格累计2000, 净领纸10000)=2000）
call('POST', '/print-packs', PRESS, {'job_id': 1, 'code': 'BOX-V1-A', 'sheets': 1200})
pack_a = next(b for b in call('GET', '/print-packs', PRESS) if b['code'] == 'BOX-V1-A')
check('箱登记即绑定V1版次', pack_a['plate_id'] == v1['id'])
call('POST', f"/print-packs/{pack_a['id']}/seal", PRESS)

# ================= 场景B：备版登记不挡旧版量产 =================
print('\n== B. 备用印版与在用印版分离 ==')
call('POST', f"/plates/{v2['id']}/measure", PRESS, {'registration_error_mm': 0.06})
call('POST', f"/plates/{v2['id']}/approve", ADMIN)
v2 = next(p for p in call('GET', '/plates', PRESS) if p['id'] == v2['id'])
check('V2认可后仍是备版，V1仍在用', v2['in_use'] is False and v2['role'] == 'backup')
# 备版期间旧版照常抽检/封箱
call('POST', '/press-samples', PRESS, {'job_id': 1, 'code': 'PS-RUN-V1-3', 'sequence': 3, 'quantity': 2400, **GOOD})
call('POST', '/print-packs', PRESS, {'job_id': 1, 'code': 'BOX-V1-B', 'sheets': 500})
pack_b = next(b for b in call('GET', '/print-packs', PRESS) if b['code'] == 'BOX-V1-B')
call('POST', f"/print-packs/{pack_b['id']}/seal", PRESS)
check('备版期间旧版封箱成功（已封1700<=2400）', True)
sc, msg = may_fail('POST', f"/plates/{v2['id']}/activate", PRESS)
check('机长无权激活在用版(403)', sc == 403)

# ================= 场景C：换版全流程成功 =================
print('\n== C. 换版：分界、首张、签印、恢复放行 ==')
sw = call('POST', '/plate-switches', PRESS, {'job_id': 1, 'new_plate_id': v2['id']})
check('换版单记录累计印量分界=2400', sw['split_quantity'] == 2400, str(sw))
check('换版单初始switching且旧版仍在用', sw['state'] == 'switching')
v1_now = next(p for p in call('GET', '/plates', PRESS) if p['id'] == v1['id'])
check('换版中旧版保留未卸除', v1_now['in_use'] is True)
# 换版期间常规抽检/封箱/发货被拒
sc, _ = may_fail('POST', '/press-samples', PRESS, {'job_id': 1, 'code': 'PS-X', 'sequence': 4, 'quantity': 2500, **GOOD})
check('换版中常规抽检拒绝', sc == 409)
sc, _ = may_fail('POST', '/print-packs', PRESS, {'job_id': 1, 'code': 'BOX-X', 'sheets': 10})
check('换版中封箱拒绝（暂停放行）', sc == 409)
# 重试不重复换版
sc, _ = may_fail('POST', '/plate-switches', PRESS, {'job_id': 1, 'new_plate_id': v2['id']})
check('重复发起换版拒绝', sc == 409)
# 待放行原因可查
st = next(x for x in call('GET', '/plates/status', PRESS) if x['job_id'] == 1)
check('待放行原因列出换版单', st['active_switch_id'] == sw['id'] and any('换版' in r for r in st['pending_release_reasons']), json.dumps(st, ensure_ascii=False))
# 新版首张沿用训练判别（合格）
fa = call('POST', f"/plate-switches/{sw['id']}/first-article", PRESS, {'job_id': 1, 'code': 'PS-V2-FIRST', 'quantity': 2500, **GOOD})
check('首张purpose=first_article且绑定V2', fa['purpose'] == 'first_article' and fa['plate_id'] == v2['id'])
sc, _ = may_fail('POST', f"/plate-switches/{sw['id']}/first-article", PRESS, {'job_id': 1, 'code': 'PS-V2-FIRST-2', 'quantity': 2501, **GOOD})
check('首张重复登记拒绝', sc == 409)
fa_rows = [x for x in call('GET', '/press-samples', PRESS) if x['purpose'] == 'first_article']
check('首张不作为封箱额度依据（gate只取routine）', len(fa_rows) == 1 and fa_rows[0]['state'] == 'passed')
# 机长不能签印
sc, _ = may_fail('POST', f"/plate-switches/{sw['id']}/sign", PRESS)
check('机长无权签印新版(403)', sc == 403)
sw = call('POST', f"/plate-switches/{sw['id']}/sign", ADMIN)
check('签印后换版succeeded', sw['state'] == 'succeeded' and bool(sw['signed_by']))
plates = call('GET', '/plates', PRESS)
v1_now = next(p for p in plates if p['id'] == v1['id'])
v2_now = next(p for p in plates if p['id'] == v2['id'])
check('成功后V2转在用、V1转备版', v2_now['in_use'] is True and v1_now['in_use'] is False)
# 终结后不能重复签印
sc, _ = may_fail('POST', f"/plate-switches/{sw['id']}/sign", ADMIN)
check('终结换版单重复签印拒绝', sc == 409)

# ================= 场景D：按版次分别核算封箱额度 =================
print('\n== D. 旧版剩余额度不转给新版；历史箱证据保留 ==')
# V2 首张不算额度：无V2 routine合格抽检前不能封箱
sc, msg = may_fail('POST', '/print-packs', PRESS, {'job_id': 1, 'code': 'BOX-V2-X', 'sheets': 10})
check('新版无自己的合格抽检前拒绝封箱（旧版合格张不作依据）', sc == 409, msg)
call('POST', '/press-samples', PRESS, {'job_id': 1, 'code': 'PS-RUN-V2-1', 'sequence': 5, 'quantity': 3000, **GOOD})
# V2 额度 = min(3000, 净领纸10000) = 3000；与 V1 已封 1700 完全独立
call('POST', '/print-packs', PRESS, {'job_id': 1, 'code': 'BOX-V2-A', 'sheets': 2800})
pa = next(b for b in call('GET', '/print-packs', PRESS) if b['code'] == 'BOX-V2-A')
call('POST', f"/print-packs/{pa['id']}/seal", PRESS)
check('V2独立额度可封2800（未受V1已封1700影响）', pa['plate_id'] == v2['id'])
sc, msg = may_fail('POST', '/print-packs', PRESS, {'job_id': 1, 'code': 'BOX-V2-B', 'sheets': 300})
check('V2剩余额度仅200，超封拒绝（旧版余额不转入）', sc == 409, msg)
call('POST', '/print-packs', PRESS, {'job_id': 1, 'code': 'BOX-V2-B', 'sheets': 200})
pb = next(b for b in call('GET', '/print-packs', PRESS) if b['code'] == 'BOX-V2-B')
call('POST', f"/print-packs/{pb['id']}/seal", PRESS)
# 历史箱：V1 的两个箱仍 sealed，快照保留 V1 依据
boxes = call('GET', '/print-packs', PRESS)
v1_boxes = [b for b in boxes if b['plate_id'] == v1['id'] and b['state'] == 'sealed']
check('V1历史箱证据保留且快照含V1版次', len(v1_boxes) == 2 and all(b['sample_snapshot']['plate']['version'] == 'V1' for b in v1_boxes))

# ================= 场景E：换版失败回退旧版 =================
print('\n== E. 换版失败：缺陷留在调机，保留旧版 ==')
v3 = call('POST', '/plates', PRESS, {'job_id': 1, 'version': 'V3', 'source_note': '第三次制版备用，客户再改版'})
call('POST', f"/plates/{v3['id']}/measure", PRESS, {'registration_error_mm': 0.05})
call('POST', f"/plates/{v3['id']}/approve", ADMIN)
sw2 = call('POST', '/plate-switches', PRESS, {'job_id': 1, 'new_plate_id': v3['id']})
# 首张缺陷（乳化）：判为不合格，不触发量产停机，状态held
call('POST', f"/plate-switches/{sw2['id']}/first-article", PRESS, {'job_id': 1, 'code': 'PS-V3-FIRST-BAD', 'quantity': 3100, **BAD_EMU})
fa2 = next(x for x in call('GET', '/press-samples', PRESS) if x['proof_id'] == next(p['id'] for p in call('GET', '/proofs', PRESS) if p['code'] == 'PS-V3-FIRST-BAD'))
check('缺陷首张held且工单未被置hold', fa2['state'] == 'held' and next(j for j in call('GET', '/jobs', PRESS) if j['id'] == 1)['status'] == 'production')
sc, _ = may_fail('POST', f"/plate-switches/{sw2['id']}/sign", ADMIN)
check('不合格首张签印拒绝', sc == 409)
sw2 = call('POST', f"/plate-switches/{sw2['id']}/fail", PRESS, {'note': '新版乳化缺陷留在调机重修，保留V2在用'})
check('换版单failed', sw2['state'] == 'failed')
plates = call('GET', '/plates', PRESS)
check('失败后V2仍在用、V3留备版', next(p for p in plates if p['id'] == v2['id'])['in_use'] is True and next(p for p in plates if p['id'] == v3['id'])['in_use'] is False)
# 回退后立即恢复放行：旧版可继续抽检
call('POST', '/press-samples', PRESS, {'job_id': 1, 'code': 'PS-RUN-V2-2', 'sequence': 7, 'quantity': 3200, **GOOD})
check('回退后旧版恢复量产抽检', True)
# failed 是历史证据，不挡新换版；重试同一换版单仍拒绝
sc, _ = may_fail('POST', f"/plate-switches/{sw2['id']}/fail", PRESS, {'note': '再次回退应被拒绝xxxx'})
check('已终结换版单不能重复处置', sc == 409)
sw3 = call('POST', '/plate-switches', PRESS, {'job_id': 1, 'new_plate_id': v3['id']})
call('POST', f"/plate-switches/{sw3['id']}/fail", PRESS, {'note': '再次验证失败回退路径可用xxxxxx'})
check('失败后可重新发起换版（历史单不拦截）', True)

# ================= 场景F：并发裁决与迟回判读 =================
print('\n== F. 并发：按所属版次裁决、迟回判读不误放行、viewer只读 ==')
# 换版并发：open 箱在换版期间暂停封箱，回退后恢复并按V2版次额度裁决
# V2 当前已封3000、额度3200（最后合格累计），剩余恰好200
call('POST', '/print-packs', PRESS, {'job_id': 1, 'code': 'BOX-V2-OPEN', 'sheets': 200})
sw4 = call('POST', '/plate-switches', PRESS, {'job_id': 1, 'new_plate_id': v3['id']})
open_box = next(b for b in call('GET', '/print-packs', PRESS) if b['code'] == 'BOX-V2-OPEN')
sc, _ = may_fail('POST', f"/print-packs/{open_box['id']}/seal", PRESS)
check('换版并发时封箱暂停（按箱版次也不放行）', sc == 409)
call('POST', f"/plate-switches/{sw4['id']}/fail", PRESS, {'note': '并发裁决演示后回退恢复V2生产xx'})
# 回退后放行恢复：该箱200张恰好打满V2额度3200
call('POST', f"/print-packs/{open_box['id']}/seal", PRESS)
check('换版结束后封箱放行恢复，V2版次累计封至3200', next(b for b in call('GET', '/print-packs', PRESS) if b['id'] == open_box['id'])['state'] == 'sealed')
# V2 额度用尽：新箱登记即拒（旧版剩余额度不转给新版的同一约束在版次内同样封顶）
sc, msg = may_fail('POST', '/print-packs', PRESS, {'job_id': 1, 'code': 'BOX-V2-FULL', 'sheets': 1})
check('V2版次额度用尽后再封拒绝', sc == 409, msg)
# 待判读拦截：构造一张 classifying 色标与对应样张（模拟worker迟回），验证 gate 不放行
from src.db import Session as Sess
from src.models import ProofSheet
from src.production_models import PressSample
import sqlalchemy as sa
with Sess() as s:
    proof = s.scalars(sa.select(ProofSheet).where(ProofSheet.code == 'PS-RUN-V2-2')).one()
    item = s.scalars(sa.select(PressSample).where(PressSample.proof_id == proof.id)).one()
    proof.classification_state = 'classifying'; item.state = 'classifying'
    s.commit()
st = next(x for x in call('GET', '/plates/status', PRESS) if x['job_id'] == 1)
check('迟回判读列入待放行原因', any('判读' in r for r in st['pending_release_reasons']), json.dumps(st, ensure_ascii=False))
sc, _ = may_fail('POST', '/print-packs', PRESS, {'job_id': 1, 'code': 'BOX-WAIT', 'sheets': 1})
check('待判读期间不误放行封箱', sc == 409)
with Sess() as s:
    proof = s.scalars(sa.select(ProofSheet).where(ProofSheet.code == 'PS-RUN-V2-2')).one()
    item = s.scalars(sa.select(PressSample).where(PressSample.proof_id == proof.id)).one()
    proof.classification_state = 'classified'; proof.pass_flag = True; proof.defect = '合格'
    item.state = 'passed'
    s.commit()
# viewer 全只读
for method, path, body in [('POST', '/plates', {'job_id': 1, 'version': 'VX', 'source_note': '观察员尝试登记xxxx'}),
                           ('POST', f"/plates/{v1['id']}/approve", {}),
                           ('POST', '/plate-switches', {'job_id': 1, 'new_plate_id': v3['id']})]:
    sc, _ = may_fail(method, path, VIEWER, body)
    check(f'viewer被拒 {method} {path}', sc == 403)

# ================= 场景G：停机与换版交叉 =================
print('\n== G. 活动停机单与换版互斥；复机按在用版裁决 ==')
# 先确认无活动换版，造一张缺陷常规抽检触发停机
call('POST', '/press-samples', PRESS, {'job_id': 1, 'code': 'PS-RUN-BAD', 'sequence': 8, 'quantity': 3300, **BAD_COLOR})
holds = call('GET', '/press-holds', PRESS)
active_hold = next(h for h in holds if h['state'] == 'active')
sc, _ = may_fail('POST', '/plate-switches', PRESS, {'job_id': 1, 'new_plate_id': v3['id']})
check('活动停机时换版拒绝', sc == 409)
# 停机复验合格 + 主管复机
call('POST', f"/press-holds/{active_hold['id']}/recheck", PRESS, {'job_id': 1, 'code': 'PS-HOLD-GOOD', **GOOD})
call('POST', f"/press-holds/{active_hold['id']}/close", ADMIN, {'resolution': '核对供墨后新复验合格，恢复V2量产'})
check('复机后无活动停机', not any(h['state'] == 'active' for h in call('GET', '/press-holds', PRESS)))
job1 = next(j for j in call('GET', '/jobs', PRESS) if j['id'] == 1)
check('工单恢复production且在用版仍为V2', job1['status'] == 'production' and next(p for p in call('GET', '/plates', PRESS) if p['id'] == v2['id'])['in_use'])

print(f'\n==== 通过 {len(passed)} 项，失败 {len(failed)} 项 ====')
if failed:
    print('失败项：'); [print(' -', x) for x in failed]
    sys.exit(1)
print('全部规则验证通过')
