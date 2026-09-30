(() => {
  if (window.__qwenPermissionCenterLoaded) return;
  window.__qwenPermissionCenterLoaded = true;
  const URL = 'http://127.0.0.1:8765/permissions';
  const style = document.createElement('style');
  style.textContent = `
    #qwen-permission-button{position:fixed;right:18px;bottom:132px;z-index:9000;display:flex;align-items:center;gap:8px;height:42px;padding:0 14px;color:#ecfeff;border:1px solid rgba(45,212,191,.38);border-radius:14px;background:linear-gradient(135deg,rgba(15,118,110,.96),rgba(17,94,89,.96));box-shadow:0 14px 34px rgba(0,0,0,.38);font:700 12px/1 Inter,"Segoe UI",sans-serif;cursor:pointer;transition:.18s}
    #qwen-permission-button:hover{transform:translateY(-2px);filter:brightness(1.09)}#qwen-permission-button svg{width:18px;height:18px}
    #qwen-permission-overlay{position:fixed;inset:0;z-index:100001;display:none;place-items:center;padding:18px;background:rgba(2,5,12,.80);backdrop-filter:blur(8px)}#qwen-permission-overlay.open{display:grid}
    #qwen-permission-panel{width:min(1180px,100%);height:min(900px,94vh);overflow:hidden;border:1px solid rgba(148,163,184,.2);border-radius:22px;background:#080b12;box-shadow:0 35px 100px rgba(0,0,0,.58);display:grid;grid-template-rows:50px 1fr}
    #qwen-permission-bar{display:flex;align-items:center;justify-content:space-between;padding:0 11px 0 17px;border-bottom:1px solid rgba(148,163,184,.14);color:#e9e7ff;background:#101522;font:700 13px/1 Inter,"Segoe UI",sans-serif}
    #qwen-permission-actions{display:flex;gap:7px}#qwen-permission-actions button,#qwen-permission-actions a{display:grid;place-items:center;width:34px;height:34px;border-radius:10px;border:1px solid rgba(148,163,184,.14);color:#cbd5e1;background:rgba(255,255,255,.03);text-decoration:none;cursor:pointer;font-size:18px}#qwen-permission-frame{width:100%;height:100%;border:0;background:#080b12}
    @media(max-width:640px){#qwen-permission-button{right:10px;bottom:122px;width:42px;padding:0;justify-content:center}#qwen-permission-button span{display:none}#qwen-permission-overlay{padding:0}#qwen-permission-panel{height:100vh;border:0;border-radius:0}}
  `;
  document.head.appendChild(style);
  const button=document.createElement('button');button.id='qwen-permission-button';button.type='button';button.title='Qwen Permission Center';button.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 3 4.5 6v5.5c0 4.5 3 7.8 7.5 9.5 4.5-1.7 7.5-5 7.5-9.5V6L12 3Z"/><path d="m9 12 2 2 4-4"/></svg><span>Permissions</span>';
  const overlay=document.createElement('div');overlay.id='qwen-permission-overlay';overlay.innerHTML=`<section id="qwen-permission-panel" role="dialog" aria-modal="true"><header id="qwen-permission-bar"><span>Qwen · Permission Center</span><div id="qwen-permission-actions"><a href="${URL}" target="_blank" title="Open in new window">↗</a><button type="button" data-close title="Close">×</button></div></header><iframe id="qwen-permission-frame" title="Permission Center" loading="lazy"></iframe></section>`;
  const open=()=>{const frame=overlay.querySelector('iframe');if(!frame.src)frame.src=URL;overlay.classList.add('open')};const close=()=>overlay.classList.remove('open');button.addEventListener('click',open);overlay.querySelector('[data-close]').addEventListener('click',close);overlay.addEventListener('click',e=>{if(e.target===overlay)close()});window.addEventListener('keydown',e=>{if(e.key==='Escape')close()});document.body.append(button,overlay);
})();
