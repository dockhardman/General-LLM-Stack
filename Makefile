# Server
run-server:
	uvicorn languru.server.app:app \
        --host=0.0.0.0 \
        --port=8680 \
        --workers=1 \
        --reload \
        --log-level=debug \
        --use-colors \
        --reload-delay=5.0

# Development
format-all:
	isort . --skip setup.py && black --exclude setup.py .

update-all:
	poetry update && \
		poetry export --without-hashes -f requirements.txt --output requirements.txt && \
		poetry export --without-hashes --with dev -f requirements.txt --output requirements-dev.txt
