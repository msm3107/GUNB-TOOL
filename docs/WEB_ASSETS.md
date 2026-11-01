# Żółta Tablica — materiały strony V2

Materiały wizualne i styl pochodzą z projektu właściciela `GUNB-WEBSITE`, udostępnionego do ponownego użycia. Wybrano wyłącznie logo i dwie ilustracje budowlane z `public/`, bez zmian bajtów. Nie przenoszono screenów bota, rzeczywistych raportów inwestycji, obietnic testu ani kodu Next.js.

- `company-logo.png`: oryginalne logo 83×83, użyte w nagłówku, stopce i jako favicon; wyświetlane bez rozciągania proporcji.
- `hero-construction.webp`: ilustracja 1400×788, dekoracyjne tło hero.
- `construction-detail.webp`: ilustracja 760×570 obok wprowadzenia do fikcyjnych przykładów. Nie przedstawia konkretnej inwestycji z próbek.
- Manrope Variable: pliki Latin i Latin-ext (polskie znaki) z `@fontsource-variable/manrope` 5.3.0 w istniejącym projekcie źródłowym. Zachowano pełną licencję SIL Open Font License 1.1 w `app/static/fonts/OFL-Manrope.txt`. Fonty serwowane lokalnie; nagłówki korzystają z systemowej Georgii.
- Kolory: granat `#0b2231`, żółty `#ffd02b`, krem `#faf8f1`; wzorowane na `src/app/globals.css` źródłowej witryny. Układ dostosowano do sekcji V2 i istniejącej architektury Jinja2.

Wszystkie zasoby są serwowane przez `/static`. Nie są ładowane skrypty, fonty lub grafiki z CDN. Projekt źródłowy pozostał niezmieniony.

## Integralność skopiowanych plików

Sumy dotyczą niezmienionych plików binarnych. Tekst licencji OFL zachowuje pełną treść; końce linii podlegają zwykłej normalizacji Git.

| Plik docelowy | Bajty | SHA-256 |
|---|---:|---|
| `app/static/images/company-logo.png` | 5347 | `ea144d3bbbb5583446343be796e8c6b6f764ffb5234e7d29787c19e8426c1cf4` |
| `app/static/images/hero-construction.webp` | 76040 | `5ef57fdf344051695bccb9732520db1817ac87bf4361eb3750b783015015f9ef` |
| `app/static/images/construction-detail.webp` | 63120 | `8371632fa3706b43ffcccc813c6026cbd492ed66d5c5aa4cee086ab5d959729a` |
| `app/static/fonts/manrope-latin-wght-normal.woff2` | 24836 | `a30ddcd349703aff7464c34bef3fffdff405ee50c113440d7c8693c02d210972` |
| `app/static/fonts/manrope-latin-ext-wght-normal.woff2` | 15120 | `3911b66d9f2e005a4b989223405d0e5032619c668597ba467cc76a23c8fffcfb` |
