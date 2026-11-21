from sqlalchemy import select,func
from fastapi import HTTPException
from .models import PrintJob,ProofSheet
from .production_models import PressSample,PressHold,PlateSet
from .dispatch_models import PrintPack,PrintShipment,ShipmentPack
from .paper_service import locked,net_paper
from .plate_service import active_switch,in_use_plate
from .services import audit

def gate(s,j):
    if j.status!='production' or s.scalar(select(PressHold.id).where(PressHold.job_id==j.id,PressHold.state=='active')):raise HTTPException(409,'须无活动停机单的量产工单')
    sw=active_switch(s,j.id)
    if sw:raise HTTPException(409,f'换版单#{sw.id}进行中，暂停封箱与发货放行')
    if s.scalar(select(PressSample.id).where(PressSample.job_id==j.id,PressSample.state=='classifying')):raise HTTPException(409,'抽检待判读，不能封箱或发货')
    plate=in_use_plate(s,j.id)
    if not plate:raise HTTPException(409,'该工单无在用印版，不能封箱')
    sample=s.scalar(select(PressSample).where(PressSample.job_id==j.id,PressSample.state=='passed',PressSample.plate_id==plate.id,PressSample.purpose=='routine').order_by(PressSample.sequence.desc()))
    if not sample:raise HTTPException(409,f'在用版{plate.version}尚无合格量产抽检，不能封箱；旧版合格张不作新版依据')
    proof=s.get(ProofSheet,sample.proof_id)
    return {'sample_id':sample.id,'sequence':sample.sequence,'quantity':sample.quantity,'proof_code':proof.code,'plate':sample.plate_snapshot,'net_paper':net_paper(s,j.id),'plate_id':plate.id}

def sealed_for_plate(s,jid,plate_id):
    """按所属版次核算已封/已发张数；旧版剩余额度不转给新版。"""
    return int(s.scalar(select(func.coalesce(func.sum(PrintPack.sheets),0)).where(PrintPack.job_id==jid,PrintPack.plate_id==plate_id,PrintPack.state.in_(['sealed','shipped']))))

def sealed(s,jid):return int(s.scalar(select(func.coalesce(func.sum(PrintPack.sheets),0)).where(PrintPack.job_id==jid,PrintPack.state.in_(['sealed','shipped']))))

def create_pack(s,b,u):
    j=locked(s,PrintJob,b.job_id);e=gate(s,j)
    # 登记即绑定当前在用版；换版后新箱按新版额度核算
    used=sealed_for_plate(s,j.id,e['plate_id'])
    cap=min(e['quantity'],e['net_paper'])
    if used>=cap:raise HTTPException(409,f"在用版封箱额度已用尽（本版合格累计/净领纸上限{cap}张，已封{used}张），旧版剩余额度不转新版")
    if b.sheets>cap-used:raise HTTPException(409,f"本箱张数超在用版剩余额度：上限{cap}张，本版已封{used}张，本次最多{cap-used}张")
    row=PrintPack(**b.model_dump(),plate_id=e['plate_id']);s.add(row);audit(s,u,f'登记待封箱 {b.code} {b.sheets}张 版次{e["plate"]["version"]}');s.commit();return row

def pack_action(s,key,u,action):
    ref=s.get(PrintPack,key)
    if not ref:raise HTTPException(404,'箱码不存在')
    j=locked(s,PrintJob,ref.job_id);row=locked(s,PrintPack,key)
    if row.state!='open':raise HTTPException(409,'仅未封箱可封签或取消；封签后数量冻结')
    if action=='cancel':row.state='cancelled'
    else:
        # 封箱按箱所属版次裁决：换版并发时旧版箱只认旧版抽检与额度
        if j.status!='production' or s.scalar(select(PressHold.id).where(PressHold.job_id==j.id,PressHold.state=='active')):raise HTTPException(409,'须无活动停机单的量产工单')
        if active_switch(s,j.id):raise HTTPException(409,'换版进行中，暂停封箱放行；待新版签印或回退旧版后处理')
        plate=row.plate_id and s.get(PlateSet,row.plate_id)
        if not plate:raise HTTPException(409,'该箱未绑定印版版次，不能封箱（历史遗留箱请重新登记）')
        sample=s.scalar(select(PressSample).where(PressSample.job_id==j.id,PressSample.state=='passed',PressSample.plate_id==plate.id,PressSample.purpose=='routine').order_by(PressSample.sequence.desc()))
        if not sample:raise HTTPException(409,f'箱属版次{plate.version}无合格量产抽检依据，不能封箱')
        proof=s.get(ProofSheet,sample.proof_id);cap=min(sample.quantity,net_paper(s,j.id))
        if sealed_for_plate(s,j.id,plate.id)+row.sheets>cap:raise HTTPException(409,f'版次{plate.version}封箱累计张数超该版合格抽检或净领纸；旧版剩余额度不转新版')
        row.state='sealed';row.sample_snapshot={'sample_id':sample.id,'sequence':sample.sequence,'quantity':sample.quantity,'proof_code':proof.code,'plate':sample.plate_snapshot,'net_paper':net_paper(s,j.id),'plate_id':plate.id}
    audit(s,u,f'成品箱{key} {action}');s.commit();return row

