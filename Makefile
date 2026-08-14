# Convenience wrappers. Every target is a one-liner you can also run by hand;
# nothing here is required to understand the system.

VENV := .venv
PY   := $(VENV)/bin/python

.PHONY: install
install:                       ## create the venv, install python and node deps
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -q -r requirements.txt
	cd web && npm install

.PHONY: seed
seed:                          ## create the database with demo users and rules
	$(PY) -m app.seed

.PHONY: api
api: seed                      ## run the API (and the built UI) on :8000
	$(VENV)/bin/uvicorn app.main:app --reload --port 8000

.PHONY: web
web:                           ## run the React dev server on :5173
	cd web && npm run dev

.PHONY: build
build:                         ## build the UI so the API can serve it at /app
	cd web && npm run build

.PHONY: replay
replay:                        ## stream events.jsonl into a running API
	$(PY) scripts/replay.py --reset

.PHONY: replay-fast
replay-fast:                   ## same, with no waiting between events
	$(PY) scripts/replay.py --reset --speed 0

.PHONY: test
test:                          ## run the test suite
	$(PY) -m pytest tests/ -q

.PHONY: reset
reset:                         ## delete the database and start over
	rm -f intraday.db intraday.db-wal intraday.db-shm notifications.log
