.PHONY: install install-dev install-nlp run train train-sentiment train-retriever test

PYTHON ?= python

install:
	$(PYTHON) -m pip install -e .
install-dev:
	$(PYTHON) -m pip install -e ".[dev]"
install-nlp:
	$(PYTHON) -m pip install -e ".[nlp]"
run:
	streamlit run app.py
train:
	$(PYTHON) scripts/train.py --ticker $(or $(TICKER),AAPL) --period $(or $(PERIOD),5y)
train-sentiment:
	$(PYTHON) -m src.train_model.train_sentiment --data $(DATA)
train-retriever:
	$(PYTHON) -m src.train_model.finetune_retriever --data $(DATA)
test:
	$(PYTHON) -m pytest
