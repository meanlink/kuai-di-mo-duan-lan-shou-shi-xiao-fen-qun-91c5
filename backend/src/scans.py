import csv
import io
import re
from collections import defaultdict
from datetime import datetime
from math import ceil
from statistics import median

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from . import disputes
from .models import PickupBatch, PickupScan, Station, audit

FIELDS = ['parcel_code', 'planned_at', 'scanned_at']
TIMESTAMP = re.compile(r'\d{4}-\d\d-\d\d \d\d:\d\d')


def station_record(station):
    return {field: getattr(station, field) for field in ('code', 'name', 'region', 'lead', 'route_type', 'sla_minutes')}


def metrics(minutes, sla):
    values = sorted(minutes)
    if not values:
        return {'count': 0, 'median': None, 'p90': None, 'late_rate': None}
    return {
        'count': len(values), 'median': median(values), 'p90': values[ceil(0.90 * len(values)) - 1],
        'late_rate': round(100 * sum(value > sla for value in values) / len(values), 2),
    }


def station_profiles(db, window):
    observations = defaultdict(list)
    records = db.execute(
        select(PickupBatch.station_code, PickupScan.elapsed_minutes)
        .join(PickupScan, PickupScan.batch_code == PickupBatch.code)
        .where(PickupBatch.business_date.between(window.start, window.end))
    )
    for code, elapsed in records:
        observations[code].append(elapsed)
    return [
        station_record(station) | metrics(observations[station.code], station.sla_minutes)
        for station in db.scalars(select(Station).order_by(Station.code))
    ]


def dispute_marks(batch, rows):
    """批次内各面单涉及的裁定标记：待裁定、裁定保留在原批、裁定补入新批。"""
    marks = {}
    for row in rows:
        if row.status == 'pending':
            marks[row.parcel_code] = '重单待裁定'
        elif row.status == 'upheld' and row.first_batch_code == batch.code:
            marks[row.parcel_code] = '重单裁定保留'
        elif row.status == 'reassigned' and row.later_batch_code == batch.code:
            marks[row.parcel_code] = '重单裁定补入'
    return marks


def batch_record(batch, include_scans=False, disputes=()):
    scans = sorted(batch.scans, key=lambda scan: (scan.scanned_at, scan.parcel_code))
    result = {
        'code': batch.code, 'station_code': batch.station_code, 'station_name': batch.station.name,
        'business_date': batch.business_date, 'shift': batch.shift, 'source_name': batch.source_name,
        'recorded_by': batch.recorded_by, 'sla_minutes': batch.station.sla_minutes,
        **metrics([scan.elapsed_minutes for scan in scans], batch.station.sla_minutes),
    }
    if include_scans:
        marks = dispute_marks(batch, disputes)
        result['scans'] = [
            {field: getattr(scan, field) for field in (*FIELDS, 'elapsed_minutes')}
            | {'dispute': marks.get(scan.parcel_code)} for scan in scans
        ]
    return result


def list_batches(db, window, station_code):
    query = (select(PickupBatch).options(selectinload(PickupBatch.scans), selectinload(PickupBatch.station))
             .where(PickupBatch.business_date.between(window.start, window.end))
             .order_by(PickupBatch.business_date.desc(), PickupBatch.code))
    if station_code:
        query = query.where(PickupBatch.station_code == station_code)
    return [batch_record(batch) for batch in db.scalars(query)]


def parse_rows(payload):
    reader = csv.reader(io.StringIO(payload.csv.lstrip('\ufeff')), strict=True)
    scans = []
    seen = set()
    try:
        if next(reader, []) != FIELDS:
            raise HTTPException(422, 'CSV 表头必须为 parcel_code,planned_at,scanned_at')
        for line, row in enumerate(reader, 2):
            if not row:
                continue
            try:
                code, planned, scanned = [field.strip() for field in row]
                if not re.fullmatch(r'[A-Z0-9-]{4,36}', code):
                    raise ValueError('面单编号只能包含大写字母、数字与连字符，长度为 4–36')
                if code in seen:
                    raise ValueError('本文件内面单编号重复')
                if not TIMESTAMP.fullmatch(planned) or not TIMESTAMP.fullmatch(scanned):
                    raise ValueError('时间格式须为 YYYY-MM-DD HH:MM（北京时间）')
                planned_at, scanned_at = datetime.fromisoformat(planned), datetime.fromisoformat(scanned)
                elapsed = int((scanned_at - planned_at).total_seconds() // 60)
                if planned_at.date() != payload.business_date or not 0 <= elapsed <= 1440:
                    raise ValueError('计划时间须属于营业日，扫描时间在计划时间后 24 小时内')
                scans.append(PickupScan(parcel_code=code, planned_at=planned_at, scanned_at=scanned_at, elapsed_minutes=elapsed))
                seen.add(code)
            except ValueError as error:
                raise HTTPException(422, f'CSV 第 {line} 行：{error}') from error
            if len(scans) > 1000:
                raise HTTPException(422, '单批最多 1000 张面单')
    except csv.Error as error:
        raise HTTPException(422, f'无效的 CSV，检查第 {reader.line_num} 行') from error
    if not scans:
        raise HTTPException(422, 'CSV 至少需要一张面单')
    return scans


def import_batch(db, payload, user):
    station = db.get(Station, payload.station_code)
    if not station:
        raise HTTPException(404, '网点不存在')
    if db.get(PickupBatch, payload.code):
        raise HTTPException(409, '批次号已存在，本批未入账')
    parsed = parse_rows(payload)
    batch = PickupBatch(code=payload.code, station_code=station.code, business_date=payload.business_date,
                        shift=payload.shift, source_name=payload.source_name, recorded_by=user)
    owners = {
        scan.parcel_code: (scan, station_code)
        for scan, station_code in db.execute(
            select(PickupScan, PickupBatch.station_code)
            .join(PickupBatch, PickupBatch.code == PickupScan.batch_code)
            .where(PickupScan.parcel_code.in_([row.parcel_code for row in parsed]))
        )
    }
    held, skipped = [], []
    for scan in parsed:
        owner = owners.get(scan.parcel_code)
        if owner is None:
            batch.scans.append(scan)
        elif owner[1] == station.code:
            # 同网点重报：保留先到记录，跳过晚到扫描
            skipped.append(scan.parcel_code)
        else:
            # 跨网点重单：不随批入账，留待主管裁定
            held.append(disputes.open_dispute(db, scan, batch, *owner))
    db.add(batch)
    db.flush()
    for row in held:
        audit(db, user, 'dispute_opened', f'dispute:{row.id}', parcel=row.parcel_code,
              first_batch=row.first_batch_code, later_batch=row.later_batch_code)
    audit(db, user, 'batch_imported', batch.code, station=station.code, scans=len(batch.scans),
          held=len(held), skipped=len(skipped))
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        if error.orig.args[0] == 1062:
            raise HTTPException(409, '批次号或面单编号已存在，本批未入账') from error
        raise
    record = batch_record(batch, include_scans=True)
    record['duplicates'] = {
        'held': [{'parcel_code': row.parcel_code, 'dispute_id': row.id,
                  'first_batch_code': row.first_batch_code} for row in held],
        'skipped': skipped,
    }
    return record
