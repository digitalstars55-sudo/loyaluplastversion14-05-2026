// ── Shared tooltip (appended to body to avoid transform stacking context) ──
const _rfTip = document.createElement('div');
_rfTip.id = 'rf-global-tip';
document.body.appendChild(_rfTip);

function rfTooltipShow(cell, text) {
  _rfTip.innerHTML = '<strong>📋 Подсказка:</strong><br>' + text.replace(/\n/g, '<br>');
  _rfTip.style.display = 'block';
  const rect = cell.getBoundingClientRect();
  const TIP_W = 320;
  const GAP   = 10;
  let left = rect.left + rect.width / 2 - TIP_W / 2;
  left = Math.max(8, Math.min(left, window.innerWidth - TIP_W - 8));
  const top = rect.top + window.scrollY - GAP;
  _rfTip.style.left = left + 'px';
  _rfTip.style.top  = top  + 'px';
}

function rfTooltipHide() {
  _rfTip.style.display = 'none';
}

// ── Build matrix grid ──────────────────────────────────────────────
function buildMatrix() {
  const { r_levels, f_levels, cells } = matrixData;
  if (!r_levels || !r_levels.length) {
    document.getElementById('matrix-container').innerHTML =
      '<div class="empty-state"><div class="icon">📊</div>Нет данных. Запустите пересчёт RF-метрик.</div>';
    return;
  }

  const container = document.getElementById('matrix-container');
  container.style.display = 'grid';
  container.style.gridTemplateColumns = `90px repeat(${f_levels.length}, 1fr)`;
  container.style.gap = '6px';

  const corner = document.createElement('div');
  corner.style.cssText = 'display:flex;align-items:flex-end;justify-content:center;padding-bottom:8px;font-size:12px;color:#94a3b8;text-align:center;';
  corner.innerHTML = '← F (частота)<br>R (давность) ↓';
  container.appendChild(corner);

  f_levels.forEach(fl => {
    const el = document.createElement('div');
    el.className = 'rf-matrix-f-header';
    el.innerHTML = `<div class="f-label">${fl.label}</div><div class="f-name">${fl.name}</div><div class="f-range">${fl.range}</div>`;
    container.appendChild(el);
  });

  r_levels.forEach(rl => {
    const rLabel = document.createElement('div');
    rLabel.className = 'rf-matrix-r-header';
    rLabel.innerHTML = `<div class="r-label">${rl.label}</div><div class="r-name">${rl.name}</div><div class="r-range">${rl.range}</div>`;
    container.appendChild(rLabel);

    f_levels.forEach(fl => {
      const key = `${rl.r_score}_${fl.f_score}`;
      const cell = cells[key] || { segment_emoji: '', segment_name: '—', count: 0, pct: 0, segment_color: '#e8e8e8', r_score: rl.r_score, f_score: fl.f_score, segment_strategy: '', segment_hint: '', segment_id: null };

      const bg = cell.segment_color || '#e8e8e8';
      const el = document.createElement('div');
      el.className = 'rf-cell';
      el.style.background = bg + '22';
      el.style.border = `2px solid ${bg}44`;
      el.dataset.r = rl.r_score;
      el.dataset.f = fl.f_score;

      const segId = cell.segment_id;
      const modeParam = `mode=${ACTIVE_MODE}`;
      const branchParam = BRANCH_PARAM ? `&${BRANCH_PARAM}` : '';
      const cellKey = `${rl.r_score}_${fl.f_score}`;
      const actionsHtml = segId ? `
        <div class="rf-cell-actions">
          <a href="#"
             class="rf-cell-btn-broadcast"
             onclick="event.stopPropagation(); event.preventDefault(); openBroadcastModal('${cellKey}');"
             title="Создать рассылку">📨 Рассылка</a>
          <a href="/analytics/rf/segment/${segId}/export-senler/?${modeParam}${branchParam}"
             class="rf-cell-btn-senler"
             onclick="event.stopPropagation();"
             title="Скачать TXT с VK ID для Senler">📥 Senler</a>
        </div>
      ` : '';

      const tipText = cell.segment_hint || cell.segment_strategy || '';
      el.innerHTML = `
        <div class="rf-cell-emoji">${cell.segment_emoji || ''}</div>
        <div class="rf-cell-name">${cell.segment_name || '—'}</div>
        <div class="rf-cell-count" style="color: black">${cell.count}</div>
        <div class="rf-cell-pct">${cell.pct}%</div>
        ${actionsHtml}
      `;
      if (tipText) {
        el.addEventListener('mouseenter', () => rfTooltipShow(el, tipText));
        el.addEventListener('mouseleave', rfTooltipHide);
      }
      el.addEventListener('click', () => selectCell(rl.r_score, fl.f_score, cell, bg));
      container.appendChild(el);
    });
  });
}

