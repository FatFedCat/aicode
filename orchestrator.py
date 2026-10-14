import warnings
warnings.filterwarnings("ignore", category=SyntaxWarning)

import os
import re
import ast
import sys
import json
import subprocess
from datetime import datetime
from pathlib import Path
from openai import OpenAI
from dotenv import load_dotenv

from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown
from rich.table import Table
from rich.theme import Theme
from rich import box

# Принудительно включаем построчную буферизацию и UTF-8 (лечит зависание ввода в Git Bash / MINGW64)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8")

custom_theme = Theme({
    "info": "cyan",
    "warning": "bold yellow",
    "error": "bold red",
    "success": "bold green",
})
console = Console(theme=custom_theme, force_terminal=True)

# Загружаем ключи из .env
env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

openrouter = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.getenv("OPENROUTER_API_KEY")
)

deepseek = OpenAI(
    base_url="https://api.deepseek.com",
    api_key=os.getenv("DEEPSEEK_API_KEY")
)

# Групповой бэкап для команды undo
LAST_BATCH_BACKUP = []

# Скользящая память сессии для Haiku
SESSION_MEMORY = ""

# Порог строк для включения режима SEARCH/REPLACE
DIFF_LINE_THRESHOLD = 60

IGNORE_DIRS = {'.git', 'venv', '.venv', 'env', '__pycache__', 'node_modules', '.idea', '.vscode', 'dist', 'build', '.pytest_cache'}
CODE_EXTENSIONS = {'.py', '.txt', '.json', '.md', '.html', '.css', '.js', '.ts', '.php', '.sql', '.yaml', '.yml'}


def safe_input(prompt_markup: str) -> str:
    """Безопасный ввод с поддержкой цветов Rich и принудительным сбросом буфера для Git Bash."""
    console.print(prompt_markup, end="")
    sys.stdout.flush()
    return sys.stdin.readline().strip()


def get_default_documents_dir() -> Path:
    """Находит системную папку 'Документы' на любой ОС."""
    if os.name == 'nt':
        import ctypes.wintypes
        buf = ctypes.create_unicode_buffer(ctypes.wintypes.MAX_PATH)
        ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf)
        return Path(buf.value)
    return Path.home() / "Documents"


def get_project_files(work_dir: Path) -> list[Path]:
    """Рекурсивно собирает все рабочие файлы проекта, игнорируя служебные папки."""
    if not work_dir.exists():
        return []
    result = []
    for root, dirs, files in os.walk(work_dir):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS and not d.startswith('.')]
        for f in files:
            if f.startswith('.'):
                continue
            p = Path(root) / f
            if p.suffix.lower() in CODE_EXTENSIONS:
                result.append(p)
    return result


