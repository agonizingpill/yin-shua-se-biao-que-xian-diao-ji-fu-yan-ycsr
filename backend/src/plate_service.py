from sqlalchemy import select,func
from fastapi import HTTPException
from .models import PrintJob,ProofSheet
from .production_models import PlateSet,PlateSwitch,PressSample,PressHold
from .services import get,audit,now

SWITCH_STATES=('switching','failed','succeeded')

def active_switch(s,job_id):
    """未终结的换版单（switching）。failed/succeeded 为历史证据，不拦截新换版。"""
    return s.scalar(select(PlateSwitch).where(PlateSwitch.job_id==job_id,PlateSwitch.state=='switching').with_for_update())

def in_use_plate(s,job_id):
    return s.scalar(select(PlateSet).where(PlateSet.job_id==job_id,PlateSet.in_use==True).with_for_update())

def latest_plate(s,job_id):
    return s.scalar(select(PlateSet).where(PlateSet.job_id==job_id).order_by(PlateSet.id.desc()).with_for_update())

def require_ready(s,job_id):
    """调机执行/签印守卫：优先在用版；尚无在用版时取最新登记版次，须已主管认可。
    备版登记不影响在用版，也不阻断在用版期间的既有流程。"""
    plate=in_use_plate(s,job_id) or latest_plate(s,job_id)
    if not plate or plate.state!='ready' or plate.registration_error_mm is None or plate.registration_error_mm>.15:raise HTTPException(409,'当前印版版本尚未主管认可就绪，不能执行调机或量产')
    return plate

def require_in_use(s,job_id):
    """量产抽检/封箱只认在用版；备版登记不挡旧版生产。"""
    plate=in_use_plate(s,job_id)
    if not plate:raise HTTPException(409,'该工单尚无在用印版：先由主管将已认可版次激活在用')
    if plate.state!='ready' or plate.registration_error_mm is None or plate.registration_error_mm>.15:raise HTTPException(409,'在用印版未保持就绪状态，不能量产')
    return plate

def no_switching(s,job_id):
    sw=active_switch(s,job_id)
    if sw:raise HTTPException(409,f'换版单#{sw.id}进行中：已暂停放行，待新版首张判读与主管签印（或回退旧版）')

def hold_reason(s,job_id):
    reasons=[]
    if s.scalar(select(PressHold.id).where(PressHold.job_id==job_id,PressHold.state=='active')):reasons.append('存在活动停机单，须复验合格并主管复机')
    if s.scalar(select(PressSample.id).where(PressSample.job_id==job_id,PressSample.state=='classifying')):reasons.append('有色标判读尚未返回，迟回判读期间不得放行')
    sw=active_switch(s,job_id)
    if sw:
        plate=s.get(PlateSet,sw.new_plate_id)
        reasons.append(f'换版单#{sw.id}进行中（{plate.version}首张待判读/签印），暂停放行')
    return reasons

def create(s,p,u):
    if not s.scalar(select(PrintJob).where(PrintJob.id==p.job_id).with_for_update()):raise HTTPException(404,'工单不存在')
    if s.scalar(select(PlateSet).where(PlateSet.job_id==p.job_id,PlateSet.version==p.version)):raise HTTPException(409,'该工单印版版本已存在')
    # 新版始终先作备版登记；备版期间旧在用版照常生产
    plate=PlateSet(**p.model_dump(),role='backup',in_use=False);s.add(plate);audit(s,u,f'登记备用印版 {p.version} 工单{p.job_id}（不影响在用版生产）');s.commit();return plate

def measure(s,key,p,u):
    plate=s.scalar(select(PlateSet).where(PlateSet.id==key).with_for_update())
    if not plate:raise HTTPException(404,'印版不存在')
    if plate.state=='ready':raise HTTPException(409,'就绪印版套准证据已冻结，请建立新版本')
    plate.registration_error_mm=p.registration_error_mm;plate.state='measured'
    audit(s,u,f'印版套准测量 {plate.version} {p.registration_error_mm}mm（备版）');s.commit();return plate

def approve(s,key,u):
    plate=s.scalar(select(PlateSet).where(PlateSet.id==key).with_for_update())
    if not plate:raise HTTPException(404,'印版不存在')
    if plate.state!='measured' or plate.registration_error_mm is None:raise HTTPException(409,'须先测量套准，且不能重复认可')
    if plate.registration_error_mm>.15:raise HTTPException(409,'套准误差超过0.15mm，不能认可就绪')
    plate.state='ready';plate.approved_by=u.email
    # 认可备版不抢在用版：无在用版时（首版）自动激活，工单尚无历史量产
    if not in_use_plate(s,plate.job_id):
        plate.role='in_use';plate.in_use=True;audit(s,u,f'主管认可印版 {plate.version} 就绪并激活为首版在用印版')
    else:audit(s,u,f'主管认可备用印版 {plate.version} 就绪（在用版不变，量产照常）')
    s.commit();return plate

