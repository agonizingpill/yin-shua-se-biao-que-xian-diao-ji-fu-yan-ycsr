from sqlalchemy import select,func
from fastapi import HTTPException
from .models import PrintJob,ProofSheet
from .production_models import PressSample,PressHold,PlateSwitch,PlateSet
from .services import get,audit,now
from .plate_service import require_in_use,active_switch,no_switching

def queue(s,proof,r,key='press-classify'):
    # Save before enqueue so the worker can see the new proof. A failed queue write
    # is converted into a persistent stop record instead of allowing silent production.
    s.commit()
    try:r.rpush(key,proof.id)
    except Exception:
        proof.classification_state='failed';proof.evidence={'error':'判读队列暂不可用'};finish(s,proof);s.commit();raise HTTPException(503,'判读队列暂不可用，已保留待处置记录')

def _snapshot(plate):return {'version':plate.version,'registration_error_mm':plate.registration_error_mm,'source_note':plate.source_note,'approved_by':plate.approved_by,'plate_id':plate.id}

def sample(s,p,u,r):
    job=s.scalar(select(PrintJob).where(PrintJob.id==p.job_id).with_for_update())
    if not job:raise HTTPException(404,'工单不存在')
    if job.status!='production' or s.scalar(select(PressHold).where(PressHold.job_id==job.id,PressHold.state=='active')):raise HTTPException(409,'仅无停机单的量产工单可以抽检，须先复验并主管复机')
    # 换版期间暂停放行：常规抽检只认在用版，备版/换版中拒绝
    no_switching(s,job.id)
    plate=require_in_use(s,job.id)
    from .inspection_service import ready
    from .paper_service import sample_guard
    ready(s,job.id);sample_guard(s,job.id,p.quantity)
    if s.scalar(select(PressSample).where(PressSample.job_id==job.id,PressSample.state=='classifying')):raise HTTPException(409,'上一张量产抽检尚在判读，请等待结果')
    last=s.scalar(select(func.max(PressSample.sequence)).where(PressSample.job_id==job.id)) or 0
    if p.sequence<=last:raise HTTPException(409,'抽检序号须递增，不得重复或倒序')
    if s.scalar(select(ProofSheet).where(ProofSheet.code==p.code)):raise HTTPException(409,'量产抽检必须使用新的唯一色标编号')
    values=p.model_dump();seq=values.pop('sequence');qty=values.pop('quantity')
    proof=ProofSheet(**values,classification_state='classifying');s.add(proof);s.flush()
    item=PressSample(job_id=job.id,proof_id=proof.id,plate_id=plate.id,sequence=seq,quantity=qty,purpose='routine',plate_snapshot=_snapshot(plate));s.add(item)
    audit(s,u,f'量产抽检 工单{job.code} 序{seq} 已印{qty}张 在用版{plate.version} 色标{proof.code}');queue(s,proof,r);return item

def first_article(s,p,u,r):
    """换版后新版首张：沿用同一SVM训练判别；缺陷留在调机，不作量产放行依据。"""
    job=s.scalar(select(PrintJob).where(PrintJob.id==p.job_id).with_for_update())
    if not job:raise HTTPException(404,'工单不存在')
    sw=active_switch(s,job.id)
    if not sw:raise HTTPException(409,'没有进行中的换版单，不能登记换版首张')
    if sw.first_sample_id:raise HTTPException(409,'换版首张已登记，等待判读与主管签印；重试不重复换版')
    new=s.scalar(select(PlateSet).where(PlateSet.id==sw.new_plate_id).with_for_update())
    if new.state!='ready':raise HTTPException(409,'新版印版未保持主管认可就绪')
    from .inspection_service import ready
    from .paper_service import sample_guard
    ready(s,job.id);sample_guard(s,job.id,p.quantity)
    if s.scalar(select(ProofSheet).where(ProofSheet.code==p.code)):raise HTTPException(409,'换版首张必须使用新的唯一色标编号')
    last=s.scalar(select(func.max(PressSample.sequence)).where(PressSample.job_id==job.id)) or 0
    values=p.model_dump();qty=values.pop('quantity')
    proof=ProofSheet(**values,classification_state='classifying');s.add(proof);s.flush()
    item=PressSample(job_id=job.id,proof_id=proof.id,plate_id=new.id,sequence=last+1,quantity=qty,purpose='first_article',plate_snapshot=_snapshot(new));s.add(item);s.flush()
    sw.first_sample_id=item.id
    audit(s,u,f'换版#{sw.id}新版{new.version}首张 已印{qty}张 色标{proof.code}（沿用训练判别）');queue(s,proof,r,key='press-first-article');return item

