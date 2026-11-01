# The full check. It needs no network once `uv sync` has built the environment.
UV_RUN := uv run --frozen

.PHONY: check lint format-check typecheck test format

check: lint format-check typecheck test

lint:
	$(UV_RUN) ruff check

format-check:
	$(UV_RUN) ruff format --check

typecheck:
	$(UV_RUN) pyrefly check

test:
	$(UV_RUN) pytest

format:
	$(UV_RUN) ruff check --fix
	$(UV_RUN) ruff format
