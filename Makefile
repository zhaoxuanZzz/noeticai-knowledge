.PHONY: install deploy-local validate verify-hermes configure-env

# Local Hermes install: configure QCC/Judge env, link plugin, enable MCP, smoke-check.
install:
	bash scripts/deploy_local_hermes.sh

# Backward-compatible alias.
deploy-local: install

configure-env:
	python3 scripts/configure_cws_env.py

validate:
	python3 scripts/validate_work_suite.py --target all .

verify-hermes:
	python3 scripts/verify_hermes_install.py
