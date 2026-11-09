// Copy buttons: any button with data-copy copies that value to the
// clipboard and flips its label to "Copied!". Loaded from base.html
// on every page so no template needs an inline onclick handler
// (the Content Security Policy blocks those).
document.addEventListener('DOMContentLoaded', function () {
  document.querySelectorAll('[data-copy]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      navigator.clipboard.writeText(btn.dataset.copy);
      btn.textContent = 'Copied!';
    });
  });
});
