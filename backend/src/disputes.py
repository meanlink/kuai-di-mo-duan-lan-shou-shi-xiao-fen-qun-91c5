from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from .dispute_models import DuplicateDispute
from .expansion import load, timeline
from .models import PickupBatch, PickupScan, audit, utcnow

DECISIONS = {'upheld': '维持原批', 'reassigned': '改判新批'}


def side(batch, planned_at, scanned_at, elapsed_minutes):
    return {
        'batch_code': batch.code, 'station_code': batch.station_code, 'station_name': batch.station.name,
        'business_date': batch.business_date, 'shift': batch.shift, 'recorded_by': batch.recorded_by,
        'sla_minutes': batch.station.sla_minutes, 'planned_at': planned_at, 'scanned_at': scanned_at,
        'elapsed_minutes': elapsed_minutes,
    }


def dispute_record(row):
    return {
        'id': row.id, 'parcel_code': row.parcel_code, 'status': row.status,
        'first': side(row.first_batch, row.first_planned_at, row.first_scanned_at, row.first_elapsed_minutes),
        'later': side(row.later_batch, row.later_planned_at, row.later_scanned_at, row.later_elapsed_minutes),
        'decision_note': row.decision_note, 'decided_by': row.decided_by,
        'decided_at': row.decided_at, 'created_at': row.created_at,
    }


def dispute_detail(db, row):
    scan = db.get(PickupScan, row.parcel_code)
    return dispute_record(row) | {
        'current_batch_code': scan.batch_code if scan else None,
        'events': timeline(db, 'dispute', row.id),
    }


def eager():
    return (joinedload(DuplicateDispute.first_batch).joinedload(PickupBatch.station),
            joinedload(DuplicateDispute.later_batch).joinedload(PickupBatch.station))


def dispute_list(db, status=None):
    query = select(DuplicateDispute).options(*eager()).order_by(DuplicateDispute.id.desc())
    if status:
        query = query.where(DuplicateDispute.status == status)
    return [dispute_record(row) for row in db.scalars(query)]


def batch_disputes(db, code):
    query = (select(DuplicateDispute).options(*eager())
             .where((DuplicateDispute.first_batch_code == code) | (DuplicateDispute.later_batch_code == code))
             .order_by(DuplicateDispute.id))
    return db.scalars(query).all()


def open_dispute(db, incoming, batch, owner_scan, owner_station):
    """晚到批次里归属其它网点的面单：不随批入账，转为待裁定记录。

    incoming 是晚到批次解析出的扫描（仅作数据载体，不写入 pickup_scans）；
    owner_scan / owner_station 是已入账的首报扫描及其网点。
    """
    row = DuplicateDispute(
        parcel_code=incoming.parcel_code,
        first_batch_code=owner_scan.batch_code, first_station_code=owner_station,
        first_planned_at=owner_scan.planned_at, first_scanned_at=owner_scan.scanned_at,
        first_elapsed_minutes=owner_scan.elapsed_minutes,
        later_batch_code=batch.code, later_station_code=batch.station_code,
        later_planned_at=incoming.planned_at, later_scanned_at=incoming.scanned_at,
        later_elapsed_minutes=incoming.elapsed_minutes,
    )
    db.add(row)
    return row


def decide(db, key, payload, actor):
    # 行锁串行化并发裁定：后到的事务等待锁后看到状态已变，只接受首个裁定。
    row = load(db, DuplicateDispute, key, '重复面单裁定', lock=True)
    if row.status != 'pending':
        raise HTTPException(409, f'该面单已由 {row.decided_by} 裁定为「{DECISIONS[row.status]}」，仅首个裁定生效')
    moved_from = None
    if payload.decision == 'reassigned':
        scan = db.scalar(select(PickupScan).where(PickupScan.parcel_code == row.parcel_code).with_for_update())
        if scan is None:
            raise HTTPException(409, '面单扫描不存在，无法改判')
        moved_from = scan.batch_code
        scan.batch_code = row.later_batch_code
        scan.planned_at = row.later_planned_at
        scan.scanned_at = row.later_scanned_at
        scan.elapsed_minutes = row.later_elapsed_minutes
    row.status = payload.decision
    row.decision_note = payload.note
    row.decided_by = actor
    row.decided_at = utcnow()
    audit(db, actor, 'dispute_decided', f'dispute:{row.id}', status=row.status, parcel=row.parcel_code,
          from_batch=moved_from or row.first_batch_code,
          to_batch=row.later_batch_code if moved_from else row.first_batch_code, note=payload.note)
    db.commit()
    return dispute_detail(db, row)
