(() => {
  if (window.__qwenVideoStudioLoaded) return;
  window.__qwenVideoStudioLoaded = true;

  const STUDIO_URL = 'http://127.0.0.1:8765/video-studio';
  const style = document.createElement('style');
  style.textContent = `
    #qwen-video-studio-button{position:fixed;right:18px;bottom:182px;z-index:9000;display:flex;align-items:center;gap:8px;height:42px;padding:0 14px;color:#fff;border:1px solid rgba(244,114,182,.38);border-radius:14px;background:linear-gradient(135deg,rgba(139,92,246,.97),rgba(219,39,119,.96));box-shadow:0 14px 34px rgba(76,29,149,.34);font:700 12px/1 Inter,"Segoe UI",sans-serif;cursor:pointer;transition:transform .18s,filter .18s}
    #qwen-video-studio-button:hover{transform:translateY(-2px);filter:brightness(1.09)}
    #qwen-video-studio-button svg{width:18px;height:18px}
    #qwen-video-studio-overlay{position:fixed;inset:0;z-index:100002;display:none;place-items:center;padding:18px;background:rgba(2,5,12,.82);backdrop-filter:blur(9px)}
    #qwen-video-studio-overlay.open{display:grid}
    #qwen-video-studio-panel{width:min(1480px,100%);height:min(940px,95vh);overflow:hidden;border:1px solid rgba(148,163,184,.2);border-radius:22px;background:#070913;box-shadow:0 35px 110px rgba(0,0,0,.64);display:grid;grid-template-rows:50px 1fr}
    #qwen-video-studio-bar{display:flex;align-items:center;justify-content:space-between;padding:0 11px 0 17px;border-bottom:1px solid rgba(148,163,184,.14);color:#f5f3ff;background:#101522;font:700 13px/1 Inter,"Segoe UI",sans-serif}
    #qwen-video-studio-actions{display:flex;gap:7px}#qwen-video-studio-actions button,#qwen-video-studio-actions a{display:grid;place-items:center;width:34px;height:34px;border-radius:10px;border:1px solid rgba(148,163,184,.14);color:#cbd5e1;background:rgba(255,255,255,.03);text-decoration:none;cursor:pointer;font-size:18px}
    #qwen-video-studio-frame{width:100%;height:100%;border:0;background:#070913}
    @media(max-width:640px){#qwen-video-studio-button{right:10px;bottom:172px;width:42px;padding:0;justify-content:center}#qwen-video-studio-button span{display:none}#qwen-video-studio-overlay{padding:0}#qwen-video-studio-panel{height:100vh;border:0;border-radius:0}}
  `;
  document.head.appendChild(style);

  const button = document.createElement('button');
  button.id = 'qwen-video-studio-button';
  button.type = 'button';
  button.title = 'สร้างวิดีโอแบบ One-Click พร้อมติดตามและ Retry อัตโนมัติ';
  button.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="5" width="14" height="14" rx="3"/><path d="m17 10 4-2v8l-4-2z"/><path d="m8.5 9 4 3-4 3z" fill="currentColor" stroke="none"/></svg><span>Video Studio</span>`;

  const overlay = document.createElement('div');
  overlay.id = 'qwen-video-studio-overlay';
  overlay.innerHTML = `<section id="qwen-video-studio-panel" role="dialog" aria-modal="true" aria-label="Qwen Video Studio"><header id="qwen-video-studio-bar"><span>Qwen · Video Studio</span><div id="qwen-video-studio-actions"><a href="${STUDIO_URL}" target="_blank" title="เปิดหน้าต่างใหม่">↗</a><button type="button" data-close title="ปิด">×</button></div></header><iframe id="qwen-video-studio-frame" title="Qwen Video Studio" loading="lazy"></iframe></section>`;
  const open = () => { const frame = overlay.querySelector('iframe'); if (!frame.src) frame.src = STUDIO_URL; overlay.classList.add('open'); };
  const close = () => overlay.classList.remove('open');
  button.addEventListener('click', open);
  overlay.querySelector('[data-close]').addEventListener('click', close);
  overlay.addEventListener('click', event => { if (event.target === overlay) close(); });
  window.addEventListener('keydown', event => { if (event.key === 'Escape') close(); });
  document.body.append(button, overlay);
})();
