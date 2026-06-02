FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts

# Default command runs the API; compose overrides it for worker/manager services.
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