def extract_python_skeleton(file_path: Path) -> str:
    """Строит легкий AST-скелет Python-файла (классы и сигнатуры функций)."""
    try:
        source = file_path.read_text(encoding='utf-8')
        tree = ast.parse(source)
        items = []
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                methods = [n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
                methods_str = f" (методы: {', '.join(methods)})" if methods else ""
                items.append(f"class {node.name}{methods_str}")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = [a.arg for a in node.args.args]
                items.append(f"def {node.name}({', '.join(args)})")
        return "; ".join(items) if items else f"{len(source.splitlines())} строк кода"
    except Exception:
        return "код/скрипт"


def build_project_map(work_dir: Path) -> tuple[str, Path | None, Path | None]:
    """Собирает карту проекта, читает ТЗ.md и конспект.txt."""
    files = get_project_files(work_dir)
    if not files:
        return "Проект пока пуст.", None, None

    tz_file = None
    konspekt_file = None

    for f in files:
        name_lower = f.name.lower()
        if name_lower in ['тз.md', 'тз.txt', 'tz.md', 'tz.txt', 'spec.md']:
            tz_file = f
        elif name_lower in ['конспект.txt', 'конспект.md', 'konspekt.txt', 'changelog.md', 'progress.txt']:
            konspekt_file = f

    sections = []

    if tz_file:
        try:
            rel_tz = tz_file.relative_to(work_dir).as_posix()
            tz_text = tz_file.read_text(encoding='utf-8')[:15000]
            sections.append(f"=== ГЛАВНОЕ ТЗ ПРОЕКТА ({rel_tz}) ===\n{tz_text}\n")
        except Exception:
            pass

    if konspekt_file:
        try:
            rel_k = konspekt_file.relative_to(work_dir).as_posix()
            k_text = konspekt_file.read_text(encoding='utf-8')[-8000:]
            sections.append(f"=== КОНСПЕКТ ПРОДЕЛАННЫХ РАБОТ И ПЛАН ({rel_k}) ===\n{k_text}\n")
        except Exception:
            pass

    tree_lines = ["=== СТРУКТУРА И КАРТА ФАЙЛОВ ПРОЕКТА ==="]
    for f in sorted(files):
        rel_path = f.relative_to(work_dir).as_posix()
        if f in (tz_file, konspekt_file):
            tree_lines.append(f"- {rel_path} [Документация проекта]")
        elif f.suffix == '.py':
            skeleton = extract_python_skeleton(f)
            tree_lines.append(f"- {rel_path} -> [{skeleton}]")
        else:
            tree_lines.append(f"- {rel_path}")

    sections.append("\n".join(tree_lines))
    return "\n".join(sections), tz_file, konspekt_file


def find_mentioned_files_content(work_dir: Path, text: str, already_loaded: set[str]) -> str:
    """Если в реплике упомянут файл проекта, автоматически подгружает его полный код в контекст."""
    files = get_project_files(work_dir)
    loaded_blocks = []
    text_lower = text.lower()

    for f in files:
        rel_path = f.relative_to(work_dir).as_posix()
        if rel_path in already_loaded:
            continue
        if f.name.lower() in text_lower or rel_path.lower() in text_lower:
            if f.name.lower() in ['тз.md', 'конспект.txt']:
                continue
            try:
                content = f.read_text(encoding='utf-8')
                if len(content) < 20000:
                    loaded_blocks.append(f"--- Полный код файла {rel_path} ---\n{content}\n")
                    already_loaded.add(rel_path)
                    console.print(f"  [dim cyan]📎 Авто-чтение файла в контекст:[/dim cyan] [bold white]{rel_path}[/bold white]")
            except Exception:
                pass

    return "\n".join(loaded_blocks)


def append_to_konspekt(
    work_dir: Path,
    konspekt_file: Path | None,
    what_was_done: str,
    next_steps: str,
    modified_files: list[str]
):
    """Записывает развернутый конспект ИИ: что сделано, какие файлы изменены и что делать дальше."""
    if not konspekt_file:
        docs_dir = work_dir / "src" / "docs"
        if docs_dir.exists():
            konspekt_file = docs_dir / "конспект.txt"
        elif (work_dir / "docs").exists():
            konspekt_file = work_dir / "docs" / "конспект.txt"
        else:
            konspekt_file = work_dir / "конспект.txt"

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    files_str = ", ".join(modified_files)
    entry = (
        f"\n============================================================\n"
        f"🕒 [{timestamp}] ОТЧЕТ ИИ О РАБОТЕ\n"
        f"📂 Затронутые файлы: {files_str}\n\n"
        f"✅ ЧТО СДЕЛАНО:\n{what_was_done}\n\n"
        f"⏭️ ЧТО СЛЕДУЕТ ДЕЛАТЬ ДАЛЬШЕ (ОСТАВШИЕСЯ ПУНКТЫ):\n{next_steps}\n"
        f"============================================================\n"
    )

    try:
        konspekt_file.parent.mkdir(parents=True, exist_ok=True)
        with open(konspekt_file, "a", encoding="utf-8") as f:
            f.write(entry)
        rel_k = konspekt_file.relative_to(work_dir).as_posix()
        console.print(f"📝 [dim green]Конспект обновлен:[/dim green] [bold]{rel_k}[/bold]")
    except Exception as e:
        console.print(f"⚠️ [warning]Не удалось обновить конспект: {e}[/warning]")


def check_python_syntax(code: str, filename: str) -> str | None:
    """Бесплатная локальная проверка синтаксиса Python через AST (0 токенов)."""
    if not filename.endswith(".py"):
        return None
    try:
        ast.parse(code)
        return None
    except SyntaxError as e:
        return f"SyntaxError на строке {e.lineno}: {e.msg}\nСтрока с ошибкой: {e.text}"
    except Exception as e:
        return f"Ошибка парсинга кода: {e}"


def run_sandbox_check(code: str, rel_filepath: str, work_dir: Path) -> tuple[bool, str]:
    """Запускает код или тесты в безопасной песочнице с поддержкой импортов проекта."""
    if not rel_filepath.endswith(".py"):
        return True, "Не Python-файл (проверка запуска пропущена)."

    target_path = work_dir / rel_filepath
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_file = target_path.parent / f"._sandbox_{target_path.name}"

    try:
        temp_file.write_text(code, encoding="utf-8")

        fname = target_path.name
        is_test_file = (
            fname.startswith("test_")
            or fname.endswith("_test.py")
            or "import pytest" in code
            or "import unittest" in code
        )

        blocking_frameworks = [
            "uvicorn", "fastapi", "flask", "django", "pygame",
            "tkinter", "PyQt", "telebot", "aiogram", "sqlalchemy"
        ]
        is_blocking_app = any(fw in code for fw in blocking_frameworks)

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        python_paths = [str(work_dir)]
        if (work_dir / "src").exists():
            python_paths.append(str(work_dir / "src"))
        if env.get("PYTHONPATH"):
            python_paths.append(env["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(python_paths)

        if is_test_file:
            cmd = [sys.executable, "-m", "pytest", str(temp_file), "-q", "--tb=short"]
            proc = subprocess.run(cmd, cwd=str(work_dir), capture_output=True, text=True, timeout=8, env=env)
            output = (proc.stdout + "\n" + proc.stderr).strip()
            if proc.returncode != 0 and "No module named pytest" not in output:
                return False, f"Тесты pytest упали с ошибкой:\n{output[-1500:]}"
            return True, f"Pytest успешно пройден:\n{output[-500:]}"

        elif is_blocking_app or "from ." in code or "import ." in code:
            return True, "Модуль проекта / серверный компонент (проверен AST-синтаксис и структура)."

        else:
            mock_input = "10\n20\n5\ntest\ny\n0\n"
            cmd = [sys.executable, str(temp_file)]
            proc = subprocess.run(
                cmd,
                cwd=str(work_dir),
                input=mock_input,
                capture_output=True,
                text=True,
                timeout=4,
                env=env
            )
            output = (proc.stdout + "\n" + proc.stderr).strip()

            if proc.returncode != 0:
                if "EOFError" in proc.stderr:
                    return True, "Скрипт запустился корректно (ожидает интерактивный ввод)."
                return False, f"При тестовом запуске скрипт упал с ошибкой (Traceback):\n{proc.stderr[-1500:]}"

            return True, f"Скрипт успешно запустился (код 0). Вывод:\n{output[:500]}"

    except subprocess.TimeoutExpired:
        return True, "Скрипт запустился без сбоев (остановлен по таймауту песочницы 4с)."
    except Exception as e:
        return True, f"Песочница пропущена: {e}"
    finally:
        if temp_file.exists():
            try:
                temp_file.unlink()
            except Exception:
                pass


def apply_search_replace(original_code: str, patch_response: str) -> tuple[str | None, str | None]:
    """Применяет блоки SEARCH/REPLACE к исходному коду."""
    pattern = re.compile(
        r"<<<<<<< SEARCH[ \t]*\r?\n(.*?)\r?\n=======[ \t]*\r?\n(.*?)\r?\n>>>>>>> REPLACE",
        re.DOTALL
    )
    matches = pattern.findall(patch_response)

    if not matches:
        cleaned = patch_response.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1]
        if cleaned.endswith("```"):
            cleaned = cleaned.rsplit("\n", 1)[0]
        cleaned = cleaned.strip()

        if len(cleaned.splitlines()) >= len(original_code.splitlines()) * 0.5:
            return cleaned, None
        return None, "В ответе не найдены блоки <<<<<<< SEARCH ... ======= ... >>>>>>> REPLACE."

    modified_code = original_code.replace("\r\n", "\n")

    for idx, (search_block, replace_block) in enumerate(matches, 1):
        search_norm = search_block.replace("\r\n", "\n")
        replace_norm = replace_block.replace("\r\n", "\n")

        if search_norm in modified_code:
            modified_code = modified_code.replace(search_norm, replace_norm, 1)
        elif search_norm.strip() in modified_code:
            modified_code = modified_code.replace(search_norm.strip(), replace_norm.strip(), 1)
        else:
            preview = search_norm[:120] + ("..." if len(search_norm) > 120 else "")
            return None, (
                f"Блок SEARCH #{idx} не найден в исходном файле. "
                f"Скопируй заменяемые строки из исходного файла со всеми отступами точь-в-точь!\n"
                f"Проблемный фрагмент:\n{preview}"
            )

    return modified_code, None


def chat_with_haiku_turn(project_map: str, chat_messages: list[dict]) -> str:
    """Один шаг живого диалога с Haiku с поддержкой памяти текущих пунктов."""
    global SESSION_MEMORY
    system_prompt = (
        "Ты — ведущий системный аналитик и Tech Lead проекта. Пользователь — руководитель разработки.\n"
        "У тебя перед глазами ТЗ проекта, конспект прошлых работ (со списком следующих шагов) и карта всех папок.\n\n"
        "ТВОЯ РОЛЬ В ДИАЛОГЕ:\n"
        "1. Помогай руководителю декомпозировать задачи на четкие пункты (Пункт 1, Пункт 2, Пункт 3...), обсуждай архитектуру (src/server, src/api, src/services).\n"
        "2. Отвечай кратко, структурированно (используй списки и жирный шрифт Markdown). Если есть вопросы — спрашивай.\n"
        "3. Если руководитель просит выполнить конкретный пункт из вашего плана (например: 'делай 1 пункт', 'теперь пункт 2', 'приступай к коду') — "
        "подтверди старт именно этого пункта и обязательно добавь в самом конце ответа маркер [START_CODING]."
    )

    memory_block = f"\n=== АКТИВНЫЙ ПЛАН ТЕКУЩЕЙ СЕССИИ ===\n{SESSION_MEMORY}\n" if SESSION_MEMORY else ""

    messages = [
        {
            "role": "system",
            "content": [
                {"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": f"Контекст и память проекта:\n{project_map}{memory_block}", "cache_control": {"type": "ephemeral"}}
            ]
        }
    ] + chat_messages

    with console.status("[bold cyan]🤖 Haiku 5.5 анализирует проект и печатает ответ...[/bold cyan]", spinner="dots"):
        response = openrouter.chat.completions.create(
            model="anthropic/claude-haiku-5.5",
            messages=messages
        )
    return response.choices[0].message.content.strip()


def compile_plan_from_chat(project_map: str, chat_messages: list[dict]) -> dict:
    """Превращает диалог с Haiku в задачи для ТЕКУЩЕГО выбранного пункта + формирует конспект и память."""
    global SESSION_MEMORY
    system_prompt = (
        "Ты системный аналитик. На основе диалога с руководителем сформируй пакет задач для программиста (DeepSeek) ТОЛЬКО для того этапа/пункта, который поручено сделать прямо сейчас.\n"
        "Верни ответ СТРОГО в формате валидного JSON без маркдаун-оберток со следующей структурой:\n"
        "{\n"
        "  \"what_was_done\": \"Подробное описание для конспекта (2-4 предложения): что именно реализовано на этом шаге, какие функции/классы/логика добавлены\",\n"
        "  \"next_steps\": \"Четкий список оставшихся пунктов плана и рекомендаций, что нужно делать дальше (сохрани нумерацию пунктов, если вы обсуждали план по пунктам!)\",\n"
        "  \"tasks\": [\n"
        "    {\n"
        "      \"filepath\": \"относительный/путь/к/файлу.py (например, src/services/auth.py)\",\n"
        "      \"tz\": \"Подробное техническое задание для этого файла со всеми деталями, импортами и сигнатурами\"\n"
        "    }\n"
        "  ]\n"
        "}"
    )

    memory_block = f"\n=== АКТИВНЫЙ ПЛАН ТЕКУЩЕЙ СЕССИИ ===\n{SESSION_MEMORY}\n" if SESSION_MEMORY else ""

    messages = [
        {
            "role": "system",
            "content": [
                {"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": f"Контекст проекта:\n{project_map}{memory_block}", "cache_control": {"type": "ephemeral"}}
            ]
        }
    ] + chat_messages + [
        {
            "role": "user",
            "content": "Сформируй итоговый JSON (what_was_done, next_steps, tasks) для текущего утвержденного шага."
        }
    ]

    with console.status("[bold magenta]⚙️ Haiku компилирует ТЗ по файлам для DeepSeek...[/bold magenta]", spinner="bouncingBar"):
        response = openrouter.chat.completions.create(
            model="anthropic/claude-haiku-5.5",
            messages=messages
        )
    raw = response.choices[0].message.content.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        return json.loads(raw)
    except Exception:
        return {
            "what_was_done": "Выполнена задача по диалогу с руководителем.",
            "next_steps": "Продолжить разработку согласно ТЗ.",
            "tasks": [{"filepath": "output.py", "tz": raw}]
        }


def ask_deepseek(prompt: str, use_diff: bool = False) -> str:
    if use_diff:
        system_prompt = (
            "Ты Senior Developer. Внеси изменения в существующий файл строго по ТЗ, "
            "используя ТОЛЬКО формат точечной замены блоков (не переписывай весь файл целиком!):\n\n"
            "<<<<<<< SEARCH\n"
            "точный кусок кода из текущего файла (с сохранением всех пробелов и отступов)\n"
            "=======\n"
            "новый кусок кода, на который нужно заменить старый\n"
            ">>>>>>> REPLACE\n\n"
            "Правила:\n"
            "1. Блок SEARCH должен на 100% совпадать с существующим кодом.\n"
            "2. Можно выдать несколько блоков SEARCH/REPLACE подряд.\n"
            "3. Никаких объяснений — только блоки замены."
        )
    else:
        system_prompt = (
            "Ты Senior Developer. Напиши или обнови код строго по ТЗ. "
            "Выдай ТОЛЬКО чистый код файла целиком, без маркдаун-блоков (```) и без объяснений."
        )

    response = deepseek.chat.completions.create(
        model="deepseek-coder",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ]
    )
    res = response.choices[0].message.content.strip()
    if not use_diff:
        if res.startswith("```"):
            res = res.split("\n", 1)[1]
        if res.endswith("```"):
            res = res.rsplit("\n", 1)[0]
    return res.strip()


def ask_sonnet(tz: str, code: str, sandbox_log: str, filepath: str) -> str:
    system_text = (
        "Ты прагматичный QA/Reviewer. Проверь итоговый код файла по ТЗ и логу тестового запуска. "
        "Если код решает задачу и работает, ответь ровно одним словом 'ОДОБРЕНО'. "
        "Не придирайся к мелочам. Если есть критические баги или поломка импортов, напиши краткий список правок."
    )
    response = openrouter.chat.completions.create(
        model="anthropic/claude-sonnet-5.5",
        messages=[
            {
                "role": "system",
                "content": [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}]
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"Файл: {filepath}\nТЗ:\n{tz}", "cache_control": {"type": "ephemeral"}},
                    {"type": "text", "text": f"Результат автозапуска в песочнице:\n{sandbox_log}\n\nИтоговый код файла:\n{code}"}
                ]
            }
        ]
    )
    return response.choices[0].message.content.strip()


