(function () {
  const VIEWS = new Set(['review', 'approved', 'skipped']);

  const getView = () => {
    const m = location.pathname.match(/^\/runs\/\d+\/(review|approved|skipped)\b/);
    return m && m[1];
  };
  const getRunId = () => {
    const m = location.pathname.match(/^\/runs\/(\d+)/);
    return m ? +m[1] : null;
  };
  const LIST_IDS = ['review-list', 'approved-list', 'skipped-list'];
  const getList = () => {
    for (const id of LIST_IDS) {
      const el = document.getElementById(id);
      if (el) return el;
    }
    return null;
  };
  const rows = () => {
    const l = getList();
    return l ? Array.from(l.querySelectorAll(':scope > div[hx-get]')) : [];
  };
  const selectedIndex = () => rows().findIndex(r => r.id === 'list-selected');

  function setSelectedRow(row) {
    if (!row) return;
    const list = row.parentElement;
    if (!list || !LIST_IDS.includes(list.id)) return;
    const cur = list.querySelector('#list-selected');
    if (cur && cur !== row) cur.removeAttribute('id');
    if (row.id !== 'list-selected') row.id = 'list-selected';
  }

  function selectIndex(i) {
    const rs = rows();
    if (i < 0 || i >= rs.length) return;
    setSelectedRow(rs[i]);
    rs[i].click();
    rs[i].scrollIntoView({ block: 'nearest' });
  }

  // Capture-phase delegate: any click on a list row (including the user's own
  // clicks) moves the list-selected id so subsequent W/S/A/D act on it.
  document.addEventListener('click', e => {
    const row = e.target.closest && e.target.closest('div[hx-get]');
    if (!row) return;
    setSelectedRow(row);
  }, true);

  function isTyping(t) {
    if (!t) return false;
    if (t.isContentEditable) return true;
    return ['INPUT', 'TEXTAREA', 'SELECT'].includes(t.tagName);
  }

  const DECIDED_CLASSES = ['row-decided-approve', 'row-decided-reject'];

  function markDecided(row, status) {
    if (!row) return;
    row.classList.remove(...DECIDED_CLASSES);
    row.classList.add(status === 'APPROVED' ? 'row-decided-approve' : 'row-decided-reject');
  }
  function clearDecided(row) {
    if (!row) return;
    row.classList.remove(...DECIDED_CLASSES);
  }

  function act(status) {
    if (document.body.classList.contains('static-export')) return;
    if (!VIEWS.has(getView())) return;
    const rs = rows();
    const i = selectedIndex();
    if (i < 0) return;
    const row = rs[i];
    const url = row.getAttribute('hx-get') || '';
    const m = url.match(/\/(\d+)$/);
    if (!m) return;
    const cid = m[1];
    const runId = getRunId();
    if (runId == null) return;
    markDecided(row, status);
    fetch(`/runs/${runId}/review/${cid}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ status }).toString(),
    })
      .then(r => {
        if (!r.ok && r.status !== 204) throw new Error(`HTTP ${r.status}`);
        const next = i + 1 < rs.length ? i + 1 : i;
        if (next !== i) selectIndex(next);
      })
      .catch(err => {
        clearDecided(row);
        console.error('review-keys:', err);
        alert('Decision failed: ' + err.message);
      });
  }

  function jumpNeighbor(direction) {
    if (document.body.classList.contains('static-export')) return;
    if (!VIEWS.has(getView())) return;
    const runId = getRunId();
    if (runId == null) return;
    fetch(`/runs/${runId}/neighbor?dir=${direction}`)
      .then(r => r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))
      .then(j => {
        if (j && j.run_id != null) {
          window.location.href = `/runs/${j.run_id}/review#select-first`;
        }
      })
      .catch(err => console.error('review-keys:', err));
  }

  function selectFirstIfHinted() {
    if (location.hash !== '#select-first') return;
    if (!VIEWS.has(getView())) return;
    const rs = rows();
    if (rs.length) selectIndex(0);
    // Clear the hint so a manual refresh doesn't keep re-selecting.
    history.replaceState(null, '', location.pathname + location.search);
  }
  // Must run AFTER htmx has bound its click handlers (htmx processes on
  // DOMContentLoaded; this script is `defer`, so it executes before that).
  // window.load fires after htmx processing, so .click() on the row will
  // actually trigger the hx-get instead of just the inline visual handler.
  if (document.readyState === 'complete') {
    selectFirstIfHinted();
  } else {
    window.addEventListener('load', selectFirstIfHinted, { once: true });
  }

  document.addEventListener('keydown', e => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (isTyping(e.target)) return;
    if (document.body.classList.contains('static-export')) return;
    if (!VIEWS.has(getView())) return;
    const k = e.key.toLowerCase();
    if (k === 'w') { e.preventDefault(); selectIndex(selectedIndex() - 1); }
    else if (k === 's') { e.preventDefault(); selectIndex(selectedIndex() + 1); }
    else if (k === 'a') { e.preventDefault(); act('APPROVED'); }
    else if (k === 'd') { e.preventDefault(); act('REJECTED'); }
    else if (k === 'n') { e.preventDefault(); jumpNeighbor('prev'); }
    else if (k === 'm') { e.preventDefault(); jumpNeighbor('next'); }
  });

  window.t2ReviewKeys = { act };
})();
