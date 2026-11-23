from fastapi import APIRouter, Depends

from . import disputes
from .auth import allow, current_user
from .db import session
from .dispute_models import DuplicateDispute
from .dispute_schemas import DisputeDecision
from .expansion import load

router = APIRouter()
managers = allow('admin')


@router.get('/disputes')
def list_disputes(status: str | None = None, db=Depends(session), _user=Depends(current_user)):
    return disputes.dispute_list(db, status)


@router.get('/disputes/{key}')
def dispute_detail(key: int, db=Depends(session), _user=Depends(current_user)):
    return disputes.dispute_detail(db, load(db, DuplicateDispute, key, '重复面单裁定'))


@router.post('/disputes/{key}/decide')
def decide(key: int, payload: DisputeDecision, db=Depends(session), user=Depends(managers)):
    return disputes.decide(db, key, payload, user.username)
