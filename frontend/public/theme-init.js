// Apply the override before the stylesheet and app load.
try {
  const preference = localStorage.getItem("ticketing-theme");
  if (preference === "light" || preference === "dark") {
    document.documentElement.dataset.theme = preference;
  }
} catch {
  // The stylesheet follows the system setting when storage is unavailable.
}
