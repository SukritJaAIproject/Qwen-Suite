// qwen-account.js - Qwen Suite add-on for Open WebUI (9 Nov 2026)
//  * Sign-in page: "ลืมรหัสผ่าน?" (opens http://127.0.0.1:8765/account/forgot in a panel) and
//    "สมัครบัญชีใหม่" (switches Open WebUI's own form to sign-up; shown only when sign-up is on).
//  * The suite's floating buttons (Permissions, Video Studio, Projects & Tasks) are hidden on the
//    sign-in page and for accounts that are not admin; the tools behind them are owner-only.
(() => {
  if (window.__qwenAccountLoaded) return;
  window.__qwenAccountLoaded = true;
  const FORGOT_URL = 'http://127.0.0.1:8765/account/forgot';
  const FLOATING = ['#qwen-permission-button', '#qwen-video-studio-button', '#qwen-project-paths-button'];
  const SIGNUP_TEXT = /^\s*(sign\s*up|register|create\s+(an\s+)?account|สมัคร\S*|ลงทะเบียน\S*)\s*$/i;
  const SIGNIN_TEXT = /^\s*(sign\s*in|log\s*in|เข้าสู่ระบบ|ลงชื่อเข้าใช้\S*)\s*$/i;
  const root = document.documentElement;
  root.classList.add('qwen-hide-suite');   // shown again once Open WebUI says this is an admin

  const style = document.createElement('style');
  style.textContent = `
    html.qwen-hide-suite ${FLOATING.join(',html.qwen-hide-suite ')}{display:none!important}
    #qwen-account-links{display:flex;flex-wrap:wrap;justify-content:center;gap:10px;margin:18px 0 4px;font:500 13px/1.2 Inter,"Noto Sans Thai","Segoe UI",sans-serif}
    #qwen-account-links button{display:inline-flex;align-items:center;gap:7px;height:36px;padding:0 15px;border-radius:999px;cursor:pointer;transition:.15s;font:inherit}
    #qwen-account-links .qa-forgot{background:transparent;border:1px solid rgba(148,163,184,.28);color:#cbd5e1}
    #qwen-account-links .qa-forgot:hover{border-color:rgba(196,181,253,.6);color:#ede9fe}
    #qwen-account-links .qa-signup{background:linear-gradient(135deg,rgba(124,58,237,.92),rgba(79,70,229,.92));border:1px solid rgba(167,139,250,.45);color:#fff}
    #qwen-account-links .qa-signup:hover{filter:brightness(1.1)}
    #qwen-account-links svg{width:15px;height:15px}
    #qwen-account-note{width:100%;text-align:center;color:#94a3b8;font-size:11.5px;margin-top:2px}
    #qwen-account-overlay{position:fixed;inset:0;z-index:100002;display:none;place-items:center;padding:18px;background:rgba(2,5,12,.78);backdrop-filter:blur(8px)}
    #qwen-account-overlay.open{display:grid}
    #qwen-account-panel{position:relative;width:min(480px,100%);height:min(640px,94vh);border-radius:20px;overflow:hidden;border:1px solid rgba(148,163,184,.2);background:#070a10;box-shadow:0 35px 100px rgba(0,0,0,.55)}
    #qwen-account-panel iframe{width:100%;height:100%;border:0;background:#070a10}
    #qwen-account-close{position:absolute;top:10px;right:10px;z-index:2;width:32px;height:32px;border-radius:10px;border:1px solid rgba(148,163,184,.2);background:rgba(15,23,42,.85);color:#cbd5e1;font-size:18px;cursor:pointer}
    @media(max-width:640px){#qwen-account-overlay{padding:0}#qwen-account-panel{height:100vh;border:0;border-radius:0}}
  `;
  document.head.appendChild(style);

  // ---------------------------------------------------------------- forgot-password panel
  const overlay = document.createElement('div');
  overlay.id = 'qwen-account-overlay';
  overlay.innerHTML = '<section id="qwen-account-panel" role="dialog" aria-modal="true" aria-label="ลืมรหัสผ่าน">' +
    '<button id="qwen-account-close" type="button" title="ปิด">×</button><iframe title="ลืมรหัสผ่าน"></iframe></section>';
  const openForgot = () => {
    overlay.querySelector('iframe').src = FORGOT_URL;   // fresh page every time
    overlay.classList.add('open');
  };
  const closeForgot = () => { overlay.classList.remove('open'); overlay.querySelector('iframe').src = 'about:blank'; };
  overlay.querySelector('#qwen-account-close').addEventListener('click', closeForgot);
  overlay.addEventListener('click', e => { if (e.target === overlay) closeForgot(); });
  window.addEventListener('keydown', e => { if (e.key === 'Escape' && overlay.classList.contains('open')) closeForgot(); });
  window.addEventListener('message', e => {
    if ((e.origin === 'http://127.0.0.1:8765' || e.origin === 'http://localhost:8765') && e.data && e.data.qwenAccount === 'close') closeForgot();
  });
  document.body.appendChild(overlay);

  // ---------------------------------------------------------------- sign-in page buttons
  const ICON_KEY = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="8" cy="15" r="4"/><path d="m10.8 12.2 8.7-8.7M16 7l2.5 2.5M14 9l2 2"/></svg>';
  const ICON_ADD = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="10" cy="8" r="4"/><path d="M3 20c.8-3.6 3.6-5.5 7-5.5 1.4 0 2.6.3 3.7.9M18 14v6M15 17h6"/></svg>';
  const authForm = () => {
    if (!location.pathname.startsWith('/auth')) return null;
    return Array.from(document.querySelectorAll('form')).find(f => f.querySelector('input[type="password"]')) || null;
  };
  const nativeToggle = form => {
    const buttons = Array.from(form.querySelectorAll('button')).filter(b => (b.getAttribute('type') || 'submit') === 'button');
    const signup = buttons.find(b => SIGNUP_TEXT.test(b.textContent || ''));
    const signin = buttons.find(b => SIGNIN_TEXT.test(b.textContent || ''));
    return { signup, signin };
  };
  let links = null;
  const buildLinks = () => {
    const box = document.createElement('div');
    box.id = 'qwen-account-links';
    box.innerHTML = `<button type="button" class="qa-forgot">${ICON_KEY}<span>ลืมรหัสผ่าน?</span></button>` +
      `<button type="button" class="qa-signup">${ICON_ADD}<span>สมัครบัญชีใหม่</span></button>` +
      '<div id="qwen-account-note">บัญชีใหม่ต้องรอเจ้าของเครื่องอนุมัติก่อนใช้งาน</div>';
    box.querySelector('.qa-forgot').addEventListener('click', openForgot);
    box.querySelector('.qa-signup').addEventListener('click', () => {
      const form = authForm();
      const toggle = form && nativeToggle(form).signup;
      if (toggle) toggle.click();
    });
    return box;
  };
  const refreshAuth = () => {
    const form = authForm();
    if (!form) {
      if (links && links.isConnected) links.remove();
      return;
    }
    const { signup, signin } = nativeToggle(form);
    const signupMode = !signup && !!signin;     // Open WebUI shows "Sign in" while its sign-up form is open
    if (!links) links = buildLinks();
    if (form.nextElementSibling !== links) form.after(links);
    links.style.display = signupMode ? 'none' : '';
    links.querySelector('.qa-signup').style.display = signup ? '' : 'none';
    links.querySelector('#qwen-account-note').style.display = signup ? '' : 'none';
  };

  // ---------------------------------------------------------------- owner-only floating buttons
  let checkedFor = null;
  const checkRole = async () => {
    if (location.pathname.startsWith('/auth')) { root.classList.add('qwen-hide-suite'); checkedFor = null; return; }
    const token = localStorage.getItem('token') || '';
    if (checkedFor === token) return;
    checkedFor = token;
    try {
      const r = await fetch('/api/v1/auths/', {
        credentials: 'same-origin',
        headers: token ? { Authorization: 'Bearer ' + token } : {},
      });
      const me = r.ok ? await r.json() : null;
      root.classList.toggle('qwen-hide-suite', !(me && me.role === 'admin'));
    } catch (e) {
      checkedFor = null;   // try again on the next change
    }
  };

  let queued = false;
  const tick = () => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => {
      queued = false;
      try { refreshAuth(); } catch (e) { }
      checkRole();
    });
  };
  new MutationObserver(tick).observe(document.body, { childList: true, subtree: true });
  window.addEventListener('popstate', tick);
  window.addEventListener('storage', e => { if (e.key === 'token') { checkedFor = null; tick(); } });
  setInterval(() => { checkedFor = null; tick(); }, 60000);
  tick();
})();