// ── Cell selection ─────────────────────────────────────────────────
let selectedR = null, selectedF = null;

function selectCell(r, f, cell, color) {
  document.querySelectorAll('.rf-cell.selected').forEach(c => c.classList.remove('selected'));
  const el = document.querySelector(`.rf-cell[data-r="${r}"][data-f="${f}"]`);
  if (el) el.classList.add('selected');

  selectedR = r; selectedF = f;

  const detailBody = document.getElementById('detail-body');
  const segId = cell.segment_id;
  const modeParam = `mode=${ACTIVE_MODE}`;
  const branchParam = BRANCH_PARAM ? `&${BRANCH_PARAM}` : '';
  const detailCellKey = `${r}_${f}`;
  const detailActionsHtml = segId ? `
    <div style="display:flex;gap:8px;margin-bottom:14px;">
      <a href="#"
         onclick="event.preventDefault(); openBroadcastModal('${detailCellKey}');"
         style="flex:1;padding:9px;background:#4a76a8;color:#fff;border:none;border-radius:8px;font-size:12px;font-weight:700;cursor:pointer;text-decoration:none;text-align:center;display:block;">
        📨 Рассылка
      </a>
      <a href="/analytics/rf/segment/${segId}/export-senler/?${modeParam}${branchParam}"
         style="flex:1;padding:9px;background:#5181b8;color:#fff;border:none;border-radius:8px;font-size:12px;font-weight:700;cursor:pointer;text-decoration:none;text-align:center;display:block;">
        📥 Senler
      </a>
    </div>
  ` : '';

  const hintText = cell.segment_hint || '';
  const strategyText = cell.segment_strategy || '';

  detailBody.innerHTML = `
    <div style="padding:16px 20px;">
      <div class="rf-detail-segment-badge" style="background:${color};">
        ${cell.segment_emoji || ''} ${cell.segment_name || '—'}
      </div>
      <div class="rf-detail-stats">
        <div class="rf-detail-stat">
          <div class="rf-detail-stat-val" style="color:black;">${cell.count}</div>
          <div class="rf-detail-stat-label">Гостей</div>
        </div>
        <div class="rf-detail-stat">
          <div class="rf-detail-stat-val" style="color:black;">${cell.pct}%</div>
          <div class="rf-detail-stat-label">Доля</div>
        </div>
        <div class="rf-detail-stat">
          <div class="rf-detail-stat-val">R${r - 1}</div>
          <div class="rf-detail-stat-label">Давность</div>
        </div>
        <div class="rf-detail-stat">
          <div class="rf-detail-stat-val">F${f}</div>
          <div class="rf-detail-stat-label">Частота</div>
        </div>
      </div>
      ${hintText ? `
        <div class="rf-detail-strategy">
          <div class="rf-detail-strategy-title">📋 Подсказка по рассылке</div>
          ${hintText.replace(/\n/g, '<br>')}
        </div>
      ` : ''}
      ${strategyText ? `
        <div class="rf-detail-strategy" style="border-left-color:#7c3aed;">
          <div class="rf-detail-strategy-title">🎯 Стратегия</div>
          ${strategyText}
        </div>
      ` : ''}
      ${detailActionsHtml}
      ${cell.count > 0 ? `
        <button onclick="loadGuests(${r}, ${f})"
          style="width:100%;padding:9px;background:${color};color:#000;border:none;border-radius:8px;font-size:12px;font-weight:700;cursor:pointer;">
          👥 Показать гостей (${cell.count})
        </button>
      ` : ''}
    </div>
  `;
}