def create_shipment(s,b,u):
    j=locked(s,PrintJob,b.job_id);gate(s,j)
    if len(set(b.pack_ids))!=len(b.pack_ids):raise HTTPException(409,'箱单包含重复箱码')
    boxes=[locked(s,PrintPack,k) for k in sorted(b.pack_ids)]
    for box in boxes:
        if box.job_id!=j.id or box.state!='sealed' or s.scalar(select(ShipmentPack.id).where(ShipmentPack.pack_id==box.id)):raise HTTPException(409,'箱码须同工单、已封且未被其他发货单占用')
    row=PrintShipment(job_id=j.id,tracking=b.tracking,customer=b.customer);s.add(row);s.flush()
    for box in boxes:s.add(ShipmentPack(shipment_id=row.id,pack_id=box.id))
    audit(s,u,f'编制发货单{b.tracking} {len(boxes)}箱');s.commit();return row

def dispatch(s,key,u):
    ref=s.get(PrintShipment,key)
    if not ref:raise HTTPException(404,'发货单不存在')
    j=locked(s,PrintJob,ref.job_id);row=locked(s,PrintShipment,key)
    if row.state!='draft':raise HTTPException(409,'发货已出库，不得重复装运')
    e=gate(s,j);ids=s.scalars(select(ShipmentPack.pack_id).where(ShipmentPack.shipment_id==row.id)).all();boxes=[locked(s,PrintPack,k) for k in sorted(ids)]
    if not boxes or any(x.job_id!=j.id or x.state!='sealed' for x in boxes):raise HTTPException(409,'箱单不完整或未封箱')
    # 逐版次复核：每版已封总量不超该版合格抽检累计（净领纸为全工单总量约束）
    for plate_id in {x.plate_id for x in boxes}:
        plate=s.get(PlateSet,plate_id)
        sample=s.scalar(select(PressSample).where(PressSample.job_id==j.id,PressSample.state=='passed',PressSample.plate_id==plate_id,PressSample.purpose=='routine').order_by(PressSample.sequence.desc()))
        if not sample:raise HTTPException(409,f'版次{plate.version if plate else plate_id}缺少合格抽检依据，禁止发货')
        if sealed_for_plate(s,j.id,plate_id)>sample.quantity:raise HTTPException(409,f'版次{plate.version}已封总量超过该版合格累计，禁止发货')
    if sealed(s,j.id)>e['net_paper']:raise HTTPException(409,'已封总量超过净领纸，禁止发货')
    row.snapshot={'job_code':j.code,'paper':j.paper,'customer':row.customer,'tracking':row.tracking,'quality':e,'boxes':[{'id':x.id,'code':x.code,'sheets':x.sheets,'plate_id':x.plate_id,'sample_snapshot':x.sample_snapshot} for x in boxes],'total_sheets':sum(x.sheets for x in boxes)}
    for box in boxes:box.state='shipped'
    row.state='dispatched';row.dispatched_by=u.email;audit(s,u,f'主管装运{row.tracking} 冻结{len(boxes)}箱');s.commit();return row

def receive(s,key,b,u):
    ref=s.get(PrintShipment,key)
    if not ref:raise HTTPException(404,'发货单不存在')
    locked(s,PrintJob,ref.job_id);row=locked(s,PrintShipment,key)
    if row.state!='dispatched':raise HTTPException(409,'须主管装运后客户签收，不得重复')
    if len(b.received_by.strip())<2:raise HTTPException(409,'签收人不能为空')
    row.state='received';row.received_by=b.received_by.strip();audit(s,u,f'客户签收{row.tracking} {row.received_by}');s.commit();return row
