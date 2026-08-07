# Codex Project Memory

Локальная проектная память для Codex через MCP. Она помогает находить уже
выполненные процедуры, отслеживать повторяющиеся попытки и сохранять только
проверенный итоговый вариант решения.

Данные каждого проекта находятся вне Git. Плагин не отправляет записи во
внешний сервис и не добавляет базу, ключи или credentials в репозиторий.

## Что сохраняется

- повторяющиеся проблемы и действия-кандидаты;
- финальные решения после минимум двух повторений и успешной проверки;
- устойчивые расположения логов без копирования сырых логов;
- сведения о явно тестовых ресурсах, включая зашифрованные credentials;
- ревизии записей и локальный audit trail.

Поиск использует SQLite FTS5. Секретные поля шифруются AES-256-GCM и не
попадают в полнотекстовый индекс.

## Требования

- Linux, macOS или WSL;
- Git;
- Python 3.10+ с модулем `venv`;
- актуальный Codex CLI с командами `codex plugin`.

## Установка с нуля

```bash
git clone https://github.com/woffko/codex-project-memory.git
cd codex-project-memory
chmod +x scripts/install.sh plugins/project-memory/scripts/run-project-memory.sh
./scripts/install.sh
```

Установщик создаёт изолированный Python runtime в
`${XDG_DATA_HOME:-~/.local/share}/codex-project-memory/runtime`, устанавливает
зависимость `cryptography`, регистрирует локальный marketplace и устанавливает
плагин `project-memory`.

Чтобы вместо локального checkout подключить marketplace непосредственно с
GitHub:

```bash
codex plugin marketplace add woffko/codex-project-memory --ref main
codex plugin add project-memory@codex-project-memory
```

При таком варианте Python всё равно должен иметь пакет `cryptography`; самый
простой воспроизводимый способ — один раз запустить `scripts/install.sh` из
клона.

## Регистрация проекта

Перейдите в корень нужного проекта:

```bash
cd /path/to/project
~/.local/share/codex-project-memory/runtime/bin/codex-project-memory enroll \
  --project-root "$PWD" \
  --project-name "my-project"
```

Обычная регистрация не разрешает хранить credentials. Если проект работает с
ресурсами, которые явно являются только тестовыми, разрешение включается
отдельно:

```bash
~/.local/share/codex-project-memory/runtime/bin/codex-project-memory enroll \
  --project-root "$PWD" \
  --project-name "my-project" \
  --allow-test-secrets
```

Флаг не означает «разрешить любые секреты». Он применяется только к записям
`test_asset` с явным `test_only: true`. Production, personal и неоднозначно
классифицированные credentials сохранять нельзя.

## Настройка Codex в проекте

Добавьте таблицы из [`examples/project-config.toml`](examples/project-config.toml)
в доверенный `.codex/config.toml`. Они оставляют чтение и обычную запись
автоматическими, но требуют подтверждения для сохранения/раскрытия тестовых
credentials и устаревания записи.

Добавьте инструкции из [`examples/AGENTS.md`](examples/AGENTS.md) в проектный
`AGENTS.md`. Плагин уже содержит skill с тем же workflow, но проектная
инструкция делает поведение явным и долговечным.

После установки или изменения конфигурации запустите новую Codex-сессию из
корня зарегистрированного проекта:

```bash
codex -C /path/to/project
```

При `resume`, если Codex предлагает выбрать рабочий каталог, используйте
каталог зарегистрированного проекта.

## Как работает накопление решения

1. В начале диагностики Codex вызывает `project_memory_status` и
   `project_memory_search`.
2. Повторяющаяся проблема или действие записывается через
   `project_memory_note_repetition`.
3. Сервер увеличивает счётчик одинакового кандидата по стабильному fingerprint.
4. До двух повторений кандидат нельзя превратить в решение.
5. После реального успешного теста Codex вызывает
   `project_memory_finalize_solution`, сохраняя точные шаги, результат и способ
   проверки.

Непроверенные гипотезы и сырые логи не должны становиться финальными решениями.

## MCP-инструменты

| Инструмент | Назначение |
| --- | --- |
| `project_memory_status` | Проверить регистрацию и число записей |
| `project_memory_search` | Найти решения и метаданные без секретов |
| `project_memory_get` | Прочитать обычную запись |
| `project_memory_note_repetition` | Учесть повтор проблемы/действия |
| `project_memory_finalize_solution` | Сохранить проверенный финальный вариант |
| `project_memory_record_log_location` | Запомнить устойчивое расположение логов |
| `project_memory_store_test_asset` | Сохранить тестовый ресурс и encrypted fields |
| `project_memory_get_test_asset` | Раскрыть encrypted fields с подтверждением |
| `project_memory_deprecate` | Мягко пометить запись устаревшей |

## Прямое подключение только MCP

Плагин удобнее, поскольку вместе с сервером устанавливает workflow skill. Если
нужен только MCP:

```bash
codex mcp add project_memory -- \
  ~/.local/share/codex-project-memory/runtime/bin/codex-project-memory serve
```

После этого проект всё равно нужно зарегистрировать командой `enroll`.

## Хранилище и резервные копии

По умолчанию:

```text
~/.local/share/codex-project-memory/
├── registry.json
├── runtime/
└── projects/<project-id>/
    ├── memory.sqlite3
    └── backups/

~/.config/codex-project-memory/
└── master.key
```

Создать согласованную SQLite-копию:

```bash
~/.local/share/codex-project-memory/runtime/bin/codex-project-memory backup \
  --project-root /path/to/project
```

Не копируйте `master.key` в репозиторий. Для восстановления зашифрованных
тестовых полей требуется и база, и соответствующий ключ.

Подробнее: [`SECURITY.md`](SECURITY.md).

## Проверка разработки

```bash
python3 plugins/project-memory/scripts/test_project_memory.py
python3 /path/to/plugin-creator/scripts/validate_plugin.py plugins/project-memory
```

Тесты проверяют минимальное число повторений, финализацию после верификации,
изоляцию проектов, отказ обычных записей принимать credentials и отсутствие
открытого тестового пароля в SQLite.

## Удаление

```bash
./scripts/uninstall.sh
```

Удаление плагина намеренно не удаляет локальные базы и резервные копии.

## Лицензия

MIT — см. [`LICENSE`](LICENSE).
