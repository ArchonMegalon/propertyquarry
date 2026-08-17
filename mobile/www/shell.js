const status = document.querySelector('#launch-status');

let settled = false;

window.addEventListener('propertyquarry:runtime-error', (event) => {
  settled = true;
  const detail = String(event.detail || '');
  if (detail.includes('too old')) {
    status.textContent = 'This app version is too old. Update PropertyQuarry from Google Play.';
    return;
  }
  status.textContent = detail || 'PropertyQuarry could not verify this app runtime.';
});

window.setTimeout(() => {
  if (status && !settled) status.textContent = 'Still connecting securely…';
}, 4500);
