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

# Настройка потоков специально для Git Bash (mintty) без блокировок
os.environ["PYTHONIOENCODING"] = "utf-8"
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8")

custom_theme = Theme({
    "info": "cyan",
    "warning": "bold yellow",
    "error": "bold red",
    "success": "bold green",
})

console = Console(
    theme=custom_theme,
    force_terminal=False,
    legacy_windows=False,
    color_system="truecolor",
    width=100
)

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

LAST_BATCH_BACKUP = []
SESSION_MEMORY = ""
DIFF_LINE_THRESHOLD = 60

# Принудительно заданные пользователем пути к ТЗ и Конспекту через слэш-команды
FORCED_TZ_PATH: Path | None = None
FORCED_KONSPEKT_PATH: Path | None = None

IGNORE_DIRS = {
    '.git', 'venv', '.venv', 'env', '__pycache__',
    'node_modules', '.idea', '.vscode', 'dist', 'build', '.pytest_cache'
}
CODE_EXTENSIONS = {
    '.py', '.txt', '.json', '.md', '.html', '.css',
    '.js', '.ts', '.php', '.sql', '.yaml', '.yml'
}


def bash_input(prompt_text: str) -> str:
    """Надежный неблокирующий ввод для Git Bash (mintty) и обычных консолей."""
    console.print(prompt_text, end="")
    sys.stdout.flush()
    line = sys.stdin.readline()
    if not line:
        return "/exit"
    return line.strip()


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
    """Строит легкий AST-скелет Python-файла (классы и сигнатуры функций за копейки токенов)."""
    try:
        source = file_path.read_text(encoding='utf-8', errors='replace')
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
    """Собирает карту проекта, гибко ищет ТЗ и конспект (с учетом принудительно заданных путей)."""
    global FORCED_TZ_PATH, FORCED_KONSPEKT_PATH
    files = get_project_files(work_dir)
    if not files and not FORCED_TZ_PATH and not FORCED_KONSPEKT_PATH:
        return "Проект пока пуст.", None, None

    tz_file = FORCED_TZ_PATH if (FORCED_TZ_PATH and FORCED_TZ_PATH.exists()) else None
    konspekt_file = FORCED_KONSPEKT_PATH if (FORCED_KONSPEKT_PATH and FORCED_KONSPEKT_PATH.exists()) else None

    # Если путь не задан принудительно, ищем автоматически
    if not tz_file or not konspekt_file:
        for f in files:
            stem_lower = f.stem.lower()
            rel_lower = f.relative_to(work_dir).as_posix().lower()

            if not konspekt_file and any(k in stem_lower for k in ['конспект', 'konspekt', 'changelog', 'progress']):
                konspekt_file = f
                continue

            if not tz_file:
                is_tz_name = (
                    stem_lower in ['тз', 'tz', 'spec', 'specification', 'требования']
                    or stem_lower.startswith(('тз ', 'тз-', 'тз_', 'тз—', 'tz ', 'tz-', 'tz_', 'spec'))
                    or 'тз' in stem_lower
                )
                if is_tz_name and f.suffix.lower() in ['.md', '.txt']:
                    if tz_file is None or 'docs/' in rel_lower:
                        tz_file = f

    sections = []

    if tz_file:
        try:
            rel_tz = tz_file.relative_to(work_dir).as_posix() if tz_file.is_relative_to(work_dir) else str(tz_file)
            tz_text = tz_file.read_text(encoding='utf-8', errors='replace')[:60000]
            sections.append(f"=== ГЛАВНОЕ ТЗ ПРОЕКТА ({rel_tz}) ===\n{tz_text}\n")
        except Exception as e:
            sections.append(f"=== ОШИБКА ЧТЕНИЯ ТЗ: {e} ===")

    if konspekt_file:
        try:
            rel_k = konspekt_file.relative_to(work_dir).as_posix() if konspekt_file.is_relative_to(work_dir) else str(konspekt_file)
            k_text = konspekt_file.read_text(encoding='utf-8', errors='replace')[-20000:]
            sections.append(f"=== КОНСПЕКТ ПРОДЕЛАННЫХ РАБОТ И ПЛАН ({rel_k}) ===\n{k_text}\n")
        except Exception:
            pass

    tree_lines = ["=== СТРУКТУРА И КАРТА ФАЙЛОВ ПРОЕКТА ==="]
    for f in sorted(files):
        rel_path = f.relative_to(work_dir).as_posix()
        if f in (tz_file, konspekt_file):
            tree_lines.append(f"- {rel_path} [Документация проекта — УЖЕ ЗАГРУЖЕНА ВЫШЕ]")
        elif f.suffix == '.py':
            skeleton = extract_python_skeleton(f)
            tree_lines.append(f"- {rel_path} -> [{skeleton}]")
        else:
            tree_lines.append(f"- {rel_path}")

    sections.append("\n".join(tree_lines))
    return "\n".join(sections), tz_file, konspekt_file


