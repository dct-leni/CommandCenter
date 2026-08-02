(function () {
  console.log('[FirefoxTabAudio] Page visibility override attached on:', window.location.href);

  // Override Page Visibility & Focus APIs so web pages (TikTok, YouTube, etc.)
  // NEVER pause video or audio when window is unfocused or backgrounded
  try {
    const code = `
      Object.defineProperty(document, 'hidden', { get: () => false, configurable: true });
      Object.defineProperty(document, 'visibilityState', { get: () => 'visible', configurable: true });
      Object.defineProperty(document, 'hasFocus', { value: () => true, configurable: true });
      window.addEventListener('visibilitychange', (e) => e.stopImmediatePropagation(), true);
      window.addEventListener('blur', (e) => e.stopImmediatePropagation(), true);
      document.addEventListener('visibilitychange', (e) => e.stopImmediatePropagation(), true);
    `;
    const script = document.createElement('script');
    script.textContent = code;
    (document.head || document.documentElement).appendChild(script);
    script.remove();
  } catch (e) {
    console.error('[FirefoxTabAudio] Visibility override error:', e);
  }
})();
