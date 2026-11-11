const mobileMenu = document.querySelector(".mobile-menu");

mobileMenu?.querySelectorAll(".mobile-menu-panel a").forEach((link) => {
  link.addEventListener("click", () => {
    mobileMenu.open = false;
  });
});
