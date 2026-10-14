FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# AMW gateway source at the pinned commit (its server package is not
# pip-installable, so it is loaded from this checkout via AMW_SOURCE_DIR).
ARG AMW_REF=b082579d4e15e21bab5a3bc65a10464817927ded
RUN git clone --quiet https://github.com/agentmiddleware/agent-middleware-api /opt/amw \
    && git -C /opt/amw checkout --quiet "$AMW_REF" \
    && pip install --no-cache-dir -r /opt/amw/requirements.txt
ENV AMW_SOURCE_DIR=/opt/amw

WORKDIR /demo
COPY pyproject.toml README.md LICENSE ./
COPY *.py ./
COPY tests ./tests
RUN pip install --no-cache-dir ".[dev]"

CMD ["python", "demo.py"]
