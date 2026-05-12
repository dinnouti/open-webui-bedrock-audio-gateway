.PHONY: run sync lint test test-docker docker-build docker-up docker-down

run:
	uv run python -m app

sync:
	uv sync

lint:
	uv run ruff check app/

test:
	./tests/test_local.sh

test-docker:
	./tests/test_docker.sh

docker-build:
	docker compose build

docker-up:
	docker compose up -d

docker-down:
	docker compose down
