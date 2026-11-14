/* Pricing table: plan tabs, row table, side drawer, in-place saving (no page reload, scroll position kept). */
(function () {
  'use strict';
  var root = document.getElementById('pricing');
  if (!root) return;
  var csrf = (document.querySelector('meta[name=csrf-token]') || {}).content || '';
  var drawer = document.getElementById('pp-drawer'), backdrop = document.getElementById('pp-backdrop');
  var openId = null, lastBtn = null;
  var $ = function (s, r) { return (r || root).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || root).querySelectorAll(s)); };

  /* ---------- tabs ---------- */
  function showPlan(id, push) {
    $$('.pp-tab').forEach(function (t) { var on = t.dataset.plan === id; t.classList.toggle('on', on); t.setAttribute('aria-selected', on ? 'true' : 'false'); });
    $$('.pp-panel').forEach(function (p) { p.hidden = p.dataset.plan !== id; });
    if (push !== false) { try { history.replaceState(null, '', '?plan=' + encodeURIComponent(id) + (openId ? '&edit=' + openId : '')); } catch (e) {} }
  }
  $$('.pp-tab').forEach(function (t) { t.addEventListener('click', function () { closeDrawer(); showPlan(t.dataset.plan); }); });

  /* ---------- selection ---------- */
  function panelOf(el) { return el.closest('.pp-panel'); }
  function updateCount(panel) {
    var n = $$('input[name=variant_ids]:checked', panel).length;
    var count = $('[data-count]', panel);
    if (!count) return;                       // a plan can legitimately have no regions before the first sync
    count.textContent = n;
    var all = $('[data-all]', panel), boxes = $$('input[name=variant_ids]', panel);
    if (!all) return;
    all.checked = n > 0 && n === boxes.length; all.indeterminate = n > 0 && n < boxes.length;
  }
  root.addEventListener('change', function (e) {
    if (e.target.matches('[data-all]')) {
      var p = panelOf(e.target); $$('input[name=variant_ids]', p).forEach(function (b) { b.checked = e.target.checked; }); updateCount(p);
    } else if (e.target.name === 'variant_ids') { updateCount(panelOf(e.target)); }
  });

  /* ---------- drawer ---------- */
  function openDrawer(vid, btn) {
    var dr = document.getElementById('dr-' + vid); if (!dr) return;
    $$('.pp-dr').forEach(function (d) { d.hidden = d !== dr; });
    $$('.pp-table tr.editing').forEach(function (r) { r.classList.remove('editing'); });
    var row = document.getElementById('row-' + vid); if (row) row.classList.add('editing');
    showPlan(dr.dataset.plan, false);
    drawer.hidden = false; backdrop.hidden = false; openId = vid; lastBtn = btn || lastBtn;
    refreshForm($('form[data-save]', dr));
    try { history.replaceState(null, '', '?plan=' + encodeURIComponent(dr.dataset.plan) + '&edit=' + vid); } catch (e) {}
    var f = $('input:not([disabled])[type=text], input.t', dr); if (f) setTimeout(function () { try { f.focus({ preventScroll: true }); } catch (e) {} }, 30);
  }
  function closeDrawer() {
    if (drawer.hidden) return;
    drawer.hidden = true; backdrop.hidden = true;
    $$('.pp-table tr.editing').forEach(function (r) { r.classList.remove('editing'); });
    var plan = ($$('.pp-tab.on')[0] || {}).dataset; openId = null;
    try { history.replaceState(null, '', '?plan=' + encodeURIComponent(plan ? plan.plan : '')); } catch (e) {}
    if (lastBtn && lastBtn.focus) { try { lastBtn.focus({ preventScroll: true }); } catch (e) {} }
  }
  root.addEventListener('click', function (e) {
    var ed = e.target.closest('[data-edit]'); if (ed) { openDrawer(ed.dataset.edit, ed); return; }
    if (e.target.closest('[data-close]')) closeDrawer();
  });
  backdrop.addEventListener('click', closeDrawer);
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && !drawer.hidden) { e.preventDefault(); closeDrawer(); } });

  /* ---------- edit form: show only the chosen pricing method; say why Save is unavailable ---------- */
  function val(form, name) { var el = form.querySelector('[name=' + name + ']'); return el ? el.value.trim() : ''; }
  function refreshForm(form) {
    if (!form) return;
    var src = (form.querySelector('[name=cost_source]:checked') || {}).value || 'manual';
    var mode = (form.querySelector('[name=pricing_mode]:checked') || {}).value || 'manual_retail';
    var hasCost = src === 'manual' ? val(form, 'manual_package_cost') !== '' : form.dataset.upOk === '1';
    var sub = form.querySelector('[data-when-source=manual]'); if (sub) sub.hidden = src !== 'manual';
    form.querySelector('[data-nocost]').hidden = hasCost;
    Array.prototype.forEach.call(form.querySelectorAll('.pp-pane'), function (p) {
      var on = p.dataset.pane === mode; p.hidden = !on;
      Array.prototype.forEach.call(p.querySelectorAll('input,select'), function (i) { i.disabled = !on; });   // hidden methods are never submitted
    });
    Array.prototype.forEach.call(form.querySelectorAll('.pp-mode'), function (m) { m.classList.toggle('on', m.querySelector('input').checked); });
    var need = (mode === 'percentage' || mode === 'fixed') && !hasCost;
    var err = form.querySelector('[data-needcost]'); err.hidden = !need;
    var retailEmpty = mode === 'manual_retail' && val(form, 'manual_sale_price') === '';
    var btn = form.querySelector('[data-savebtn]');
    btn.disabled = need || retailEmpty;
    btn.title = need ? '请先填写套餐成本' : (retailEmpty ? '请先填写零售价' : '');
  }
  root.addEventListener('input', function (e) { var f = e.target.closest('form[data-save]'); if (f) { refreshForm(f); clearResult(f); } });
  root.addEventListener('change', function (e) { var f = e.target.closest('form[data-save]'); if (f) { refreshForm(f); clearResult(f); } });
  function clearResult(form) { var r = form.querySelector('[data-result]'); if (r) { r.textContent = ''; r.className = 'pp-result'; } }
  $$('form[data-save]').forEach(refreshForm);

  /* ---------- in-place saving ---------- */
  function setText(el, t) { if (el && el.textContent !== t) el.textContent = t; }
  function applyVariant(v) {
    var row = document.getElementById('row-' + v.id);
    var dr = document.getElementById('dr-' + v.id);
    [row, dr].forEach(function (scope) {
      if (!scope) return;
      ['package_cost', 'other_cost', 'total_cost', 'price', 'profit'].forEach(function (k) { setText(scope.querySelector('[data-k=' + k + ']'), v[k]); });
    });
    if (row) {
      setText(row.querySelector('[data-k=mode_text]'), v.mode_text);
      setText(row.querySelector('[data-k=cost_note]'), v.cost_note);
      var st = row.querySelector('[data-k=status]'); setText(st, v.status); st.classList.toggle('on', v.enabled);
      row.classList.toggle('on', v.enabled);
      row.querySelector('[data-k=package_cost]').classList.toggle('dim', !v.has_cost);
      var pf = row.querySelector('[data-k=profit]'); pf.classList.toggle('bad', v.loss); pf.classList.toggle('dim', v.profit.indexOf('暂无') === 0);
      row.querySelector('[data-k=enabled]').value = v.enabled ? '0' : '1';
      var tb = row.querySelector('[data-k=toggle]');
      tb.textContent = v.enabled ? '关闭销售' : '上架销售'; tb.className = 'btn sm ' + (v.enabled ? 'danger' : 'primary');
      if (v.enabled) tb.setAttribute('data-confirm', '关闭后，新用户将无法选择该地区，历史订单不受影响。确定关闭吗？'); else tb.removeAttribute('data-confirm');
    }
    if (dr) {   // normalise what the form shows to what was really saved
      var f = dr.querySelector('form[data-save]'), x = v.fields;
      f.dataset.upOk = v.up_ok ? '1' : '0';
      var set = function (n, val) { var el = f.querySelector('[name=' + n + ']'); if (el) el.value = val; };
      set('manual_package_cost', x.manual_cost); set('other_cost', x.other_cost); set('markup_percent', x.markup); set('fixed_profit', x.fixed_profit);
      set('manual_sale_price', x.retail);
      Array.prototype.forEach.call(f.querySelectorAll('[name=rounding]'), function (s) { s.value = x.rounding; });
      Array.prototype.forEach.call(f.querySelectorAll('[name=cost_source]'), function (r) { r.checked = r.value === x.cost_source; });
      Array.prototype.forEach.call(f.querySelectorAll('[name=pricing_mode]'), function (r) { r.checked = r.value === x.mode; });
      var an = dr.querySelector('[data-anomaly]'); if (an && !v.anomaly) an.remove();
      refreshForm(f);
    }
  }
  function applyPlan(panelEl, data) {
    (data.variants || []).forEach(applyVariant);
    var s = data.summary || {};
    setText(panelEl.querySelector('[data-s=live]'), s.live + ' / ' + s.total);
    setText(panelEl.querySelector('[data-s=min]'), s.min_price);
    setText(panelEl.querySelector('[data-s=strategy]'), s.strategy);
    var tab = document.querySelector('[data-live-tab="' + panelEl.dataset.plan + '"]'); if (tab) setText(tab, s.live + '/' + s.total);
  }
  function stamp() { var d = new Date(); return ('0' + d.getHours()).slice(-2) + ':' + ('0' + d.getMinutes()).slice(-2) + ':' + ('0' + d.getSeconds()).slice(-2); }

  root.addEventListener('submit', function (e) {
    var form = e.target.closest('form[data-ajax]'); if (!form) return;
    e.preventDefault();
    var sub = e.submitter || null;
    var conf = sub && sub.getAttribute('data-confirm');
    if (conf && !window.confirm(conf)) return;
    var fd = new FormData(form);
    if (sub && sub.name) fd.append(sub.name, sub.value);
    var panelEl = form.closest('.pp-panel') || document.getElementById('plan-' + (form.closest('.pp-dr') || {}).dataset.plan) || $('.pp-panel:not([hidden])');
    var msgEl = form.matches('[data-save]') ? form.querySelector('[data-result]') : (form.matches('[data-bulk]') ? form.querySelector('[data-msg]') : null);
    var btn = sub || form.querySelector('button'); var old = btn ? btn.textContent : '';
    if (btn) { btn.disabled = true; btn.textContent = '正在保存…'; }
    var show = function (ok, text) {
      var el = msgEl || (form.closest('.pp-dr') ? form.closest('.pp-dr').querySelector('[data-result]') : null) || panelEl.querySelector('[data-msg]');
      if (!el) return;
      el.textContent = (ok ? '✓ ' : '✕ ') + text + (ok ? '（' + stamp() + '）' : '');
      el.className = (el.className.split(' ')[0] || 'pp-msg') + ' ' + (ok ? 'ok' : 'bad') + ' show';
    };
    fetch(form.action, { method: 'POST', body: fd, credentials: 'same-origin', headers: { 'X-Requested-With': 'fetch', 'Accept': 'application/json', 'X-CSRF-Token': csrf } })
      .then(function (r) { return r.json().catch(function () { return { ok: false, message: '网络开小差了，请稍后重试' }; }); },
            function () { return { ok: false, message: '网络连接失败，内容没有丢，可以再点一次保存' }; })
      .then(function (d) {
        if (btn) { btn.textContent = old; btn.disabled = false; }
        if (d.variants && panelEl) applyPlan(panelEl, d);
        if (form.matches('[data-save]')) refreshForm(form);
        show(!!d.ok, d.message || (d.ok ? '已保存' : '保存失败'));
        if (d.ok && form.matches('[data-bulk]')) { $$('input[name=variant_ids]', panelEl).forEach(function (b) { b.checked = false; }); updateCount(panelEl); }
        if (d.ok) { (d.variants || []).forEach(function (v) { var r = document.getElementById('row-' + v.id); if (r && (form.closest('.pp-dr') ? form.closest('.pp-dr').dataset.vid == v.id : true)) { r.classList.remove('flash'); void r.offsetWidth; r.classList.add('flash'); } }); }
      });
  });

  /* ---------- initial state ---------- */
  $$('.pp-panel').forEach(updateCount);
  if (window.PP_OPEN) openDrawer(String(window.PP_OPEN));
})();