// ── Load guests ────────────────────────────────────────────────────
function loadGuests(r, f) {
  const card  = document.getElementById('guest-list-card');
  const body  = document.getElementById('guest-list-body');
  const title = document.getElementById('guest-list-title');
  const count = document.getElementById('guest-list-count');

  card.style.display = '';
  body.innerHTML = '<div class="empty-state"><div class="icon">⏳</div>Загрузка гостей...</div>';
  card.scrollIntoView({ behavior: 'smooth', block: 'start' });

  const params = new URLSearchParams();
  params.set('r_score', r);
  params.set('f_score', f);
  params.set('mode', ACTIVE_MODE);
  if (BRANCH_PARAM) params.set('branch_ids', BRANCH_PARAM.replace('branches=', ''));

  fetch(`/api/v1/analytics/rf/?${params.toString()}`)
    .then(r => r.json())
    .then(data => {
      const guests = data.guests || [];
      title.textContent = `Гости: ${data.segment_name || 'Сегмент'} (R${r - 1} · F${f})`;
      count.textContent = `${guests.length} чел.`;

      if (!guests.length) {
        body.innerHTML = '<div class="empty-state"><div class="icon">👥</div>Нет гостей в этом сегменте</div>';
        return;
      }

      const rows = guests.map((g, i) => `
        <tr>
          <td style="color:#94a3b8;font-size:11px;">${i + 1}</td>
          <td>
            <div class="guest-name">${g.first_name || ''} ${g.last_name || ''}</div>
            <div class="guest-vk-id">VK ID: ${g.vk_id}</div>
          </td>
          <td>${g.last_visit}</td>
          <td>${g.frequency}</td>
          <td>${g.recency_days} дн.</td>
          <td style="font-weight:700;">${g.coins}</td>
        </tr>
      `).join('');

      body.innerHTML = `
        <table class="guest-table">
          <thead>
            <tr>
              <th>#</th>
              <th>Гость</th>
              <th>Последний визит</th>
              <th>Визитов</th>
              <th>Давность</th>
              <th>Коины</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      `;
    })
    .catch(() => {
      body.innerHTML = '<div class="empty-state"><div class="icon">⚠️</div>Ошибка загрузки</div>';
    });
}

document.addEventListener('DOMContentLoaded', buildMatrix);

// ── Recalculate RF ─────────────────────────────────────────────────
function recalcRF() {
  const btn    = document.getElementById('btn-recalc');
  const status = document.getElementById('recalc-status');

  btn.disabled = true;
  btn.textContent = '⏳ Считаем...';
  btn.style.opacity = '0.6';
  status.style.display = 'inline';
  status.style.color   = '#94a3b8';
  status.textContent   = 'Идёт пересчёт...';

  const body = new URLSearchParams();
  body.set('mode', ACTIVE_MODE);
  if (BRANCH_PARAM) body.set('branch_ids', BRANCH_PARAM.replace('branches=', ''));

  fetch('/api/v1/analytics/rf/recalculate/', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'X-CSRFToken': getCookie('csrftoken') },
    body: body.toString(),
  })
    .then(r => r.json())
    .then(data => {
      if (data.detail || data.non_field_errors) {
        status.style.color = '#dc2626';
        status.textContent = 'Ошибка: ' + (data.detail || JSON.stringify(data));
      } else {
        status.style.color = '#16a34a';
        status.textContent = `✓ Готово: обновлено ${data.updated}, создано ${data.created}, миграций ${data.migrations} (${data.duration_ms} мс)`;
        setTimeout(() => location.reload(), 1500);
      }
    })
    .catch(() => {
      status.style.color = '#dc2626';
      status.textContent = 'Ошибка соединения';
    })
    .finally(() => {
      btn.disabled = false;
      btn.textContent = '🔄 Пересчитать';
      btn.style.opacity = '1';
    });
}

