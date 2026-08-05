const status = document.querySelector('#launch-status');

window.addEventListener('propertyquarry:runtime-error', (event) => {
  status.textContent = event.detail || 'PropertyQuarry could not verify this app runtime.';
});

window.setTimeout(() => {
  if (status) status.textContent = 'Still connecting securely…';
}, 4500);
