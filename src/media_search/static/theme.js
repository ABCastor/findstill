// The host controller for the unmodified Castor theme-toggle fragment.
(() => {
  const choices = ['system', 'light', 'dark'];
  const storageKey = 'media-search-theme';
  let choice = 'system';
  try { choice = localStorage.getItem(storageKey) || choice; } catch {}
  if (!choices.includes(choice)) choice = 'system';
  const root = document.documentElement;
  root.dataset.themeChoice = choice;
  document.addEventListener('DOMContentLoaded', () => {
    const button = document.querySelector('[data-theme-toggle]');
    if (!button) return;
    function render() {
      root.dataset.themeChoice = choice;
      const next = choices[(choices.indexOf(choice) + 1) % choices.length];
      button.setAttribute('aria-label', `Theme: ${choice}. Switch to ${next}`);
      button.title = `Theme: ${choice}. Switch to ${next}`;
      button.setAttribute('aria-pressed', String(choice !== 'system'));
    }
    button.addEventListener('click', () => {
      choice = choices[(choices.indexOf(choice) + 1) % choices.length];
      try { localStorage.setItem(storageKey, choice); } catch {}
      render();
    });
    render();
  });
})();