function getCookie(name) {
  const m = document.cookie.match('(?:^|;)\\s*' + name + '=([^;]*)');
  return m ? decodeURIComponent(m[1]) : '';
}

// ── Broadcast modal ────────────────────────────────────────────────
// _modalCell — данные ячейки сегмента; null при режиме «всем оцифрованным».
// _modalMode — 'segment' | 'all'. Влияет на тексты, AI-prompt и параметры запроса.
// _variants — массив {percent, text} для A/B/% сплита; всегда минимум 1 элемент.
let _modalCell = null;
let _modalMode = 'segment';
let _variants  = [{ percent: 100, text: '' }];

function _resetModalUI() {
  const statusEl = document.getElementById('modal-status');
  statusEl.style.display = 'none';
  statusEl.textContent = '';
  document.getElementById('btn-send').disabled = false;
  document.getElementById('btn-send').textContent = '📨 Отправить';
  document.getElementById('modal-warning').style.display = 'none';
  document.getElementById('modal-branches').style.display = 'none';
  _variants = [{ text: '' }];
  _renderVariants();
  removeModalImage();
}

// ── Single message block + AI ────────────────────────────────────
function _renderVariants() {
  const container = document.getElementById('modal-variants');
  if (!container) return;
  container.innerHTML = '';
  const v = _variants[0];
  const lenColor = v.text.length > 4096 ? '#dc2626' : '#94a3b8';
  const card = document.createElement('div');
  card.style.cssText = 'border:1px solid #e2e8f0;border-radius:10px;padding:12px 14px;margin-bottom:10px;background:#fff;';
  card.innerHTML = `
    <div style="font-size:13px;font-weight:700;color:#0f172a;margin-bottom:8px;">Текст сообщения</div>
    <textarea class="modal-textarea" oninput="updateVariantText(this.value)"
              placeholder="Введите текст или нажмите «🤖 AI»..."
              maxlength="4096">${_escapeHtml(v.text)}</textarea>
    <div style="display:flex;align-items:center;justify-content:space-between;margin-top:6px;">
      <button type="button" id="btn-ai-0" onclick="generateAIText()"
              style="font-size:12px;padding:6px 12px;border:1px solid #cbd5e1;border-radius:6px;background:#fff;cursor:pointer;color:#0f172a;font-weight:600;">
        🤖 Сгенерировать AI
      </button>
      <div style="font-size:12px;color:${lenColor};">${v.text.length} / 4096</div>
    </div>
  `;
  container.appendChild(card);
}

