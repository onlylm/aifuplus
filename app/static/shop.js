(function () {
  // theme toggle (default follows the system)
  var root = document.documentElement, tbtn = document.getElementById('theme');
  function isDark() {
    var t = root.getAttribute('data-theme');
    return t ? t === 'dark' : window.matchMedia('(prefers-color-scheme: dark)').matches;
  }
  if (tbtn) tbtn.addEventListener('click', function () {
    var next = isDark() ? 'light' : 'dark';
    root.setAttribute('data-theme', next);
    try { localStorage.setItem('theme', next); } catch (e) {}
  });

  // copy buttons
  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
    return new Promise(function (resolve, reject) {
      var ta = document.createElement('textarea');
      ta.value = text; ta.setAttribute('readonly', ''); ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta); ta.select();
      try { document.execCommand('copy') ? resolve() : reject(); } catch (e) { reject(e); }
      document.body.removeChild(ta);
    });
  }
  document.addEventListener('click', function (e) {
    var b = e.target.closest('[data-copy]');
    if (!b) return;
    var v = b.getAttribute('data-copy'), text;
    if (v.charAt(0) === '#') text = document.querySelector(v).innerText.trim();
    else if (v === '*') text = Array.prototype.map.call(document.querySelectorAll('.key-card .k'), function (n) { return n.innerText.trim(); }).join('\n');
    else text = v;
    var old = b.getAttribute('data-label') || b.textContent;
    b.setAttribute('data-label', old);
    copyText(text).then(function () { b.textContent = '已复制'; }, function () { b.textContent = '请手动复制'; });
    setTimeout(function () { b.textContent = old; }, 1600);
  });

  // quantity stepper (display only; the server always recomputes the amount)
  var q = document.querySelector('[data-qty]');
  if (q) {
    var input = q.querySelector('input'), unit = +q.getAttribute('data-unit'), out = document.getElementById('total'), max = +input.max || 1;
    var render = function () {
      var n = Math.min(max, Math.max(1, parseInt(input.value, 10) || 1)), c = unit * n;
      if (out) out.textContent = Math.floor(c / 100) + '.' + ('0' + (c % 100)).slice(-2);
    };
    q.addEventListener('click', function (e) {
      var d = e.target.getAttribute('data-step');
      if (!d) return;
      input.value = Math.min(max, Math.max(1, (parseInt(input.value, 10) || 1) + (+d)));
      render();
    });
    input.addEventListener('input', render); render();
  }

  // region radios carry the server-computed price; this only updates what is displayed (the server recomputes at checkout)
  var regs = document.querySelectorAll('input[name=region][data-price]');
  if (regs.length) {
    var show = function () {
      var r = document.querySelector('input[name=region]:checked');
      if (!r) return;
      var c = +r.getAttribute('data-price'), t = Math.floor(c / 100) + '.' + ('0' + (c % 100)).slice(-2);
      ['total', 'shown-price'].forEach(function (id) { var el = document.getElementById(id); if (el) el.textContent = t; });
    };
    regs.forEach(function (r) { r.addEventListener('change', show); }); show();
  }

  // account-session helper: tells the buyer, in plain words, what was recognised
  var ta = document.querySelector('[data-session]'), submit = document.getElementById('submit');
  if (ta) {
    var items = {}; document.querySelectorAll('#chk li').forEach(function (li) { items[li.getAttribute('data-k')] = li; });
    var email = document.getElementById('em');
    var check = function () {
      var d = null, got = { email: '', token: false, acct: false, sess: false };
      try { d = JSON.parse(ta.value); } catch (e) { d = null; }
      if (d && typeof d === 'object') {
        got.email = (d.user && d.user.email) || '';
        got.token = !!d.accessToken; got.acct = !!(d.account_id || d.accountId); got.sess = !!d.sessionToken;
      }
      var mark = function (k, ok) { items[k].classList.toggle('ok', !!ok); };
      mark('email', got.email); mark('token', got.token); mark('acct', got.acct); mark('sess', got.sess);
      var span = items.email.querySelector('span');
      if (span) span.textContent = got.email ? '：' + got.email.replace(/^(.).*(@.*)$/, '$1•••••$2') : '';
      if (got.email && email && !email.value) email.value = got.email;
      if (submit) submit.disabled = !(got.email && got.token && got.acct);
    };
    ta.addEventListener('input', check); check();
  }

  // order page: countdown + status polling
  var page = document.querySelector('[data-order]');
  if (page) {
    var deadline = +page.getAttribute('data-deadline') * 1000, cd = document.getElementById('left');
    if (cd) setInterval(function () {
      var s = Math.max(0, Math.floor((deadline - Date.now()) / 1000));
      cd.textContent = Math.floor(s / 60) + ':' + ('0' + (s % 60)).slice(-2);
    }, 1000);
    var pay = page.getAttribute('data-pay'), ful = page.getAttribute('data-ful'), url = page.getAttribute('data-status');
    if (pay !== 'closed' && ful !== 'delivered') {
      setInterval(function () {
        fetch(url, { credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (d) {
          if (d.payment !== pay || d.fulfillment !== ful) {
            if (pay === 'unpaid' && d.payment === 'paid') location.href = location.pathname.replace('/pay/', '/orders/') + location.search;
            else location.reload();
          }
        }).catch(function () {});
      }, 3000);
    }
  }
})();