def find_mentioned_files_content(work_dir: Path, text: str, already_loaded: set[str]) -> str:
    """Умная подгрузка любых упомянутых в реплике файлов проекта."""
    files = get_project_files(work_dir)
    loaded_blocks = []
    norm_text = text.replace('\\', '/').replace('—', '-').replace('–', '-').lower()

    for f in files:
        rel_path = f.relative_to(work_dir).as_posix()
        if rel_path in already_loaded:
            continue

        norm_name = f.name.replace('—', '-').replace('–', '-').lower()
        norm_stem = f.stem.replace('—', '-').replace('–', '-').lower()
        norm_rel = rel_path.replace('—', '-').replace('–', '-').lower()

        matched = (
            norm_name in norm_text
            or norm_rel in norm_text
            or (len(norm_stem) > 3 and norm_stem in norm_text)
        )

        if matched:
            try:
                content = f.read_text(encoding='utf-8', errors='replace')
                if len(content) > 60000:
                    content = content[:60000] + "\n...[Файл обрезан до 60 000 символов]..."
                loaded_blocks.append(f"--- Полный текст файла {rel_path} ---\n{content}\n")
                already_loaded.add(rel_path)
                console.print(f"  📎 [cyan]Авто-чтение файла в контекст:[/cyan] [bold]{rel_path}[/bold]")
            except Exception as e:
                console.print(f"  ⚠️ [warning]Ошибка чтения {rel_path}: {e}[/warning]")

    return "\n".join(loaded_blocks)


def append_to_konspekt(
    work_dir: Path,
    konspekt_file: Path | None,
    what_was_done: str,
    next_steps: str,
    modified_files: list[str]
):
    """Записывает развернутый конспект ИИ: что сделано, какие файлы изменены и что делать дальше."""
    global FORCED_KONSPEKT_PATH
    target_konspekt = FORCED_KONSPEKT_PATH if FORCED_KONSPEKT_PATH else konspekt_file

    if not target_konspekt:
        docs_dir = work_dir / "src" / "docs"
        if docs_dir.exists():
            target_konspekt = docs_dir / "конспект.txt"
        elif (work_dir / "docs").exists():
            target_konspekt = work_dir / "docs" / "конспект.txt"
        else:
            target_konspekt = work_dir / "конспект.txt"

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
        target_konspekt.parent.mkdir(parents=True, exist_ok=True)
        with open(target_konspekt, "a", encoding="utf-8") as f:
            f.write(entry)
        rel_k = target_konspekt.relative_to(work_dir).as_posix() if target_konspekt.is_relative_to(work_dir) else str(target_konspekt)
        console.print(f"📝 [green]Конспект обновлен:[/green] [bold]{rel_k}[/bold]")
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
    """Запускает код или тесты в безопасной песочнице."""
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
        else:
            mock_input = "10\n20\n5\ntest\ny\n0\n"
            cmd = [sys.executable, str(temp_file)]
            proc = subprocess.run(cmd, cwd=str(work_dir), input=mock_input, capture_output=True, text=True, timeout=4, env=env)
            output = (proc.stdout + "\n" + proc.stderr).strip()
            if proc.returncode != 0 and "EOFError" not in proc.stderr:
                return False, f"Скрипт упал с ошибкой:\n{proc.stderr[-1500:]}"
            return True, f"Скрипт успешно запустился (код 0)."
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
            return None, f"Блок SEARCH #{idx} не найден в файле:\n{preview}"

    return modified_code, None


