.PHONY: install install-training test backend-check frontend-check verify eval train-forecast train-sentiment finetune-retriever

PYTHON ?= python
PNPM ?= pnpm

install:
	$(PYTHON) -m pip install -r backend/requirements-test.txt

install-training:
	$(PYTHON) -m pip install -r backend/requirements-train.txt

test:
	$(PYTHON) -m pytest

backend-check:
	$(PYTHON) -m compileall -q backend src evaluation

frontend-check:
	cd frontend && $(PNPM) install --frozen-lockfile && $(PNPM) run lint && $(PNPM) run build

verify: backend-check test frontend-check

eval:
	$(PYTHON) -m evaluation.run_rag_eval

train-forecast:
	$(PYTHON) -m src.train_model.train_forecaster --ticker $(or $(TICKER),AAPL) --period 5y

train-sentiment:
	$(PYTHON) -m src.train_model.train_sentiment --data $(DATA) --mode baseline

finetune-retriever:
	$(PYTHON) -m src.train_model.finetune_retriever --data $(DATA)
