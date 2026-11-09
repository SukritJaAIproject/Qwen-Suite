(() => {
  if (window.__qwenProjectPathsLoaded) return;
  window.__qwenProjectPathsLoaded = true;

  const MANAGER_URL = 'http://127.0.0.1:8765/path-manager';
  // These titles follow Open WebUI's own language (<html lang>); the panel shows the suite page in the suite's language.
  const thai = () => /^th\b/i.test(document.documentElement.lang || '');
  const style = document.createElement('style');
  style.textContent = `
    #qwen-project-paths-button {
      position: fixed; right: 18px; bottom: 82px; z-index: 9000;
      display: flex; align-items: center; gap: 8px; height: 42px; padding: 0 14px;
      color: #f5f3ff; border: 1px solid rgba(167,139,250,.34); border-radius: 14px;
      background: linear-gradient(135deg, rgba(124,58,237,.96), rgba(76,29,149,.96));
      box-shadow: 0 14px 34px rgba(31, 18, 63, .42); font: 700 12px/1 Inter, "Segoe UI", sans-serif;
      cursor: pointer; backdrop-filter: blur(12px); transition: transform .18s, filter .18s;
    }
    #qwen-project-paths-button:hover { transform: translateY(-2px); filter: brightness(1.09); }
    #qwen-project-paths-button svg { width: 18px; height: 18px; }
    #qwen-project-paths-overlay {
      position: fixed; inset: 0; z-index: 100000; display: none; place-items: center;
      padding: 18px; background: rgba(2, 5, 12, .78); backdrop-filter: blur(8px);
    }
    #qwen-project-paths-overlay.open { display: grid; }
    #qwen-project-paths-panel {
      width: min(1240px, 100%); height: min(900px, 94vh); overflow: hidden;
      border: 1px solid rgba(148,163,184,.20); border-radius: 22px; background: #080b12;
      box-shadow: 0 35px 100px rgba(0,0,0,.58); display: grid; grid-template-rows: 50px 1fr;
    }
    #qwen-project-paths-bar {
      display: flex; align-items: center; justify-content: space-between; padding: 0 11px 0 17px;
      border-bottom: 1px solid rgba(148,163,184,.14); color: #e9e7ff; background: #101522;
      font: 700 13px/1 Inter, "Segoe UI", sans-serif;
    }
    #qwen-project-paths-actions { display: flex; gap: 7px; }
    #qwen-project-paths-actions button, #qwen-project-paths-actions a {
      display: grid; place-items: center; width: 34px; height: 34px; border-radius: 10px;
      border: 1px solid rgba(148,163,184,.14); color: #cbd5e1; background: rgba(255,255,255,.03);
      text-decoration: none; cursor: pointer; font-size: 18px;
    }
    #qwen-project-paths-frame { width: 100%; height: 100%; border: 0; background: #080b12; }
    @media (max-width: 640px) {
      #qwen-project-paths-button { right: 10px; bottom: 72px; width: 42px; padding: 0; justify-content: center; }
      #qwen-project-paths-button span { display: none; }
      #qwen-project-paths-overlay { padding: 0; }
      #qwen-project-paths-panel { height: 100vh; border-radius: 0; border: 0; }
    }
  `;
  document.head.appendChild(style);

  const button = document.createElement('button');
  button.id = 'qwen-project-paths-button';
  button.type = 'button';
  button.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3.5 6.5h6l2 2h9v9.7a1.8 1.8 0 0 1-1.8 1.8H5.3a1.8 1.8 0 0 1-1.8-1.8V6.5Z"/><path d="M8.7 14h6.6M12 10.7v6.6"/></svg><span>Projects & Tasks</span>`;

  const overlay = document.createElement('div');
  overlay.id = 'qwen-project-paths-overlay';
  overlay.innerHTML = `
    <section id="qwen-project-paths-panel" role="dialog" aria-modal="true" aria-label="Project Path Manager">
      <header id="qwen-project-paths-bar">
        <span>Qwen · Project Control Center</span>
        <div id="qwen-project-paths-actions">
          <a href="${MANAGER_URL}" target="_blank" data-open-new>↗</a>
          <button type="button" data-close>×</button>
        </div>
      </header>
      <iframe id="qwen-project-paths-frame" title="Project Path Manager" loading="lazy"></iframe>
    </section>`;

  const open = () => {
    const frame = overlay.querySelector('iframe');
    if (!frame.src) frame.src = MANAGER_URL;
    overlay.classList.add('open');
  };
  const close = () => overlay.classList.remove('open');
  button.addEventListener('click', open);
  overlay.querySelector('[data-close]').addEventListener('click', close);
  overlay.addEventListener('click', event => { if (event.target === overlay) close(); });
  window.addEventListener('keydown', event => { if (event.key === 'Escape') close(); });
  const relabel = () => {
    const th = thai();
    button.title = th ? 'จัดการ Path และติดตาม Task แยกตาม Project' : 'Manage paths and follow tasks per Project';
    overlay.querySelector('[data-open-new]').title = th ? 'เปิดเป็นหน้าต่างใหม่' : 'Open in a new window';
    overlay.querySelector('[data-close]').title = th ? 'ปิด' : 'Close';
  };
  relabel();
  new MutationObserver(relabel).observe(document.documentElement, { attributes: true, attributeFilter: ['lang'] });
  document.body.append(button, overlay);
})();
