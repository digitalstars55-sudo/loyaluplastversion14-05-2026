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
      // «Назначить награду» — пустой сегмент назначать некому, кнопка гаснет.
      const rewardDisabled = !cell.count;
      const rewardHtml = rewardDisabled
        ? `<a href="#" style="background:#7c3aed;opacity:.4;cursor:not-allowed;transform:none;"
             onclick="event.stopPropagation(); event.preventDefault();"
             title="В сегменте нет гостей">🎁 Награда</a>`
        : `<a href="#" style="background:#7c3aed;"
             onclick="event.stopPropagation(); event.preventDefault(); openRewardModal('${cellKey}');"
             title="Назначить награду сегменту">🎁 Награда</a>`;
      const actionsHtml = segId ? `
        <div class="rf-cell-actions">
          <a href="#"
             class="rf-cell-btn-broadcast"
             onclick="event.stopPropagation(); event.preventDefault(); openBroadcastModal('${cellKey}');"
             title="Создать рассылку">📨 Рассылка</a>
          ${rewardHtml}
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
  // «Назначить награду» рядом с «Рассылкой»; для пустой ячейки — неактивна.
  const detailRewardHtml = cell.count
    ? `<a href="#"
         onclick="event.preventDefault(); openRewardModal('${detailCellKey}');"
         style="flex:1;padding:9px;background:#7c3aed;color:#fff;border:none;border-radius:8px;font-size:12px;font-weight:700;cursor:pointer;text-decoration:none;text-align:center;display:block;">
        🎁 Награда
      </a>`
    : `<span title="В сегменте нет гостей"
         style="flex:1;padding:9px;background:#7c3aed;opacity:.4;color:#fff;border-radius:8px;font-size:12px;font-weight:700;cursor:not-allowed;text-align:center;display:block;">
        🎁 Награда
      </span>`;
  const detailActionsHtml = segId ? `
    <div style="display:flex;gap:8px;margin-bottom:14px;">
      <a href="#"
         onclick="event.preventDefault(); openBroadcastModal('${detailCellKey}');"
         style="flex:1;padding:9px;background:#4a76a8;color:#fff;border:none;border-radius:8px;font-size:12px;font-weight:700;cursor:pointer;text-decoration:none;text-align:center;display:block;">
        📨 Рассылка
      </a>
      ${detailRewardHtml}
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
  // Тот же период, что у матрицы страницы — иначе список гостей ячейки
  // считается по дефолтному окну 30 дней и расходится с её цифрой.
  if (typeof PERIOD_START !== 'undefined' && PERIOD_START) params.set('start', PERIOD_START);
  if (typeof PERIOD_END !== 'undefined' && PERIOD_END) params.set('end', PERIOD_END);

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
// _pendingCampaignId — id RFM-кампании, когда рассылку открыли с экрана
// результата «Назначить награду»; уходит в send-broadcast как campaign_id,
// и бэкенд шлёт ровно по snapshot кампании (без контрольной группы).
let _modalCell = null;
let _modalMode = 'segment';
let _variants  = [{ percent: 100, text: '' }];
let _pendingCampaignId = null;

