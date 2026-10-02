export function registerDialogDirective(Alpine) {
  Alpine.directive('dialog', (element, { expression }, { evaluateLater, effect, cleanup }) => {
    const isOpen = evaluateLater(expression);
    let returnFocus = null;

    effect(() => {
      isOpen(shouldOpen => {
        if (shouldOpen && !element.open) {
          returnFocus = document.activeElement;
          element.showModal();
          const initialFocus = element.querySelector('[autofocus]') || element;
          initialFocus.focus();
        } else if (!shouldOpen && element.open) {
          element.close();
        }
      });
    });

    // data-return-focus (a CSS selector, read when the dialog closes) names a better
    // target than the opener, e.g. the row of the log entry shown last.
    const restoreFocus = () => {
      const selector = element.dataset?.returnFocus;
      const preferred = selector ? document.querySelector(selector) : null;
      const target = preferred?.isConnected ? preferred : returnFocus;
      if (target?.isConnected) target.focus();
      returnFocus = null;
    };
    const closeOnBackdropClick = event => {
      if (event.target === element && element.open) {
        element.dispatchEvent(new Event('cancel', { cancelable: true }));
      }
    };
    element.addEventListener('close', restoreFocus);
    element.addEventListener('click', closeOnBackdropClick);
    cleanup(() => {
      element.removeEventListener('close', restoreFocus);
      element.removeEventListener('click', closeOnBackdropClick);
    });
  });
}
