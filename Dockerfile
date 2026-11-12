FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Copy requirements.txt
COPY requirements.txt .

# Install the specified packages
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY slack_bot.py .
COPY streamlit_app.py .
COPY claude_orchestrator.py .
COPY dynamodb_database.py .
COPY servers/ ./servers/

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
