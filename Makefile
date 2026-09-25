PYTHON ?= python3

.PHONY: demo eval test fixture
fixture:
	$(PYTHON) -m paper_risk.fixture --check
demo:
	$(PYTHON) -m paper_risk.cli demo
eval:
	$(PYTHON) -m paper_risk.cli eval --check
test:
	$(PYTHON) -m pytest -q
