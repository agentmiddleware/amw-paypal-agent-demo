FROM python:3.12-slim

# Builds from this repository alone: the gateway (mini_gateway/) and the two
# SDKs (vendor/) are all in the build context. Nothing is fetched from GitHub.
WORKDIR /demo
COPY pyproject.toml requirements.txt README.md LICENSE ./
COPY vendor ./vendor
COPY mini_gateway ./mini_gateway
COPY *.py ./
COPY tests ./tests
RUN pip install --no-cache-dir -r requirements.txt

CMD ["python", "demo.py"]