def finish(s,proof):
    item=s.scalar(select(PressSample).where(PressSample.proof_id==proof.id).with_for_update())
    if not item or item.state!='classifying':return
    ok=proof.classification_state=='classified' and proof.pass_flag and proof.defect=='合格'
    if item.purpose=='first_article':
        # 首张不触发量产停机/封箱额度；结果只决定能否进入主管签印
        item.state='passed' if ok else 'held'
        return
    job=s.scalar(select(PrintJob).where(PrintJob.id==item.job_id).with_for_update())
    if ok:item.state='passed';return
    item.state='held';job.status='hold'
    hold=PressHold(job_id=job.id,sample_id=item.id,reason='量产抽检 '+proof.code+' / '+(proof.defect or '判读失败'),created_at=now(),reproof_ids=[]);s.add(hold)

def sign_first_article(s,key,u):
    """主管对新版首张签印：合格才交接在用版并恢复放行；失败走fail_switch回退旧版。"""
    sw=s.scalar(select(PlateSwitch).where(PlateSwitch.id==key).with_for_update())
    if not sw:raise HTTPException(404,'换版单不存在')
    if sw.state!='switching':raise HTTPException(409,'换版单已终结，不能重复签印')
    if not sw.first_sample_id:raise HTTPException(409,'须先登记新版首张并等待判读')
    item=s.get(PressSample,sw.first_sample_id);proof=get(s,ProofSheet,item.proof_id)
    if proof.classification_state=='classifying':raise HTTPException(409,'首张判读尚未返回，迟回判读不误放行，请等待结果')
    if proof.classification_state!='classified' or not proof.pass_flag or proof.defect!='合格':raise HTTPException(409,'新版首张未判为合格：缺陷留在调机，请登记换版失败并保留旧版')
    old=s.scalar(select(PlateSet).where(PlateSet.id==sw.old_plate_id).with_for_update())
    new=s.scalar(select(PlateSet).where(PlateSet.id==sw.new_plate_id).with_for_update())
    # 成功后才交接：旧版转备版、新版转在用；旧版合格张不作为新版依据
    old.role='backup';old.in_use=False;new.role='in_use';new.in_use=True
    sw.state='succeeded';sw.signed_by=u.email;sw.signed_at=now();sw.closed_at=now()
    audit(s,u,f'换版#{sw.id}首张{proof.code}合格，主管签印：{old.version}转备版，{new.version}转在用，恢复放行');s.commit();return sw

def recheck(s,key,p,u,r):
    current=get(s,PressHold,key)
    job=s.scalar(select(PrintJob).where(PrintJob.id==current.job_id).with_for_update())
    hold=s.scalar(select(PressHold).where(PressHold.id==key).with_for_update())
    if hold.state!='active' or job.status!='hold':raise HTTPException(409,'仅活动停机单可以登记复机复验')
    if p.job_id!=job.id:raise HTTPException(409,'复机色标必须来自停机单同一工单')
    if hold.reproof_id and get(s,ProofSheet,hold.reproof_id).classification_state=='classifying':raise HTTPException(409,'上一张复验仍在判读，请等待')
    if s.scalar(select(ProofSheet).where(ProofSheet.code==p.code)):raise HTTPException(409,'停机后须重新取样，不能复用已有色标')
    proof=ProofSheet(**p.model_dump(),classification_state='classifying');s.add(proof);s.flush();hold.reproof_id=proof.id;hold.reproof_ids=[*(hold.reproof_ids or []),proof.id]
    audit(s,u,f'停机复验 {key} 新色标{p.code}');queue(s,proof,r);return hold

def close(s,key,p,u):
    current=get(s,PressHold,key);job=s.scalar(select(PrintJob).where(PrintJob.id==current.job_id).with_for_update());hold=s.scalar(select(PressHold).where(PressHold.id==key).with_for_update())
    if hold.state!='active' or job.status!='hold':raise HTTPException(409,'停机单已关闭，不能重复复机')
    # 复机按所属版次裁决：换版期间不允许绕过首张流程复机
    no_switching(s,job.id)
    require_in_use(s,job.id)
    if not hold.reproof_id:raise HTTPException(409,'须登记停机后的新色标复验')
    proof=get(s,ProofSheet,hold.reproof_id)
    if proof.job_id!=job.id or proof.classification_state!='classified' or not proof.pass_flag or proof.defect!='合格' or not proof.classified_at or proof.classified_at<hold.created_at:raise HTTPException(409,'停机后新色标未判为合格，不能复机')
    hold.state='closed';hold.resolution=p.resolution;hold.closed_by=u.email;hold.closed_at=now();job.status='production'
    audit(s,u,f'主管复机 停机单{key} 依据{p.resolution}');s.commit();return hold
