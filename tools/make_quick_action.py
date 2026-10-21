"""Ставит Quick Action для dev-версии (из папки проекта, через app.cli)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.quick_action import install  # noqa: E402

# stderr = консольный лог; полный лог и так в logs/kadr.log
print("Установлено:", install(f'cd "{ROOT}" && ./.venv/bin/python -m app.cli "$@" 2>/dev/null'))
