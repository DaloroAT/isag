PYTHON ?= python

.PHONY: help install-tools clean build check publish-test publish

help:
	@echo "Targets:"
	@echo "  install-tools  Install/upgrade build + twine (run once)"
	@echo "  clean          Remove dist/, build/, and *.egg-info/"
	@echo "  build          Build sdist and wheel into dist/"
	@echo "  check          Run twine check on built artefacts"
	@echo "  publish-test   Upload dist/* to TestPyPI"
	@echo "  publish        Upload dist/* to PyPI"

install-tools:
	$(PYTHON) -m pip install --upgrade build twine

clean:
	rm -rf dist/ build/ *.egg-info/

build: clean
	$(PYTHON) -m build

check:
	$(PYTHON) -m twine check dist/*

publish-test: build check
	$(PYTHON) -m twine upload --repository testpypi dist/*

publish: build check
	$(PYTHON) -m twine upload dist/*