function _resetModalUI() {
  const statusEl = document.getElementById('modal-status');
  statusEl.style.display = 'none';
  statusEl.textContent = '';
  document.getElementById('btn-send').disabled = false;
  document.getElementById('btn-send').textContent = '📨 Отправить';
  document.getElementById('modal-warning').style.display = 'none';
  document.getElementById('modal-branches').style.display = 'none';
  document.getElementById('modal-campaign-note').style.display = 'none';
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

// campaignName — только для подписи в шапке; аудиторию определяет campaignId.
function openBroadcastModal(cellKey, campaignId, campaignName) {
  const cell = matrixData.cells[cellKey];
  if (!cell) return;
  _modalCell = cell;
  _modalMode = 'segment';
  _pendingCampaignId = campaignId || null;
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
  // _resetModalUI прячет плашку кампании — для перехода из «Награды» показываем.
  if (_pendingCampaignId) {
    document.getElementById('modal-campaign-note-name').textContent = campaignName || `Кампания №${_pendingCampaignId}`;
    document.getElementById('modal-campaign-note').style.display = '';
  }
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
  _pendingCampaignId = null;

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
  _pendingCampaignId = null;
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

  // Текст из поля — это пожелания/черновик пользователя, передаём в AI.
  const draft = ((_variants[0] && _variants[0].text) || '').trim();
  if (draft) payload.draft = draft;

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
    // Переход из «Награды»: получатели — snapshot кампании, не живая ячейка.
    if (_pendingCampaignId) {
      confirmMsg = `Отправить рассылку получателям RFM-кампании №${_pendingCampaignId}? Контрольная группа сообщение не получит.`;
    }
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
    // Контекст ячейки: бэкенд собирает аудиторию ровно как показанную цифру
    // (режим/область/период) и отбивает отправку, если фактическая аудитория
    // заметно больше показанной (инцидент 2026-08-21).
    if (_modalCell.r_score) formData.append('r_score', _modalCell.r_score);
    if (_modalCell.f_score) formData.append('f_score', _modalCell.f_score);
    if (typeof _modalCell.count === 'number') formData.append('expected_count', _modalCell.count);
    // Период страницы: без него бэкенд берёт окно 30 дней, а страница может
    // показывать «всё время» → «в сегменте нет получателей» при полной ячейке.
    if (typeof PERIOD_START !== 'undefined' && PERIOD_START) formData.append('start', PERIOD_START);
    if (typeof PERIOD_END !== 'undefined' && PERIOD_END) formData.append('end', PERIOD_END);
  }
  formData.append('message_text', text);
  formData.append('mode', ACTIVE_MODE);
  formData.append('branch_ids', branchIds);
  // Переход из «Назначить награду»: аудитория = snapshot кампании, а не живая
  // ячейка — состав между начислением и отправкой меняться не должен.
  if (_pendingCampaignId) formData.append('campaign_id', _pendingCampaignId);
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

// ══════════════════════════════════════════════════════════════════════
// RFM-награды (ТЗ «RFM-награды» §2-§3)
// ══════════════════════════════════════════════════════════════════════

// ── Reward modal ───────────────────────────────────────────────────
// _rewardCell     — ячейка матрицы, из которой открыли модалку.
// _rewardCellKey  — 'r_f' ключ ячейки: нужен для «Перейти к рассылке».
// _rewardCatalog  — кэш GET reward-catalog (null = ещё не грузили).
// _rewardCampaign — созданная кампания, показывается на экране результата.
let _rewardCell     = null;
let _rewardCellKey  = null;
let _rewardType     = 'gift';
let _rewardCatalog  = null;
let _rewardCampaign = null;
let _rewardNameTouched = false;

function _setRewardStatus(msg, type) {
  const el = document.getElementById('reward-status');
  el.textContent = msg;
  el.className = 'modal-status ' + (type || '');
  el.style.display = msg ? 'block' : 'none';
}

function _rewardMoney(v) {
  const n = Number(v || 0);
  return n.toLocaleString('ru-RU', { maximumFractionDigits: 2 });
}

function openRewardModal(cellKey) {
  const cell = matrixData.cells[cellKey];
  if (!cell || !cell.segment_id) return;
  // Пустой сегмент: награду назначать некому — кнопка и так неактивна.
  if (!cell.count) return;

  _rewardCell     = cell;
  _rewardCellKey  = cellKey;
  _rewardCampaign = null;
  _rewardType     = 'gift';
  _rewardNameTouched = false;

  document.getElementById('reward-modal-title').textContent = '🎁 Назначить награду сегменту';

  const bg   = cell.segment_color || '#e8e8e8';
  const code = cell.segment_code || `R${(cell.r_score || 1) - 1}F${cell.f_score || 1}`;
  document.getElementById('reward-segment-info').innerHTML = `
    <span class="modal-segment-badge" style="background:${bg};">${cell.segment_emoji || ''} ${cell.segment_name || '—'}</span>
    <span style="font-size:12px;color:#64748b;">${code}</span>
    <span class="modal-segment-count">${cell.count} гостей</span>
  `;

  // Экран формы, экран результата спрятан.
  document.getElementById('reward-form').style.display   = '';
  document.getElementById('reward-footer').style.display = '';
  document.getElementById('reward-result').style.display = 'none';
  document.getElementById('reward-result').innerHTML     = '';

  const btn = document.getElementById('btn-reward-submit');
  btn.disabled = false;
  _setRewardStatus('', '');

  document.getElementById('reward-points-amount').value  = '100';
  document.getElementById('reward-lifetime-days').value  = '';
  document.getElementById('reward-holdout').value        = '10';
  document.getElementById('reward-comment').value        = '';
  document.getElementById('reward-name').value           = '';

  setRewardType('gift');
  _loadRewardCatalog();

  document.getElementById('reward-modal').classList.add('active');
}

function closeRewardModal() {
  document.getElementById('reward-modal').classList.remove('active');
  _rewardCell     = null;
  _rewardCellKey  = null;
  _rewardCampaign = null;
  _setRewardStatus('', '');
}

function setRewardType(type) {
  _rewardType = (type === 'points') ? 'points' : 'gift';
  const isGift = _rewardType === 'gift';

  const btnGift   = document.getElementById('reward-type-gift');
  const btnPoints = document.getElementById('reward-type-points');
  const on  = 'background:#7c3aed;border-color:#7c3aed;color:#fff;';
  const off = 'background:#fff;border-color:#cbd5e1;color:#334155;';
  btnGift.style.cssText   = 'flex:1;padding:9px;border:1px solid;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer;' + (isGift ? on : off);
  btnPoints.style.cssText = 'flex:1;padding:9px;border:1px solid;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer;' + (isGift ? off : on);

  document.getElementById('reward-gift-block').style.display     = isGift ? '' : 'none';
  document.getElementById('reward-lifetime-block').style.display = isGift ? '' : 'none';
  document.getElementById('reward-points-block').style.display   = isGift ? 'none' : '';

  _refreshRewardName();
  renderRewardPreview();
}

function _loadRewardCatalog() {
  const sel = document.getElementById('reward-catalog-select');
  if (_rewardCatalog) { _renderRewardCatalogSelect(); return; }

  sel.innerHTML = '<option value="">Загрузка каталога...</option>';
  sel.disabled = true;

  fetch('/api/v1/analytics/rf/reward-catalog/')
    .then(r => r.json())
    .then(data => {
      _rewardCatalog = data.items || [];
      _renderRewardCatalogSelect();
    })
    .catch(() => {
      _rewardCatalog = null;
      sel.innerHTML = '<option value="">Ошибка загрузки каталога</option>';
      sel.disabled = true;
      const note = document.getElementById('reward-catalog-note');
      note.textContent = 'Не удалось загрузить каталог наград — обновите страницу и попробуйте снова.';
      note.style.display = '';
      renderRewardPreview();
    });
}

function _rewardItemLabel(it) {
  const parts = [it.name];
  if (it.tier) parts.push(it.tier);
  parts.push(`${_rewardMoney(it.cost_price)} ₽`);
  if (it.default_lifetime_days) parts.push(`срок ${it.default_lifetime_days} дн`);
  return parts.join(' · ');
}

function _renderRewardCatalogSelect() {
  const sel   = document.getElementById('reward-catalog-select');
  const note  = document.getElementById('reward-catalog-note');
  const items = _rewardCatalog || [];

  if (!items.length) {
    sel.innerHTML = '<option value="">—</option>';
    sel.disabled  = true;
    note.textContent = 'Каталог наград пуст — заведите позиции в админке (Инвентарь → Каталог наград).';
    note.style.display = '';
  } else {
    sel.innerHTML = items
      .map(it => `<option value="${it.id}">${_escapeHtml(_rewardItemLabel(it))}</option>`)
      .join('');
    sel.disabled = false;
    note.style.display = 'none';
  }
  onRewardCatalogChange();
}

function _rewardSelectedItem() {
  const items = _rewardCatalog || [];
  if (!items.length) return null;
  const id = parseInt(document.getElementById('reward-catalog-select').value, 10);
  return items.find(it => it.id === id) || null;
}

function onRewardCatalogChange() {
  const it   = _rewardSelectedItem();
  const meta = document.getElementById('reward-catalog-meta');
  const life = document.getElementById('reward-lifetime-days');

  if (it) {
    // Плейсхолдер срока = срок позиции: пустое поле — берётся он.
    life.placeholder = it.default_lifetime_days ? String(it.default_lifetime_days) : '';
    const bits = [];
    if (it.branch) bits.push(`точка: ${_escapeHtml(it.branch)}`);
    if (it.min_order_amount) bits.push(`мин. заказ ${_rewardMoney(it.min_order_amount)} ₽`);
    if (it.remaining_issues !== null && it.remaining_issues !== undefined) {
      bits.push(`остаток выдач: ${it.remaining_issues}`);
    }
    meta.innerHTML = bits.join(' · ');
  } else {
    life.placeholder = '';
    meta.innerHTML = '';
  }

  _refreshRewardName();
  renderRewardPreview();
}

function onRewardNameInput() {
  _rewardNameTouched = true;
}

function _rewardDefaultName() {
  const seg = (_rewardCell && _rewardCell.segment_name) || 'Сегмент';
  let reward;
  if (_rewardType === 'points') {
    reward = `${_rewardPointsAmount()} баллов`;
  } else {
    const it = _rewardSelectedItem();
    reward = it ? it.name : 'Подарок';
  }
  const d = new Date();
  const dd = String(d.getDate()).padStart(2, '0');
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  return `RFM / ${seg} / ${reward} / ${dd}.${mm}.${d.getFullYear()}`;
}

function _refreshRewardName() {
  // Автозаполнение живёт до первой ручной правки — потом не перетираем.
  if (_rewardNameTouched) return;
  document.getElementById('reward-name').value = _rewardDefaultName();
}

function _rewardPointsAmount() {
  const n = parseInt(document.getElementById('reward-points-amount').value, 10);
  return isNaN(n) ? 0 : n;
}

function _rewardHoldoutPercent() {
  let n = parseInt(document.getElementById('reward-holdout').value, 10);
  if (isNaN(n)) n = 0;
  // Бэкенд режет процент в диапазон 0..50 — показываем то же, что произойдёт.
  return Math.max(0, Math.min(n, 50));
}

function _rewardLifetimeDays() {
  const raw = (document.getElementById('reward-lifetime-days').value || '').trim();
  if (!raw) return null;
  const n = parseInt(raw, 10);
  return isNaN(n) ? null : n;
}

function renderRewardPreview() {
  const box = document.getElementById('reward-preview');
  if (!box || !_rewardCell) return;

  // Автоназвание зависит от суммы баллов / позиции — держим его актуальным
  // на каждой перерисовке (до первой ручной правки поля).
  _refreshRewardName();

  const total = _rewardCell.count || 0;
  const pct   = _rewardHoldoutPercent();
  let control = Math.round(total * pct / 100);
  // Контрольная группа осмысленна только если обе группы непусты (как в бэке).
  if (control >= total) control = 0;
  const receive = Math.max(total - control, 0);

  let costRow = '';
  if (_rewardType === 'gift') {
    const it = _rewardSelectedItem();
    if (it) {
      costRow = `
        <div style="display:flex;justify-content:space-between;gap:10px;">
          <span>Потенциальная себестоимость</span>
          <b>${_rewardMoney(receive * Number(it.cost_price || 0))} ₽</b>
        </div>`;
    }
  } else {
    costRow = `
      <div style="display:flex;justify-content:space-between;gap:10px;">
        <span>Всего баллов</span>
        <b>${(receive * _rewardPointsAmount()).toLocaleString('ru-RU')}</b>
      </div>`;
  }

  box.innerHTML = `
    <div style="font-size:12px;font-weight:700;text-transform:uppercase;color:#94a3b8;letter-spacing:.5px;margin-bottom:6px;">Предпросмотр</div>
    <div style="display:flex;justify-content:space-between;gap:10px;">
      <span>Аудитория ячейки</span><b>${total}</b>
    </div>
    <div style="display:flex;justify-content:space-between;gap:10px;">
      <span>Контрольная группа (${pct}%)</span><b>~${control}</b>
    </div>
    <div style="display:flex;justify-content:space-between;gap:10px;">
      <span>Получат награду</span><b style="color:#7c3aed;">~${receive}</b>
    </div>
    ${costRow}
  `;

  const btn = document.getElementById('btn-reward-submit');
  const noCatalog = _rewardType === 'gift' && !(_rewardCatalog && _rewardCatalog.length);
  btn.disabled = noCatalog || receive <= 0;
  btn.textContent = noCatalog ? '🎁 Каталог наград пуст' : `🎁 Назначить ${receive} гостям`;
}

function submitRewardCampaign() {
  if (!_rewardCell || !_rewardCell.segment_id) return;

  const holdout = _rewardHoldoutPercent();
  const payload = {
    segment_id:      _rewardCell.segment_id,
    mode:            ACTIVE_MODE,
    branch_ids:      BRANCH_PARAM ? BRANCH_PARAM.replace('branches=', '') : '',
    reward_type:     _rewardType,
    holdout_percent: holdout,
    name:            (document.getElementById('reward-name').value || '').trim(),
    comment:         (document.getElementById('reward-comment').value || '').trim(),
  };

  // Контекст ячейки — ровно как в рассылке по сегменту: режим/область/период
  // + expected_count, чтобы бэкенд отбил изменившуюся аудиторию (409).
  if (_rewardCell.r_score) payload.r_score = _rewardCell.r_score;
  if (_rewardCell.f_score) payload.f_score = _rewardCell.f_score;
  if (typeof _rewardCell.count === 'number') payload.expected_count = _rewardCell.count;
  if (typeof PERIOD_START !== 'undefined' && PERIOD_START) payload.start = PERIOD_START;
  if (typeof PERIOD_END !== 'undefined' && PERIOD_END) payload.end = PERIOD_END;

  if (_rewardType === 'gift') {
    const it = _rewardSelectedItem();
    if (!it) { _setRewardStatus('Выберите позицию каталога наград', 'error'); return; }
    payload.catalog_item_id = it.id;
    const life = _rewardLifetimeDays();
    if (life !== null) {
      if (life <= 0) { _setRewardStatus('Срок действия должен быть больше нуля', 'error'); return; }
      payload.lifetime_days = life;
    }
  } else {
    const pts = _rewardPointsAmount();
    if (pts <= 0) { _setRewardStatus('Количество баллов должно быть больше нуля', 'error'); return; }
    payload.points_amount = pts;
  }

  const total   = _rewardCell.count || 0;
  let control   = Math.round(total * holdout / 100);
  if (control >= total) control = 0;
  const receive = Math.max(total - control, 0);
  const segName = ((_rewardCell.segment_emoji || '') + ' ' + (_rewardCell.segment_name || '')).trim();
  if (!confirm(`Назначить награду сегменту «${segName}»? Получат ~${receive} гостей, контрольная группа ~${control}.`)) return;

  const btn = document.getElementById('btn-reward-submit');
  const oldLabel = btn.textContent;
  btn.disabled = true;
  btn.textContent = '⏳ Назначаем...';
  _setRewardStatus('Создаём кампанию и фиксируем аудиторию...', 'loading');

  fetch('/api/v1/analytics/rf/campaigns/', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCookie('csrftoken') },
    body: JSON.stringify(payload),
  })
    .then(r => r.json())
    .then(data => {
      if (data.error) {
        // 400 / 403 / 409 — текст из {"error": ...}; 409 = аудитория изменилась.
        _setRewardStatus('Ошибка: ' + data.error, 'error');
        btn.disabled = false;
        btn.textContent = oldLabel;
        return;
      }
      _rewardCampaign = data.campaign || null;
      _setRewardStatus('', '');
      _renderRewardResult();
      // История ниже по странице — подтянем, если она раскрыта.
      if (document.getElementById('rfm-campaigns-body').style.display !== 'none') {
        loadRFMCampaigns();
      }
    })
    .catch(() => {
      _setRewardStatus('Ошибка соединения', 'error');
      btn.disabled = false;
      btn.textContent = oldLabel;
    });
}

