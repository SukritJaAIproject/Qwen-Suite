(() => {
  if (window.__qwenAutoContinueLoaded) return;
  window.__qwenAutoContinueLoaded = true;

  const ENABLED_KEY = 'qwen:auto-continue-enabled';
  const SETTINGS_VERSION_KEY = 'qwen:auto-continue-settings-version';
  const SETTINGS_VERSION = '3';
  const MAX_SEGMENTS = 6;
  const CLICK_DELAY_MS = 1400;
  const MIN_CLICK_GAP_MS = 3500;
  const CHAIN_RESET_MS = 30 * 60 * 1000;
  const CONTINUE_SELECTOR = [
    'button#continue-response-button',
    'button[aria-label="Continue Response"]',
    'button[title="Continue Response"]',
    'button[aria-label="\u0e15\u0e48\u0e2d\u0e04\u0e33\u0e15\u0e2d\u0e1a"]',
    'button[title="\u0e15\u0e48\u0e2d\u0e04\u0e33\u0e15\u0e2d\u0e1a"]'
  ].join(',');

  const continuationSignals = [
    /\[\s*\u0e22\u0e31\u0e07\u0e21\u0e35\u0e15\u0e48\u0e2d[^\]]*\]/i,
    /\[\s*(?:continue|continued|to be continued|more follows)[^\]]*\]/i,
    /(?:\u0e22\u0e31\u0e07\u0e21\u0e35\u0e15\u0e48\u0e2d|to be continued)\s*[\u2026.!]*\s*$/i
  ];

  // Open WebUI can finish a tool-calling turn without emitting a final assistant
  // message. In that state the UI shows only the tool summary (for example,
  // "Explored 7 ...") and exposes Continue Response. Treat that as an
  // incomplete turn, while leaving ordinary completed answers alone.
  const toolOnlySignals = [
    /\bExplored\s+\d+\b/i,
    /\bView Result from\b/i,
    /\b(?:function_call|tool_call|tool result)\b/i
  ];

  // Never auto-click through transport or model failures. Retrying these with
  // Continue Response can create a loop of stale long-running Ollama requests.
  const failureSignals = [
    /(?:network|connection|request|fetch|socket).*(?:error|failed|reset|closed|timeout)/i,
    /(?:error|failed|reset|closed|timeout).*(?:network|connection|request|fetch|socket)/i,
    /\b(?:TransferEncodingError|ConnectionResetError|RemoteProtocolError)\b/i,
    /\bOllama\b.*\b(?:error|failed|unavailable|timeout)\b/i,
    /(?:no user query found|tool execution failed|internal server error)/i
  ];

  // Migrate existing installations to the new default-on behavior once. A user
  // can still disable it afterward through window.qwenAutoContinue.disable().
  if (localStorage.getItem(SETTINGS_VERSION_KEY) !== SETTINGS_VERSION) {
    localStorage.setItem(ENABLED_KEY, 'true');
    localStorage.setItem(SETTINGS_VERSION_KEY, SETTINGS_VERSION);
  } else if (localStorage.getItem(ENABLED_KEY) === null) {
    localStorage.setItem(ENABLED_KEY, 'true');
  }

  let scheduled = 0;
  let clicking = false;
  let lastClickAt = 0;
  let chainStartedAt = 0;
  let chainCount = 0;
  let routeKey = location.pathname;
  const clickedFingerprints = new Set();

  const enabled = () => localStorage.getItem(ENABLED_KEY) !== 'false';

  const visible = (element) => {
    if (!element || !element.isConnected) return false;
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  };

  const messageFor = (button) =>
    button.closest('[id^="message-"]') ||
    button.closest('[data-message-id]') ||
    button.closest('article');

  const hasLengthStop = (message) => {
    let element = message;
    for (let depth = 0; element && depth < 5; depth += 1, element = element.parentElement) {
      for (const attribute of ['data-finish-reason', 'data-done-reason', 'data-stop-reason']) {
        if ((element.getAttribute(attribute) || '').toLowerCase() === 'length') return true;
      }
    }
    return false;
  };

  const hasVisibleAssistantText = (message) => {
    const contentSelectors = [
      '[data-testid="message-content"]',
      '[data-message-content]',
      '.message-content',
      '.prose'
    ];
    const contentNodes = [...message.querySelectorAll(contentSelectors.join(','))]
      .filter((node) => visible(node));
    if (!contentNodes.length) return false;

    return contentNodes.some((node) => {
      const text = (node.innerText || node.textContent || '')
        .replace(/\s+/g, ' ')
        .trim();
      return text.length > 0 && !toolOnlySignals.some((pattern) => pattern.test(text));
    });
  };

  const needsContinuation = (message) => {
    if (!message) return false;
    const text = message.innerText || message.textContent || '';
    if (failureSignals.some((pattern) => pattern.test(text))) return false;
    if (hasLengthStop(message) || continuationSignals.some((pattern) => pattern.test(text))) {
      return true;
    }

    const isToolTurn = toolOnlySignals.some((pattern) => pattern.test(text));
    return isToolTurn && !hasVisibleAssistantText(message);
  };

  const fingerprintFor = (message) => {
    const text = (message.innerText || message.textContent || '').trim();
    return `${location.pathname}|${message.id || 'message'}|${text.length}|${text.slice(-320)}`;
  };

  const resetForRoute = () => {
    if (routeKey === location.pathname) return;
    routeKey = location.pathname;
    chainStartedAt = 0;
    chainCount = 0;
    clickedFingerprints.clear();
  };

  const findCandidate = () => {
    const buttons = [...document.querySelectorAll(CONTINUE_SELECTOR)]
      .filter((button) => !button.disabled && visible(button));
    for (let index = buttons.length - 1; index >= 0; index -= 1) {
      const button = buttons[index];
      const message = messageFor(button);
      if (!needsContinuation(message)) continue;
      const fingerprint = fingerprintFor(message);
      if (!clickedFingerprints.has(fingerprint)) return { button, fingerprint };
    }
    return null;
  };

  const evaluate = () => {
    scheduled = 0;
    resetForRoute();
    if (!enabled() || clicking) return;

    const now = Date.now();
    if (chainStartedAt && now - chainStartedAt > CHAIN_RESET_MS) {
      chainStartedAt = 0;
      chainCount = 0;
      clickedFingerprints.clear();
    }
    if (now - lastClickAt < MIN_CLICK_GAP_MS) return schedule(MIN_CLICK_GAP_MS);

    const candidate = findCandidate();
    if (!candidate || chainCount >= MAX_SEGMENTS) return;

    clicking = true;
    window.setTimeout(() => {
      try {
        if (!enabled() || candidate.button.disabled || !visible(candidate.button)) return;
        if (!chainStartedAt) chainStartedAt = Date.now();
        clickedFingerprints.add(candidate.fingerprint);
        chainCount += 1;
        lastClickAt = Date.now();
        candidate.button.click();
        console.info(`[Qwen Auto Continue] segment ${chainCount}/${MAX_SEGMENTS}`);
      } finally {
        clicking = false;
        schedule(1200);
      }
    }, CLICK_DELAY_MS);
  };

  function schedule(delay = 500) {
    if (scheduled) return;
    scheduled = window.setTimeout(evaluate, delay);
  }

  const observer = new MutationObserver(() => schedule());
  observer.observe(document.documentElement, {
    subtree: true,
    childList: true,
    attributes: true,
    attributeFilter: ['disabled', 'aria-label', 'title', 'data-finish-reason', 'data-done-reason']
  });

  window.addEventListener('popstate', () => schedule());
  window.setInterval(() => schedule(), 2000);

  window.qwenAutoContinue = {
    enable() {
      localStorage.setItem(ENABLED_KEY, 'true');
      schedule(0);
    },
    disable() {
      localStorage.setItem(ENABLED_KEY, 'false');
    },
    reset() {
      chainStartedAt = 0;
      chainCount = 0;
      clickedFingerprints.clear();
      schedule(0);
    },
    status() {
      return { enabled: enabled(), chainCount, maxSegments: MAX_SEGMENTS };
    }
  };

  schedule(0);
})();
