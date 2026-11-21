import os,redis
from fastapi import APIRouter,Depends,HTTPException
from sqlalchemy import select
from .db import db_session
from .auth import current,writers,admin
from .schemas import ProofInput
from .production_models import PlateSet,PressSample,PressHold,PlateSwitch
from .production_schemas import PlateInput,MeasurementInput,SampleInput,HoldClose,FirstArticleInput,SwitchStart,SwitchFail
from .services import pack
from . import plate_service,sampling_service
router=APIRouter();queue=redis.Redis.from_url(os.environ['REDIS_URL'],decode_responses=True)
@router.get('/plates')
def plates(s=Depends(db_session),u=Depends(current)):return [pack(x) for x in s.scalars(select(PlateSet).order_by(PlateSet.id.desc()))]
@router.get('/plates/status')
def plate_status(s=Depends(db_session),u=Depends(current)):return plate_service.status(s)
@router.post('/plates')
def create(p:PlateInput,s=Depends(db_session),u=Depends(writers)):return pack(plate_service.create(s,p,u))
@router.post('/plates/{key}/measure')
def measure(key:int,p:MeasurementInput,s=Depends(db_session),u=Depends(writers)):return pack(plate_service.measure(s,key,p,u))
@router.post('/plates/{key}/approve')
def approve(key:int,s=Depends(db_session),u=Depends(admin)):return pack(plate_service.approve(s,key,u))
@router.post('/plates/{key}/activate')
def activate(key:int,s=Depends(db_session),u=Depends(admin)):return pack(plate_service.activate(s,key,u))
@router.get('/plate-switches')
def switches(s=Depends(db_session),u=Depends(current)):return [pack(x) for x in s.scalars(select(PlateSwitch).order_by(PlateSwitch.id.desc()))]
@router.post('/plate-switches')
def start_switch(p:SwitchStart,s=Depends(db_session),u=Depends(writers)):return pack(plate_service.start_switch(s,p,u))
@router.post('/plate-switches/{key}/fail')
def fail_switch(key:int,p:SwitchFail,s=Depends(db_session),u=Depends(writers)):return pack(plate_service.fail_switch(s,key,p,u))
@router.post('/plate-switches/{key}/first-article')
def first_article(key:int,p:FirstArticleInput,s=Depends(db_session),u=Depends(writers)):
    sw=s.get(PlateSwitch,key)
    if not sw:raise HTTPException(404,'换版单不存在')
    if p.job_id!=sw.job_id:raise HTTPException(409,'首张必须来自换版单同一工单')
    return pack(sampling_service.first_article(s,p,u,queue))
@router.post('/plate-switches/{key}/sign')
def sign_first(key:int,s=Depends(db_session),u=Depends(admin)):return pack(sampling_service.sign_first_article(s,key,u))
@router.get('/press-samples')
def samples(s=Depends(db_session),u=Depends(current)):return [pack(x) for x in s.scalars(select(PressSample).order_by(PressSample.id.desc()))]
@router.post('/press-samples')
def sample(p:SampleInput,s=Depends(db_session),u=Depends(writers)):return pack(sampling_service.sample(s,p,u,queue))
@router.get('/press-holds')
def holds(s=Depends(db_session),u=Depends(current)):return [pack(x) for x in s.scalars(select(PressHold).order_by(PressHold.id.desc()))]
@router.post('/press-holds/{key}/recheck')
def recheck(key:int,p:ProofInput,s=Depends(db_session),u=Depends(writers)):return pack(sampling_service.recheck(s,key,p,u,queue))
@router.post('/press-holds/{key}/close')
def close(key:int,p:HoldClose,s=Depends(db_session),u=Depends(admin)):return pack(sampling_service.close(s,key,p,u))
