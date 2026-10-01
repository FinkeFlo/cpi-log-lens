// Copy text to the clipboard. The Clipboard API exists only in secure contexts
// (HTTPS or localhost); the app is often opened over plain HTTP on a LAN address, so
// fall back to a hidden textarea and execCommand('copy'). The textarea goes into
// `container` (an open modal dialog makes everything outside it inert).

export async function copyText(text, container = document.body) {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // permission denied or not focused: try the fallback
    }
  }
  const previousFocus = document.activeElement;
  const area = document.createElement('textarea');
  area.value = text;
  area.setAttribute('readonly', '');
  area.setAttribute('aria-hidden', 'true');
  Object.assign(area.style, { position: 'fixed', top: '0', left: '0', opacity: '0', pointerEvents: 'none' });
  container.appendChild(area);
  area.select();
  let copied = false;
  try {
    copied = document.execCommand('copy');
  } catch {
    copied = false;
  }
  area.remove();
  previousFocus?.focus?.();
  return copied;
}