function _renderRewardResult() {
  const c = _rewardCampaign || {};
  const links = c.links || [];

  document.getElementById('reward-form').style.display   = 'none';
  document.getElementById('reward-footer').style.display = 'none';
  document.getElementById('reward-modal-title').textContent = '✅ Кампания создана';

  const linksList = links.length > 1 ? `
    <div style="margin-top:10px;border-top:1px solid #e2e8f0;padding-top:10px;">
      <div style="font-size:12px;font-weight:700;color:#94a3b8;text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px;">Ссылки по точкам</div>
      ${links.map((l, i) => `
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;">
          <span style="flex:1;font-size:12px;color:#334155;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${_escapeHtml(l.branch || '')}</span>
          <button type="button" onclick="rewardCopyLink(${i}, this)"
                  style="font-size:12px;padding:5px 10px;border:1px solid #cbd5e1;border-radius:6px;background:#fff;cursor:pointer;color:#334155;font-weight:600;white-space:nowrap;">
            🔗 Копировать
          </button>
        </div>`).join('')}
    </div>` : '';

  document.getElementById('reward-result').innerHTML = `
    <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;padding:14px 16px;margin-bottom:14px;">
      <div style="font-size:15px;font-weight:800;color:#166534;">🎉 Кампания создана, начисление идёт в фоне</div>
      <div style="font-size:13px;color:#166534;margin-top:6px;line-height:1.45;">
        ${_escapeHtml(c.name || '')}<br>
        Аудитория <b>${c.audience_total || 0}</b> · контрольная группа <b>${c.control_count || 0}</b>.
        Прогресс — в блоке «История RFM-кампаний» под матрицей.
      </div>
    </div>

    <div style="display:flex;gap:8px;flex-wrap:wrap;">
      <button type="button" onclick="rewardGoToBroadcast()"
              style="flex:1;min-width:170px;padding:11px;border:none;border-radius:8px;background:#4a76a8;color:#fff;font-size:14px;font-weight:700;cursor:pointer;">
        📨 Перейти к рассылке
      </button>
      <button type="button" id="btn-reward-copy" onclick="rewardCopyLink(0, this)"
              ${links.length ? '' : 'disabled'}
              style="flex:1;min-width:170px;padding:11px;border:1px solid #cbd5e1;border-radius:8px;background:#fff;color:#334155;font-size:14px;font-weight:700;cursor:pointer;${links.length ? '' : 'opacity:.5;cursor:not-allowed;'}">
        🔗 Скопировать ссылку
      </button>
    </div>
    ${linksList}

    <div style="margin-top:14px;text-align:center;">
      <a href="#" onclick="event.preventDefault(); closeRewardModal();" style="font-size:13px;color:#64748b;">Закрыть</a>
    </div>
  `;
  document.getElementById('reward-result').style.display = '';
}