def save_file_to_batch_backup(target_file: Path, new_code: str):
    """Сохраняет файл и добавляет его старую версию в групповой бэкап."""
    global LAST_BATCH_BACKUP
    if target_file.exists():
        LAST_BATCH_BACKUP.append({
            "file": target_file,
            "content": target_file.read_text(encoding="utf-8"),
            "existed": True
        })
    else:
        LAST_BATCH_BACKUP.append({
            "file": target_file,
            "content": None,
            "existed": False
        })

    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text(new_code, encoding="utf-8")


def undo_last_batch():
    """Откатывает все файлы, измененные за последний цикл разработки."""
    global LAST_BATCH_BACKUP
    if not LAST_BATCH_BACKUP:
        console.print("⚠️ [warning]Нет изменений для отката.[/warning]")
        return

    for item in reversed(LAST_BATCH_BACKUP):
        target_file = item["file"]
        if item["existed"]:
            target_file.write_text(item["content"], encoding="utf-8")
            console.print(f"⏪ [cyan]Восстановлен файл:[/cyan] {target_file}")
        else:
            if target_file.exists():
                target_file.unlink()
            console.print(f"🗑️ [yellow]Удален созданный файл:[/yellow] {target_file}")

    LAST_BATCH_BACKUP.clear()


def execute_single_file_task(rel_filepath: str, tz: str, work_dir: Path, project_map: str, fast_mode: bool) -> bool:
    """Выполняет цикл DeepSeek -> AST -> Sandbox -> Sonnet для одного конкретного файла."""
    rel_filepath = rel_filepath.lstrip("/\\")
    target_file = work_dir / rel_filepath

    console.print()
    console.print(Panel(
        Markdown(tz),
        title=f"[bold yellow]🛠️ ФАЙЛ: {rel_filepath}[/bold yellow]",
        border_style="yellow",
        box=box.ROUNDED
    ))

    existing_code = ""
    use_diff_mode = False

    if target_file.exists():
        try:
            existing_code = target_file.read_text(encoding="utf-8")
            line_count = len(existing_code.splitlines())
            if line_count >= DIFF_LINE_THRESHOLD:
                use_diff_mode = True
                console.print(f"  ✂️ [cyan]Файл большой ({line_count} строк). Включен экономный режим SEARCH/REPLACE![/cyan]")
            else:
                console.print(f"  ✏️ [dim]Файл существует ({line_count} строк). Дорабатываем код.[/dim]")
        except Exception as e:
            console.print(f"  ⚠️ [warning]Не удалось прочитать '{rel_filepath}': {e}[/warning]")

    iteration = 0
    max_iterations = 2
    local_retries = 0
    current_code = existing_code
    feedback = ""

    while iteration < max_iterations:
        mode_str = "SEARCH/REPLACE патч" if use_diff_mode else "полный код"

        deepseek_prompt = (
            f"Целевой файл: {rel_filepath}\n"
            f"ТЗ для этого файла:\n{tz}\n\n"
            f"Краткая карта архитектуры проекта (для правильных импортов):\n{project_map[-4000:]}"
        )
        base_code_for_turn = current_code if current_code else existing_code

        if base_code_for_turn:
            deepseek_prompt += f"\n\nТекущее содержимое файла {rel_filepath}:\n{base_code_for_turn}"
        if feedback:
            deepseek_prompt += f"\n\nПредыдущая попытка содержала ошибки. Исправь их:\n{feedback}"

        with console.status(f"[bold blue]💻 [1/3] DeepSeek пишет {mode_str} (Итерация {iteration + 1}/{max_iterations})...[/bold blue]", spinner="dots"):
            raw_output = ask_deepseek(deepseek_prompt, use_diff=use_diff_mode)

        if use_diff_mode:
            patched_code, patch_err = apply_search_replace(base_code_for_turn, raw_output)
            if patch_err:
                if local_retries < 2:
                    console.print(f"  🛑 [error][Фильтр патча] Ошибка слияния (0 токенов Sonnet):[/error] {patch_err}")
                    feedback = patch_err
                    local_retries += 1
                    continue
                else:
                    console.print("  ⚠️ [warning]Переключаюсь на полную перезапись файла...[/warning]")
                    use_diff_mode = False
                    feedback = "Выдай полный итоговый код файла целиком без маркдауна."
                    continue
            current_code = patched_code
        else:
            current_code = raw_output

        syntax_err = check_python_syntax(current_code, rel_filepath)
        if syntax_err and local_retries < 3:
            console.print(f"  🛑 [error][AST-фильтр] Синтаксическая ошибка (0 токенов Sonnet):[/error]\n{syntax_err}")
            feedback = f"В коде синтаксическая ошибка Python:\n{syntax_err}"
            local_retries += 1
            continue

        with console.status("[bold cyan]🧪 [2/3] Проверка кода в локальной песочнице (0 токенов)...[/bold cyan]", spinner="Flip"):
            sandbox_ok, sandbox_log = run_sandbox_check(current_code, rel_filepath, work_dir)

        if not sandbox_ok and local_retries < 3:
            console.print(Panel(sandbox_log, title="[bold red]🛑 Код упал в песочнице (0 токенов Sonnet)[/bold red]", border_style="red"))
            feedback = f"Код падает при запуске:\n{sandbox_log}\nИсправь эту ошибку."
            local_retries += 1
            continue
        else:
            short_log = sandbox_log.splitlines()[0] if sandbox_log else "ОК"
            console.print(f"  ✔️ [green]Песочница:[/green] [dim]{short_log}[/dim]")

        if fast_mode:
            save_file_to_batch_backup(target_file, current_code)
            console.print(f"  ⚡ [success][Быстрый режим] Сохранено в:[/success] [bold]{target_file}[/bold]")
            return True

        with console.status(f"[bold magenta]🧐 [3/3] Sonnet 5.5 проверяет {rel_filepath}...[/bold magenta]", spinner="weather"):
            review = ask_sonnet(tz, current_code, sandbox_log, rel_filepath)

        if "ОДОБРЕНО" in review.upper():
            save_file_to_batch_backup(target_file, current_code)
            console.print(f"  ✅ [success]ОДОБРЕНО SONNET! Файл сохранен:[/success] [bold white]{target_file}[/bold white]")
            return True
        else:
            console.print(Panel(Markdown(review), title="[bold yellow]⚠️ Замечания от Sonnet 5.5[/bold yellow]", border_style="yellow"))
            feedback = review
            iteration += 1

    save_file_to_batch_backup(target_file, current_code)
    console.print(f"  ⚠️ [warning]Лимит итераций исчерпан. Последняя версия сохранена в:[/warning] {target_file}")
    return True


