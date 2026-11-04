# The full check. It needs no network once `uv sync` has built the environment.
UV_RUN := uv run --frozen

.PHONY: check lint format-check typecheck test format deb eval-help eval-replies eval-fixtures

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

# The .deb package in dist/ (docs/releasing.md).
deb:
	python3 packaging/deb/build_deb.py

# Generated skill eval fixtures, outside the checkout by default.
eval-help:
	@bash skill/evals/_stubs/generate.sh help "$(OUT)"

eval-replies:
	@bash skill/evals/_stubs/generate.sh replies "$(OUT)"

eval-fixtures:
	@bash skill/evals/_stubs/generate.sh all "$(OUT)"
