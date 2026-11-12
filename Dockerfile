FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Copy requirements.txt
COPY requirements.txt .

# Install the specified packages
# CPU-only PyTorch first (the default Linux wheel bundles ~2 GB of CUDA we don't use).
# --index-url, not --extra-index-url: with an extra index, pip may take a same-named package
# from whichever index has the higher version (dependency confusion).
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch torchvision && \
    pip install --no-cache-dir -r requirements.txt

COPY slack_bot.py .
COPY streamlit_app.py .
COPY claude_orchestrator.py .
COPY dynamodb_database.py .
COPY servers/ ./servers/

# Fetch the YOLO weights at build time and verify their SHA-256, so the running container
# never downloads code-bearing files (.pt weights are pickles) and needs less egress.
RUN python -c "from servers.traffic_crowd_server import vision as v; assert v.detector_name() == 'YOLOv8n', v._model_error"

# Run as an unprivileged user: a compromise of the app (e.g. via a malicious tool
# result or dependency) must not get root inside the container. The app writes
# result files under /app, so that user owns it.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

# API keys are not baked into the image. Pass them at runtime, e.g.
#   docker run --env-file .env -p 8501:8501 travelgenie
# or via ECS task environment variables / AWS Secrets Manager.

EXPOSE 8501

# Default: run the Streamlit web app.
# To run the Slack bot instead: docker run --env-file .env travelgenie python slack_bot.py
CMD ["streamlit", "run", "streamlit_app.py", "--server.address=0.0.0.0", "--server.port=8501"]