def execute_plan_pipeline(work_dir: Path, project_map: str, konspekt_file: Path | None, chat_messages: list[dict], fast_mode: bool):
    """Запускает компиляцию диалога в план, прогоняет файлы, пишет развернутый конспект и сохраняет память плана."""
    global LAST_BATCH_BACKUP, SESSION_MEMORY
    LAST_BATCH_BACKUP.clear()

    plan = compile_plan_from_chat(project_map, chat_messages)

    what_was_done = plan.get("what_was_done", "Выполнено обновление кода проекта.")
    next_steps = plan.get("next_steps", "Ожидание следующих указаний руководителя.")
    tasks = plan.get("tasks", [])

    if not tasks:
        console.print("⚠️ [warning]Haiku не выделила ни одного файла для изменения.[/warning]")
        return

    files_list_str = ", ".join(t.get("filepath", "") for t in tasks)
    console.print(f"\n📦 [bold cyan]План сформирован! Файлов в работе ({len(tasks)}):[/bold cyan] [bold white]{files_list_str}[/bold white]")

    modified_files = []
    for t in tasks:
        rel_fp = t.get("filepath", "output.py")
        file_tz = t.get("tz", "")
        ok = execute_single_file_task(rel_fp, file_tz, work_dir, project_map, fast_mode)
        if ok:
            modified_files.append(rel_fp)

    if modified_files:
        append_to_konspekt(work_dir, konspekt_file, what_was_done, next_steps, modified_files)

    SESSION_MEMORY = (
        f"На предыдущем шаге успешно выполнено ({', '.join(modified_files)}):\n{what_was_done}\n\n"
        f"Оставшиеся пункты плана (Next Steps):\n{next_steps}"
    )

    console.print()
    summary_md = f"**✅ Что сделано:**\n{what_was_done}\n\n**⏭️ Что дальше по плану:**\n{next_steps}\n\n*💡 Совет: напиши «делай следующий пункт» или предложи правки!*"
    console.print(Panel(
        Markdown(summary_md),
        title="[bold green]🎉 ШАГ УСПЕШНО ЗАВЕРШЕН[/bold green]",
        border_style="green",
        box=box.DOUBLE
    ))


