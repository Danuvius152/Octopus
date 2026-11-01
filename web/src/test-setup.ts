import '@testing-library/jest-dom/vitest';

// JSDOM omits Element.scrollBy. Virtuoso uses it after prepending a page;
// apply the offset immediately and publish the scroll event for its observer.
Element.prototype.scrollBy = function (optionsOrX: ScrollToOptions | number = {}, y: number = 0) {
  const { left = 0, top = 0 } = typeof optionsOrX === "number" ? { left: optionsOrX, top: y } : optionsOrX;
  this.scrollLeft += left;
  this.scrollTop += top;
  this.dispatchEvent(new Event("scroll"));
};
