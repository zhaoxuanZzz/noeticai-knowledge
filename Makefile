.PHONY: deploy-local validate verify-hermes

deploy-local:
	bash scripts/deploy_local_hermes.sh

validate:
	python3 scripts/validate_work_suite.py --target all .

verify-hermes:
	python3 scripts/verify_hermes_install.py
