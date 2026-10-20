#!/bin/zsh
# двойной клик → следит за inbox/; закрыть окно или Ctrl+C — остановить
cd "${0:A:h:h}" && exec ./.venv/bin/python -m app.watch
