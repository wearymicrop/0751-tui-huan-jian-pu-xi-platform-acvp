const dispatchState = {id: null, tasks: [], delta: null};

function dispatchTaskRow(task, status) {
  const row = document.createElement('tr');
  if (status) row.className = `task-${status}`;
  const cells = [
    ['task_id', task.task_id], ['serial', task.serial], ['state', task.state],
    ['action_label', task.action_label], ['lane_label', task.lane_label],
    ['priority', task.priority], ['active_parent', task.active_parent || ''],
    ['status', status || ''], ['evidences', task.exposures.map(e => `${e.path.join('→')} 行 ${e.mount_rows.join('/')}`).join('；')],
  ];
  for (const [field, value] of cells) {
    const cell = row.insertCell();
    cell.dataset.field = field;
    if (field === 'serial' || field === 'active_parent') {
      if (value) cell.append(serialButton(value)); else cell.textContent = '—';
    } else if (field === 'state') {
      cell.textContent = stateLabels[value] || value || '—';
    } else if (field === 'status') {
      const labels = {retained: '保留', withdrawn: '撤回', added: '新增', unchanged: '沿用', preview: '待下发'};
      cell.textContent = labels[value] || '—';
    } else {
      cell.textContent = value ?? '—';
    }
  }
  return row;
}

function renderDispatch(data) {
  dispatchState.id = data.dispatch_id;
  dispatchState.tasks = data.tasks;
  const body = document.querySelector('#dispatch-table').tBodies[0];
  body.replaceChildren();
  document.querySelector('#dispatch-summary').textContent =
    `修订 ${data.revision_seq} · ${data.summary.tasks} 件任务 · 批次 ${data.dispatch_id.slice(0, 10)}`;
  const delta = data.delta || {counts: {}, retained: [], withdrawn: [], added: []};
  const status = new Map();
  for (const task of delta.retained) status.set(task.task_id, 'retained');
  for (const task of delta.withdrawn) status.set(task.task_id, 'withdrawn');
  for (const task of delta.added) status.set(task.task_id, 'added');
  for (const task of data.tasks) body.append(dispatchTaskRow(task, status.get(task.task_id) || 'unchanged'));
  for (const task of delta.withdrawn) body.append(dispatchTaskRow(task, 'withdrawn'));
  const banner = document.querySelector('#dispatch-delta');
  banner.replaceChildren();
  const counts = delta.counts;
  const line = elem('div', `第 ${data.revision_seq} 版：保留 ${counts.retained || 0} 件 · 撤回 ${counts.withdrawn || 0} 件 · 新增 ${counts.added || 0} 件`, 'delta-line');
  banner.append(line);
  for (const warning of data.warnings || []) banner.append(elem('div', `${warning.message}：${warning.serials.join('、')}`, 'delta-warn'));
  if (data.reused) banner.append(elem('div', '与上一版一致，未重复派单。', 'delta-note'));
  if (data.supplement) {
    const added = Object.entries(data.supplement.added || {}).filter(([, value]) => value)
      .map(([field, value]) => `${field} +${value}`).join(' · ');
    banner.append(elem('div', `本次补录：${added || '无新增'}${data.supplement.duplicate ? ` · 重复 ${data.supplement.duplicate} 条` : ''}`, 'delta-note'));
  }
  const basis = document.querySelector('#dispatch-basis');
  basis.replaceChildren();
  const scope = data.window || {};
  basis.append(elem('span', `窗口 [${compactTime(scope.start)}, ${compactTime(scope.end)}) UTC`));
  basis.append(elem('span', `规则 ${data.basis.rule_version} · 来源 ${data.basis.source_digest.slice(0, 12)}`));
  basis.append(elem('span', `输入 ${data.basis.input ? data.basis.input.kind : '—'}`));
  renderDispatchHistory(data);
}

function renderDispatchHistory(data) {
  const host = document.querySelector('#dispatch-delta');
  if (!data.revisions || !data.revisions.length) return;
  const details = elem('details', undefined, 'delta-history');
  const summary = elem('summary', `修订与补录台账 · ${data.revisions.length} 版 / ${(data.inputs || []).length} 次输入`);
  details.append(summary);
  for (const revision of data.revisions) {
    details.append(elem('div', `第 ${revision.seq} 版 · ${revision.revision_id.slice(0, 10)} · ${revision.summary.tasks} 件 · ${revision.input ? revision.input.kind : '—'}`, 'delta-entry'));
  }
  for (const input of data.inputs || []) {
    const label = {initial: '首次', supplement: '补录', duplicate_supplement: '重复补录已忽略', refresh: '重跑', resubmit: '重新提交'}[input.kind] || input.kind;
    details.append(elem('div', `第 ${input.seq} 次 · ${label} · ${input.created}`, 'delta-entry'));
  }
  const failed = (data.jobs || []).filter(job => job.status === 'failed');
  for (const job of failed) details.append(elem('div', `待续跑作业 ${job.job_id} · ${job.kind} · ${job.error || ''}`, 'delta-entry error'));
  host.append(details);
}

