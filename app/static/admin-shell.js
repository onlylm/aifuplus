/* Admin shell: mobile sidebar drawer (menu button, scrim, close button, Escape). */
(function () {
  'use strict';
  var shell = document.getElementById('adm-shell'), side = document.getElementById('adm-side');
  var btn = document.getElementById('adm-menu'), scrim = document.getElementById('adm-scrim'), x = document.getElementById('adm-close');
  if (!shell || !btn) return;
  function setOpen(open) {
    shell.classList.toggle('adm-open', open);
    scrim.hidden = !open;
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    document.documentElement.classList.toggle('adm-lock', open);
    if (open) { var a = side.querySelector('a'); if (a) a.focus(); } else { btn.focus(); }
  }
  btn.addEventListener('click', function () { setOpen(true); });
  scrim.addEventListener('click', function () { setOpen(false); });
  x.addEventListener('click', function () { setOpen(false); });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && shell.classList.contains('adm-open')) { e.preventDefault(); setOpen(false); } });
  side.addEventListener('click', function (e) { if (e.target.closest('.adm-nav a') && shell.classList.contains('adm-open')) setOpen(false); });
  window.addEventListener('resize', function () { if (window.innerWidth > 900 && shell.classList.contains('adm-open')) setOpen(false); });
})();
