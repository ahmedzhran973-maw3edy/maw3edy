(function () {
  const supported = ["en", "ar", "fr", "es", "de", "tr"];
  const fallbackLanguage = "en";
  let catalog = {};
  let activeLanguage = fallbackLanguage;

  function lookup(key) {
    return key.split(".").reduce((value, part) => value && value[part], catalog[activeLanguage]);
  }

  function languageFromBrowser() {
    const candidates = navigator.languages || [navigator.language || fallbackLanguage];
    return candidates
      .map((language) => language.toLowerCase().split("-")[0])
      .find((language) => supported.includes(language)) || fallbackLanguage;
  }

  function chooseLanguage() {
    const queryLanguage = new URLSearchParams(window.location.search).get("lang");
    const storedLanguage = window.localStorage.getItem("maw3edy-language");
    const language = [queryLanguage, storedLanguage, languageFromBrowser()]
      .find((candidate) => supported.includes(candidate));
    return language || fallbackLanguage;
  }

  function translateElement(element) {
    const translated = lookup(element.dataset.i18n);
    if (typeof translated === "string") element.textContent = translated;
  }

  function apply() {
    document.documentElement.lang = activeLanguage;
    document.documentElement.dir = activeLanguage === "ar" ? "rtl" : "ltr";
    document.querySelectorAll("[data-i18n]").forEach(translateElement);
    document.querySelectorAll("[data-i18n-placeholder]").forEach((element) => {
      const translated = lookup(element.dataset.i18nPlaceholder);
      if (typeof translated === "string") element.placeholder = translated;
    });
    document.querySelectorAll("[data-i18n-day]").forEach((element) => {
      const translated = lookup(`days.${element.dataset.i18nDay}`);
      if (typeof translated === "string") element.textContent = translated;
    });
    document.querySelectorAll("[data-language-selector]").forEach((selector) => {
      selector.value = activeLanguage;
    });
    document.querySelectorAll("[data-current-language]").forEach((element) => {
      element.textContent = activeLanguage.toUpperCase();
    });
  }

  function selectLanguage(language) {
    if (!supported.includes(language)) return;
    activeLanguage = language;
    window.localStorage.setItem("maw3edy-language", language);
    const url = new URL(window.location.href);
    url.searchParams.set("lang", language);
    window.location.assign(url.toString());
  }

  const ready = fetch("/translations.json", { cache: "no-store" })
    .then((response) => response.json())
    .then((translations) => {
      catalog = translations;
      activeLanguage = chooseLanguage();
      apply();
      document.querySelectorAll("[data-language-selector]").forEach((selector) => {
        selector.addEventListener("change", (event) => selectLanguage(event.target.value));
      });
      return { language: activeLanguage, catalog };
    });

  window.Maw3edyI18n = {
    ready,
    apply,
    t: (key, fallback = key) => lookup(key) || fallback,
    language: () => activeLanguage,
    formatLocalSlot: (isoString) => {
      const date = new Date(isoString);
      return new Intl.DateTimeFormat(undefined, {
        dateStyle: "medium",
        timeStyle: "short"
      }).format(date);
    },
    browserTimezone: () => Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"
  };
})();