.PHONY: install test cov smoke eval run clean

install:
	pip install -e ".[dev]"

test:
	pytest

cov:
	pytest --cov --cov-report=term-missing

smoke:
	PAYMENT_AGENT_LIVE_SMOKE=1 python -m payment_agent.smoke

eval:
	python -m eval.runner

run:
	python -m payment_agent.cli

clean:
	rm -rf .pytest_cache .coverage htmlcov build dist *.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} +