def chat_with_haiku_turn(project_map: str, chat_messages: list[dict]) -> str:
    """Один шаг живого диалога с Haiku с поддержкой памяти текущих пунктов."""
    global SESSION_MEMORY
    system_prompt = (
        "Ты — ведущий системный аналитик и Tech Lead проекта. Пользователь — руководитель разработки.\n"
        "У тебя перед глазами ТЗ проекта, конспект прошлых работ (со списком следующих шагов) и карта всех папок.\n\n"
        "ТВОЯ РОЛЬ В ДИАЛОГЕ:\n"
        "1. Помогай руководителю декомпозировать задачи на четкие пункты (Пункт 1, Пункт 2, Пункт 3...), обсуждай архитектуру.\n"
        "2. Отвечай кратко, структурированно (используй списки и жирный шрифт Markdown).\n"
        "3. Если руководитель просит выполнить конкретный пункт из плана (или набрал команду /go) — "
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

    console.print("  ⏳ [cyan]Haiku 5.5 анализирует проект и формирует ответ...[/cyan]")
    response = openrouter.chat.completions.create(
        model="anthropic/claude-haiku-5.5",
        messages=messages
    )
    return response.choices[0].message.content.strip()


def compile_plan_from_chat(project_map: str, chat_messages: list[dict]) -> dict:
    """Превращает диалог с Haiku в задачи для ТЕКУЩЕГО выбранного пункта."""
    global SESSION_MEMORY
    system_prompt = (
        "Ты системный аналитик. На основе диалога с руководителем сформируй пакет задач для программиста (DeepSeek) ТОЛЬКО для того этапа/пункта, который поручено сделать прямо сейчас.\n"
        "Верни ответ СТРОГО в формате валидного JSON без маркдаун-оберток со следующей структурой:\n"
        "{\n"
        "  \"what_was_done\": \"Подробное описание для конспекта (2-4 предложения): что именно реализовано на этом шаге\",\n"
        "  \"next_steps\": \"Четкий список оставшихся пунктов плана и рекомендаций, что делать дальше\",\n"
        "  \"tasks\": [\n"
        "    {\n"
        "      \"filepath\": \"относительный/путь/к/файлу.py\",\n"
        "      \"tz\": \"Подробное техническое задание для этого файла\"\n"
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

    console.print("\n⚙️ [bold magenta]Haiku компилирует ТЗ по файлам для DeepSeek...[/bold magenta]")
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
            "используя ТОЛЬКО формат точечной замены блоков:\n\n"
            "<<<<<<< SEARCH\nточный кусок кода\n=======\nновый кусок кода\n>>>>>>> REPLACE\n"
            "Никаких объяснений — только блоки замены."
        )
    else:
        system_prompt = "Ты Senior Developer. Напиши чистый код файла целиком без маркдауна и объяснений."

    response = deepseek.chat.completions.create(
        model="deepseek-coder",
        messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}]
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
        "Ты прагматичный QA/Reviewer. Проверь итоговый код файла по ТЗ и логу. "
        "Если код работает, ответь ровно одним словом 'ОДОБРЕНО'. Иначе напиши список критических правок."
    )
    response = openrouter.chat.completions.create(
        model="anthropic/claude-sonnet-5.5",
        messages=[
            {"role": "system", "content": [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}]},
            {"role": "user", "content": [
                {"type": "text", "text": f"Файл: {filepath}\nТЗ:\n{tz}", "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": f"Результат песочницы:\n{sandbox_log}\n\nКод:\n{code}"}
            ]}
        ]
    )
    return response.choices[0].message.content.strip()


def save_file_to_batch_backup(target_file: Path, new_code: str):
    global LAST_BATCH_BACKUP
    if target_file.exists():
        LAST_BATCH_BACKUP.append({"file": target_file, "content": target_file.read_text(encoding="utf-8", errors="replace"), "existed": True})
    else:
        LAST_BATCH_BACKUP.append({"file": target_file, "content": None, "existed": False})
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text(new_code, encoding="utf-8")


