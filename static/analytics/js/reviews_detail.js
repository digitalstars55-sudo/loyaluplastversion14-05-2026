function getCsrf() {
  return document.cookie.split(';').map(c => c.trim()).find(c => c.startsWith('csrftoken='))?.split('=')[1] || '';
}

function toggleReply(convId) {
  const panel = document.getElementById('reply-' + convId);
  const isHidden = panel.style.display === 'none';
  panel.style.display = isHidden ? 'block' : 'none';
  if (isHidden) {
    document.getElementById('reply-text-' + convId).focus();
    const chat = document.getElementById('chat-' + convId);
    chat.scrollTop = chat.scrollHeight;
  }
}

function appendMsg(convId, text) {
  const chat = document.getElementById('chat-' + convId);
  const now = new Date();
  const fmt = now.toLocaleDateString('ru', { day: '2-digit', month: '2-digit' })
            + ' ' + now.toTimeString().slice(0, 5);
  const div = document.createElement('div');
  div.className = 'msg admin';
  div.innerHTML = `${escHtml(text)}<div class="msg-meta">администратор · ${fmt}</div>`;
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
}

function escHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function setStatus(convId, msg, color) {
  const el = document.getElementById('reply-status-' + convId);
  el.textContent = msg;
  el.style.color = color || '#6b7280';
}

function sendReply(convId) {
  const ta   = document.getElementById('reply-text-' + convId);
  const text = ta.value.trim();
  if (!text) return;

  const btn = document.querySelector('#reply-' + convId + ' .btn-send');
  if (btn) btn.disabled = true;
  setStatus(convId, 'Отправка...', '#6b7280');

  fetch(REVIEW_REPLY_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrf() },
    body: JSON.stringify({ conv_id: convId, reply_text: text }),
  })
    .then(r => r.json())
    .then(data => {
      if (data.ok) {
        appendMsg(convId, text);
        ta.value = '';
        setStatus(convId, '✓ Отправлено', '#16a34a');
        const card = document.getElementById('card-' + convId);
        card.dataset.replied = '1';
        const header = card.querySelector('.card-header');
        if (header && !header.querySelector('.text-success')) {
          const badge = document.createElement('span');
          badge.className = 'small text-success';
          badge.textContent = '✓ отвечено';
          header.querySelector('.d-flex').appendChild(badge);
        }
        if (document.getElementById('hide-replied').checked) {
          card.style.display = 'none';
        }
      } else {
        setStatus(convId, '✗ ' + (data.error || 'Ошибка'), '#dc2626');
        if (btn) btn.disabled = false;
      }
    })
    .catch(() => {
      setStatus(convId, '✗ Сетевая ошибка', '#dc2626');
      if (btn) btn.disabled = false;
    });
}

function generateAI(convId, btn) {
  const orig = btn.textContent;
  btn.disabled = true;
  btn.textContent = '⏳ Генерация...';
  setStatus(convId, '', '');

  fetch(REVIEW_AI_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrf() },
    body: JSON.stringify({ conv_id: convId, draft: document.getElementById('reply-text-' + convId).value.trim() }),
  })
    .then(r => r.json())
    .then(data => {
      if (data.text) {
        document.getElementById('reply-text-' + convId).value = data.text;
        setStatus(convId, '✓ Текст сгенерирован', '#16a34a');
      } else {
        setStatus(convId, '✗ ' + (data.error || 'Ошибка AI'), '#dc2626');
      }
    })
    .catch(() => setStatus(convId, '✗ Сетевая ошибка', '#dc2626'))
    .finally(() => { btn.disabled = false; btn.textContent = orig; });
}

function toggleReplied() {
  const hide = document.getElementById('hide-replied').checked;
  document.querySelectorAll('.card[data-replied="1"]').forEach(card => {
    card.style.display = hide ? 'none' : '';
  });
}

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.chat-area').forEach(el => el.scrollTop = el.scrollHeight);
});

// ── Автоответ ИИ: отмена запланированной отправки ───────────────────────────
// Баннер «🤖 ИИ ответит автоматически в HH:MM · Отменить автоответ» появляется
// только у тредов с auto_send_status='scheduled' (то есть когда владелец
// включил автоотправку в настройках). После успеха баннер заменяется строкой.
function cancelAutoSend(convId, btn) {
  const banner = document.getElementById('autosend-banner-' + convId);
  const orig = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = 'Отменяем...'; }

  fetch(REVIEW_CANCEL_AUTOSEND_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrf() },
    body: JSON.stringify({ conversation_id: convId }),
  })
    .then(r => r.json().then(data => ({ status: r.status, data })))
    .then(({ status, data }) => {
      if (data.ok) {
        if (banner) {
          banner.style.background = '';
          banner.style.color = '#9ca3af';
          banner.style.fontSize = '11px';
          banner.textContent = '🤖 автоответ отменён: отменено сотрудником';
        }
        return;
      }
      if (status === 409) {
        if (banner) {
          banner.style.background = '#ecfdf5';
          banner.style.color = '#065f46';
          banner.textContent = '🤖 ИИ уже отправил ответ';
        }
        return;
      }
      if (btn) { btn.disabled = false; btn.textContent = orig; }
      alert(data.error || 'Не удалось отменить автоответ');
    })
    .catch(() => {
      if (btn) { btn.disabled = false; btn.textContent = orig; }
      alert('Сетевая ошибка');
    });
}
