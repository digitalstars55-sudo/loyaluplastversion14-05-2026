function donut(id, vals, colors) {
  const el = document.getElementById(id);
  if (!el) return;
  const total = vals.reduce((a, b) => a + b, 0);
  new Chart(el, {
    type: 'doughnut',
    data: {
      datasets: [{
        data: total ? vals : [1],
        backgroundColor: total ? colors : ['#e5e7eb'],
        borderWidth: 2,
        borderColor: '#fff',
      }],
    },
    options: {
      cutout: '68%',
      plugins: { legend: { display: false }, tooltip: { enabled: !!total } },
      animation: { duration: 600 },
    },
  });
}

document.addEventListener('DOMContentLoaded', () => {
  donut('ch-repeat',  [D.repeat_visits.repeat,        D.repeat_visits.first_time],                                ['#2e7d32', '#d1d5db']);
  donut('ch-sources', [D.sources.from_cafe,           D.sources.from_stories],                                    ['#1565c0', '#00897b']);
  donut('ch-stories', [D.vk_stories.uploaded,         D.vk_stories.not_uploaded],                                 ['#c62828', '#d1d5db']);
  donut('ch-gifts',   [D.gift_sources.free,           D.gift_sources.coins,         D.gift_sources.not_taken],    ['#0d9488', '#6a1b9a', '#d1d5db']);
  donut('ch-staff',   [D.staff_involvement.served,    D.staff_involvement.not_served],                            ['#00897b', '#d1d5db']);
  donut('ch-quality', [D.reviews_sentiment.positive,  D.reviews_sentiment.partial,  D.reviews_sentiment.negative], ['#16a34a', '#eab308', '#dc2626']);
  donut('ch-reviews', [D.reviews_ratio.left,          D.reviews_ratio.not_left],                                  ['#3b82f6', '#d1d5db']);
  donut('ch-quests',  [D.quests.completed,            D.quests.pending,             D.quests.not_entered],        ['#00bcd4', '#fb923c', '#d1d5db']);

  (function loadSlowStats() {
    const params = new URLSearchParams(window.location.search);
    const apiParams = new URLSearchParams();
    ['period', 'start', 'end'].forEach(k => { if (params.has(k)) apiParams.set(k, params.get(k)); });
    if (params.has('branches')) apiParams.set('branch_ids', params.get('branches'));
    fetch('/api/v1/analytics/stats/slow/?' + apiParams.toString())
      .then(r => r.ok ? r.json() : Promise.reject(r.status))
      .then(data => {
        const posEl  = document.getElementById('stat-pos-guests');
        const scanEl = document.getElementById('stat-scan-index');
        if (posEl) {
          posEl.textContent = data.pos_guests ?? '0';
          if (data.pos_guests > 0) posEl.classList.replace('c-grey', 'c-green');
        }
        if (scanEl) {
          scanEl.textContent = data.scan_index ? data.scan_index + '%' : '0%';
          if (data.scan_index > 0) scanEl.classList.replace('c-grey', 'c-orange');
        }
      })
      .catch(() => {
        const posEl  = document.getElementById('stat-pos-guests');
        const scanEl = document.getElementById('stat-scan-index');
        if (posEl)  posEl.textContent = '—';
        if (scanEl) scanEl.textContent = '—';
      });
  })();
});