function _escapeHtml(s) {
  return (s || '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function updateVariantText(value) {
  if (!_variants[0]) return;
  _variants[0].text = value;
  const card = document.getElementById('modal-variants').firstElementChild;
  if (!card) return;
  const counter = card.querySelector('div[style*="4096"]');
  if (counter) {
    counter.textContent = `${value.length} / 4096`;
    counter.style.color = value.length > 4096 ? '#dc2626' : '#94a3b8';
  }
}

// ── Branch picker (показывается только в режиме «всем») ───────────────
function _renderModalBranches() {
  const container = document.getElementById('modal-branches-list');
  container.innerHTML = '';
  const preselect = new Set((ACTIVE_BRANCH_IDS && ACTIVE_BRANCH_IDS.length) ? ACTIVE_BRANCH_IDS : ALL_BRANCHES.map(b => b.id));
  ALL_BRANCHES.forEach(b => {
    const id = `modal-branch-cb-${b.id}`;
    const label = document.createElement('label');
    label.style.cssText = 'display:inline-flex;align-items:center;gap:10px;font-size:15px;color:#0f172a;cursor:pointer;user-select:none;padding:6px 10px;border-radius:6px;font-weight:500;';
    label.innerHTML = `<input type="checkbox" id="${id}" value="${b.id}" ${preselect.has(b.id) ? 'checked' : ''} onchange="updateModalBranchSummary()" style="width:18px;height:18px;cursor:pointer;"> ${b.name}`;
    container.appendChild(label);
  });
  updateModalBranchSummary();
}

function modalBranchesSelectAll(checked) {
  document.querySelectorAll('#modal-branches-list input[type=checkbox]').forEach(cb => { cb.checked = checked; });
  updateModalBranchSummary();
}

function _getSelectedBranchIds() {
  return Array.from(document.querySelectorAll('#modal-branches-list input[type=checkbox]:checked'))
              .map(cb => parseInt(cb.value, 10))
              .filter(n => !isNaN(n));
}

function updateModalBranchSummary() {
  const selected = _getSelectedBranchIds();
  const summary = document.getElementById('modal-branches-summary');
  if (selected.length === 0) {
    summary.style.color = '#dc2626';
    summary.textContent = '⚠️ Не выбрано ни одной точки — рассылка не уйдёт';
  } else if (selected.length === ALL_BRANCHES.length) {
    summary.style.color = '#64748b';
    summary.textContent = `Выбраны ВСЕ ${ALL_BRANCHES.length} торговых точек`;
  } else {
    summary.style.color = '#64748b';
    summary.textContent = `Выбрано ${selected.length} из ${ALL_BRANCHES.length} торговых точек`;
  }
}

function _totalDigitisedFromMatrix() {
  // Сумма ячеек матрицы = число оцифрованных гостей по текущей области.
  if (!matrixData || !matrixData.cells) return 0;
  let n = 0;
  for (const k in matrixData.cells) n += (matrixData.cells[k].count || 0);
  return n;
}

function openBroadcastModal(cellKey) {
  const cell = matrixData.cells[cellKey];
  if (!cell) return;
  _modalCell = cell;
  _modalMode = 'segment';
  document.querySelector('.modal-header-title').textContent = '📨 Рассылка по сегменту';

  const info     = document.getElementById('modal-segment-info');
  const hint     = document.getElementById('modal-hint');
  const hintText = document.getElementById('modal-hint-text');

  const bg = cell.segment_color || '#e8e8e8';
  info.innerHTML = `
    <span class="modal-segment-badge" style="background:${bg};">${cell.segment_emoji || ''} ${cell.segment_name || '—'}</span>
    <span style="font-size:12px;color:#64748b;">${cell.segment_code || ''}</span>
    <span class="modal-segment-count">${cell.count} гостей</span>
  `;

  const tipText = cell.segment_hint || '';
  if (tipText) {
    hintText.innerHTML = tipText.replace(/\n/g, '<br>');
    hint.style.display = '';
  } else {
    hint.style.display = 'none';
  }

  _resetModalUI();
  const modal = document.getElementById('broadcast-modal');
  modal.classList.add('active');
  // Фокус на textarea первого варианта
  const ta = document.querySelector('#modal-variants textarea');
  if (ta) ta.focus();
}

function openBroadcastModalAll() {
  // Режим «всем оцифрованным» — без segment_id.
  _modalCell = null;
  _modalMode = 'all';

  const info = document.getElementById('modal-segment-info');
  const hint = document.getElementById('modal-hint');

  const total = _totalDigitisedFromMatrix();
  info.innerHTML = `
    <span class="modal-segment-badge" style="background:#1565c0;color:#fff;">📨 Рассылка ВСЕЙ базе</span>
    <span class="modal-segment-count">~${total} гостей</span>
  `;

  // Подсказка по сегменту здесь не нужна — есть отдельный warning.
  hint.style.display = 'none';

  _resetModalUI();
  // _resetModalUI скрывает warning/branches — для режима «всем» показываем.
  document.getElementById('modal-warning').style.display = '';
  document.getElementById('modal-branches').style.display = '';
  _renderModalBranches();

  document.querySelector('.modal-header-title').textContent = '📨 Рассылка всем гостям';

  const modal = document.getElementById('broadcast-modal');
  modal.classList.add('active');
  const ta = document.querySelector('#modal-variants textarea');
  if (ta) ta.focus();
}

function closeBroadcastModal() {
  document.getElementById('broadcast-modal').classList.remove('active');
  _modalCell = null;
  _modalMode = 'segment';
  _variants  = [{ text: '' }];
  removeModalImage();
}

// ── Image upload handling ──────────────────────────────────────────
let _modalImageFile = null;

function handleImageSelect(input) {
  const file = input.files && input.files[0];
  if (!file) return;
  if (file.size > 5 * 1024 * 1024) {
    _setModalStatus('Файл слишком большой (максимум 5 МБ)', 'error');
    return;
  }
  _modalImageFile = file;
  const reader = new FileReader();
  reader.onload = function(e) {
    document.getElementById('modal-image-preview-img').src = e.target.result;
    document.getElementById('modal-image-preview').style.display = 'block';
    document.getElementById('modal-image-drop').style.display = 'none';
  };
  reader.readAsDataURL(file);
}

function removeModalImage() {
  _modalImageFile = null;
  document.getElementById('modal-image-preview').style.display = 'none';
  document.getElementById('modal-image-drop').style.display = '';
  document.getElementById('modal-image-preview-img').src = '';
  document.getElementById('modal-image-input').value = '';
}

(function() {
  const drop = document.getElementById('modal-image-drop');
  if (!drop) return;
  ['dragenter', 'dragover'].forEach(e => drop.addEventListener(e, function(ev) {
    ev.preventDefault(); ev.stopPropagation(); drop.classList.add('dragover');
  }));
  ['dragleave', 'drop'].forEach(e => drop.addEventListener(e, function(ev) {
    ev.preventDefault(); ev.stopPropagation(); drop.classList.remove('dragover');
  }));
  drop.addEventListener('drop', function(ev) {
    const file = ev.dataTransfer.files && ev.dataTransfer.files[0];
    if (file && file.type.startsWith('image/')) {
      const dt = new DataTransfer();
      dt.items.add(file);
      document.getElementById('modal-image-input').files = dt.files;
      handleImageSelect(document.getElementById('modal-image-input'));
    }
  });
})();

document.getElementById('broadcast-modal').addEventListener('click', function(e) {
  if (e.target === this) closeBroadcastModal();
});

document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') closeBroadcastModal();
});