async function loadDispatch() {
  const listed = await api('/api/dispatches');
  const want = {batch: batch.value.toUpperCase(), start: null, end: null};
  if (startInput.value) want.start = `${startInput.value}T00:00:00Z`;
  if (endInput.value) want.end = `${endInput.value}T00:00:00Z`;
  const candidates = listed.dispatches.filter(row => row.batch === want.batch);
  const exact = candidates.find(row => !want.start && !want.end ? !row.start && !row.end : row.start === want.start && row.end === want.end);
  const chosen = exact || candidates[0];
  if (chosen) {
    const data = await api(`/api/dispatches/${encodeURIComponent(chosen.id)}`);
    renderDispatch(data);
    return;
  }
  await previewDispatch();
}

async function previewDispatch() {
  const payload = {batch: batch.value};
  if (selector.value !== 'archive') payload.sample = selector.value;
  if (startInput.value) payload.start = `${startInput.value}T00:00:00Z`;
  if (endInput.value) payload.end = `${endInput.value}T00:00:00Z`;
  try {
    const data = await api('/api/dispatches/preview', payload);
    renderPreview(data);
  } catch (error) { feedback(error.message, true); }
}

function renderPreview(data) {
  const body = document.querySelector('#dispatch-table').tBodies[0];
  body.replaceChildren();
  for (const task of data.tasks) body.append(dispatchTaskRow(task, 'preview'));
  document.querySelector('#dispatch-summary').textContent =
    `未下发预览 · ${data.summary.tasks} 件任务 · 批次 ${data.dispatch_id.slice(0, 10)}`;
  const banner = document.querySelector('#dispatch-delta');
  banner.replaceChildren(elem('div', '尚未下发。确认范围后点击「按当前召回下发」，任务才会派到现场。', 'delta-note'));
  for (const warning of data.warnings || []) banner.append(elem('div', `${warning.message}：${warning.serials.join('、')}`, 'delta-warn'));
  const basis = document.querySelector('#dispatch-basis');
  basis.replaceChildren(elem('span', `窗口 [${compactTime(data.window.start)}, ${compactTime(data.window.end)}) UTC`));
  if (data.basis) {
    basis.append(elem('span', `规则 ${data.basis.rule_version} · 来源 ${data.basis.source_digest.slice(0, 12)}`));
    if (data.existing) basis.append(elem('span', `该召回已有第 ${data.existing.revision_seq} 版，下发将按补录合并`));
  }
}

async function postDispatch(path, payload) {
  feedback('正在下发…');
  try {
    const data = await api(path, payload);
    renderDispatch(data);
    feedback(data.reused ? '与上一版一致，未重复派单' : `第 ${data.revision_seq} 版已下发`);
    return data;
  } catch (error) {
    feedback(error.message, true);
    return null;
  }
}

document.querySelector('#dispatch-run')?.addEventListener('click', async () => {
  const payload = {batch: batch.value};
  if (selector.value !== 'archive') payload.sample = selector.value;
  if (startInput.value) payload.start = `${startInput.value}T00:00:00Z`;
  if (endInput.value) payload.end = `${endInput.value}T00:00:00Z`;
  await postDispatch('/api/dispatches', payload);
});

document.querySelector('#dispatch-refresh')?.addEventListener('click', async () => {
  if (!dispatchState.id) return feedback('先下发一次', true);
  await postDispatch(`/api/dispatches/${encodeURIComponent(dispatchState.id)}/refresh`, {});
});

document.querySelector('#supplement-send')?.addEventListener('click', async () => {
  if (!dispatchState.id) return feedback('先下发一次', true);
  const file = document.querySelector('#supplement-file').files[0];
  if (!file) return feedback('请先选择补录 JSON', true);
  let payload;
  try { payload = JSON.parse(await file.text()); }
  catch { return feedback('补录文件不是有效 JSON', true); }
  await postDispatch(`/api/dispatches/${encodeURIComponent(dispatchState.id)}/supplements`, payload);
});