function rewardGoToBroadcast() {
  // Открываем СУЩЕСТВУЮЩУЮ модалку рассылки этой ячейки; отправка добавит
  // campaign_id — бэкенд разошлёт ровно по snapshot кампании.
  const key  = _rewardCellKey;
  const camp = _rewardCampaign;
  if (!key || !camp) return;
  closeRewardModal();
  openBroadcastModal(key, camp.id, camp.name);
}

function rewardCopyLink(index, btn) {
  const links = (_rewardCampaign && _rewardCampaign.links) || [];
  const link  = links[index || 0];
  if (!link || !link.url) {
    _setRewardStatus('Ссылка недоступна — нет активных точек у кампании', 'error');
    return;
  }
  _rfCopyText(link.url, btn);
}

function _rfCopyText(text, btn) {
  const done = () => {
    if (!btn) return;
    const old = btn.textContent;
    btn.textContent = '✅ Скопировано';
    setTimeout(() => { btn.textContent = old; }, 1800);
  };
  const fallback = () => {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.cssText = 'position:fixed;top:-1000px;left:-1000px;';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); done(); }
    catch (e) { _setRewardStatus('Не удалось скопировать: ' + text, 'error'); }
    document.body.removeChild(ta);
  };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(done).catch(fallback);
  } else {
    fallback();
  }
}

