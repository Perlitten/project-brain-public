# Tooling gaps / Пробелы в инструментах

This document lists browser and UI automation capabilities that **cannot** be added inside
the Project Brain repository, and what **was** added as an optional local scaffold.

---

## Чего нет (platform / agent limits)

### Computer Use / кликер по экрану

Управление произвольными кликами по рабочему столу (screen clicker, OS-level Computer Use)
доступно только если **Cursor или ОС** предоставят соответствующий API агенту. Это не часть
Project Brain и не настраивается из этого репозитория.

### Прямой доступ к встроенному браузеру Cursor как UI

Публичного API для управления **Cursor Simple Browser** / встроенной вкладкой браузера IDE
нет. Агенты в чате не могут «подключиться» к этому окну из кода в `project-brain`.

**Обходные пути:**

- отдельный браузер через **Playwright** (см. `eval/browser_automation/`);
- ручной copy-paste URL/скриншотов между агентом и пользователем;
- внешние API (например OpenAI) с собственным browser tool, если клиент их поддерживает.

### Puppeteer / Selenium

- **Puppeteer** — экосистема Node.js; в этом репозитории основной путь — **Python Playwright**.
  При необходимости Puppeteer можно использовать в отдельном Node-проекте.
- **Selenium** намеренно не добавлен: Playwright покрывает типовые E2E-сценарии с меньшим
  объёмом зависимостей.

---

## What is missing (English)

| Capability | Status in repo |
|------------|----------------|
| Computer Use / desktop screen clicker | **Not available** — requires Cursor/OS agent APIs |
| Control Cursor embedded Simple Browser | **No API** — use Playwright, copy-paste, or external browser tools |
| Puppeteer | **Not bundled** — Node.js; Playwright (Python) is the primary path |
| Selenium | **Not bundled** — Playwright is sufficient for planned E2E |

---

## Что добавлено / What was added

- **`playwright`** in `pyproject.toml` dev dependencies (`pip install -e ".[dev]"`).
- **`eval/browser_automation/`** — README, smoke test (`smoke_test.py`), optional `BROWSER_HEADLESS`
  and `BROWSER_SMOKE_URL` in `.env.example`.
- **README § Browser automation (optional)** — how to install and run the smoke test.

This scaffold is for **optional local** browser automation and future E2E experiments. It does
**not** grant agents control over Cursor's UI or the host desktop.
