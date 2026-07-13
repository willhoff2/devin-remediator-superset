PY := .venv/bin/python

.PHONY: test lint run mock scan smoke reset

test:
	$(PY) -m pytest tests/ -q

lint:
	.venv/bin/ruff check src/ scripts/ tests/

run:            ## the real thing: dispatcher + monitor + dashboard on :8090
	docker compose up --build

mock:           ## full-flow rehearsal against the local mock Devin API
	@trap 'kill 0' EXIT; \
	.venv/bin/uvicorn scripts.mock_devin:app --port 9095 --log-level warning & \
	DEVIN_API_BASE=http://127.0.0.1:9095 PR_VERIFY_ENABLED=false \
	POLL_INTERVAL_ISSUES=3 POLL_INTERVAL_SESSIONS=1 $(PY) -m src.main

scan:           ## file the remediation issues (the event that feeds the pipeline)
	$(PY) -m src.scanner

smoke:          ## real-API environment-cost gate (run before anything else)
	$(PY) -m scripts.setup smoke

reset:          ## dry-run of the demo reset; make reset ARGS=--yes to apply
	$(PY) -m scripts.reset_demo $(ARGS)