def undo_last_batch():
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
            console.print(f"🗑️ [yellow]Удален файл:[/yellow] {target_file}")
    LAST_BATCH_BACKUP.clear()


def execute_single_file_task(rel_filepath: str, tz: str, work_dir: Path, project_map: str, fast_mode: bool) -> bool:
    rel_filepath = rel_filepath.lstrip("/\\")
    target_file = work_dir / rel_filepath

    console.print()
    console.print(Panel(Markdown(tz), title=f"[bold yellow]🛠️ ФАЙЛ: {rel_filepath}[/bold yellow]", border_style="yellow", box=box.ROUNDED))

    existing_code = ""
    use_diff_mode = False

    if target_file.exists():
        try:
            existing_code = target_file.read_text(encoding="utf-8", errors="replace")
            line_count = len(existing_code.splitlines())
            if line_count >= DIFF_LINE_THRESHOLD:
                use_diff_mode = True
                console.print(f"  ✂️ [cyan]Файл большой ({line_count} строк). Включен SEARCH/REPLACE![/cyan]")
        except Exception:
            pass

    iteration = 0
    max_iterations = 2
    local_retries = 0
    current_code = existing_code
    feedback = ""

    while iteration < max_iterations:
        mode_str = "SEARCH/REPLACE патч" if use_diff_mode else "полный код"
        deepseek_prompt = f"Целевой файл: {rel_filepath}\nТЗ:\n{tz}\n\nКарта проекта:\n{project_map[-4000:]}"
        base_code = current_code if current_code else existing_code

        if base_code:
            deepseek_prompt += f"\n\nТекущий код:\n{base_code}"
        if feedback:
            deepseek_prompt += f"\n\nИсправь ошибку:\n{feedback}"

        console.print(f"  💻 [bold blue]DeepSeek пишет {mode_str} (Итерация {iteration + 1})...[/bold blue]")
        raw_output = ask_deepseek(deepseek_prompt, use_diff=use_diff_mode)

        if use_diff_mode:
            patched_code, patch_err = apply_search_replace(base_code, raw_output)
            if patch_err:
                if local_retries < 2:
                    console.print(f"  🛑 [error]Ошибка слияния патча:[/error] {patch_err}")
                    feedback = patch_err
                    local_retries += 1
                    continue
                else:
                    use_diff_mode = False
                    feedback = "Выдай полный итоговый код файла целиком без маркдауна."
                    continue
            current_code = patched_code
        else:
            current_code = raw_output

        syntax_err = check_python_syntax(current_code, rel_filepath)
        if syntax_err and local_retries < 3:
            console.print(f"  🛑 [error]Синтаксическая ошибка AST:[/error]\n{syntax_err}")
            feedback = f"Синтаксическая ошибка:\n{syntax_err}"
            local_retries += 1
            continue

        console.print("  🧪 [cyan]Проверка в песочнице...[/cyan]")
        sandbox_ok, sandbox_log = run_sandbox_check(current_code, rel_filepath, work_dir)

        if not sandbox_ok and local_retries < 3:
            console.print(Panel(sandbox_log, title="[bold red]🛑 Код упал в песочнице[/bold red]", border_style="red"))
            feedback = f"Код падает при запуске:\n{sandbox_log}"
            local_retries += 1
            continue

        if fast_mode:
            save_file_to_batch_backup(target_file, current_code)
            console.print(f"  ⚡ [success]Сохранено (Fast Mode):[/success] {target_file}")
            return True

        console.print(f"  🧐 [bold magenta]Sonnet проверяет {rel_filepath}...[/bold magenta]")
        review = ask_sonnet(tz, current_code, sandbox_log, rel_filepath)

        if "ОДОБРЕНО" in review.upper():
            save_file_to_batch_backup(target_file, current_code)
            console.print(f"  ✅ [success]ОДОБРЕНО! Сохранено:[/success] {target_file}")
            return True
        else:
            console.print(Panel(Markdown(review), title="[bold yellow]⚠️ Замечания от Sonnet[/bold yellow]", border_style="yellow"))
            feedback = review
            iteration += 1

    save_file_to_batch_backup(target_file, current_code)
    console.print(f"  ⚠️ [warning]Лимит итераций исчерпан. Сохранено:[/warning] {target_file}")
    return True