document.getElementById('reward-modal').addEventListener('click', function(e) {
  if (e.target === this) closeRewardModal();
});

document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') closeRewardModal();
});

// ── История RFM-кампаний (ТЗ «RFM-награды» §9) ─────────────────────
const _RFM_STATUS_COLORS = {
  processing:       '#f59e0b',
  completed:        '#16a34a',
  partially_failed: '#dc2626',
  cancelled:        '#94a3b8',
};
// Отменять есть смысл, пока награды у гостей: отменённую повторно не трогаем.
const _RFM_CANCELLABLE = ['processing', 'completed', 'partially_failed'];

let _rfmCampaignsLoaded = false;

function toggleRFMCampaigns() {
  const body   = document.getElementById('rfm-campaigns-body');
  const toggle = document.getElementById('rfm-campaigns-toggle');
  const open   = body.style.display === 'none';
  body.style.display = open ? '' : 'none';
  toggle.textContent = open ? 'Свернуть ▴' : 'Показать ▾';
  if (open && !_rfmCampaignsLoaded) loadRFMCampaigns();
}

function _rfmDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '—';
  const p = n => String(n).padStart(2, '0');
  return `${p(d.getDate())}.${p(d.getMonth() + 1)}.${d.getFullYear()} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function loadRFMCampaigns() {
  const body = document.getElementById('rfm-campaigns-body');
  body.innerHTML = '<div class="empty-state" style="padding:24px;"><div class="icon">⏳</div>Загрузка...</div>';

  fetch('/api/v1/analytics/rf/campaigns/')
    .then(r => r.json())
    .then(data => {
      _rfmCampaignsLoaded = true;
      _renderRFMCampaigns(data.campaigns || []);
    })
    .catch(() => {
      body.innerHTML = '<div class="empty-state" style="padding:24px;"><div class="icon">⚠️</div>Ошибка загрузки истории кампаний</div>';
    });
}

function _renderRFMCampaigns(list) {
  const body = document.getElementById('rfm-campaigns-body');
  if (!list.length) {
    body.innerHTML = '<div class="empty-state" style="padding:24px;"><div class="icon">🎁</div>Кампаний ещё не было — назначьте награду из ячейки матрицы</div>';
    return;
  }

  const rows = list.map(c => {
    const color = _RFM_STATUS_COLORS[c.status] || '#94a3b8';
    const cancelBtn = _RFM_CANCELLABLE.indexOf(c.status) !== -1
      ? `<button type="button" onclick="cancelRFMCampaign(${c.id})"
                 style="font-size:12px;padding:5px 10px;border:1px solid #fecaca;border-radius:6px;background:#fff;color:#b91c1c;cursor:pointer;font-weight:600;white-space:nowrap;">
           Отменить
         </button>`
      : '<span style="color:#cbd5e1;font-size:12px;">—</span>';

    return `
      <tr>
        <td style="width:26px;">
          <a href="#" onclick="event.preventDefault(); toggleRFMCampaignDetail(${c.id});"
             id="rfm-camp-chev-${c.id}" title="Детали"
             style="color:#94a3b8;text-decoration:none;font-size:13px;">▸</a>
        </td>
        <td style="white-space:nowrap;color:#64748b;font-size:12px;">${_rfmDate(c.created_at)}</td>
        <td><div class="guest-name">${_escapeHtml(c.name || '')}</div>
            <div class="guest-vk-id">${_escapeHtml(c.created_by || '')}</div></td>
        <td style="font-size:12px;">${_escapeHtml(c.segment_label || '')}</td>
        <td style="font-size:12px;">${_escapeHtml(c.reward_label || '')}</td>
        <td style="font-weight:700;">${c.audience_total || 0}</td>
        <td style="font-size:12px;white-space:nowrap;">
          <span style="color:#16a34a;font-weight:700;">${c.assigned_count || 0}</span> /
          <span style="color:#94a3b8;">${c.skipped_count || 0}</span> /
          <span style="color:#dc2626;">${c.failed_count || 0}</span> /
          <span style="color:#7c3aed;">${c.control_count || 0}</span>
        </td>
        <td><span class="guest-badge" style="background:${color};">${_escapeHtml(c.status_label || c.status || '')}</span></td>
        <td>${cancelBtn}</td>
      </tr>
      <tr id="rfm-camp-det-${c.id}" style="display:none;">
        <td colspan="9" style="background:#f8fafc;">
          <div id="rfm-camp-det-body-${c.id}" style="padding:10px 4px;font-size:13px;color:#334155;">Загрузка...</div>
        </td>
      </tr>
    `;
  }).join('');

  body.innerHTML = `
    <div style="overflow-x:auto;">
      <table class="guest-table">
        <thead>
          <tr>
            <th></th>
            <th>Дата</th>
            <th>Название</th>
            <th>Ячейка</th>
            <th>Награда</th>
            <th>Аудитория</th>
            <th title="назначено / пропущено / ошибок / контроль">Назн. / проп. / ош. / контр.</th>
            <th>Статус</th>
            <th></th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
  `;
}

function toggleRFMCampaignDetail(id) {
  const row  = document.getElementById(`rfm-camp-det-${id}`);
  const chev = document.getElementById(`rfm-camp-chev-${id}`);
  if (!row) return;
  const open = row.style.display === 'none';
  row.style.display = open ? '' : 'none';
  if (chev) chev.textContent = open ? '▾' : '▸';
  if (!open) return;

  const box = document.getElementById(`rfm-camp-det-body-${id}`);
  box.textContent = 'Загрузка...';

  fetch(`/api/v1/analytics/rf/campaigns/${id}/`)
    .then(r => r.json())
    .then(c => {
      if (c.error) { box.textContent = 'Ошибка: ' + c.error; return; }

      const f = c.gift_funnel;
      const funnel = f ? `
        <div style="display:flex;flex-wrap:wrap;gap:16px;margin-bottom:8px;">
          <span>Назначено: <b>${f.assigned || 0}</b></span>
          <span style="color:#16a34a;">Активировали: <b>${f.activated || 0}</b></span>
          <span style="color:#94a3b8;">Ждут: <b>${f.waiting || 0}</b></span>
          <span style="color:#dc2626;">Не забрали (сгорело): <b>${f.claim_expired || 0}</b></span>
        </div>` : '';

      const links = (c.links || []).map((l, i) =>
        `<div style="margin-top:4px;font-size:12px;color:#64748b;">${_escapeHtml(l.branch || '')}: <span style="color:#1565c0;">${_escapeHtml(l.url || '')}</span></div>`
      ).join('');

      const meta = [];
      if (c.holdout_percent) meta.push(`контрольная группа ${c.holdout_percent}%`);
      if (c.lifetime_days)   meta.push(`срок ${c.lifetime_days} дн`);
      if (c.finished_at)     meta.push(`завершена ${_rfmDate(c.finished_at)}`);

      box.innerHTML = `
        ${funnel}
        ${meta.length ? `<div style="color:#64748b;font-size:12px;">${_escapeHtml(meta.join(' · '))}</div>` : ''}
        ${c.comment ? `<div style="margin-top:6px;">💬 ${_escapeHtml(c.comment)}</div>` : ''}
        ${links}
      `;
    })
    .catch(() => { box.textContent = 'Ошибка соединения'; });
}

function cancelRFMCampaign(id) {
  if (!confirm('Отменить кампанию?\n\nНеактивированные подарки будут отозваны, баллы откатятся в пределах остатка.')) return;

  fetch(`/api/v1/analytics/rf/campaigns/${id}/cancel/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCookie('csrftoken') },
    body: '{}',
  })
    .then(r => r.json())
    .then(data => {
      if (data.error) { alert('Ошибка: ' + data.error); return; }
      const bits = [];
      if (data.revoked)  bits.push(`отозвано подарков: ${data.revoked}`);
      if (data.refunded) bits.push(`откачено баллов у ${data.refunded} гостей`);
      if (data.kept)     bits.push(`осталось у гостей: ${data.kept}`);
      alert('Кампания отменена' + (bits.length ? ` (${bits.join(', ')})` : ''));
      loadRFMCampaigns();
    })
    .catch(() => alert('Ошибка соединения'));
}
