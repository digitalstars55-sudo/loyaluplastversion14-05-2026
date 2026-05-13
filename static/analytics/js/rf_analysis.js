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
let _modalCell = null;
let _modalMode = 'segment';

function _resetModalUI() {
  const textarea = document.getElementById('modal-message');
  const statusEl = document.getElementById('modal-status');
  textarea.value = '';
  statusEl.style.display = 'none';
  statusEl.textContent = '';
  document.getElementById('btn-ai').disabled = false;
  document.getElementById('btn-send').disabled = false;
  document.getElementById('btn-send').textContent = '📨 Отправить';
  removeModalImage();
  updateCharCount();
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
  document.getElementById('modal-message').focus();
}

function openBroadcastModalAll() {
  // Режим «всем оцифрованным» — без segment_id.
  _modalCell = null;
  _modalMode = 'all';

  const info     = document.getElementById('modal-segment-info');
  const hint     = document.getElementById('modal-hint');
  const hintText = document.getElementById('modal-hint-text');

  const total = _totalDigitisedFromMatrix();
  info.innerHTML = `
    <span class="modal-segment-badge" style="background:#1565c0;">📨 Все оцифрованные гости</span>
    <span style="font-size:12px;color:#64748b;">по всем выбранным точкам</span>
    <span class="modal-segment-count">~${total} гостей</span>
  `;

  hintText.innerHTML =
    'Сообщение получат все активные гости с привязанным VK ID. ' +
    'Фильтры по сегментам не применяются.';
  hint.style.display = '';

  _resetModalUI();
  const modal = document.getElementById('broadcast-modal');
  modal.classList.add('active');
  document.getElementById('modal-message').focus();
}

function closeBroadcastModal() {
  document.getElementById('broadcast-modal').classList.remove('active');
  _modalCell = null;
  _modalMode = 'segment';
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

function updateCharCount() {
  const textarea = document.getElementById('modal-message');
  const counter  = document.getElementById('modal-char-count');
  const len = textarea.value.length;
  counter.textContent = `${len} / 4096`;
  counter.className = 'modal-char-count' + (len > 4096 ? ' over' : '');
}

function _setModalStatus(msg, type) {
  const el = document.getElementById('modal-status');
  el.textContent = msg;
  el.className = 'modal-status ' + type;
  el.style.display = msg ? 'block' : 'none';
}

function generateAIText() {
  // Допустимые режимы: 'segment' (требует _modalCell.segment_id) и 'all' (без segment_id).
  if (_modalMode === 'segment' && (!_modalCell || !_modalCell.segment_id)) return;

  const btn = document.getElementById('btn-ai');
  btn.disabled = true;
  btn.textContent = '⏳ Генерация...';
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
        _setModalStatus('Ошибка: ' + data.error, 'error');
      } else {
        document.getElementById('modal-message').value = data.text || '';
        updateCharCount();
        _setModalStatus('✓ Текст сгенерирован — проверьте и нажмите «Отправить»', 'success');
      }
    })
    .catch(() => _setModalStatus('Ошибка соединения', 'error'))
    .finally(() => {
      btn.disabled = false;
      btn.textContent = '🤖 Сгенерировать AI';
    });
}

function sendBroadcast() {
  if (_modalMode === 'segment' && (!_modalCell || !_modalCell.segment_id)) return;

  const text = document.getElementById('modal-message').value.trim();
  if (!text) { _setModalStatus('Введите текст рассылки', 'error'); return; }
  if (text.length > 4096) { _setModalStatus('Текст превышает 4096 символов', 'error'); return; }

  let confirmMsg;
  if (_modalMode === 'segment') {
    const segName = (_modalCell.segment_emoji || '') + ' ' + (_modalCell.segment_name || '');
    confirmMsg = `Отправить рассылку сегменту «${segName.trim()}» (${_modalCell.count} гостей)?`;
  } else {
    const total = _totalDigitisedFromMatrix();
    confirmMsg = `Отправить рассылку ВСЕМ оцифрованным гостям (~${total} получателей)?`;
  }
  if (!confirm(confirmMsg)) return;

  const btnSend = document.getElementById('btn-send');
  const btnAI   = document.getElementById('btn-ai');
  btnSend.disabled = true;
  btnAI.disabled   = true;
  btnSend.textContent = '⏳ Отправка...';
  _setModalStatus('Рассылка отправляется, подождите...', 'loading');

  const branchIds = BRANCH_PARAM ? BRANCH_PARAM.replace('branches=', '') : '';

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
        btnAI.disabled   = false;
        btnSend.textContent = '📨 Отправить';
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
        btnAI.disabled   = false;
        btnSend.textContent = '📨 Отправить';
      }, 3000);
    })
    .catch(() => {
      _setModalStatus('Ошибка соединения', 'error');
      btnSend.disabled = false;
      btnAI.disabled   = false;
      btnSend.textContent = '📨 Отправить';
    });
}