function _setModalStatus(msg, type) {
  const el = document.getElementById('modal-status');
  el.textContent = msg;
  el.className = 'modal-status ' + type;
  el.style.display = msg ? 'block' : 'none';
}

function generateAIText() {
  if (_modalMode === 'segment' && (!_modalCell || !_modalCell.segment_id)) return;

  const btn = document.getElementById('btn-ai-0');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Генерация...'; }
  _setModalStatus('AI генерирует текст рассылки...', 'loading');

  const payload = _modalMode === 'segment'
    ? { segment_id: _modalCell.segment_id }
    : {};

  fetch('/api/v1/analytics/rf/generate-broadcast-text/', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCookie('csrftoken') },
    body: JSON.stringify(payload),
  })
    .then(r => r.json())
    .then(data => {
      if (data.error) {
        _setModalStatus('Ошибка AI: ' + data.error, 'error');
      } else {
        _variants[0].text = data.text || '';
        _renderVariants();
        _setModalStatus('✓ Текст сгенерирован — проверьте и нажмите «Отправить»', 'success');
      }
    })
    .catch(() => _setModalStatus('Ошибка соединения', 'error'))
    .finally(() => {
      const btn2 = document.getElementById('btn-ai-0');
      if (btn2) { btn2.disabled = false; btn2.textContent = '🤖 Сгенерировать AI'; }
    });
}

