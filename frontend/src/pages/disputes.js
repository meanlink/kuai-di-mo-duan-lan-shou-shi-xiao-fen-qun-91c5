import {api} from '../api.js';
import {action, badge, closeDialog, displayTime, errorText, esc, notice, showDialog, table} from '../components.js';
import {disputeStates, isManager, shifts} from '../state.js';

const tones = {pending: 'amber', upheld: 'green', reassigned: 'coral'};
export const disputeBadge = status => badge(disputeStates[status] ?? status, tones[status] ?? 'neutral');
const time = value => esc(String(value ?? '').replace('T', ' ').slice(0, 16));

function sideCard(title, side) {
  return `<div class="dispute-side"><h3>${esc(title)}</h3><dl class="task-brief">
    <div><dt>批次</dt><dd class="mono">${esc(side.batch_code)}</dd></div>
    <div><dt>网点</dt><dd>${esc(side.station_name)}（${esc(side.station_code)}）</dd></div>
    <div><dt>营业日 / 班次</dt><dd>${esc(side.business_date)} · ${esc(shifts[side.shift])}</dd></div>
    <div><dt>计划时间</dt><dd class="mono">${time(side.planned_at)}</dd></div>
    <div><dt>首扫时间</dt><dd class="mono">${time(side.scanned_at)}</dd></div>
    <div><dt>耗时 / SLA</dt><dd>${side.elapsed_minutes} 分 / ${side.sla_minutes} 分</dd></div>
    <div><dt>录入人</dt><dd>${esc(side.recorded_by)}</dd></div></dl></div>`;
}

function eventLine(event) {
  const action = {dispute_opened: '重单留待裁定', dispute_decided: disputeStates[event.status] ?? event.action}[event.action] ?? event.action;
  const transfer = event.from_batch && event.to_batch && event.from_batch !== event.to_batch
    ? ` · ${esc(event.from_batch)} → ${esc(event.to_batch)}` : '';
  return `<li><span>${displayTime(event.created_at)}</span> ${esc(event.actor)} · ${esc(action)}${transfer}
    ${event.note ? `<p>${esc(event.note)}</p>` : ''}</li>`;
}

export async function openDispute(id, refresh) {
  const row = await api.dispute(id);
  const decided = row.status !== 'pending';
  const dialog = showDialog(`重复面单 ${row.parcel_code}`, `
    <div class="detail-strip"><span>当前归属批次 <strong class="mono">${esc(row.current_batch_code ?? '—')}</strong></span>${disputeBadge(row.status)}</div>
    <div class="dispute-sides">${sideCard('先到入账 · 首报', row.first)}${sideCard('晚到补报 · 待裁定', row.later)}</div>
    ${decided ? `<h3>裁定依据</h3><p class="note">${esc(row.decision_note)}</p>
      <p class="muted">${esc(row.decided_by)} 于 ${displayTime(row.decided_at)} 裁定${row.status === 'reassigned' ? '，面单已从原批次扣掉并补进晚到批次' : '，面单保留在先到批次'}</p>` : ''}
    ${row.events.length ? `<details class="shift-events"><summary>查看流转记录（${row.events.length}）</summary>
      <ol class="timeline">${row.events.map(eventLine).join('')}</ol></details>` : ''}
    ${!decided && isManager() ? `<h3>主管裁定</h3><form id="dispute-decide" class="form-stack">
      <label>裁定结果<select name="decision">
        <option value="upheld">维持原批：先到记录保留，晚到上报作废</option>
        <option value="reassigned">改判新批：原批次扣掉这票，补进晚到批次</option></select></label>
      <label>裁定依据<textarea name="note" required minlength="12" maxlength="400" rows="2"
        placeholder="说明核对过程与判定理由，至少 12 字"></textarea></label>
      ${errorText}<div class="form-actions"><button class="button primary" type="submit">提交裁定</button></div></form>`
      : !decided ? '<p class="muted">待区域主管裁定，同批其余面单已照常入账。</p>' : ''}`);
  const form = dialog.querySelector('#dispute-decide');
  if (form) action(form, async fields => {
    await api.decideDispute(row.id, fields);
    closeDialog();
    await refresh();
    notice('裁定已生效，两边网点画像已更新');
  });
}

export function disputeSection(rows) {
  if (!rows.length) return '';
  return `<section class="paper dispute-section"><header class="section-head"><div><h2>重复面单裁定</h2>
    <p>手持机离线恢复同步后，两个网点报来同一面单。先到照常入账，晚到留待主管裁定。</p></div></header>
    ${table(['面单', '先到批次 / 网点', '晚到批次 / 网点', '状态', '裁定依据'], rows.map(row => [
      `<button class="text-button mono" data-dispute="${row.id}">${esc(row.parcel_code)}</button>`,
      `<span class="mono">${esc(row.first.batch_code)}</span><small>${esc(row.first.station_name)}</small>`,
      `<span class="mono">${esc(row.later.batch_code)}</span><small>${esc(row.later.station_name)}</small>`,
      disputeBadge(row.status),
      row.decision_note ? esc(row.decision_note) : '<span class="muted">待主管裁定</span>',
    ]))}</section>`;
}

export function bindDisputeButtons(node, refresh) {
  node.querySelectorAll('[data-dispute]').forEach(button => {
    button.onclick = async () => {
      try { await openDispute(Number(button.dataset.dispute), refresh); }
      catch (error) { notice(error.message); }
    };
  });
}