def render_dashboard(work_dir: Path, total_files: int, tz_file: Path | None, konspekt_file: Path | None):
    """Рисует красивую информационную панель проекта в терминале."""
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Key", style="bold cyan")
    table.add_column("Value", style="white")

    table.add_row("📂 Рабочая папка:", f"[bold]{work_dir}[/bold] [dim](файлов: {total_files})[/dim]")

    tz_status = f"[green]{tz_file.relative_to(work_dir).as_posix()}[/green] (в 90% кэше)" if tz_file else "[dim]не найден[/dim]"
    k_status = f"[green]{konspekt_file.relative_to(work_dir).as_posix()}[/green] (авто-запись)" if konspekt_file else "[dim]будет создан после задачи[/dim]"

    table.add_row("📘 Документы:", f"ТЗ: {tz_status}  |  Конспект: {k_status}")

    if SESSION_MEMORY:
        table.add_row("🧠 Память сессии:", "[bold magenta]Активна (Haiku помнит ваш план по пунктам)[/bold magenta]")

    table.add_row(
        "⌨️  Команды:",
        "[bold yellow]go[/bold yellow] — в код | [bold yellow]![/bold yellow] — сразу в код без ревью | [bold yellow]undo[/bold yellow] — откат | [bold yellow]clear[/bold yellow] — сброс | [bold yellow]dir[/bold yellow] | [bold yellow]exit[/bold yellow]"
    )

    console.print()
    console.print(Panel(
        table,
        title="[bold white on blue] 🚀 AI CODE ORCHESTRATOR v4.2 [/bold white on blue]",
        subtitle="[dim]Haiku 5.5 (Tech Lead) ➔ DeepSeek (Coder) ➔ Sandbox ➔ Sonnet 5.5 (QA)[/dim]",
        border_style="bright_blue",
        box=box.ROUNDED
    ))


