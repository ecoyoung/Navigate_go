.PHONY: setup check-secrets migrate seed-catalog docker-up docker-down docker-logs probe-source crawl-catalog crawl-due crawl-scheduler process-content classify-beauty classify-beauty-llm rebuild-strict-duplicates sync-beauty-domain extract-entities extract-entities-llm review-entity-candidates add-event-constraint rebuild-events score-content prepare-frontend-editorials frontend-snapshot daily-report daily-report-zh backend frontend test check

setup:
	uv sync --project backend --dev

check-secrets:
	cd backend && uv run python -m scripts.check_secrets

migrate:
	cd backend && uv run alembic upgrade head

seed-catalog:
	cd backend && uv run python -m scripts.seed_catalog

probe-source:
	@echo "Usage: cd backend && uv run python -m scripts.probe_source URL [--input-file FILE]"

crawl-catalog:
	cd backend && uv run python -m scripts.crawl_catalog

crawl-due:
	cd backend && uv run python -m scripts.run_due_sources

crawl-scheduler:
	cd backend && uv run python -m scripts.run_due_sources --watch --poll-seconds 60

process-content:
	cd backend && uv run python -m scripts.process_content

classify-beauty:
	cd backend && uv run python -m scripts.classify_domain --domain beauty --apply

classify-beauty-llm:
	cd backend && uv run python -m scripts.classify_domain_llm --domain beauty --apply

rebuild-strict-duplicates:
	cd backend && uv run python -m scripts.rebuild_strict_duplicates --apply

sync-beauty-domain:
	cd backend && uv run python -m scripts.sync_domain_assignments --domain-key beauty --domain-name 美妆 --processor-name domain_rules --processor-version beauty-relevance.v2 --activate --apply

extract-entities:
	cd backend && uv run python -m scripts.extract_entities --domain-key beauty --apply

extract-entities-llm:
	@echo "Usage: cd backend && uv run python -m scripts.extract_entities_llm --content-id ID [--content-id ID ...] --apply"

review-entity-candidates:
	cd backend && uv run python -m scripts.review_entity_candidates sync
	cd backend && uv run python -m scripts.review_entity_candidates list

add-event-constraint:
	@echo "Usage: cd backend && uv run python -m scripts.add_event_constraint --left ID --right ID --relation must_link --reason 'evidence' --created-by NAME [--apply]"

rebuild-events:
	cd backend && uv run python -m scripts.rebuild_events --apply

score-content:
	@echo "Usage: cd backend && uv run python -m scripts.score_content --domain beauty --as-of 2026-08-30T00:00:00+08:00 [--apply]"

frontend-snapshot:
	cd backend && uv run python -m scripts.export_frontend_snapshot --domain beauty

prepare-frontend-editorials:
	cd backend && uv run python -m scripts.prepare_frontend_editorials --domain beauty --apply

daily-report:
	cd backend && uv run python -m scripts.generate_daily_report --domain beauty

daily-report-zh:
	cd backend && uv run python -m scripts.generate_daily_report --domain beauty --llm --llm-model deepseek-v4-flash

backend:
	cd backend && uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

frontend:
	npm --prefix frontend run dev -- --host 127.0.0.1 --port 3000

test:
	cd backend && uv run pytest

check:
	cd backend && uv run ruff check app tests scripts
	cd backend && uv run pytest

docker-up:
	docker compose up -d --build

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f --tail=100
