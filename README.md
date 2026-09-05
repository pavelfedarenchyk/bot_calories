# bot_calories

Telegram-бот для учёта калорий. Текущая версия: **0.5.0**.

## Возможности

- Дневник питания: сегодня, неделя, история
- Расчёт дневной нормы по параметрам профиля
- Свой каталог продуктов
- Друзья и недельный рейтинг
- Достижения

## Быстрый старт

1. Скопируйте `.env.example` в `.env` и заполните переменные.
2. Не коммитьте `.env` — он уже в `.gitignore`.
3. Установите зависимости: `pip install -r requirements.txt` (или `uv pip install -r requirements.txt`), затем запустите `python bot.py`.

Версии фиксируются git-тегами (`v0.5.0`) и [GitHub Releases](https://github.com/pavelfedarenchyk/bot_calories/releases). История изменений — в [CHANGELOG.md](CHANGELOG.md).

## Лицензия

MIT. См. [LICENSE](LICENSE).
