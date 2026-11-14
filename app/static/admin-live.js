/* Light live updates for the admin order list (every ~9s) and order detail (every ~5s).
   Read-only: only GETs a display-only JSON. Never touches inputs, never reloads, pauses while the tab is hidden. */
(function () {
  'use strict';
  var setText = function (el, t) { if (el && el.textContent !== t) el.textContent = t; };
  var setTone = function (el, tone) {
    if (!el) return;
    el.className = el.className.replace(/\b(ok|bad|warn|wait|off)\b/g, '').replace(/\s+/g, ' ').trim() + ' ' + tone;
  };
  function poller(fn, base) {
    var delay = base, timer = null, running = false;
    function schedule() { clearTimeout(timer); timer = setTimeout(tick, delay); }
    function tick() {
      if (document.visibilityState !== 'visible') { schedule(); return; }          // hidden tab: wait, do not fetch
      if (running) { schedule(); return; }
      running = true;
      fn().then(function () { delay = base; }, function () { delay = Math.min(delay * 2, 60000); })
        .then(function () { running = false; schedule(); });
    }
    document.addEventListener('visibilitychange', function () { if (document.visibilityState === 'visible') { clearTimeout(timer); timer = setTimeout(tick, 600); } });
    schedule();
  }
  function getJSON(url) {
    return fetch(url, { credentials: 'same-origin', headers: { 'Accept': 'application/json' }, cache: 'no-store' }).then(function (r) {
      if (!r.ok) throw new Error('http'); return r.json();
    });
  }

  /* ---------- order list ---------- */
  var table = document.getElementById('ord-table');
  if (table) {
    var note = document.getElementById('live-note'), base = +(note && note.dataset.interval) || 9000;
    poller(function () {
      var rows = Array.prototype.slice.call(table.querySelectorAll('tbody tr[data-oid]'));
      if (!rows.length) return Promise.resolve();
      return getJSON('/admin/api/orders/live?ids=' + rows.map(function (r) { return r.dataset.oid; }).join(',')).then(function (d) {
        rows.forEach(function (tr) {
          var v = d.rows && d.rows[tr.dataset.oid]; if (!v) return;
          var pay = tr.querySelector('[data-live=pay]'), st = tr.querySelector('[data-live=stage]'), why = tr.querySelector('[data-live=reason]'), open = tr.querySelector('[data-live=open]');
          setText(pay, v.payment_text); setTone(pay, v.payment_tone);
          setText(st, v.stage_text); setTone(st, v.tone);
          if (why) { setText(why, v.reason); why.hidden = !v.reason; }
          tr.classList.toggle('attn', !!v.needs_attention);
          if (open) { setText(open, v.needs_attention ? '去处理' : '查看'); open.classList.toggle('primary', !!v.needs_attention); }
        });
        var at = document.getElementById('attn-total');
        if (at) { at.hidden = !d.attention_total; at.textContent = d.attention_total ? '· 全部订单中有 ' + d.attention_total + ' 笔需要人工处理' : ''; }
      });
    }, base);
  }

  /* ---------- order detail ---------- */
  var live = document.getElementById('live');
  if (live) {
    var oid = live.dataset.oid, needs = live.dataset.needs === '1';
    poller(function () {
      return getJSON('/admin/api/orders/' + oid + '/live').then(function (d) {
        var ol = document.getElementById('stages'), html = '';
        var sig = JSON.stringify([d.steps, d.terminal]);
        if (ol.dataset.sig !== sig) {                       // rebuild only when the real stage changed (no flicker)
          ol.innerHTML = '';
          d.steps.forEach(function (s) { var li = document.createElement('li'); li.className = s.state; li.textContent = s.text; ol.appendChild(li); });
          if (d.terminal) { var t = document.createElement('li'); t.className = 'terminal ' + d.terminal.tone; t.textContent = d.terminal.text; ol.appendChild(t); }
          ol.dataset.sig = sig;
        }
        var pay = live.querySelector('[data-live=pay]'); setText(pay, d.payment.text); setTone(pay, d.payment.tone);
        setText(live.querySelector('[data-live=worker]'), d.worker_text);
        var up = live.querySelector('[data-live=upstream]'); if (up) setText(up, d.upstream_text);
        var st = live.querySelector('[data-live=stage]'); setText(st, d.stage.text); setTone(st, d.stage.tone);
        setText(document.querySelector('#headline-card [data-live=headline]'), d.headline);
        var d0 = new Date(); setText(document.getElementById('live-time'), '已自动更新 ' + ('0' + d0.getHours()).slice(-2) + ':' + ('0' + d0.getMinutes()).slice(-2) + ':' + ('0' + d0.getSeconds()).slice(-2));
        if (d.needs_action !== needs) { document.getElementById('live-changed').hidden = false; }
        if (d.stage.key === 'done' || d.stage.key === 'closed') { live.dataset.final = '1'; }
      });
    }, +live.dataset.interval || 5000);
  }
})();
