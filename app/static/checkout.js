/* Checkout dialog: fill in -> confirm -> pay -> result, all in one dialog. No account needed.
   Privacy: the credential text lives only in memory (never in storage / URL / console). */
(function () {
  'use strict';
  var root = document.getElementById('co');
  if (!root) return;
  var $ = function (s, r) { return (r || root).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || root).querySelectorAll(s)); };
  var csrf = (document.querySelector('meta[name=csrf-token]') || {}).content || '';
  var data = {};
  try { data = JSON.parse(document.getElementById('plans-data').textContent); } catch (e) { data = { plans: [] }; }

  var S = { plan: null, region: '', step: 1, valid: null, order: null, busy: false, timer: null, poll: null, trigger: null };
  var drafts = {};                    // per plan, memory only: { region, contact, session }

  var store = {
    get: function (k) { try { return JSON.parse(sessionStorage.getItem(k) || 'null'); } catch (e) { return null; } },
    set: function (k, v) { try { sessionStorage.setItem(k, JSON.stringify(v)); } catch (e) {} },
    del: function (k) { try { sessionStorage.removeItem(k); } catch (e) {} }
  };

  function api(method, url, body) {
    var opt = { method: method, credentials: 'same-origin', headers: { 'Accept': 'application/json', 'X-CSRF-Token': csrf } };
    if (body) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
    return fetch(url, opt).then(function (r) {
      return r.json().catch(function () { return { ok: false, message: '网络开小差了，请稍后重试' }; });
    }, function () { return { ok: false, message: '网络连接失败，请检查网络后重试' }; });
  }
  function money(c) { return Math.floor(c / 100) + '.' + ('0' + (c % 100)).slice(-2); }
  function fmtLeft(deadline) {
    var s = Math.max(0, Math.floor(deadline - Date.now() / 1000));
    return Math.floor(s / 60) + ':' + ('0' + (s % 60)).slice(-2);
  }

  /* ---------- error display ---------- */
  function clearErrors() {
    $$('.co-err').forEach(function (e) { e.hidden = true; e.textContent = ''; });
    $$('.co-input.bad').forEach(function (e) { e.classList.remove('bad'); });
  }
  function showError(field, msg) {
    var el = field && $('[data-err="' + field + '"]');
    if (el) {
      el.textContent = msg; el.hidden = false;
      var input = { contact: '#co-contact', session: '#co-session' }[field];
      if (input) { $(input).classList.add('bad'); $(input).focus(); }
    } else { var m = $('#co-error'); m.textContent = msg; m.hidden = false; }
  }

  /* ---------- credential parsing (display only; the server re-validates everything) ---------- */
  function parseSession(text) {
    var out = { email: '', token: false, acct: false };
    try {
      var d = JSON.parse(text);
      if (d && typeof d === 'object') {
        out.email = (d.user && typeof d.user.email === 'string' && /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(d.user.email)) ? d.user.email.trim() : '';
        out.token = typeof d.accessToken === 'string' && !!d.accessToken.trim();
        out.acct = !!(d.account_id || d.accountId);
      }
    } catch (e) {}
    return out;
  }
  function refreshCheck() {
    var d = drafts[S.plan.id], p = parseSession(d.session || '');
    var okAll = p.email && p.token && p.acct && /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test((d.contact || '').trim()) && !!S.region;
    var mark = function (k, ok) { $('[data-k=' + k + ']').classList.toggle('ok', !!ok); };
    mark('account', p.email); mark('valid', p.email && p.token && p.acct); mark('ready', okAll);
    var box = $('#co-account');
    box.hidden = !p.email;
    $('#co-account-email').textContent = p.email;
    renderFoot();
  }

  /* ---------- rendering ---------- */
  function setState(text, tone) { var s = $('#co-state'); s.textContent = text; s.className = 'co-state' + (tone ? ' ' + tone : ''); }
  function curPrice() {
    var r = S.plan.regions.filter(function (x) { return x.code === S.region; })[0];
    return r ? r.price : null;
  }
  function renderPrice() { var p = curPrice(); $('#co-price').textContent = p === null ? '--' : money(p); }
  function renderRegions() {
    var box = $('#co-regions'); box.innerHTML = '';
    S.plan.regions.forEach(function (r) {
      var b = document.createElement('button');
      b.type = 'button'; b.className = 'co-region'; b.setAttribute('role', 'radio');
      b.setAttribute('aria-checked', r.code === S.region ? 'true' : 'false'); b.dataset.code = r.code;
      var n = document.createElement('b'); n.textContent = r.label;
      var p = document.createElement('span'); p.textContent = '¥' + r.price_text;
      b.appendChild(n); b.appendChild(p); box.appendChild(b);
    });
  }
  function goStep(n) {
    S.step = n; clearErrors();
    $$('.co-pane').forEach(function (p) { p.hidden = +p.dataset.pane !== n; });
    $$('#co-steps li').forEach(function (li) {
      var s = +li.dataset.s; li.className = s === n ? 'on' : (s < n ? 'done' : '');
    });
    if (n <= 2) setState('未下单');
    renderFoot();
    $('#co-body').scrollTop = 0;
  }
  function btn(label, cls, id, fn, disabled) {
    var b = document.createElement('button'); b.type = 'button'; b.className = 'btn ' + (cls || ''); b.textContent = label;
    if (id) b.id = id; b.disabled = !!disabled; b.addEventListener('click', fn); return b;
  }
  function link(label, cls, href) {
    var a = document.createElement('a'); a.className = 'btn ' + (cls || ''); a.textContent = label; a.href = href; return a;
  }
  function loading(b, on, text) {
    if (!b) return; if (on) { b.dataset.t = b.textContent; b.textContent = text || '请稍候…'; b.classList.add('loading'); }
    else { b.textContent = b.dataset.t || b.textContent; b.classList.remove('loading'); }
  }
  function renderFoot() {
    var f = $('#co-foot'); f.innerHTML = '';
    if (S.step === 1) {
      var d = drafts[S.plan.id], p = parseSession(d.session || '');
      var ready = p.email && p.token && p.acct && /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test((d.contact || '').trim()) && !!S.region;
      f.appendChild(btn('下一步：确认订单', 'dark', 'co-next', toConfirm, !ready || S.busy));
    } else if (S.step === 2) {
      f.appendChild(btn('返回修改', 'ghost', 'co-back', function () { goStep(1); }));
      f.appendChild(btn('确认并去付款', '', 'co-go', createOrder, !$('#co-confirm').checked));
    } else if (S.step === 3) {
      f.appendChild(btn('重新获取二维码', 'ghost', 'co-reqr', refetchQr));
      f.appendChild(btn('我已完成付款', 'dark', 'co-paid', checkPaid));
    } else if (S.step === 4) {
      var st = S.order && S.order.stage;
      if (st === 'done') f.appendChild(btn('完成', '', 'co-done', function () { clearOrder(); closeDlg(); }));
      else if (st === 'closed') f.appendChild(btn('重新下单', '', 'co-again', function () { clearOrder(); resetToFirst(); }));
      else if (st === 'attention') f.appendChild(link('联系客服', 'dark', '/help#support'));
      if (S.order) f.appendChild(link('查看订单进度', 'ghost', '/orders/' + S.order.no + '?t=' + encodeURIComponent(S.order.t)));
    }
  }

  /* ---------- step 1 -> 2 ---------- */
  function toConfirm() {
    if (S.busy) return; clearErrors();
    var d = drafts[S.plan.id], b = $('#co-next'); S.busy = true; loading(b, true, '正在核对…');
    api('POST', '/api/checkout/validate', { product_id: S.plan.id, region: S.region, contact_email: d.contact, session: d.session }).then(function (r) {
      S.busy = false; loading(b, false);
      if (!r.ok) { showError(r.field, r.message); renderFoot(); return; }
      S.valid = r;
      $('#co-s-plan').textContent = r.plan; $('#co-s-region').textContent = r.region_label;
      $('#co-s-contact').textContent = r.contact_email; $('#co-s-amount').textContent = r.amount_text;
      $('#co-s-account').textContent = r.account_email; $('#co-confirm').checked = false;
      $('#co-price').textContent = r.amount_text;
      goStep(2);
    });
  }

  /* ---------- step 2 -> 3 : creates an UNPAID order only ---------- */
  function createOrder() {
    if (S.busy) return;
    if (!$('#co-confirm').checked) { showError('confirm', '请先勾选确认'); return; }
    clearErrors();
    var d = drafts[S.plan.id], b = $('#co-go'); S.busy = true; loading(b, true, '正在创建订单…');
    api('POST', '/api/checkout/order', { product_id: S.plan.id, region: S.region, contact_email: d.contact, session: d.session, confirmed: true }).then(function (r) {
      S.busy = false; loading(b, false);
      if (!r.ok) {
        if (r.field === 'session' || r.field === 'contact' || r.field === 'region' || r.field === 'product') { goStep(1); }
        showError(r.field, r.message); renderFoot(); return;
      }
      S.order = { no: r.order_no, t: r.token, stage: 'pay', deadline: r.deadline, amount: r.amount_text, account: S.valid ? S.valid.account_email : r.account_masked };
      store.set('aifu.order', { pid: S.plan.id, no: r.order_no, t: r.token });
      d.session = ''; $('#co-session').value = '';          // the credential is no longer needed in the browser
      showPay(r);
    });
  }

  /* ---------- step 3: pay ---------- */
  function showPay(r) {
    goStep(3); setState('待付款', 'wait');
    $('#co-p-order').textContent = S.order.no; $('#co-p-amount').textContent = r.amount_text || S.order.amount;
    $('#co-price').textContent = r.amount_text || S.order.amount;
    var qr = $('#co-qr');
    if (r.qr_svg) qr.innerHTML = r.qr_svg; else qr.textContent = '二维码还没有生成，请点「重新获取二维码」';
    $('#co-p-status').textContent = '等待付款';
    tickLeft(); startPolling();
  }
  function tickLeft() {
    clearInterval(S.timer);
    var el = $('#co-p-left');
    var upd = function () { el.textContent = S.order ? fmtLeft(S.order.deadline) : ''; };
    upd(); S.timer = setInterval(upd, 1000);
  }
  function refetchQr() {
    var b = $('#co-reqr'); loading(b, true, '获取中…');
    api('POST', '/api/orders/' + S.order.no + '/qr?t=' + encodeURIComponent(S.order.t)).then(function (r) {
      loading(b, false);
      if (r.error) { showError('pay', r.error); return; }
      if (r.qr_svg) $('#co-qr').innerHTML = r.qr_svg;
      apply(r);
    });
  }
  function checkPaid() {
    var b = $('#co-paid'); loading(b, true, '正在核对付款…'); clearErrors();
    api('POST', '/api/orders/' + S.order.no + '/check?t=' + encodeURIComponent(S.order.t)).then(function (r) {
      loading(b, false);
      if (!r.ok) { showError('pay', r.message); return; }
      if (r.stage === 'pay') showError('pay', '还没有收到付款。请先在支付宝里完成付款，几秒钟后再点一次。');
      apply(r);
    });
  }

  /* ---------- polling + step 4 ---------- */
  function startPolling() {
    stopPolling();
    var tick = function () {
      if (!S.order) return;
      api('GET', '/api/orders/' + S.order.no + '/status?t=' + encodeURIComponent(S.order.t)).then(function (r) { if (r.ok) apply(r); });
    };
    S.poll = setInterval(tick, 3000);
  }
  function stopPolling() { clearInterval(S.poll); S.poll = null; clearInterval(S.timer); S.timer = null; }

  function apply(r) {
    if (!S.order) return;
    var prev = S.order.stage; S.order.stage = r.stage; S.order.deadline = r.deadline || S.order.deadline;
    if (r.stage === 'pay') { if (S.step !== 3) showPay(r); return; }
    if (prev === r.stage && S.step === 4) { renderResult(r); return; }
    goStep(4); renderResult(r);
    if (r.stage === 'done' || r.stage === 'closed') { stopPolling(); }
    if (r.stage === 'attention') { stopPolling(); }
  }
  function renderResult(r) {
    var box = $('#co-result'), acct = (S.valid && S.order && S.valid.account_email) || r.account_masked || '';
    var map = {
      recharging: ['wait', '…', '正在充值', r.submitted ? '付款已确认，充值请求已提交，通常几分钟内完成。' : '付款已确认，正在为你的账号充值，通常几分钟内完成。', '充值中', 'wait'],
      done: ['', '✓', '充值完成', '已为下面的账号完成充值，请登录确认套餐已生效。', '已完成', 'ok'],
      attention: ['bad', '!', '充值暂时没有完成', '付款记录已经保存，不会丢失。请带上订单号联系客服，我们会尽快处理。', '需要处理', 'bad'],
      closed: ['off', '×', '订单已关闭', '超过付款时限没有收到付款，订单已关闭。如果你其实已经付款，请带上订单号联系客服。', '已关闭', '']
    }[r.stage];
    setState(map[4], map[5]);
    box.innerHTML = '';
    var ico = document.createElement('div'); ico.className = 'ico ' + map[0]; ico.textContent = map[1];
    var h = document.createElement('h3'); h.textContent = map[2];
    var p = document.createElement('p'); p.textContent = map[3];
    box.appendChild(ico); box.appendChild(h); box.appendChild(p);
    var dl = document.createElement('dl'); dl.className = 'co-facts';
    [['订单号', r.order_no || S.order.no, 'co-mono'], ['套餐', (r.plan || '') + ' · ' + (r.region || ''), ''], ['金额', '¥' + (r.amount_text || ''), ''],
     r.stage === 'done' || r.stage === 'recharging' ? ['充值账号', acct, ''] : null].forEach(function (row) {
      if (!row) return;
      var dt = document.createElement('dt'), dd = document.createElement('dd'); dt.textContent = row[0]; dd.textContent = row[1]; if (row[2]) dd.className = row[2];
      dl.appendChild(dt); dl.appendChild(dd);
    });
    box.appendChild(dl);
    renderFoot();
  }

  /* ---------- open / close ---------- */
  function clearOrder() { store.del('aifu.order'); S.order = null; S.valid = null; stopPolling(); }
  function resetToFirst() { goStep(1); setState('未下单'); renderRegions(); renderPrice(); refreshCheck(); }

  function openFor(pid, trigger) {
    var plan = (data.plans || []).filter(function (p) { return p.id === +pid; })[0];
    if (!plan) return false;
    S.plan = plan; S.trigger = trigger || null; S.busy = false;
    var d = drafts[plan.id] = drafts[plan.id] || { region: '', contact: '', session: '' };
    var saved = store.get('aifu.draft');
    if (saved && saved.pid === plan.id) { d.region = d.region || saved.region || ''; d.contact = d.contact || saved.contact || ''; }
    S.region = plan.regions.some(function (r) { return r.code === d.region; }) ? d.region : (plan.regions[0] || {}).code || '';
    $('#co-title').textContent = plan.name;
    $('#co-contact').value = d.contact; $('#co-session').value = d.session;
    root.hidden = false; document.documentElement.classList.add('co-open');
    renderRegions(); renderPrice(); $('#co-error').hidden = true;
    var o = store.get('aifu.order');
    if (o && o.pid === plan.id) { resume(o); } else { S.order = null; goStep(1); refreshCheck(); }
    setTimeout(function () { var f = S.step === 1 ? $('#co-contact') : $('.co-dialog'); if (f) f.focus(); }, 30);
    // refresh prices/regions from the server in case the admin changed them since the page loaded
    api('GET', '/api/checkout/plans').then(function (r) {
      if (!r.ok || S.step !== 1) return;
      var fresh = (r.plans || []).filter(function (p) { return p.id === plan.id; })[0];
      if (!fresh) { showError('region', '这个套餐暂时不能购买，请刷新页面。'); return; }
      S.plan.regions = fresh.regions;
      if (!fresh.regions.some(function (x) { return x.code === S.region; })) S.region = (fresh.regions[0] || {}).code || '';
      renderRegions(); renderPrice(); refreshCheck();
    });
    return true;
  }
  function resume(o) {
    S.order = { no: o.no, t: o.t, stage: 'pay', deadline: 0, amount: '' };
    api('GET', '/api/orders/' + o.no + '/status?t=' + encodeURIComponent(o.t)).then(function (r) {
      if (!r.ok) { clearOrder(); goStep(1); refreshCheck(); return; }
      if (r.stage === 'done' || r.stage === 'closed') { clearOrder(); goStep(1); refreshCheck(); return; }   // finished earlier: start fresh
      S.order.deadline = r.deadline; S.order.amount = r.amount_text;
      if (r.stage === 'pay') {
        api('POST', '/api/orders/' + o.no + '/qr?t=' + encodeURIComponent(o.t)).then(function (q) { showPay(q.ok === false ? r : q); if (q.qr_svg) $('#co-qr').innerHTML = q.qr_svg; });
      } else { goStep(4); apply(r); startPolling(); }
    });
  }
  function closeDlg() {
    root.hidden = true; document.documentElement.classList.remove('co-open'); stopPolling();
    if (S.trigger && S.trigger.focus) S.trigger.focus();
  }

  /* ---------- events ---------- */
  document.addEventListener('click', function (e) {
    var a = e.target.closest('[data-checkout]');
    if (a && !e.metaKey && !e.ctrlKey && !e.shiftKey) { if (openFor(a.getAttribute('data-checkout'), a)) e.preventDefault(); }
  });
  $('#co-close').addEventListener('click', closeDlg);
  document.addEventListener('keydown', function (e) {
    if (root.hidden) return;
    if (e.key === 'Escape') { e.preventDefault(); closeDlg(); return; }
    if (e.key === 'Tab') {           // keep keyboard focus inside the dialog
      var f = $$('button:not([disabled]),input:not([disabled]),textarea,a[href],summary').filter(function (x) { return x.offsetParent !== null; });
      if (!f.length) return;
      var first = f[0], last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  });
  $('#co-regions').addEventListener('click', function (e) {
    var b = e.target.closest('.co-region'); if (!b) return;
    S.region = b.dataset.code; drafts[S.plan.id].region = S.region;
    store.set('aifu.draft', { pid: S.plan.id, region: S.region, contact: drafts[S.plan.id].contact });
    renderRegions(); renderPrice(); clearErrors(); refreshCheck();
  });
  $('#co-contact').addEventListener('input', function (e) {
    var d = drafts[S.plan.id]; d.contact = e.target.value; e.target.classList.remove('bad');
    store.set('aifu.draft', { pid: S.plan.id, region: S.region, contact: d.contact }); refreshCheck();
  });
  $('#co-session').addEventListener('input', function (e) {
    drafts[S.plan.id].session = e.target.value; e.target.classList.remove('bad'); refreshCheck();
  });
  $('#co-confirm').addEventListener('change', function () { clearErrors(); renderFoot(); });
})();
