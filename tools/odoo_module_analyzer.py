#!/usr/bin/env python3
"""
odoo_module_analyzer.py
-----------------------
Анализира Odoo модул (или repo с много модули) и генерира memory файл
за claude.ai — съдържа инструкции за дистанционна работа чрез XML-RPC.

Употреба:
    python odoo_module_analyzer.py /path/to/module
    python odoo_module_analyzer.py /path/to/repo --all-modules
    python odoo_module_analyzer.py /path/to/module --output /path/to/memory/

Изисква:
    pip install anthropic

API ключ: задай ANTHROPIC_API_KEY в средата или в ~/.anthropic_key
"""

import argparse
import ast
import os
import sys
import textwrap
from pathlib import Path

try:
    import anthropic
except ImportError:
    print("Грешка: pip install anthropic")
    sys.exit(1)

# ── Конфигурация ─────────────────────────────────────────────────────────────

DEFAULT_MEMORY_DIR = Path.cwd() / "memory"
MODEL = "claude-sonnet-4-20250514"
MAX_TOKENS = 8000

# Файлове, които се включват в анализа
INCLUDE_PATTERNS = [
    "__manifest__.py",
    "models/*.py",
    "models/**/*.py",
    "wizard/*.py",
    "wizard/**/*.py",
    "report/*.py",
    "controllers/*.py",
    "views/*.xml",
    "views/**/*.xml",
    "security/ir.model.access.csv",
    "data/*.xml",
    "README.md",
    "CHANGELOG.md",
]

# Файлове, които се пропускат
SKIP_DIRS = {".git", "__pycache__", "node_modules", "static", "tests", "test"}
SKIP_FILES = {"__init__.py"}
MAX_FILE_SIZE = 80 * 1024  # 80KB на файл
MAX_TOTAL_SIZE = 400 * 1024  # 400KB общо


# ── Събиране на файлове ───────────────────────────────────────────────────────

def is_odoo_module(path: Path) -> bool:
    """Проверява дали директорията е Odoo модул."""
    return (path / "__manifest__.py").exists()


def find_modules(root: Path) -> list[Path]:
    """Намира всички Odoo модули в директория."""
    modules = []
    if is_odoo_module(root):
        return [root]
    for item in sorted(root.iterdir()):
        if item.is_dir() and item.name not in SKIP_DIRS:
            if is_odoo_module(item):
                modules.append(item)
    return modules


def collect_files(module_path: Path) -> dict[str, str]:
    """
    Събира съдържанието на релевантните файлове.
    Връща dict {relative_path: content}.
    """
    files = {}
    total_size = 0

    # Приоритетен ред: manifest първо, после models, после views
    priority_order = [
        "__manifest__.py",
        "README.md",
        "CHANGELOG.md",
    ]

    def add_file(fpath: Path):
        nonlocal total_size
        rel = str(fpath.relative_to(module_path))
        if fpath.name in SKIP_FILES and fpath.name != "__manifest__.py":
            return
        size = fpath.stat().st_size
        if size > MAX_FILE_SIZE:
            files[rel] = f"[ФАЙЛЪТ Е ПРЕКАЛЕНО ГОЛЯМ: {size // 1024}KB — пропуснат]"
            return
        if total_size + size > MAX_TOTAL_SIZE:
            files[rel] = "[ДОСТИГНАТ ЛИМИТ — пропуснат]"
            return
        try:
            content = fpath.read_text(encoding="utf-8", errors="replace")
            files[rel] = content
            total_size += size
        except Exception as e:
            files[rel] = f"[ГРЕШКА ПРИ ЧЕТЕНЕ: {e}]"

    # Приоритетни файлове
    for name in priority_order:
        p = module_path / name
        if p.exists():
            add_file(p)

    # Останалите файлове
    for fpath in sorted(module_path.rglob("*")):
        if not fpath.is_file():
            continue
        rel = str(fpath.relative_to(module_path))
        if rel in files:
            continue
        # Пропусни директории
        parts = fpath.parts
        if any(d in SKIP_DIRS for d in parts):
            continue
        # Включи само релевантни разширения
        if fpath.suffix not in {".py", ".xml", ".csv", ".json", ".md"}:
            continue
        # Пропусни static
        if "static" in fpath.parts:
            continue
        add_file(fpath)

    return files