def activate(s,key,u):
    """主管显式激活已认可备版为在用版（无换版流程的首版/备版启用）。"""
    plate=s.scalar(select(PlateSet).where(PlateSet.id==key).with_for_update())
    if not plate:raise HTTPException(404,'印版不存在')
    if plate.state!='ready':raise HTTPException(409,'仅主管认可就绪的印版可激活在用')
    current=in_use_plate(s,plate.job_id)
    if current and current.id==plate.id:raise HTTPException(409,'该版次已是在用版，无需重复激活')
    if active_switch(s,plate.job_id):raise HTTPException(409,'换版进行中，不能绕过换版流程直接激活')
    if current:current.role='backup';current.in_use=False
    plate.role='in_use';plate.in_use=True
    audit(s,u,f'主管激活印版 {plate.version} 为在用版'+(f'，原在用版 {current.version} 转备版' if current else ''));s.commit();return plate

def start_switch(s,p,u):
    job=s.scalar(select(PrintJob).where(PrintJob.id==p.job_id).with_for_update())
    if not job:raise HTTPException(404,'工单不存在')
    if job.status!='production':raise HTTPException(409,'仅已签印量产工单可以换版')
    if s.scalar(select(PressHold.id).where(PressHold.job_id==job.id,PressHold.state=='active')):raise HTTPException(409,'存在活动停机单，须先复机再换版')
    if active_switch(s,job.id):raise HTTPException(409,'已有进行中的换版单，重试不重复换版')
    old=in_use_plate(s,job.id)
    if not old:raise HTTPException(409,'该工单无在用印版，不能换版')
    new=s.scalar(select(PlateSet).where(PlateSet.id==p.new_plate_id).with_for_update())
    if not new or new.job_id!=job.id:raise HTTPException(404,'新版印版不存在或不属于该工单')
    if new.id==old.id:raise HTTPException(409,'新印版不能与在用版相同')
    if new.state!='ready':raise HTTPException(409,'备用新版须先完成套准测量与主管认可，才能换版')
    if s.scalar(select(PressSample.id).where(PressSample.job_id==job.id,PressSample.state=='classifying')):raise HTTPException(409,'尚有抽检色标判读未返回，迟回判读期间不能换版')
    # 累计印量分界：换版前历史最高累计读数
    split=int(s.scalar(select(func.coalesce(func.max(PressSample.quantity),0)).where(PressSample.job_id==job.id,PressSample.purpose=='routine')))
    sw=PlateSwitch(job_id=job.id,old_plate_id=old.id,new_plate_id=new.id,state='switching',split_quantity=split,created_at=now());s.add(sw);s.flush()
    # 旧版保留：不立即卸除；待首张签印成功才交接，失败可回退
    audit(s,u,f'发起换版#{sw.id} {old.version}→{new.version}，累计印量分界{split}张，暂停放行');s.commit();return sw

def fail_switch(s,key,p,u):
    sw=s.scalar(select(PlateSwitch).where(PlateSwitch.id==key).with_for_update())
    if not sw:raise HTTPException(404,'换版单不存在')
    if sw.state!='switching':raise HTTPException(409,'换版单已终结，不能重复处置')
    if sw.first_sample_id:
        item=s.get(PressSample,sw.first_sample_id)
        proof=s.get(ProofSheet,item.proof_id)
        if proof.classification_state=='classifying':raise HTTPException(409,'首张判读尚未返回，迟回判读期间不能回退，请等待结果')
    old=s.scalar(select(PlateSet).where(PlateSet.id==sw.old_plate_id).with_for_update())
    # 切换失败保留旧版：在用版不变，新版仍为备版
    old.role='in_use';old.in_use=True
    new=s.get(PlateSet,sw.new_plate_id);new.role='backup';new.in_use=False
    sw.state='failed';sw.note=p.note.strip();sw.closed_at=now()
    audit(s,u,f'换版#{sw.id}失败回退，保留旧在用版 {old.version}，新版 {new.version} 留备版调机');s.commit();return sw

def status(s):
    rows=[]
    for j in s.scalars(select(PrintJob).order_by(PrintJob.id)).all():
        current=in_use_plate(s,j.id);latest=latest_plate(s,j.id);sw=active_switch(s,j.id)
        rows.append({'job_id':j.id,'job_code':j.code,'job_title':j.title,'job_status':j.status,
                     'in_use_plate':current and {'id':current.id,'version':current.version,'state':current.state,'approved_by':current.approved_by},
                     'latest_plate':latest and {'id':latest.id,'version':latest.version,'state':latest.state,'role':latest.role},
                     'active_switch_id':sw and sw.id,
                     'pending_release_reasons':hold_reason(s,j.id)})
    return rows
