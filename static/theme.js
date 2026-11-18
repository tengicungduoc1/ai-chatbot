// ponytail: one local preference shared by login and calendar; add account sync only if cross-device theme is needed.
(() => {
  const root = document.documentElement;
  const key = 'calendar-theme';
  let choice;
  try { choice = localStorage.getItem(key); } catch { choice = null; }
  const preferred = () => choice === 'light' || choice === 'dark'
    ? choice : matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  const apply = () => {
    const theme = preferred();
    root.dataset.theme = theme;
    root.style.colorScheme = theme;
    const button = document.getElementById('theme-toggle');
    if (button) {
      button.textContent = theme === 'dark' ? 'Giao diện sáng' : 'Giao diện tối';
      button.setAttribute('aria-label', `Chuyển sang giao diện ${theme === 'dark' ? 'sáng' : 'tối'}`);
    }
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', theme === 'dark' ? '#111114' : '#f5f6f8');
  };
  apply();
  document.addEventListener('DOMContentLoaded', () => {
    apply();
    document.getElementById('theme-toggle')?.addEventListener('click', () => {
      choice = root.dataset.theme === 'dark' ? 'light' : 'dark';
      try { localStorage.setItem(key, choice); } catch { /* private mode still switches for this page */ }
      apply();
    });
  });
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => { if (choice !== 'dark' && choice !== 'light') apply(); });
})();