# ── Изграждане на prompt ──────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an expert Odoo developer analyzing source code to create 
a memory file for Claude.ai. The memory file will be used by Claude in future conversations 
to provide accurate assistance with remote Odoo administration via XML-RPC/JSON-RPC.

Write in Bulgarian. Use markdown. Be precise and concise — focus on what's needed for 
remote work, not general documentation.

The memory file must follow this exact structure:

---
name: <module technical name> — <short description>
description: <one sentence what this module does and why it matters for remote ops>
type: module
---

## Модул: `<technical_name>`

### Какво прави
<2-3 изречения описание на функционалността>

### Ключови модели и полета
За всеки модел: `model.name` — описание на предназначението
Само полетата, важни за remote операции (create/write/search).

### XML-RPC операции (готови за копиране)

За всяка важна операция — готов Python код с `models.execute_kw(...)`.
Включи: search_read, create, write, специфични методи.
Ползвай тези placeholder-и: `URL`, `DB`, `UID`, `API_KEY`.

```python
# <описание на операцията>
result = models.execute_kw(DB, UID, API_KEY,
    'model.name', 'method',
    [domain_or_args],
    {'fields': [...], 'limit': N})
```

### Бързи команди
Кратки trigger фрази и какво правят. Пример:
- "покажи X" → search_read на model Y с полета [...]
- "създай X" → create с минималните задължителни полета

### Зависимости и ограничения
- Задължителни depends модули за инсталация
- Важни ограничения при remote работа (напр. multi-company, sudo rights)
- Потенциални грешки и как да се заобиколят

