PYTHON ?= python3

.PHONY: zipapp test clean

zipapp:
	rm -rf build/zipapp dist
	mkdir -p build/zipapp dist
	$(PYTHON) -m pip install --quiet --target build/zipapp --no-compile .
	rm -rf build/zipapp/*.dist-info build/zipapp/bin
	$(PYTHON) -m zipapp build/zipapp \
		-m "hiera_gc.cli:entry" \
		-p "/usr/bin/env python3" \
		-o dist/hiera-gc
	@echo "built dist/hiera-gc"

test:
	tox

clean:
	rm -rf build dist .tox *.egg-info
