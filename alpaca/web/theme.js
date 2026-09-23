/* Run before stylesheets so the first painted frame uses the saved theme. */
(() => {
  'use strict';

  const key = 'alpaca.theme';
  const root = document.documentElement;
  // Light is the default; an older stored "system" choice also reads as light.
  const normalize = value => value === 'dark' ? 'dark' : 'light';
  let storage = null;
  let preference = 'light';

  try {
    storage = window.localStorage;
    preference = normalize(storage.getItem(key));
  } catch (_) {
    // Private or restricted browsing still supports the current page's setting.
  }

  function apply() {
    const dark = preference === 'dark';
    root.dataset.theme = preference;
    root.style.colorScheme = preference;
    document.querySelectorAll('[data-theme-control]').forEach(control => {
      control.setAttribute('aria-checked', String(dark));
      control.title = dark ? 'Switch to light theme' : 'Switch to dark theme';
    });
    window.dispatchEvent(new CustomEvent('alpaca:themechange', {
      detail: {preference, resolved: preference}
    }));
  }

  // Colors cross-fade only while the switch is pressed, never on first paint.
  let fade = 0;
  function crossFade() {
    try { if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return; } catch (_) { return; }
    root.classList.add('theme-fading');
    clearTimeout(fade);
    fade = setTimeout(() => root.classList.remove('theme-fading'), 450);
  }

  apply();
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', apply, {once: true});
  }
  document.addEventListener('click', event => {
    if (!event.target.closest?.('[data-theme-control]')) return;
    preference = preference === 'dark' ? 'light' : 'dark';
    try { storage?.setItem(key, preference); } catch (_) { /* Page setting remains usable. */ }
    crossFade();
    apply();
  });
  window.addEventListener('storage', event => {
    if (event.key !== key && event.key !== null) return;
    if (event.storageArea && event.storageArea !== storage) return;
    preference = normalize(event.key === null ? null : event.newValue);
    apply();
  });
})();