def execute_plan_pipeline(work_dir: Path, project_map: str, konspekt_file: Path | None, chat_messages: list[dict], fast_mode: bool):
    """Запускает компиляцию диалога в план, прогоняет файлы и пишет отчет в конспект."""
    global LAST_BATCH_BACKUP, SESSION_MEMORY
    LAST_BATCH_BACKUP.clear()

    plan = compile_plan_from_chat(project_map, chat_messages)
    what_was_done = plan.get("what_was_done", "Обновление кода.")
    next_steps = plan.get("next_steps", "Дальнейшие шаги.")
    tasks = plan.get("tasks", [])

    if not tasks:
        console.print("⚠️ [warning]Haiku не выделила файлов для изменения.[/warning]")
        return

    modified_files = []
    for t in tasks:
        rel_fp = t.get("filepath", "output.py")
        tz = t.get("tz", "")
        ok = execute_single_file_task(rel_fp, tz, work_dir, project_map, fast_mode)
        if ok:
            modified_files.append(rel_fp.lstrip("/\\"))

    if modified_files:
        append_to_konspekt(work_dir, konspekt_file, what_was_done, next_steps, modified_files)

    SESSION_MEMORY = (
        f"На предыдущем шаге выполнено ({', '.join(modified_files)}):\n{what_was_done}\n\n"
        f"Следующие шаги (Next Steps):\n{next_steps}"
    )

    console.print()
    summary_md = f"**✅ Что сделано:**\n{what_was_done}\n\n**⏭️ Что дальше:**\n{next_steps}"
    console.print(Panel(Markdown(summary_md), title="[bold green]🎉 ШАГ УСПЕШНО ЗАВЕРШЕН[/bold green]", border_style="green", box=box.DOUBLE))


def render_dashboard(work_dir: Path, total_files: int, tz_file: Path | None, konspekt_file: Path | None):
    """Рисует информационную панель проекта в терминале."""
    global FORCED_TZ_PATH, FORCED_KONSPEKT_PATH
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column("Key", style="bold cyan")
    table.add_column("Value")

    table.add_row("📂 Папка:", f"[bold]{work_dir}[/bold] (файлов: {total_files})")

    tz_status = f"[green]{tz_file.relative_to(work_dir).as_posix() if tz_file.is_relative_to(work_dir) else tz_file}[/green]" if tz_file else "[dim]не задано[/dim]"
    k_status = f"[green]{konspekt_file.relative_to(work_dir).as_posix() if konspekt_file.is_relative_to(work_dir) else konspekt_file}[/green]" if konspekt_file else "[dim]авто-создание[/dim]"

    table.add_row("📘 Документы:", f"ТЗ: {tz_status} | Конспект: {k_status}")
    if SESSION_MEMORY:
        table.add_row("🧠 Память сессии:", "[bold magenta]Активна[/bold magenta]")

    table.add_row(
        "⌨️ Слэш-команды:",
        "[bold yellow]/go[/bold yellow] | [bold yellow]/go![/bold yellow] | [bold yellow]/tz <путь>[/bold yellow] | [bold yellow]/konspekt <путь>[/bold yellow] | [bold yellow]/undo[/bold yellow] | [bold yellow]/clear[/bold yellow] | [bold yellow]/dir[/bold yellow] | [bold yellow]/exit[/bold yellow]"
    )

    console.print()
    console.print(Panel(
        table,
        title="[bold cyan]🚀 AI CODE ORCHESTRATOR v4.5 (Slash Commands)[/bold cyan]",
        subtitle="[dim]Haiku 5.5 ➔ DeepSeek ➔ Sandbox ➔ Sonnet 5.5[/dim]",
        border_style="bright_blue",
        box=box.ROUNDED
    ))


