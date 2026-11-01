PYTHON ?= python3

.PHONY: test check format setup-format hooks
test:
	$(PYTHON) tools/test_all.py
check:
	$(PYTHON) tools/quality.py check
format:
	$(PYTHON) tools/quality.py format
setup-format:
	$(PYTHON) tools/quality.py setup
hooks:
	$(PYTHON) -m pre_commit install