function sendBroadcast() {
  if (_modalMode === 'segment' && (!_modalCell || !_modalCell.segment_id)) return;

  const text = (_variants[0] && _variants[0].text || '').trim();
  if (!text) { _setModalStatus('Введите текст рассылки', 'error'); return; }
  if (text.length > 4096) { _setModalStatus('Текст превышает 4096 символов', 'error'); return; }

  let branchIds;
  let confirmMsg;

  if (_modalMode === 'segment') {
    const segName = (_modalCell.segment_emoji || '') + ' ' + (_modalCell.segment_name || '');
    confirmMsg = `Отправить рассылку сегменту «${segName.trim()}» (${_modalCell.count} гостей)?`;
    branchIds = BRANCH_PARAM ? BRANCH_PARAM.replace('branches=', '') : '';
  } else {
    const selected = _getSelectedBranchIds();
    if (selected.length === 0) {
      _setModalStatus('Выберите хотя бы одну торговую точку', 'error');
      return;
    }
    const allSelected = selected.length === ALL_BRANCHES.length;
    const pointsLabel = allSelected ? `ВСЕМ ${ALL_BRANCHES.length} точкам` : `${selected.length} точкам`;
    const total = _totalDigitisedFromMatrix();
    confirmMsg = `Отправить рассылку ВСЕМ гостям по ${pointsLabel}? (~${total} получателей)`;
    branchIds = selected.join(',');
  }
  if (!confirm(confirmMsg)) return;

  const btnSend = document.getElementById('btn-send');
  btnSend.disabled = true;
  btnSend.textContent = '⏳ Отправка...';
  _setModalStatus('Рассылка отправляется, подождите...', 'loading');

  const formData = new FormData();
  if (_modalMode === 'segment') {
    formData.append('segment_id', _modalCell.segment_id);
  }
  formData.append('message_text', text);
  formData.append('mode', ACTIVE_MODE);
  formData.append('branch_ids', branchIds);
  if (_modalImageFile) formData.append('image', _modalImageFile);

  fetch('/api/v1/analytics/rf/send-broadcast/', {
    method: 'POST',
    headers: { 'X-CSRFToken': getCookie('csrftoken') },
    body: formData,
  })
    .then(r => r.json())
    .then(data => {
      if (data.error) {
        _setModalStatus('Ошибка: ' + data.error, 'error');
        btnSend.disabled = false;
        btnSend.textContent = '📨 Отправить';
        return;
      }

      if (data.queued) {
        const qd = (data.results || [])
          .filter(r => r.status === 'queued')
          .map(r => `${r.branch}${r.variant ? ' ' + r.variant : ''}: ${r.total}`)
          .join(' · ');
        _setModalStatus(
          `🚀 Рассылка запущена в фоне: ${data.total_recipients || 0} получателей` +
          (qd ? ` (${qd})` : '') +
          '. Прогресс — в админке: Senler → Рассылки.',
          'success'
        );
        btnSend.textContent = '✅ В очереди';
        setTimeout(() => {
          closeBroadcastModal();
          btnSend.disabled = false;
          btnSend.textContent = '📨 Отправить';
        }, 4000);
        return;
      }

      let summary = `✅ Отправлено ${data.total_sent} сообщений`;
      if (data.results && data.results.length > 0) {
        const details = data.results.map(r =>
          `${r.branch}: ${r.sent} отпр.` +
          (r.failed  ? `, ${r.failed} ош.`   : '') +
          (r.skipped ? `, ${r.skipped} проп.` : '')
        ).join(' · ');
        summary += ` (${details})`;
      }

      _setModalStatus(summary, 'success');
      btnSend.textContent = '✅ Отправлено';

      setTimeout(() => {
        closeBroadcastModal();
        btnSend.disabled = false;
        btnSend.textContent = '📨 Отправить';
      }, 3000);
    })
    .catch(() => {
      _setModalStatus('Ошибка соединения', 'error');
      btnSend.disabled = false;
      btnSend.textContent = '📨 Отправить';
    });
}