def main():
    global SESSION_MEMORY, FORCED_TZ_PATH, FORCED_KONSPEKT_PATH

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

    work_dir = current_dir

    while True:
        project_map, tz_file, konspekt_file = build_project_map(work_dir)
        total_files = len(get_project_files(work_dir))
        render_dashboard(work_dir, total_files, tz_file, konspekt_file)

        chat_messages = []
        loaded_files_in_chat = set()

        while True:
            prompt_str = "\n[bold green]💬 Вы (обсудите план или введите /go):[/bold green] "
            user_input = bash_input(prompt_str)
            if not user_input:
                continue

            cmd_lower = user_input.lower()

            # Обработка слэш-команд
            if cmd_lower in ['/exit', '/quit', '/q']:
                console.print("\n👋 [bold cyan]До встречи![/bold cyan]")
                return
            if cmd_lower == '/undo':
                undo_last_batch()
                break
            if cmd_lower == '/dir':
                new_dir = bash_input("📂 Укажи новую рабочую папку: ").strip('"\'')
                if new_dir:
                    work_dir = Path(new_dir).resolve()
                    SESSION_MEMORY = ""
                break
            if cmd_lower in ['/clear', '/reset']:
                SESSION_MEMORY = ""
                console.print("🧹 [cyan]Диалог и память плана очищены.[/cyan]")
                break
            if cmd_lower.startswith('/tz '):
                p_str = user_input[4:].strip().strip('"\'')
                p = Path(p_str)
                if not p.is_absolute():
                    p = (work_dir / p).resolve()
                if p.exists():
                    FORCED_TZ_PATH = p
                    console.print(f"✅ [green]ТЗ успешно принудительно подключено:[/green] {p}")
                else:
                    console.print(f"❌ [error]Файл ТЗ не найден по пути:[/error] {p}")
                break
            if cmd_lower.startswith('/konspekt '):
                p_str = user_input[10:].strip().strip('"\'')
                p = Path(p_str)
                if not p.is_absolute():
                    p = (work_dir / p).resolve()
                if p.exists():
                    FORCED_KONSPEKT_PATH = p
                    console.print(f"✅ [green]Конспект успешно принудительно подключен:[/green] {p}")
                else:
                    console.print(f"❌ [error]Файл конспекта не найден по пути:[/error] {p}")
                break
            if cmd_lower in ['/go', '/go!']:
                if not chat_messages:
                    console.print("⚠️ [warning]Сначала обсудите задачу с Haiku в чате![/warning]")
                    continue
                execute_plan_pipeline(work_dir, project_map, konspekt_file, chat_messages, fast_mode=(cmd_lower == '/go!'))
                break

            # Обычное сообщение в диалог с Haiku
            extra_code = find_mentioned_files_content(work_dir, user_input, loaded_files_in_chat)
            full_msg = user_input
            if extra_code:
                full_msg += f"\n\n[Автоматически приложенный код упомянутых файлов]:\n{extra_code}"

            chat_messages.append({"role": "user", "content": full_msg})

            try:
                haiku_reply = chat_with_haiku_turn(project_map, chat_messages)
                chat_messages.append({"role": "assistant", "content": haiku_reply})

                if "[START_CODING]" in haiku_reply:
                    clean_reply = haiku_reply.replace("[START_CODING]", "").strip()
                    if clean_reply:
                        console.print()
                        console.print(Panel(Markdown(clean_reply), title="[bold cyan]🤖 Haiku 5.5 (Tech Lead)[/bold cyan]", border_style="cyan", box=box.ROUNDED))
                    execute_plan_pipeline(work_dir, project_map, konspekt_file, chat_messages, fast_mode=False)
                    break
                else:
                    console.print()
                    console.print(Panel(Markdown(haiku_reply), title="[bold cyan]🤖 Haiku 5.5 (Tech Lead)[/bold cyan]", border_style="cyan", box=box.ROUNDED))

            except Exception as e:
                console.print(f"\n❌ [error]Ошибка связи с моделью:[/error] {e}")


if __name__ == "__main__":
    main()
