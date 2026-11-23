# Дизайн-система Telegram Mini App (Design System)

Цей документ фіксує візуальні токени, правила теми оформлення, стилізацію спайдерплоту та UX-принципи для `miniapp/` (React + TypeScript + Vite + Chart.js).

## 1. Режими теми (`dark` / `light` / `system`)

Mini App підтримує 3 режими:
- **`dark`** — висококонтрастна темна тема (чистий чорний фон, білий текст).
- **`light`** — висококонтрастна світла тема (білий фон, чорний текст).
- **`system` (Auto)** — за замовчуванням зчитує `Telegram.WebApp.colorScheme` і реагує на `themeChanged`.

Перемикання відбувається через атрибут `data-theme="dark"` або `data-theme="light"` на `<html>`:

```css
:root[data-theme="dark"] {
  --bg-primary: #000000;
  --bg-surface: #141414;
  --border-subtle: rgba(255, 255, 255, 0.14);
  --text-primary: #FFFFFF;
  --text-secondary: #A0A0A0;
  --accent: #3B82F6;
  --accent-fill: rgba(59, 130, 246, 0.25);
}

:root[data-theme="light"] {
  --bg-primary: #FFFFFF;
  --bg-surface: #F5F5F7;
  --border-subtle: rgba(0, 0, 0, 0.12);
  --text-primary: #000000;
  --text-secondary: #555555;
  --accent: #2563EB;
  --accent-fill: rgba(37, 99, 235, 0.22);
}
```

## 2. Інтеграція з Telegram WebApp SDK та відсутність блимання (FOUC)

- **Анти-FOUC:** інлайн-скрипт у `miniapp/index.html` читає кешований режим із `localStorage` (або `Telegram.WebApp.colorScheme`) і виставляє `data-theme` на `<html>` ще до монтування React.
- **Системна шапка Telegram:** при кожній зміні теми викликаються `Telegram.WebApp.setHeaderColor(bg)` та `Telegram.WebApp.setBackgroundColor(bg)` (`#000000` для темної або `#FFFFFF` для світлої).
- **Синхронізація між пристроями:** вибір користувача зберігається через `PATCH /api/users/settings` (поле `theme`).

## 3. Палітра спайдерплоту (Chart.js `<canvas>`)

Оскільки `<canvas>` не читає CSS-змінні автоматично, конфіг `Radar Chart` оновлюється динамічно при зміні теми:

| Параметр Chart.js | Темна тема (`dark`) | Світла тема (`light`) |
| --- | --- | --- |
| `scales.r.pointLabels.color` | `#FFFFFF` | `#000000` |
| `scales.r.ticks.color` | `#A0A0A0` | `#555555` |
| `scales.r.grid.color` | `rgba(255, 255, 255, 0.20)` | `rgba(0, 0, 0, 0.15)` |
| `scales.r.angleLines.color` | `rgba(255, 255, 255, 0.20)` | `rgba(0, 0, 0, 0.15)` |
| `datasets[0].borderColor` | `#3B82F6` | `#2563EB` |
| `datasets[0].backgroundColor` | `rgba(59, 130, 246, 0.25)` | `rgba(37, 99, 235, 0.22)` |

Осі зі статусом `coming_soon` (*Шрайбен*, *Хьорен* у MVP) відображаються на шкалі з балом `0` і приглушеним бейджем «Скоро» у картках під графіком.

## 4. Верстка, навігація та тексти

- **Mobile-first:** оптимізація під вертикальний екран смартфона в Telegram WebView з акуратним центруванням (`max-width: 560px`) на десктопі.
- **Навігація:** фіксована нижня панель із 3 вкладками (`Статистика`, `Словник`, `Налаштування`) з урахуванням `env(safe-area-inset-bottom)`.
- **Інтерактивність:** мінімальний розмір клікабельних елементів — `44×44 px`, явні стани завантаження, порожнього словника та помилки API.
- **Мова інтерфейсу:** чиста, природна українська мова без кальок і русизмів.