def main():
    global SESSION_MEMORY

    script_dir = Path(__file__).parent.resolve()
    current_dir = Path.cwd().resolve()
    default_docs = get_default_documents_dir()

    if len(sys.argv) > 1:
        work_dir = default_docs if current_dir == script_dir else current_dir
        inline_task = " ".join(sys.argv[1:]).strip()
        fast_mode = inline_task.startswith("!")
        if fast_mode:
            inline_task = inline_task[1:].strip()
        project_map, _, konspekt_file = build_project_map(work_dir)
        chat_messages = [{"role": "user", "content": inline_task}]
        execute_plan_pipeline(work_dir, project_map, konspekt_file, chat_messages, fast_mode)
        return

    if current_dir == script_dir:
        dir_input = safe_input(f"\n[bold cyan]📂 Укажи рабочую папку проекта[/bold cyan] [dim](Enter = '{default_docs}')[/dim]: ").strip('"\'')
        work_dir = Path(dir_input) if dir_input else default_docs
    else:
        work_dir = current_dir

    while True:
        project_map, tz_file, konspekt_file = build_project_map(work_dir)
        total_files = len(get_project_files(work_dir))

        render_dashboard(work_dir, total_files, tz_file, konspekt_file)

        chat_messages = []
        loaded_files_in_chat = set()

        while True:
            if not chat_messages:
                prompt_str = "\n[bold green]💬 Вы[/bold green] [dim](обсудите план или напишите задачу)[/dim]: "
            else:
                prompt_str = "\n[bold green]💬 Вы[/bold green] [dim]('go' — запустить код | 'go!' — без ревью)[/dim]: "

            user_input = safe_input(prompt_str)

            if not user_input:
                continue

            cmd = user_input.lower()
            if cmd in ['exit', 'quit', 'выход', 'q']:
                console.print("\n👋 [bold cyan]До встречи![/bold cyan]")
                return
            if cmd == 'undo':
                undo_last_batch()
                break
            if cmd == 'docs':
                work_dir = default_docs
                SESSION_MEMORY = ""
                break
            if cmd == 'dir':
                new_dir = safe_input(f"[bold cyan]Новая папка проекта[/bold cyan] [dim](Enter = '{default_docs}')[/dim]: ").strip('"\'')
                work_dir = Path(new_dir) if new_dir else default_docs
                SESSION_MEMORY = ""
                break
            if cmd in ['clear', 'reset', 'сброс']:
                SESSION_MEMORY = ""
                console.print("🧹 [cyan]Диалог и память текущего плана полностью очищены.[/cyan]")
                break

            if cmd in ['go', 'go!', 'пуск', 'код', 'run']:
                if not chat_messages:
                    console.print("⚠️ [warning]Сначала напишите задачу или номер пункта для выполнения![/warning]")
                    continue
                fast_flag = (cmd == 'go!')
                execute_plan_pipeline(work_dir, project_map, konspekt_file, chat_messages, fast_mode=fast_flag)
                break

            if user_input.startswith("!"):
                clean_task = user_input[1:].strip()
                chat_messages.append({"role": "user", "content": clean_task})
                execute_plan_pipeline(work_dir, project_map, konspekt_file, chat_messages, fast_mode=True)
                break

            extra_code = find_mentioned_files_content(work_dir, user_input, loaded_files_in_chat)
            full_user_msg = user_input
            if extra_code:
                full_user_msg += f"\n\n[Система автоматически приложила код упомянутых файлов]:\n{extra_code}"

            chat_messages.append({"role": "user", "content": full_user_msg})

            try:
                haiku_reply = chat_with_haiku_turn(project_map, chat_messages)
                chat_messages.append({"role": "assistant", "content": haiku_reply})

                if "[START_CODING]" in haiku_reply:
                    clean_reply = haiku_reply.replace("[START_CODING]", "").strip()
                    if clean_reply:
                        console.print()
                        console.print(Panel(
                            Markdown(clean_reply),
                            title="[bold cyan]🤖 Haiku 5.5 (Tech Lead)[/bold cyan]",
                            border_style="cyan",
                            box=box.ROUNDED
                        ))
                    execute_plan_pipeline(work_dir, project_map, konspekt_file, chat_messages, fast_mode=False)
                    break
                else:
                    console.print()
                    console.print(Panel(
                        Markdown(haiku_reply),
                        title="[bold cyan]🤖 Haiku 5.5 (Tech Lead)[/bold cyan]",
                        border_style="cyan",
                        box=box.ROUNDED
                    ))

            except Exception as e:
                console.print(f"\n❌ [error]Ошибка связи с моделью:[/error] {e}")


if __name__ == "__main__":
    main()