### Важни бележки
Всичко специфично, което не се вижда очевидно от кода но е критично за работата.
"""


def build_user_prompt(module_name: str, files: dict[str, str]) -> str:
    parts = [f"Анализирай Odoo модула `{module_name}` и създай memory файл.\n\n"]
    parts.append("## Файлове на модула:\n\n")

    for rel_path, content in files.items():
        parts.append(f"### {rel_path}\n```\n{content}\n```\n\n")

    return "".join(parts)


# ── Извикване на API ──────────────────────────────────────────────────────────

def get_api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    key_file = Path.home() / ".anthropic_key"
    if key_file.exists():
        return key_file.read_text().strip()
    print("Грешка: няма ANTHROPIC_API_KEY.")
    print("  export ANTHROPIC_API_KEY=sk-ant-...")
    print("  или запиши в ~/.anthropic_key")
    sys.exit(1)


def analyze_module(module_name: str, files: dict[str, str]) -> str:
    """Изпраща файловете към Claude и получава memory файл."""
    client = anthropic.Anthropic(api_key=get_api_key())

    user_prompt = build_user_prompt(module_name, files)

    print(f"  Изпращам {len(files)} файла ({sum(len(v) for v in files.values()) // 1024}KB) към Claude...")

    message = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )

    return message.content[0].text


# ── Запис на резултата ────────────────────────────────────────────────────────

def update_memory_index(memory_dir: Path, module_name: str, description: str):
    """Добавя новия модул в MEMORY.md индекса."""
    memory_md = memory_dir / "MEMORY.md"
    entry = f"- [module_{module_name}.md](module_{module_name}.md) — {description}\n"

    if not memory_md.exists():
        memory_md.write_text(f"# Memory Index\n\n{entry}")
        return

    content = memory_md.read_text(encoding="utf-8")

    # Замени ако вече съществува
    marker = f"module_{module_name}.md"
    lines = content.splitlines(keepends=True)
    new_lines = [l for l in lines if marker not in l]
    new_lines.append(entry)
    memory_md.write_text("".join(new_lines))


def extract_description(memory_content: str, module_name: str) -> str:
    """Извлича description от frontmatter на генерирания файл."""
    for line in memory_content.splitlines():
        if line.startswith("description:"):
            return line.replace("description:", "").strip()
    return f"{module_name} module analysis"


def save_memory_file(memory_dir: Path, module_name: str, content: str) -> Path:
    memory_dir.mkdir(parents=True, exist_ok=True)
    out_path = memory_dir / f"module_{module_name}.md"
    out_path.write_text(content, encoding="utf-8")
    return out_path


# ── Main ──────────────────────────────────────────────────────────────────────

def process_module(module_path: Path, memory_dir: Path, dry_run: bool = False):
    module_name = module_path.name
    print(f"\n{'='*60}")
    print(f"Модул: {module_name}")
    print(f"Път:   {module_path}")

    files = collect_files(module_path)
    print(f"Файлове: {len(files)}")

    if not files:
        print("  Няма файлове за анализ — пропускам.")
        return

    if dry_run:
        print("  [DRY RUN] Файлове, които ще се изпратят:")
        for f in files:
            print(f"    {f}")
        return

    memory_content = analyze_module(module_name, files)
    description = extract_description(memory_content, module_name)

    out_path = save_memory_file(memory_dir, module_name, memory_content)
    update_memory_index(memory_dir, module_name, description)

    print(f"  Записан: {out_path}")
    print(f"  Описание: {description}")


def main():
    parser = argparse.ArgumentParser(
        description="Анализира Odoo модул и генерира memory файл за claude.ai",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
        Примери:
          # Един модул
          python odoo_module_analyzer.py ~/Проекти/odoo/odoo-18.0/l10n-bulgaria/l10n_bg_config

          # Цяло repo (всички модули)
          python odoo_module_analyzer.py ~/Проекти/odoo/odoo-18.0/l10n-bulgaria --all-modules

          # Задай друга изходна директория
          python odoo_module_analyzer.py /path/to/module --output /path/to/memory/

          # Виж само кои файлове ще се изпратят
          python odoo_module_analyzer.py /path/to/module --dry-run
        """),
    )
    parser.add_argument(
        "source",
        type=Path,
        help="Път до модул или repo директория",
    )
    parser.add_argument(
        "--all-modules",
        action="store_true",
        help="Анализирай всички модули в директорията",
    )
    parser.add_argument(
        "--output", "-o",
        type=Path,
        default=DEFAULT_MEMORY_DIR,
        help=f"Изходна директория (default: {DEFAULT_MEMORY_DIR})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Само покажи кои файлове ще се изпратят, без API извикване",
    )
    parser.add_argument(
        "--module", "-m",
        help="Анализирай само конкретен модул от repo (по name)",
    )

    args = parser.parse_args()

    source = args.source.expanduser().resolve()
    if not source.exists():
        print(f"Грешка: {source} не съществува")
        sys.exit(1)

    memory_dir = args.output.expanduser().resolve()
    print(f"Изходна директория: {memory_dir}")

    modules = find_modules(source)
    if not modules:
        print(f"Грешка: няма Odoo модули в {source}")
        sys.exit(1)

    if args.module:
        modules = [m for m in modules if m.name == args.module]
        if not modules:
            print(f"Грешка: модул '{args.module}' не е намерен")
            sys.exit(1)

    if not args.all_modules and len(modules) > 1:
        print(f"Намерени {len(modules)} модула. Ползвай --all-modules за всички,")
        print("или --module <name> за конкретен. Налични:")
        for m in modules:
            print(f"  {m.name}")
        sys.exit(0)

    for module_path in modules:
        try:
            process_module(module_path, memory_dir, dry_run=args.dry_run)
        except KeyboardInterrupt:
            print("\nПрекъснато.")
            sys.exit(0)
        except Exception as e:
            print(f"  ГРЕШКА при {module_path.name}: {e}")
            if "--debug" in sys.argv:
                raise

    if not args.dry_run:
        print(f"\nГотово. Memory файловете са в: {memory_dir}")
        print(f"MEMORY.md индексът е обновен.")
